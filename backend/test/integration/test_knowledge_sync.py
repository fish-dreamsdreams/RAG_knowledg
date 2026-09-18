"""知识单元启停与删除的检索副本同步（tasklist 5.4，不变量 P9 / P10）。

需要 PostgreSQL 与 Milvus。用例自建自清（服务层内部提交，因此结束时必须清理）。
"""

from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pytest

from app.common.config import EMBED_DIM, settings
from app.common.db import SessionLocal
from app.common.errors import AppError
from app.engines.retrieve import milvus_store
from app.engines.retrieve.milvus_store import COLLECTION_NAME, ChunkVector
from app.models.knowledge import Chunk, KnowledgeUnit
from app.repositories import knowledge as knowledge_repo
from app.services import knowledge as knowledge_service

pytestmark = pytest.mark.integration


def unit_vector(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return (vector / np.linalg.norm(vector)).tolist()


async def create_unit_with_chunks(session, *, source_path: str, chunks: int = 2):
    """建一个知识单元 + 切片：PG 行与 Milvus 实体同时就位。"""

    unit = KnowledgeUnit(
        title=f"测试单元 {uuid4().hex[:6]}",
        category="测试",
        format="md",
        source_path=source_path,
        source_filename=Path(source_path).name,
        file_size=10,
        status="enabled",
        parse_status="indexed",
    )
    session.add(unit)
    await session.flush()

    chunk_rows = [
        Chunk(
            unit_id=unit.id,
            level="child",
            ordinal=index,
            content=f"第 {index} 条测试内容。",
            char_count=9,
        )
        for index in range(chunks)
    ]
    session.add_all(chunk_rows)
    await session.flush()

    milvus_store.upsert_chunks(
        [
            ChunkVector(
                chunk_id=str(row.id),
                unit_id=str(unit.id),
                parent_id=str(uuid4()),
                enabled=True,
                dense=unit_vector(index),
                sparse={1903 + index: 0.2},
            )
            for index, row in enumerate(chunk_rows)
        ]
    )
    await session.commit()
    return unit


def enabled_hits(unit_id: UUID, query: list[float]) -> set[str]:
    hits = milvus_store.get_client().search(
        COLLECTION_NAME,
        data=[query],
        anns_field="dense",
        limit=20,
        filter=f'enabled == true and unit_id == "{unit_id}"',
        search_params={"metric_type": "COSINE", "params": {"ef": 64}},
        output_fields=["chunk_id"],
    )
    return {hit["id"] for hit in hits[0]}


async def cleanup(unit_id: UUID) -> None:
    """兜底清理：Milvus 实体 + PG 行。

    只清 Milvus 是不够的：这些用例里服务层会自己提交，PG 行不会随 `session` 回滚消失，
    漏删会在演示库里堆下一批「测试单元 xxxxxx」（真机上已发生，还连带把 Milvus 塞满孤儿
    实体，检索结果被稀释）。故意不调 `delete_unit`：删除用例本身就在验证它，兜底清理
    不能依赖被测代码。
    """

    milvus_store.delete_by_unit(str(unit_id))
    # 单独开会话：用例中途挂了时，测试会话可能已经处于失败事务里
    async with SessionLocal() as session:
        unit = await knowledge_repo.get_unit(session, unit_id)
        if unit is None:
            return
        await knowledge_repo.delete_unit_row(session, unit)
        await session.commit()


async def test_disable_then_enable_switches_milvus_visibility(session) -> None:
    unit = await create_unit_with_chunks(session, source_path="var/uploads/none.md")
    unit_id = unit.id
    query = unit_vector(0)

    try:
        assert len(enabled_hits(unit_id, query)) == 2

        disabled = await knowledge_service.set_unit_enabled(session, unit_id, enabled=False)

        assert disabled.status == "disabled"
        assert enabled_hits(unit_id, query) == set(), "P9：停用后不得再被检索到"
        # 停用不是删除：实体仍在库里，只是被 enabled 过滤
        assert milvus_store.count_unit_chunks(str(unit_id)) == 2

        enabled = await knowledge_service.set_unit_enabled(session, unit_id, enabled=True)

        assert enabled.status == "enabled"
        assert len(enabled_hits(unit_id, query)) == 2
    finally:
        await cleanup(unit_id)


async def test_delete_cascades_pg_chunks_milvus_entities_and_source_file(session) -> None:
    """P10：删除后 PG 切片、Milvus 实体、原件文件都不得残留。"""

    upload_dir = Path(settings.upload_dir).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    source_file = upload_dir / f"p10-{uuid4().hex[:8]}.md"
    source_file.write_text("原件内容", encoding="utf-8")

    unit = await create_unit_with_chunks(session, source_path=str(source_file), chunks=3)
    unit_id = unit.id
    query = unit_vector(1)

    try:
        assert source_file.exists()
        assert milvus_store.count_unit_chunks(str(unit_id)) == 3

        await knowledge_service.delete_unit(session, unit_id)

        assert await knowledge_repo.get_unit(session, unit_id) is None
        assert await knowledge_repo.count_chunks(session, unit_id) == 0
        assert milvus_store.count_unit_chunks(str(unit_id)) == 0
        assert enabled_hits(unit_id, query) == set()
        assert not source_file.exists(), "原件文件必须一并删除"
    finally:
        await cleanup(unit_id)
        source_file.unlink(missing_ok=True)


async def test_delete_keeps_files_outside_upload_dir(session) -> None:
    """`source_path` 指向上传目录之外时不得删除（防止误删磁盘上其他文件）。"""

    outside = Path("var/p10-outside.txt").resolve()
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("不该被删除", encoding="utf-8")

    unit = await create_unit_with_chunks(session, source_path=str(outside), chunks=1)
    unit_id = unit.id

    try:
        await knowledge_service.delete_unit(session, unit_id)

        assert await knowledge_repo.get_unit(session, unit_id) is None
        assert outside.exists(), "上传目录之外的文件必须保留"
    finally:
        await cleanup(unit_id)
        outside.unlink(missing_ok=True)


async def test_unknown_unit_raises_not_found(session) -> None:
    missing = uuid4()

    with pytest.raises(AppError) as disabled:
        await knowledge_service.set_unit_enabled(session, missing, enabled=False)
    assert disabled.value.code == "KB_NOT_FOUND"

    with pytest.raises(AppError) as deleted:
        await knowledge_service.delete_unit(session, missing)
    assert deleted.value.code == "KB_NOT_FOUND"


async def test_enable_works_when_milvus_has_no_entities(session) -> None:
    """Milvus 侧没有实体时（尚未入库/已清理）启停仍应幂等成功。"""

    unit = KnowledgeUnit(
        title=f"未入库单元 {uuid4().hex[:6]}",
        format="md",
        source_path="var/uploads/never-indexed.md",
        source_filename="never-indexed.md",
        status="disabled",
        parse_status="queued",
    )
    session.add(unit)
    await session.commit()
    unit_id = unit.id

    try:
        enabled = await knowledge_service.set_unit_enabled(session, unit_id, enabled=True)

        assert enabled.status == "enabled"
        assert milvus_store.count_unit_chunks(str(unit_id)) == 0
    finally:
        await knowledge_service.delete_unit(session, unit_id)
