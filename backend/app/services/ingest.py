"""单文件导入流水线（tasklist 6.3，design.md §2.4）。

阶段：`queued → parsing → chunking → embedding → indexed`，每步同时写
`knowledge_units.parse_status` 与 `ingest_tasks.stage/progress`，并**逐步提交**——
接口侧要轮询到进度，不能等整条链跑完才可见。

只有 Celery worker 调用本模块（TECH_SPEC §8.0：**API 请求线程不得触发解析**）。

失败一定收敛（P15）：任一步异常 → `parse_status=failed` + `parse_error`（写前先脱敏），
并清理本次已写入的 PG 切片、Milvus 实体与图片对象，知识单元保持不可检索（`status` 不动）。

**单元在导入途中被删除**是另一回事（真机缺陷）：解析要跑分钟级，管理员完全可能在此期间删掉
它，于是本任务是往一个已经不存在的单元上写——`UPDATE knowledge_units` 影响 0 行直接报
`StaleDataError`，接着错误处理又没有先回滚，把 `PendingRollbackError` 再抛一遍，任务带着一串
traceback 退出；更坑的是删除那一刻清掉的 Milvus 实体，会被随后写完的批次重新写回去，留下
「有向量、无正文」的孤儿向量，检索结果被稀释。处置：每步之前确认单元还在；确实没了就回滚、
清掉本次写入的检索副本与对象，按 `skipped` 收场——这不是导入失败，重试也没有目标。

原件与解析产物都在对象存储（TECH_SPEC §8.0）：导入时先把原件取回本地临时文件交给
MinerU（它只接受路径），再把返回的原始 ZIP 归档到 `parsed/` 前缀供解图与重放。

并发：同一 `unit_id` 同时只允许一条导入生效（Redis 锁）。否则「重投 + 重试」并发时
会出现两份切片，Milvus 也会有重复实体（不变量：同一单元不产生重复切片）。

阻塞调用（切分/编码/Milvus）统一走 `asyncio.to_thread`：worker 的循环虽是任务私有，
但本模块也会被测试与将来可能的同步入口调用，不应阻塞事件循环。

连接类资源（数据库、Redis）都必须**按事件循环新建**：`asyncio.run` 每次都换循环，
复用进程级单例会报 `got Future attached to a different loop`（实际缺陷：worker 进程里
只有第一次导入能成功）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.common.config import settings
from app.common.redis import new_redis
from app.engines.chunking import Chunker
from app.engines.embed import Embedder, get_embedder
from app.engines.mineru import (
    ExtractedAsset,
    MinerUClient,
    ParsedDocument,
    extract_assets,
)
from app.engines.retrieve import milvus_store
from app.engines.retrieve.milvus_store import ChunkVector
from app.engines.storage import object_store
from app.engines.vision import CAPTION_PREFIX, VisionClient
from app.models.knowledge import Chunk, IngestTask, KnowledgeUnit, KnowledgeUnitAsset
from app.repositories import faq as faq_repo

logger = logging.getLogger(__name__)

STAGE_QUEUED = "queued"
STAGE_PARSING = "parsing"
STAGE_CHUNKING = "chunking"
STAGE_EMBEDDING = "embedding"
STAGE_INDEXED = "indexed"
STAGE_FAILED = "failed"

# 阶段开始时的进度百分比；embedding 阶段内部再按批推进到 95
PROGRESS = {
    STAGE_QUEUED: 0,
    STAGE_PARSING: 10,
    STAGE_CHUNKING: 50,
    STAGE_EMBEDDING: 80,
    STAGE_INDEXED: 100,
    STAGE_FAILED: 100,
}
# 编码分批大小：一批编码完就刷新进度，大文档不至于长时间没反馈
EMBED_BATCH_SIZE = 64
# 图注生成期间的进度上限：与 chunking(50) 留出间隔，避免观感上的进度回退
VISION_PROGRESS_CEIL = 45
# 单单元导入锁的 TTL（兜底：worker 崩了也不会永久锁死）
INGEST_LOCK_TTL_SECONDS = 3600
ERROR_MESSAGE_LIMIT = 500
# 单元中途被删除时写进 `IngestOutcome.error` 的说明（任务已作废，不是失败）
UNIT_GONE_MESSAGE = "知识单元已删除，本次导入作废"

_chunker = Chunker()


class UnitGoneError(Exception):
    """导入过程中单元被删除：当次导入作废，且不允许再往该单元写任何东西。"""


@dataclass
class IngestOutcome:
    unit_id: UUID
    # indexed | failed | skipped
    status: str
    # 写入 PG 的切片行数（父 + 子）
    chunks: int = 0
    # 写入 Milvus 的向量数（只有子块入库）
    vectors: int = 0
    error: str | None = None


def safe_error(exc: BaseException) -> str:
    """把异常转成可入库、可展示的短消息：截断 + 抹掉 MinerU 密钥。"""

    message = f"{type(exc).__name__}: {exc}"[:ERROR_MESSAGE_LIMIT]
    api_key = settings.mineru_api_key
    if api_key:
        message = message.replace(api_key, "***")
    return message


@asynccontextmanager
async def _unit_lock(unit_id: UUID) -> AsyncIterator[bool]:
    """同一知识单元的导入互斥锁；未拿到锁返回 False。

    用 `new_redis()` 而不是缓存的 `get_redis()`：Celery 每个任务一个新事件循环，
    复用缓存的客户端会把旧循环的连接带进来（`got Future attached to a different loop`）。
    """

    redis = new_redis()
    key = f"kb:ingest:lock:{unit_id}"
    try:
        acquired = await redis.set(key, "1", nx=True, ex=INGEST_LOCK_TTL_SECONDS)
        try:
            yield bool(acquired)
        finally:
            if acquired:
                await redis.delete(key)
    finally:
        await redis.aclose()


async def run_ingest(
    session: AsyncSession,
    unit_id: UUID,
    *,
    task_id: str | None = None,
    mineru: MinerUClient | None = None,
    embedder: Embedder | None = None,
    vision: VisionClient | None = None,
) -> IngestOutcome:
    """跑完一个知识单元的导入流水线；异常不外抛，统一收敛为 failed。"""

    async with _unit_lock(unit_id) as acquired:
        if not acquired:
            logger.warning("知识单元 %s 已有导入在跑，本次跳过以避免重复切片", unit_id)
            return IngestOutcome(unit_id=unit_id, status="skipped")
        logger.debug("单元 %s：已取得导入锁", unit_id)

        unit = await session.get(KnowledgeUnit, unit_id)
        if unit is None:
            logger.warning("知识单元 %s 已不存在，跳过导入", unit_id)
            return IngestOutcome(unit_id=unit_id, status="skipped")
        logger.debug("单元 %s：已读取行，开始跑阶段", unit_id)

        try:
            return await _run_stages(
                session,
                unit,
                task_id=task_id,
                mineru=mineru or MinerUClient(),
                embedder=embedder or get_embedder(),
                vision=vision or VisionClient(),
            )
        except (StaleDataError, UnitGoneError) as exc:
            message = safe_error(exc)
            if not await _unit_vanished(session, unit_id):
                # 行还在：不是「被删」，照常按失败收敛（重试仍有目标）
                logger.exception("知识单元 %s 导入失败：%s", unit_id, message)
                await _mark_failed(session, unit_id, task_id=task_id, error=message)
                return IngestOutcome(unit_id=unit_id, status="failed", error=message)
            logger.warning("知识单元 %s 已在导入过程中被删除，本次导入作废：%s", unit_id, message)
            await _discard_ingested_artifacts(unit_id)
            return IngestOutcome(unit_id=unit_id, status="skipped", error=UNIT_GONE_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - 任一步失败都必须收敛成 failed（P15）
            message = safe_error(exc)
            logger.exception("知识单元 %s 导入失败：%s", unit_id, message)
            await _mark_failed(session, unit_id, task_id=task_id, error=message)
            return IngestOutcome(unit_id=unit_id, status="failed", error=message)


async def _run_stages(
    session: AsyncSession,
    unit: KnowledgeUnit,
    *,
    task_id: str | None,
    mineru: MinerUClient,
    embedder: Embedder,
    vision: VisionClient,
) -> IngestOutcome:
    # ⓪ 先清上一次的产物：切片、向量与图片。必须排在重建之前——否则 ③ 的图片资产
    # 会被随后的一次清理删掉，留下“表里有行、对象已没”的不一致（P16）
    await _clear_artifacts(session, unit.id)

    # ① 解析：文本直读、PDF/DOCX 走 MinerU
    await _advance(session, unit, task_id, STAGE_PARSING)
    logger.debug("单元 %s：进入 parsing", unit.id)
    document = await _parse(session, unit, mineru)
    logger.debug("单元 %s：解析完成，%s 字符", unit.id, len(document.markdown))

    # ② 图片资产：解出、生成图注，写对象存储与 assets 表，正文引用改为相对 key
    markdown = await _ingest_assets(session, unit, document, vision, task_id)
    # parsed_text 存改写后的正文：管理员核对“为什么这段没被检索到”时看到的就是切块输入
    unit.parsed_text = markdown
    await session.flush()

    # ③ 切块
    await _advance(session, unit, task_id, STAGE_CHUNKING)
    parent_count, children = await _write_chunks(session, unit, markdown)
    logger.debug("单元 %s：切块完成，父 %s / 子 %s", unit.id, parent_count, len(children))

    # ④ 编码并写 Milvus
    await _advance(session, unit, task_id, STAGE_EMBEDDING)
    await _index_vectors(session, unit, task_id, children, embedder)
    logger.debug("单元 %s：向量写入完成", unit.id)

    # ⑤ 收尾：只有走到这里才算可检索
    await _advance(session, unit, task_id, STAGE_INDEXED)
    await _settle_gap(session, unit)
    logger.info(
        "知识单元 %s 导入完成：父块 %s / 子块 %s", unit.id, parent_count, len(children)
    )
    return IngestOutcome(
        unit_id=unit.id,
        status=STAGE_INDEXED,
        chunks=parent_count + len(children),
        vectors=len(children),
    )


@asynccontextmanager
async def _local_source(unit: KnowledgeUnit) -> AsyncIterator[Path]:
    """把原件准备成本地可读路径，供 MinerU 上传。

    对象存储是权威源（`source_path` 存对象 key）；历史单元可能仍是本地路径。
    从对象存储取回的一律写临时文件，退出时删除——MinerU 只接受路径，不要字节流。
    """

    source = unit.source_path
    if not source.startswith(f"{object_store.UNIT_PREFIX}/"):
        path = Path(source)
        if not path.exists():
            # 只报文件名：parse_error 会展示给管理员，不回显服务器路径
            raise FileNotFoundError(f"原件不存在：{unit.source_filename}")
        yield path
        return

    suffix = Path(unit.source_filename).suffix or ".bin"
    handle, name = tempfile.mkstemp(prefix="kb_source_", suffix=suffix)
    os.close(handle)
    path = Path(name)
    try:
        data = await object_store.get_bytes(source)
        await asyncio.to_thread(path.write_bytes, data)
        yield path
    finally:
        path.unlink(missing_ok=True)


async def _archive_parsed_zip(unit: KnowledgeUnit, document: ParsedDocument) -> None:
    """把原始 ZIP 归档到对象存储 `parsed/` 前缀（解图与重放都以它为准）。

    失败只告警：正文已经拿到，归档属于增强（与 ZIP 本地保存失败同一策略）；
    本地保存失败的单元直接跳过，不影响导入结果。
    """

    if document.zip_path is None:
        return
    key = object_store.parsed_key(unit.id, Path(unit.source_filename).stem)
    if key is None:
        logger.warning("解析结果归档跳过：文件名不可用（unit=%s）", unit.id)
        return
    try:
        content = await asyncio.to_thread(document.zip_path.read_bytes)
        await object_store.put_bytes(key, content, content_type="application/zip")
    except (OSError, object_store.ObjectStoreError):
        logger.warning("解析结果归档失败：unit=%s", unit.id, exc_info=True)


async def _parse(
    session: AsyncSession, unit: KnowledgeUnit, mineru: MinerUClient
) -> ParsedDocument:
    """解析原件并归档原始 ZIP，返回文档对象（正文 + ZIP 本地路径）。"""

    async with _local_source(unit) as path:
        documents = await mineru.parse_documents([path])
        document = documents.get(str(path))
        if document is None or not document.markdown.strip():
            raise ValueError(f"解析结果为空：{unit.source_filename}")
        await _archive_parsed_zip(unit, document)
    return document


# Markdown 图片引用：`![alt](target)`
_IMAGE_REFERENCE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def _rewrite_image_refs(
    markdown: str, name_by_source: dict[str, str], captions: dict[str, str]
) -> str:
    """把正文里的图片引用改成相对 key（`assets/{name}`），并在其后插入图注行。

    只认相对 key：**不得**写入对象存储 endpoint、桶名或预签名 URL（P17）——地址变更
    不该触发全量重嵌，长 URL 还会污染 sparse 词面权重。未命中映射的引用原样保留。
    图注与图片之间空一行（Markdown 惯例），切块器保证两者同块（P18）。
    """

    def _replace(match: re.Match[str]) -> str:
        alt, target = match.group(1), match.group(2)
        name = name_by_source.get(target)
        if name is None:
            # ZIP 条目名可能带目录，按 basename 再匹配一次
            name = name_by_source.get(target.replace("\\", "/").split("/")[-1])
        if name is None:
            return match.group(0)
        reference = f"![{alt}]({object_store.ASSETS_DIR}/{name})"
        caption = captions.get(name)
        if caption is None:
            return reference
        return f"{reference}\n\n{CAPTION_PREFIX}{caption}"

    return _IMAGE_REFERENCE.sub(_replace, markdown)


async def _report_vision_progress(
    session: AsyncSession,
    task_id: str | None,
    done: int,
    total: int,
) -> None:
    """把图注进度映射到 parsing 阶段内（10 → 45）：pdf/docx 导入会被拉到分钟级。"""

    if task_id is None or total <= 0:
        return
    span = VISION_PROGRESS_CEIL - PROGRESS[STAGE_PARSING]
    progress = PROGRESS[STAGE_PARSING] + int(span * done / total)
    await _mark_task(session, task_id, STAGE_PARSING, min(progress, VISION_PROGRESS_CEIL))
    await session.commit()


async def _captions_for(
    session: AsyncSession,
    unit: KnowledgeUnit,
    assets: list[ExtractedAsset],
    vision: VisionClient,
    task_id: str | None,
) -> list[str | None]:
    """并发生成图注，顺序与入参一致；超上限的图不生成，单张失败降为 `None`。

    并发上限在 `VisionClient` 内部（`VISION_CONCURRENCY`）；这里只负责限张数与报进度。
    """

    if not vision.enabled:
        return [None] * len(assets)

    limit = max(1, settings.vision_max_images_per_unit)
    selected = assets[:limit]
    if len(assets) > limit:
        logger.info(
            "单元 %s 图片 %s 张超过视觉上限 %s，其余不生成图注",
            unit.id,
            len(assets),
            limit,
        )

    total = len(selected)
    done = 0
    # AsyncSession 不是并发安全的：进度落库串行化，识别图片的调用仍并发
    lock = asyncio.Lock()

    async def _one(asset: ExtractedAsset) -> str | None:
        nonlocal done
        caption = await vision.caption_image(asset.content, asset.media_type)
        async with lock:
            done += 1
            await _report_vision_progress(session, task_id, done, total)
        return caption

    captions = list(await asyncio.gather(*(_one(asset) for asset in selected)))
    return captions + [None] * (len(assets) - total)


async def _ingest_assets(
    session: AsyncSession,
    unit: KnowledgeUnit,
    document: ParsedDocument,
    vision: VisionClient,
    task_id: str | None,
) -> str:
    """解出图片与图注，写入对象存储与 assets 表，并返回改写后的正文。

    图片是增强、正文才是检索本体，因此任一环节失败都只告警、不阻断导入（TECH_SPEC §8.0）。
    `zip_path` 为空（文本直读或 ZIP 保存失败）时直接返回原文。
    """

    markdown = document.markdown
    if document.zip_path is None:
        return markdown

    try:
        assets = await asyncio.to_thread(extract_assets, document.zip_path)
    except OSError:
        logger.warning("图片解出失败：unit=%s", unit.id, exc_info=True)
        return markdown
    if not assets:
        return markdown

    captions = await _captions_for(session, unit, assets, vision, task_id)

    name_by_source: dict[str, str] = {}
    captions_by_name: dict[str, str] = {}
    rows: list[KnowledgeUnitAsset] = []
    written: list[str] = []
    try:
        for asset, caption in zip(assets, captions, strict=True):
            key = object_store.asset_key(unit.id, asset.name)
            if key is None:
                continue
            await object_store.put_bytes(
                key, asset.content, content_type=asset.media_type
            )
            written.append(key)
            name_by_source[asset.source] = asset.name
            if caption:
                captions_by_name[asset.name] = caption
            rows.append(
                KnowledgeUnitAsset(
                    unit_id=unit.id,
                    rel_path=f"{object_store.ASSETS_DIR}/{asset.name}",
                    name=asset.name,
                    media_type=asset.media_type,
                    byte_size=len(asset.content),
                    sha256=hashlib.sha256(asset.content).hexdigest(),
                    caption=caption,
                    ordinal=asset.ordinal,
                )
            )
    except object_store.ObjectStoreError:
        # 写到一半失败：清掉已写对象，正文退回无图状态（重导会重来一遍）
        logger.warning("图片写入对象存储失败：unit=%s", unit.id, exc_info=True)
        for key in written:
            try:
                await object_store.delete(key)
            except object_store.ObjectStoreError:
                logger.warning("回滚图片对象失败：%s", key, exc_info=True)
        return markdown

    session.add_all(rows)
    await session.flush()
    logger.info(
        "单元 %s 图片资产：%s 张，图注 %s 条", unit.id, len(rows), len(captions_by_name)
    )
    return _rewrite_image_refs(markdown, name_by_source, captions_by_name)


async def _write_chunks(
    session: AsyncSession, unit: KnowledgeUnit, markdown: str
) -> tuple[int, list[Chunk]]:
    """切分并写 chunks，返回 (父块数, 子块行)——子块才进 Milvus。"""

    parents = await asyncio.to_thread(_chunker.split, markdown)
    if not parents:
        raise ValueError("切分结果为空：文档里没有可索引内容")

    children: list[Chunk] = []
    for parent in parents:
        # 显式给父块 id：列默认值要等 INSERT 才生效，而子块现在就要引用它（
        # 否则 parent_id 写进去的是 None，父块回溯与上下文拼接全部失效）
        parent_row = Chunk(
            id=uuid4(),
            unit_id=unit.id,
            level="parent",
            ordinal=parent.ordinal,
            content=parent.content,
            char_count=parent.char_count,
            content_hash=parent.content_hash,
        )
        session.add(parent_row)
        for child in parent.children:
            child_row = Chunk(
                unit_id=unit.id,
                parent_id=parent_row.id,  # 父块 id 已显式生成，无需先 flush
                level="child",
                ordinal=child.ordinal,
                content=child.content,
                char_count=child.char_count,
                content_hash=child.content_hash,
            )
            session.add(child_row)
            children.append(child_row)

    await session.flush()
    return len(parents), children


async def _index_vectors(
    session: AsyncSession,
    unit: KnowledgeUnit,
    task_id: str | None,
    children: list[Chunk],
    embedder: Embedder,
) -> None:
    """分批编码并写 Milvus；`enabled` 跟随知识单元当前启用态。"""

    if not children:
        return

    enabled = unit.status == "enabled"
    total = len(children)
    for start in range(0, total, EMBED_BATCH_SIZE):
        batch = children[start : start + EMBED_BATCH_SIZE]
        embedding = await asyncio.to_thread(
            embedder.encode, [row.content for row in batch]
        )
        vectors = [
            ChunkVector(
                chunk_id=str(row.id),
                unit_id=str(unit.id),
                parent_id=str(row.parent_id),
                enabled=enabled,
                dense=embedding.dense[index].tolist(),
                sparse=embedding.sparse[index],
            )
            for index, row in enumerate(batch)
        ]
        # 编码是分钟级里最慢的一段，期间单元很可能已被删：写之前再确认一次，
        # 避免把向量写到不存在的单元上（这一步没拦住也有兜底清理）
        if not await _unit_alive(session, unit.id):
            raise UnitGoneError(f"知识单元 {unit.id} 已不存在")
        await asyncio.to_thread(milvus_store.upsert_chunks, vectors)

        done = start + len(batch)
        progress = PROGRESS[STAGE_EMBEDDING] + int(
            15 * done / total  # 80 → 95
        )
        await _mark_task(session, task_id, STAGE_EMBEDDING, min(progress, 95))
        await session.commit()


async def _settle_gap(session: AsyncSession, unit: KnowledgeUnit) -> None:
    """把由本单元补上的知识缺口置为 `filled`（tasklist 13.1，P16）。

    只在 `indexed` 之后调用：解析失败或中断时缺口要留在 `converted`，等管理员重试——
    文档没进检索库就宣称「已补全」，缺口清单就失去了意义。
    """

    settled = await faq_repo.mark_gap_filled_by_unit(session, unit.id)
    if settled:
        await session.commit()
        logger.info("知识单元 %s 已补全 %s 个知识缺口", unit.id, settled)


# --- 阶段与清理 -----------------------------------------------------------


async def _advance(
    session: AsyncSession, unit: KnowledgeUnit, task_id: str | None, stage: str
) -> None:
    """推进阶段并提交：接口侧要能立刻轮询到新进度。

    每步之前先确认单元还在：排查要跑分钟级，这期间被删掉就应当立刻收手，而不是接着切块、
    编码、往 Milvus 写孤儿向量，最后在某个 `commit()` 上撞 `StaleDataError`。
    """

    if not await _unit_alive(session, unit.id):
        raise UnitGoneError(f"知识单元 {unit.id} 已不存在")

    unit.parse_status = stage
    if stage == STAGE_INDEXED:
        unit.parse_error = None
    await _mark_task(session, task_id, stage, PROGRESS[stage])
    await session.commit()


async def _unit_alive(session: AsyncSession, unit_id: UUID) -> bool:
    """单元行是否还在（只查主键，不加载整行）。"""

    return (
        await session.execute(select(KnowledgeUnit.id).where(KnowledgeUnit.id == unit_id))
    ).scalar_one_or_none() is not None


async def _unit_vanished(session: AsyncSession, unit_id: UUID) -> bool:
    """先回滚脏事务，再回单元是否已被删除。

    失败往往就发生在 flush/commit 上，此时事务已被服务端中止，不回滚连一句 SELECT 都发不出去。
    """

    await session.rollback()
    return not await _unit_alive(session, unit_id)


async def _discard_ingested_artifacts(unit_id: UUID) -> None:
    """单元已不在库里时，清掉本次导入可能写下的检索副本与对象。

    必要性来自真机现象：`delete_unit` 只在它那一刻清 Milvus，而解析任务若在那之后才写
    （或正在写一批），实体就会被写回去——单元已经没了，这些向量永远没人清，检索结果里就
    多出一批无法溯源的噪声。全单元前缀一起清是安全的：单元 id 不会重用，删了就代表这
    份文档整体作废。
    """

    try:
        await asyncio.to_thread(milvus_store.delete_by_unit, str(unit_id))
    except Exception:  # noqa: BLE001 - 收尾失败只能记日志，不能反过来把任务弄挂
        logger.exception("清理已删单元 %s 的向量失败", unit_id)
    try:
        await object_store.delete_unit_prefix(unit_id)
    except object_store.ObjectStoreError:
        logger.warning("清理已删单元 %s 的对象失败", unit_id, exc_info=True)


async def _mark_task(
    session: AsyncSession,
    task_id: str | None,
    stage: str,
    progress: int,
    error: str | None = None,
) -> None:
    if not task_id:
        return
    task = await session.get(IngestTask, task_id)
    if task is None:
        logger.warning("导入任务行不存在，跳过进度更新：%s", task_id)
        return
    task.stage = stage
    task.progress = progress
    if error is not None:
        task.error = error


async def _clear_artifacts(session: AsyncSession, unit_id: UUID) -> None:
    """删除该单元已写入的 PG 切片、Milvus 实体与图片对象（重试前与失败清理共用）。

    只清 `assets/`：原件与解析产物在重导时还要用（重导会覆盖写入）。
    """

    await session.execute(delete(Chunk).where(Chunk.unit_id == unit_id))
    await session.flush()
    await asyncio.to_thread(milvus_store.delete_by_unit, str(unit_id))
    try:
        await object_store.delete_prefix(object_store.assets_prefix(unit_id))
    except object_store.ObjectStoreError:
        logger.warning("清理单元图片对象失败：%s", unit_id, exc_info=True)


async def _mark_failed(
    session: AsyncSession,
    unit_id: UUID,
    *,
    task_id: str | None,
    error: str,
) -> None:
    """失败收敛（P15）：先清残留，再落 failed 状态。

    进来时事务多半是脏的（失败往往就发生在 flush/commit 上），所以第一件事就是回滚：否则
    连清理用的 DELETE 与状态 UPDATE 都会被 `PendingRollbackError` 挡住，任务带着一串
    traceback 退出、`ingest_tasks` 停在半路（真机现象）。

    只接 `unit_id` 而不是 ORM 实例：回滚会把实例上的属性全部 expire 掉，再去读 `unit.id`
    会触发一次异步环境里做不了的懒加载。
    """

    await session.rollback()

    try:
        await _clear_artifacts(session, unit_id)
    except Exception:  # noqa: BLE001 - 清理失败不能掩盖原始失败原因
        logger.exception("清理知识单元 %s 的残留切片失败", unit_id)
        await session.rollback()

    try:
        # 单元可能在失败前后被删除：没有可写的状态，也不该把一条不存在的导入报成 failed
        unit = await session.get(KnowledgeUnit, unit_id)
        if unit is None:
            logger.info("知识单元 %s 已删除，跳过 failed 状态写入", unit_id)
            return
        unit.parse_status = STAGE_FAILED
        unit.parse_error = error
        await _mark_task(session, task_id, STAGE_FAILED, PROGRESS[STAGE_FAILED], error)
        await session.commit()
    except Exception:  # noqa: BLE001 - 落状态再失败就只能记日志了
        logger.exception("标记知识单元 %s 为 failed 时出错", unit_id)
        await session.rollback()
