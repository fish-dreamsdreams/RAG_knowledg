"""FAQ 审核、发布、维护与缓存同步（tasklist 11.2 / 11.3）。

数据库是权威，Redis 是它的投影；两者不同步的后果是「改了答案但命中还返回旧的」。所以每次
写库之后都要同步缓存，且按操作选代价最小的方式：

- 新增 / 改答案 / 重新启用 → `upsert` 单条（含编码问句）
- 切 `cache_enabled=false` → `set_enabled`，无需重建
- 下线 → `remove`，Hash 与向量一起删

**缓存同步失败不回滚数据库**：权威数据已经写对，缓存随时可由 `rebuild_cache` 重放。为了一个
可重建的投影而回滚已提交的业务操作，是把代价搞反了。

编码是同步的 GPU 推理，一律经 `run_in_threadpool`，否则会阻塞整个事件循环。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.common.errors import AppError
from app.common.redis import get_redis
from app.engines import faq_cache
from app.engines.embed import get_embedder
from app.models import Faq, FaqCandidate
from app.repositories import faq as faq_repo
from app.schemas.faq import CandidateItem, FaqItem, FaqUpdate

logger = logging.getLogger(__name__)


def _client(redis: Redis | None) -> Redis:
    """默认取进程单例；Celery 任务应显式传 `new_redis()`（事件循环不同）。"""

    return redis if redis is not None else get_redis()


def _dump_candidate(row: FaqCandidate) -> CandidateItem:
    return CandidateItem(
        candidate_id=row.id,
        question=row.representative_question,
        similar_questions=list(row.similar_questions or []),
        freq=row.freq,
        confidence=row.confidence,
        suggested_answer=row.suggested_answer,
        source_unit_ids=list(row.source_unit_ids or []),
        status=row.status,
        reject_reason=row.reject_reason,
        created_at=row.created_at,
    )


def _dump_faq(row: Faq) -> FaqItem:
    return FaqItem(
        faq_id=row.id,
        question=row.question,
        answer=row.answer,
        cache_enabled=row.cache_enabled,
        status=row.status,
        source_candidate_id=row.source_candidate_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _encode_many(questions: Sequence[str]) -> list[list[float]]:
    """批量编码问句。**同步**函数，调用方必须用 `run_in_threadpool` 包住。"""

    if not questions:
        return []
    embedding = get_embedder().encode(list(questions), return_sparse=False)
    return [[float(value) for value in row] for row in embedding.dense]


def _entry(item: FaqItem, vector: Sequence[float]) -> faq_cache.FaqEntry:
    return faq_cache.FaqEntry(
        faq_id=str(item.faq_id),
        question=item.question,
        answer=item.answer,
        enabled=item.cache_enabled and item.status == faq_repo.FAQ_PUBLISHED,
        vector=vector,
    )


async def _sync_upsert(client: Redis, item: FaqItem) -> None:
    vectors = await run_in_threadpool(_encode_many, [item.question])
    await faq_cache.upsert(client, _entry(item, vectors[0]))


async def list_candidates(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    status: str | None = None,
) -> tuple[list[CandidateItem], int]:
    rows, total = await faq_repo.list_candidates(
        session, offset=offset, limit=limit, status=status
    )
    return [_dump_candidate(row) for row in rows], total


async def publish_candidate(
    session: AsyncSession,
    candidate_id: UUID,
    *,
    answer: str | None,
    reviewer_id: UUID,
    redis: Redis | None = None,
) -> FaqItem:
    """发布候选：建 FAQ、关闭候选、同步缓存。

    `answer` 省略时回退到候选的 `suggested_answer`；两者都没有就拒绝发布——一条没有答案的
    FAQ 会命中却答不出内容，比不命中更糟。
    """

    candidate = await faq_repo.get_candidate(session, candidate_id)
    if candidate is None:
        raise AppError.not_found("FAQ 候选不存在")
    if candidate.status != faq_repo.CANDIDATE_PENDING:
        raise AppError.candidate_closed()

    final_answer = (answer or candidate.suggested_answer or "").strip()
    if not final_answer:
        raise AppError.validation("发布前必须提供答案")

    faq = await faq_repo.create_faq(
        session,
        question=candidate.representative_question,
        answer=final_answer,
        cache_enabled=True,
        status=faq_repo.FAQ_PUBLISHED,
        source_candidate_id=candidate.id,
        created_by=reviewer_id,
    )
    candidate.status = faq_repo.CANDIDATE_PUBLISHED
    candidate.reviewed_by = reviewer_id
    candidate.reviewed_at = datetime.now(UTC)

    # 必须在 commit 之前取值：commit 会让实例过期，之后访问属性会触发懒加载
    result = _dump_faq(faq)
    await session.commit()

    await _sync_upsert(_client(redis), result)
    return result


async def reject_candidate(
    session: AsyncSession,
    candidate_id: UUID,
    *,
    reason: str,
    reviewer_id: UUID,
) -> CandidateItem:
    """驳回候选。不动已发布 FAQ，因此无需同步缓存。"""

    candidate = await faq_repo.get_candidate(session, candidate_id)
    if candidate is None:
        raise AppError.not_found("FAQ 候选不存在")
    if candidate.status != faq_repo.CANDIDATE_PENDING:
        raise AppError.candidate_closed()

    candidate.status = faq_repo.CANDIDATE_REJECTED
    candidate.reject_reason = reason
    candidate.reviewed_by = reviewer_id
    candidate.reviewed_at = datetime.now(UTC)

    result = _dump_candidate(candidate)
    await session.commit()
    return result


async def list_faqs(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    keyword: str | None = None,
    status: str | None = None,
) -> tuple[list[FaqItem], int]:
    rows, total = await faq_repo.list_faqs(
        session, offset=offset, limit=limit, keyword=keyword, status=status
    )
    return [_dump_faq(row) for row in rows], total


async def update_faq(
    session: AsyncSession,
    faq_id: UUID,
    payload: FaqUpdate,
    *,
    redis: Redis | None = None,
) -> FaqItem:
    """改答案 / 切 `cache_enabled` / 下线，并让缓存与之保持一致。"""

    faq = await faq_repo.get_faq(session, faq_id)
    if faq is None:
        raise AppError.not_found("FAQ 不存在")

    if payload.answer is not None:
        faq.answer = payload.answer
    if payload.cache_enabled is not None:
        faq.cache_enabled = payload.cache_enabled
    if payload.status is not None:
        faq.status = payload.status

    result = _dump_faq(faq)
    await session.commit()

    await _sync_after_update(_client(redis), result)
    return result


async def delete_faq(
    session: AsyncSession, faq_id: UUID, *, redis: Redis | None = None
) -> FaqItem:
    """删除 FAQ 行并清掉缓存条目（维护脚本用，控制台不提供删除入口）。

    下线走 `update_faq`（保留行、只改状态）；真删是清历史遗留——比如重复问句的多余行。

    顺序：**先清缓存再提交删除**。反过来的话，删库成功而清缓存失败（或中途被杀）会留下
    一条命中得了、却已经不在库里的问答，用户拿到一个查不到来源的答案；而现在的最坏情况是
    暂少一次命中，`rebuild_cache` 一跑就恢复。
    """

    faq = await faq_repo.get_faq(session, faq_id)
    if faq is None:
        raise AppError.not_found("FAQ 不存在")

    result = _dump_faq(faq)
    await faq_cache.remove(_client(redis), str(faq_id))
    await faq_repo.delete_faq(session, faq)
    await session.commit()
    return result


@dataclass(frozen=True)
class DuplicateGroup:
    """同一问句的多条 FAQ（按 `faq_cache.normalize` 归并）。

    `keep` 是保留项（`created_at` 最早的一条，即最初那条），`drop` 是待删的其余行。
    `answers_differ` 供调用方提醒人先看一眼：保留口径只认时间，而最早的那条未必是答复更好的那条。
    """

    key: str
    keep: FaqItem
    drop: list[FaqItem]

    @property
    def answers_differ(self) -> bool:
        return len({item.answer for item in [self.keep, *self.drop]}) > 1


async def find_duplicate_faqs(session: AsyncSession) -> list[DuplicateGroup]:
    """找问句重复的 FAQ 分组（归一化口径与命中判定一致）。

    维护脚本与清场工具共用一处实现：两份分组逻辑迟早会漂，而它们的差异恰好落在「删哪条」上。
    """

    grouped: dict[str, list[FaqItem]] = {}
    for row in await faq_repo.all_faqs(session):
        grouped.setdefault(faq_cache.normalize(row.question), []).append(_dump_faq(row))

    groups = [
        DuplicateGroup(key=key, keep=items[0], drop=items[1:])
        for key, items in grouped.items()
        if len(items) > 1
    ]
    groups.sort(key=lambda group: group.keep.created_at)
    return groups


async def delete_faqs(
    session: AsyncSession, faq_ids: Sequence[UUID], *, redis: Redis | None = None
) -> list[FaqItem]:
    """批量删除 FAQ（逐条走 `delete_faq`，保持「先清缓存再删行」的顺序）。"""

    deleted: list[FaqItem] = []
    for faq_id in faq_ids:
        deleted.append(await delete_faq(session, faq_id, redis=redis))
    return deleted


async def delete_candidate(session: AsyncSession, candidate_id: UUID) -> CandidateItem:
    """删除候选行（维护脚本用）。

    候选的正规出路是驳回（软路径、留审计）；删除只用于清脏数据——比如挖掘重复写出的同问句
    候选。引用它的 FAQ 的 `source_candidate_id` 会在仓储层置空，FAQ 本身不受影响。
    """

    candidate = await faq_repo.get_candidate(session, candidate_id)
    if candidate is None:
        raise AppError.not_found("候选不存在")

    result = _dump_candidate(candidate)
    await faq_repo.delete_candidate(session, candidate)
    await session.commit()
    return result


async def _sync_after_update(client: Redis, item: FaqItem) -> None:
    if item.status != faq_repo.FAQ_PUBLISHED:
        await faq_cache.remove(client, str(item.faq_id))
    elif not item.cache_enabled:
        # 条目可能压根不在缓存里（先下线再禁用），补一次完整写入
        if not await faq_cache.set_enabled(client, str(item.faq_id), False):
            await _sync_upsert(client, item)
    else:
        await _sync_upsert(client, item)


async def rebuild_cache(session: AsyncSession, *, redis: Redis | None = None) -> int:
    """从数据库全量重建 FAQ 缓存（种子加载、缓存漂移、换 Embedding 模型后调用）。

    问句一次性批量编码：逐条编码会把 N 次前向的开销叠加成 N 倍，而批量编码的显存占用
    由 batch_size 控制，是同一个量级。
    """

    rows = await faq_repo.published_faqs(session)
    client = _client(redis)

    if not rows:
        return await faq_cache.rebuild(client, [])

    items = [_dump_faq(row) for row in rows]
    vectors = await run_in_threadpool(_encode_many, [item.question for item in items])

    entries = [
        _entry(item, vector) for item, vector in zip(items, vectors)
    ]
    written = await faq_cache.rebuild(client, entries)
    logger.info("FAQ 缓存重建：写入 %s 条", written)
    return written
