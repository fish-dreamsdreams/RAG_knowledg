"""导入流水线测试（tasklist 6.3 / 6.5）。

需要 PostgreSQL + Redis + Milvus；编码器除一个端到端用例外都用假的，便于注入失败与放大竞态。
流水线内部会提交事务，因此用例自建自清。
"""

from __future__ import annotations

import asyncio
import time
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pytest
from sqlalchemy import delete

from app.common.config import EMBED_DIM, settings
from app.common.db import SessionLocal, worker_session
from app.engines.celery.ingest import ingest_unit
from app.engines.embed import Embedding, get_embedder
from app.engines.mineru import MinerUClient, MinerUError, ParsedDocument
from app.engines.retrieve import milvus_store
from app.engines.storage import object_store
from app.engines.vision import VisionClient
from app.models.faq import KnowledgeGap
from app.models.knowledge import Chunk, IngestTask, KnowledgeUnit, KnowledgeUnitAsset
from app.repositories import faq as faq_repo
from app.repositories import knowledge as knowledge_repo
from app.services.ingest import (
    STAGE_FAILED,
    STAGE_INDEXED,
    UNIT_GONE_MESSAGE,
    _unit_lock,
    run_ingest,
)

pytestmark = pytest.mark.integration

# 合法 PNG 魔数头（解图链路要求魔数与扩展名一致）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32

MARKDOWN = """# 差旅费报销制度

## 住宿标准

一线城市住宿上限为 600 元每晚，其他城市为 400 元每晚。

## 报销流程

出差结束后 5 个工作日内提交报销单，由部门负责人审批后交财务。
"""


class FakeEmbedder:
    """确定性假编码器：可注入失败、可加延时以放大并发窗口。"""

    def __init__(self, *, fail_on_call: int | None = None, delay: float = 0.0) -> None:
        self.fail_on_call = fail_on_call
        self.delay = delay
        self.calls = 0

    def encode(self, texts: list[str], **kwargs) -> Embedding:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail_on_call is not None and self.calls >= self.fail_on_call:
            raise RuntimeError("编码失败（测试注入）")

        vectors = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
        sparse: list[dict[int, float]] = []
        for index in range(len(texts)):
            rng = np.random.default_rng(abs(hash((self.calls, index))) % (2**32))
            vector = rng.standard_normal(EMBED_DIM).astype(np.float32)
            vectors[index] = vector / np.linalg.norm(vector)
            sparse.append({1903 + index: 0.25})
        return Embedding(dense=vectors, sparse=sparse)


class FailingMinerU:
    """提交即失败的 MinerU 替身。"""

    def __init__(self) -> None:
        self.calls = 0

    async def parse_documents(self, paths: list[Path]) -> dict[str, ParsedDocument]:
        self.calls += 1
        raise MinerUError("MinerU 业务错误 code=1002 msg=quota exceeded")


async def _delete_unit_concurrently(unit_id: UUID) -> None:
    """用独立连接删掉单元（等价于管理员在导入进行中点了删除）。

    连接与事件循环都必须是新的：导入流水线用的是它自己的会话，两者不能共用一个事务。
    """

    async with worker_session() as session:
        unit = await session.get(KnowledgeUnit, unit_id)
        if unit is not None:
            await session.delete(unit)
            await session.commit()
    # 与真实 `delete_unit` 一致：先清检索副本
    milvus_store.delete_by_unit(str(unit_id))


class DeletingMinerU:
    """先删单元、再返回文档：模拟管理员在解析期间删掉了这份文档。"""

    def __init__(self, inner: MinerUClient, unit_id: UUID) -> None:
        self.inner = inner
        self.unit_id = unit_id

    async def parse_documents(self, paths: list[Path]) -> dict[str, ParsedDocument]:
        await _delete_unit_concurrently(self.unit_id)
        return await self.inner.parse_documents(paths)


def mineru_with_markdown(
    markdown: str, images: dict[str, bytes] | None = None
) -> MinerUClient:
    """假造 MinerU 全链路，返回指定 markdown（走真实 httpx 客户端代码）。

    `images` 是 ZIP 内图片条目（键为条目名），用于验证解图与引用改写链路。
    """

    import httpx

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", markdown)
        for name, content in (images or {}).items():
            archive.writestr(name, content)
    zip_bytes = buffer.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/file-urls/batch"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "b1",
                        "file_urls": ["https://upload.example.com/p"],
                    },
                },
            )
        if url.startswith("https://upload.example.com/"):
            return httpx.Response(200)
        if "extract-results" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "b1",
                        "extract_result": [
                            {
                                "file_name": "制度.pdf",
                                "data_id": "0",
                                "state": "done",
                                "full_zip_url": "https://result.example.com/out.zip",
                            }
                        ],
                    },
                },
            )
        if url.startswith("https://result.example.com/"):
            return httpx.Response(200, content=zip_bytes)
        raise AssertionError(f"未预期请求：{url}")

    return MinerUClient(
        base_url="https://mineru.example.com/api/v4",
        api_key="test-key",
        poll_interval=0,
        transport=httpx.MockTransport(handler),
        output_dir=str(Path(settings.mineru_output_dir) / "test"),
    )


def _vision_client(
    reply: str = "图中为三级审批流程。", *, status: int = 200
) -> VisionClient:
    """假造视觉模型（走真实 httpx 客户端代码）。"""

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return VisionClient(
        base_url="https://vision.example.com/v1",
        api_key="test-key",
        model="test-vision",
        transport=httpx.MockTransport(handler),
        enabled=True,
    )


@pytest.fixture
async def unit_factory():
    """建知识单元（可选写真实原件），用例结束清理行、向量与文件。"""

    created: list[UUID] = []
    files: list[Path] = []

    async def _make(
        *,
        content: str | None = MARKDOWN,
        filename: str = "制度.md",
        write_file: bool = True,
        parse_status: str = "queued",
        status: str = "enabled",
        source_path: str | None = None,
        in_store: bool = False,
    ) -> KnowledgeUnit:
        """建单元。`in_store=True` 写对象存储（与真实导入一致），否则写本地临时文件。"""

        upload_dir = Path(settings.upload_dir).resolve()
        upload_dir.mkdir(parents=True, exist_ok=True)

        unit_id = uuid4()
        path: Path | None = None
        stored_key: str | None = None
        if source_path is None and write_file:
            if in_store:
                stored_key = object_store.source_key(unit_id, filename)
                assert stored_key is not None
                await object_store.put_bytes(stored_key, (content or "").encode("utf-8"))
            else:
                path = upload_dir / f"test-{uuid4().hex[:8]}-{filename}"
                path.write_text(content or "", encoding="utf-8")
                files.append(path)

        async with SessionLocal() as session:
            unit = KnowledgeUnit(
                # 显式给 id：对象 key 依赖它，列默认值要等 INSERT 才生效
                id=unit_id,
                title=f"流水线测试 {uuid4().hex[:6]}",
                format=filename.rsplit(".", 1)[-1],
                source_path=source_path or stored_key or str(path),
                source_filename=filename,
                file_size=len(content or ""),
                status=status,
                parse_status=parse_status,
            )
            session.add(unit)
            await session.commit()
            unit_id = unit.id
            created.append(unit_id)
            session.expunge(unit)
        return unit

    yield _make

    for unit_id in created:
        async with SessionLocal() as session:
            unit = await session.get(KnowledgeUnit, unit_id)
            if unit is not None:
                await session.delete(unit)
                await session.commit()
        milvus_store.delete_by_unit(str(unit_id))
        await object_store.delete_unit_prefix(unit_id)
    for path in files:
        path.unlink(missing_ok=True)


async def new_task(unit_id: UUID, stage: str = "queued") -> str:
    task_id = uuid4().hex
    async with SessionLocal() as session:
        await knowledge_repo.create_ingest_task(
            session, IngestTask(id=task_id, unit_id=unit_id, stage=stage, progress=0)
        )
        await session.commit()
    return task_id


# --- 正常链路 -------------------------------------------------------------


async def test_markdown_unit_is_indexed_end_to_end(unit_factory) -> None:
    """真实编码器 + 真实 Milvus 的端到端：md 直读、切块、编码、入库。"""

    unit = await unit_factory()
    task_id = await new_task(unit.id)

    async with SessionLocal() as session:
        outcome = await run_ingest(session, unit.id, task_id=task_id, embedder=get_embedder())

    assert outcome.status == STAGE_INDEXED
    assert outcome.chunks > 0

    async with SessionLocal() as session:
        refreshed = await session.get(KnowledgeUnit, unit.id)
        assert refreshed is not None
        assert refreshed.parse_status == STAGE_INDEXED
        assert refreshed.parse_error is None
        assert refreshed.parsed_text == MARKDOWN

        assert await knowledge_repo.count_chunks(session, unit.id) == outcome.chunks
        assert (
            await knowledge_repo.count_chunks(session, unit.id, level="child")
            == outcome.vectors
        )

        task = await session.get(IngestTask, task_id)
        assert task is not None
        assert task.stage == STAGE_INDEXED
        assert task.progress == 100

    assert milvus_store.count_unit_chunks(str(unit.id)) == outcome.vectors


async def test_pdf_unit_content_flows_from_mineru(unit_factory) -> None:
    unit = await unit_factory(content=None, filename="制度.pdf", write_file=True)
    task_id = await new_task(unit.id)
    mineru = mineru_with_markdown("# 来自 MinerU\n\n住宿标准为 600 元。")

    async with SessionLocal() as session:
        outcome = await run_ingest(
            session, unit.id, task_id=task_id, mineru=mineru, embedder=FakeEmbedder()
        )

    assert outcome.status == STAGE_INDEXED

    async with SessionLocal() as session:
        refreshed = await session.get(KnowledgeUnit, unit.id)
        assert refreshed is not None and refreshed.parsed_text is not None
        assert "来自 MinerU" in refreshed.parsed_text

        from sqlalchemy import select

        rows = await session.execute(select(Chunk).where(Chunk.unit_id == unit.id))
        contents = "".join(row.content for row in rows.scalars())
        assert "住宿标准为 600 元" in contents

    # 原始 ZIP 归档到 parsed/ 前缀（解图与重放都以它为准）
    archived = object_store.parsed_key(unit.id, "制度")
    assert archived is not None
    assert await object_store.exists(archived)


async def test_child_chunks_reference_their_parent(unit_factory) -> None:
    """子块必须挂到真实父块上：上下文拼接与溯源都靠这个外键。"""

    unit = await unit_factory()

    async with SessionLocal() as session:
        outcome = await run_ingest(session, unit.id, embedder=FakeEmbedder())
    assert outcome.status == STAGE_INDEXED

    from sqlalchemy import select

    async with SessionLocal() as session:
        parents = (
            (
                await session.execute(
                    select(Chunk.id).where(
                        Chunk.unit_id == unit.id, Chunk.level == "parent"
                    )
                )
            )
            .scalars()
            .all()
        )
        children = (
            (
                await session.execute(
                    select(Chunk.parent_id).where(
                        Chunk.unit_id == unit.id, Chunk.level == "child"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert parents and children
    assert None not in children, "子块 parent_id 不得为空"
    assert set(children) == set(parents), "每个子块都要指向本单元内真实存在的父块"


async def test_imported_disabled_unit_is_not_searchable(unit_factory) -> None:
    """导入即未授权：向量写入但 `enabled=false`，启用前检索不到（P9 在导入链上成立）。"""

    unit = await unit_factory(status="disabled")
    task_id = await new_task(unit.id)

    async with SessionLocal() as session:
        outcome = await run_ingest(session, unit.id, task_id=task_id, embedder=FakeEmbedder())

    assert outcome.status == STAGE_INDEXED
    assert milvus_store.count_unit_chunks(str(unit.id)) == outcome.vectors

    query = np.random.default_rng(7).standard_normal(EMBED_DIM).astype(np.float32)
    hits = milvus_store.get_client().search(
        milvus_store.COLLECTION_NAME,
        data=[(query / np.linalg.norm(query)).tolist()],
        anns_field="dense",
        limit=5,
        filter=f'enabled == true and unit_id == "{unit.id}"',
        output_fields=["chunk_id"],
    )
    assert hits == [[]], "未启用单元的切片不得出现在可检索结果里"


# --- 幂等与并发 -----------------------------------------------------------


async def test_reingest_does_not_duplicate_chunks(unit_factory) -> None:
    unit = await unit_factory()

    async with SessionLocal() as session:
        first = await run_ingest(session, unit.id, embedder=FakeEmbedder())
    async with SessionLocal() as session:
        second = await run_ingest(session, unit.id, embedder=FakeEmbedder())

    assert first.chunks == second.chunks > 0

    async with SessionLocal() as session:
        assert await knowledge_repo.count_chunks(session, unit.id) == first.chunks
    assert milvus_store.count_unit_chunks(str(unit.id)) == first.vectors


async def test_second_ingest_skips_while_lock_is_held(unit_factory) -> None:
    """同一单元已有导入在跑时直接跳过，不产生第二份切片。"""

    unit = await unit_factory()

    async with _unit_lock(unit.id) as acquired:
        assert acquired is True
        async with SessionLocal() as session:
            outcome = await run_ingest(session, unit.id, embedder=FakeEmbedder())

    assert outcome.status == "skipped"
    async with SessionLocal() as session:
        assert await knowledge_repo.count_chunks(session, unit.id) == 0


async def test_concurrent_ingest_of_same_unit_produces_one_set_of_chunks(unit_factory) -> None:
    """两条独立会话并发导入同一单元：一条 indexed、一条 skipped。"""

    unit = await unit_factory()

    async def _run() -> str:
        async with SessionLocal() as session:
            outcome = await run_ingest(
                session, unit.id, embedder=FakeEmbedder(delay=0.2)
            )
        return outcome.status

    statuses = await asyncio.gather(_run(), _run())

    assert sorted(statuses) == ["indexed", "skipped"]

    async with SessionLocal() as session:
        child_count = await knowledge_repo.count_chunks(session, unit.id, level="child")
    assert child_count > 0
    assert milvus_store.count_unit_chunks(str(unit.id)) == child_count


# --- 失败收敛（P15） ------------------------------------------------------


async def test_embedding_failure_cleans_chunks_and_vectors(unit_factory) -> None:
    """编码阶段失败：已写入的 PG 切片与 Milvus 实体都必须清干净。"""

    unit = await unit_factory()
    task_id = await new_task(unit.id)

    async with SessionLocal() as session:
        outcome = await run_ingest(
            session,
            unit.id,
            task_id=task_id,
            embedder=FakeEmbedder(fail_on_call=1),
        )

    assert outcome.status == STAGE_FAILED
    assert outcome.error and "编码失败" in outcome.error

    async with SessionLocal() as session:
        refreshed = await session.get(KnowledgeUnit, unit.id)
        assert refreshed is not None
        assert refreshed.parse_status == STAGE_FAILED
        assert refreshed.parse_error
        assert await knowledge_repo.count_chunks(session, unit.id) == 0

        task = await session.get(IngestTask, task_id)
        assert task is not None and task.stage == STAGE_FAILED and task.error

    assert milvus_store.count_unit_chunks(str(unit.id)) == 0


async def test_parse_failure_marks_failed_and_writes_nothing(unit_factory) -> None:
    unit = await unit_factory(content=None, filename="制度.pdf")
    task_id = await new_task(unit.id)
    mineru = FailingMinerU()

    async with SessionLocal() as session:
        outcome = await run_ingest(
            session, unit.id, task_id=task_id, mineru=mineru, embedder=FakeEmbedder()
        )

    assert outcome.status == STAGE_FAILED
    assert outcome.error and "1002" in outcome.error

    async with SessionLocal() as session:
        refreshed = await session.get(KnowledgeUnit, unit.id)
        assert refreshed is not None and refreshed.parse_status == STAGE_FAILED
        assert await knowledge_repo.count_chunks(session, unit.id) == 0
        # 失败发生在解析前，编码器不该被调用
    assert milvus_store.count_unit_chunks(str(unit.id)) == 0


async def test_blank_document_fails(unit_factory) -> None:
    unit = await unit_factory(content="   \n\n  ")

    async with SessionLocal() as session:
        outcome = await run_ingest(session, unit.id, embedder=FakeEmbedder())

    assert outcome.status == STAGE_FAILED
    assert outcome.error and "解析结果为空" in outcome.error


# --- 导入途中单元被删除 --------------------------------------------------


async def test_unit_deleted_during_parse_aborts_quietly(unit_factory) -> None:
    """解析期间单元被删：任务作废、不抛异常、什么都不往库里写。

    修前的真机现象：`UPDATE knowledge_units` 影响 0 行报 StaleDataError，错误处理又没先回滚，
    再抛一次 PendingRollbackError，整个任务带一串 traceback 退出。
    """

    unit = await unit_factory(content=None, filename="制度.pdf", write_file=True)
    task_id = await new_task(unit.id)
    mineru = DeletingMinerU(
        mineru_with_markdown("# 制度\n\n正文。"), unit.id
    )

    async with SessionLocal() as session:
        # 不抛异常就是本用例的一半断言
        outcome = await run_ingest(
            session, unit.id, task_id=task_id, mineru=mineru, embedder=FakeEmbedder()
        )

    assert outcome.status == "skipped"
    assert outcome.error == UNIT_GONE_MESSAGE

    async with SessionLocal() as session:
        assert await session.get(KnowledgeUnit, unit.id) is None
        assert await knowledge_repo.count_chunks(session, unit.id) == 0

    assert milvus_store.count_unit_chunks(str(unit.id)) == 0
    # 解析产物（parsed ZIP 与原件）都归在本单元前缀下，作废后一并清掉
    assert await object_store.list_keys(f"{object_store.unit_prefix(unit.id)}/") == []


async def test_unit_deleted_after_vectors_written_leaves_no_orphans(
    unit_factory, monkeypatch
) -> None:
    """删除落在「向量已经写进 Milvus」之后：收尾必须把它们清掉。

    这是孤儿向量的真实成因——`delete_unit` 清完之后任务才把向量写回去，单元已经没了，
    向量永远没人清，检索结果里就多一批无法溯源的噪声。
    """

    unit = await unit_factory()
    task_id = await new_task(unit.id)

    written: list[int] = []
    deleted: list[bool] = []
    real_upsert = milvus_store.upsert_chunks

    def upsert_then_delete(vectors):
        """向量真的写进 Milvus 之后，才把单元删掉。"""

        result = real_upsert(vectors)
        written.append(len(vectors))
        if not deleted:
            asyncio.run(_delete_unit_concurrently(unit.id))
            deleted.append(True)
        return result

    monkeypatch.setattr(milvus_store, "upsert_chunks", upsert_then_delete)

    async with SessionLocal() as session:
        outcome = await run_ingest(session, unit.id, task_id=task_id, embedder=FakeEmbedder())

    assert written, "本用例要求向量已经落地，才能验证收尾清理"
    assert deleted, "向量落地后应当已删掉单元"
    assert outcome.status == "skipped"
    assert outcome.error == UNIT_GONE_MESSAGE

    async with SessionLocal() as session:
        assert await session.get(KnowledgeUnit, unit.id) is None
        assert await knowledge_repo.count_chunks(session, unit.id) == 0

    assert milvus_store.count_unit_chunks(str(unit.id)) == 0, "不得留下孤儿向量"
    assert await object_store.list_keys(f"{object_store.unit_prefix(unit.id)}/") == []


async def test_images_are_extracted_and_refs_rewritten(unit_factory) -> None:
    """PDF 里的图片：写对象存储 + assets 表，正文引用改成相对 key（P17）。"""

    unit = await unit_factory(content=None, filename="制度.pdf", write_file=True)
    mineru = mineru_with_markdown(
        "# 来自 MinerU\n\n![图示](images/flow.png)\n\n正文。",
        images={"images/flow.png": PNG_BYTES},
    )

    async with SessionLocal() as session:
        outcome = await run_ingest(session, unit.id, mineru=mineru, embedder=FakeEmbedder())
    assert outcome.status == STAGE_INDEXED

    root = object_store.unit_prefix(unit.id)
    assert f"{root}/assets/flow.png" in await object_store.list_keys(f"{root}/")

    from sqlalchemy import select

    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(KnowledgeUnitAsset).where(
                        KnowledgeUnitAsset.unit_id == unit.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].rel_path == "assets/flow.png"
        assert rows[0].name == "flow.png"
        assert rows[0].media_type == "image/png"
        assert rows[0].byte_size == len(PNG_BYTES)
        assert rows[0].sha256
        assert rows[0].caption is None, "视觉默认关闭，图注留空"

        refreshed = await session.get(KnowledgeUnit, unit.id)
        assert refreshed is not None and refreshed.parsed_text is not None
        assert "assets/flow.png" in refreshed.parsed_text
        assert "images/flow.png" not in refreshed.parsed_text
        # P17：正文只留相对 key，不得出现任何存储地址
        for forbidden in ("http", "minio", "19000"):
            assert forbidden not in refreshed.parsed_text


async def test_caption_is_injected_next_to_image(unit_factory) -> None:
    """视觉开启：图注带 `图注：` 前缀紧跟图片标记，且与图片落在同一子块（P18）。"""

    unit = await unit_factory(content=None, filename="制度.pdf", write_file=True)
    mineru = mineru_with_markdown(
        "# 来自 MinerU\n\n![图示](images/flow.png)\n\n正文。",
        images={"images/flow.png": PNG_BYTES},
    )

    async with SessionLocal() as session:
        outcome = await run_ingest(
            session,
            unit.id,
            mineru=mineru,
            embedder=FakeEmbedder(),
            vision=_vision_client("图中为三级审批流程。"),
        )
    assert outcome.status == STAGE_INDEXED

    from sqlalchemy import select

    async with SessionLocal() as session:
        asset = (
            (
                await session.execute(
                    select(KnowledgeUnitAsset).where(
                        KnowledgeUnitAsset.unit_id == unit.id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert asset.caption == "图中为三级审批流程。", "caption 存不带前缀的摘要原文"

        chunks = (
            (
                await session.execute(
                    select(Chunk).where(
                        Chunk.unit_id == unit.id, Chunk.level == "child"
                    )
                )
            )
            .scalars()
            .all()
        )

    texts = [chunk.content for chunk in chunks]
    with_image = [text for text in texts if "assets/flow.png" in text]
    assert with_image, texts
    assert all("图注：图中为三级审批流程。" in text for text in with_image), with_image
    # P17：切块文本只留相对 key，不得出现任何存储地址或桶名
    for text in texts:
        for forbidden in ("http", "minio", "19000"):
            assert forbidden not in text


async def test_vision_failure_does_not_block_ingest(unit_factory) -> None:
    """视觉调用失败：图片照常入库，只是该图没有图注（TECH_SPEC §8.0）。"""

    unit = await unit_factory(content=None, filename="制度.pdf", write_file=True)
    mineru = mineru_with_markdown(
        "# 来自 MinerU\n\n![图示](images/flow.png)\n\n正文。",
        images={"images/flow.png": PNG_BYTES},
    )

    async with SessionLocal() as session:
        outcome = await run_ingest(
            session,
            unit.id,
            mineru=mineru,
            embedder=FakeEmbedder(),
            vision=_vision_client(status=500),
        )
    assert outcome.status == STAGE_INDEXED

    from sqlalchemy import select

    async with SessionLocal() as session:
        asset = (
            (
                await session.execute(
                    select(KnowledgeUnitAsset).where(
                        KnowledgeUnitAsset.unit_id == unit.id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert asset.caption is None, "降级只影响图注"

    # 图片与引用都还在：降级不影响图片本身
    assert await object_store.exists(
        f"{object_store.unit_prefix(unit.id)}/assets/flow.png"
    )


async def test_missing_source_file_fails(unit_factory) -> None:
    unit = await unit_factory(source_path="var/uploads/never-existed.md")

    async with SessionLocal() as session:
        outcome = await run_ingest(session, unit.id, embedder=FakeEmbedder())

    assert outcome.status == STAGE_FAILED
    assert outcome.error and "原件不存在" in outcome.error


async def test_source_object_is_fetched_from_store(unit_factory) -> None:
    """原件在对象存储（真实导入的形态）时也能导入：取回临时文件 → 解析 → 入库。"""

    unit = await unit_factory(in_store=True)
    key = unit.source_path
    assert key.startswith(f"{object_store.UNIT_PREFIX}/")

    async with SessionLocal() as session:
        outcome = await run_ingest(session, unit.id, embedder=FakeEmbedder())

    assert outcome.status == STAGE_INDEXED
    assert outcome.chunks > 0
    # 权威源仍是对象存储：临时文件已删，但原件对象必须还在
    assert await object_store.exists(key)


async def test_unknown_unit_is_skipped_without_error() -> None:
    async with SessionLocal() as session:
        outcome = await run_ingest(session, uuid4(), embedder=FakeEmbedder())

    assert outcome.status == "skipped"


# --- worker 入口 -----------------------------------------------------------


async def test_celery_task_entrypoint_runs_repeatedly(unit_factory) -> None:
    """直接跑 Celery 任务体两次，模拟 worker 连续处理两个任务。

    每次调用都 `asyncio.run`（新事件循环）、跑在独立线程里（线程池）：
    数据库连接用 NullPool、Redis 用 `new_redis()`，两边都不能跨循环复用连接。
    这条用例就是真机自检抓到的缺陷（worker 进程里只有第一次导入成功）的回归测试。
    """

    unit = await unit_factory()

    first = await asyncio.to_thread(ingest_unit, str(unit.id), None)
    second = await asyncio.to_thread(ingest_unit, str(unit.id), None)

    assert first["status"] == STAGE_INDEXED, first
    assert second["status"] == STAGE_INDEXED, second
    assert first["chunks"] == second["chunks"] > 0
    assert milvus_store.count_unit_chunks(str(unit.id)) == second["vectors"]

    async with SessionLocal() as session:
        assert await knowledge_repo.count_chunks(session, unit.id) == second["chunks"]


# --- 缺口补全（tasklist 13.1 / P16） ---------------------------------------


@pytest.fixture
async def linked_gap(unit_factory):
    """建一个「已转建」的缺口并挂到新单元上，返回 (gap_id, unit)。"""

    created: list[UUID] = []

    async def _make() -> tuple[UUID, KnowledgeUnit]:
        unit = await unit_factory()
        gap_id = uuid4()
        async with SessionLocal() as session:
            session.add(
                KnowledgeGap(
                    id=gap_id,
                    question_text=f"缺口补全测试 {uuid4().hex[:8]}",
                    freq=1,
                    status=faq_repo.GAP_CONVERTED,
                    filled_unit_id=unit.id,
                    last_asked_at=datetime.now(UTC),
                )
            )
            await session.commit()
        created.append(gap_id)
        return gap_id, unit

    yield _make

    async with SessionLocal() as session:
        await session.execute(delete(KnowledgeGap).where(KnowledgeGap.id.in_(created)))
        await session.commit()


async def test_indexed_unit_settles_linked_gap(linked_gap) -> None:
    """单元走到 `indexed`，缺口才算补全：文档没进检索库就置 filled 是自欺欺人。"""

    gap_id, unit = await linked_gap()
    task_id = await new_task(unit.id)

    async with SessionLocal() as session:
        outcome = await run_ingest(
            session, unit.id, task_id=task_id, embedder=FakeEmbedder()
        )
    assert outcome.status == STAGE_INDEXED

    async with SessionLocal() as session:
        gap = await session.get(KnowledgeGap, gap_id)
        assert gap is not None and gap.status == faq_repo.GAP_FILLED


async def test_failed_ingest_keeps_gap_converted(linked_gap) -> None:
    """导入失败时缺口留在 `converted`，等管理员重试——否则缺口清单会显示"已补全"却查不到内容。"""

    gap_id, unit = await linked_gap()
    task_id = await new_task(unit.id)

    async with SessionLocal() as session:
        outcome = await run_ingest(
            session, unit.id, task_id=task_id, embedder=FakeEmbedder(fail_on_call=1)
        )
    assert outcome.status == STAGE_FAILED

    async with SessionLocal() as session:
        gap = await session.get(KnowledgeGap, gap_id)
        assert gap is not None
        assert gap.status == faq_repo.GAP_CONVERTED
        assert gap.filled_unit_id == unit.id
