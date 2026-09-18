"""FAQ 候选与已发布 FAQ 的数据访问（design.md §2：`repositories` 是唯一 SQL 出口）。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Faq, FaqCandidate
from app.models.chat import QaAuditLog
from app.models.faq import KnowledgeGap

CANDIDATE_PENDING = "pending"
CANDIDATE_PUBLISHED = "published"
CANDIDATE_REJECTED = "rejected"

FAQ_PUBLISHED = "published"
FAQ_OFFLINE = "offline"

# 缺口状态：open 未处理 → converted 已转建（等导入）→ filled 已补全（单元已 indexed）
GAP_OPEN = "open"
GAP_CONVERTED = "converted"
GAP_FILLED = "filled"


async def _count(session: AsyncSession, statement: Select) -> int:
    """统计同一组过滤条件下的总行数（分页要真实 total，不是当前页长度）。"""

    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    return int(total or 0)


async def list_candidates(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    status: str | None = None,
) -> tuple[list[FaqCandidate], int]:
    """候选列表。默认按频次降序：高频问题先审，收益最大。"""

    statement = select(FaqCandidate)
    if status is not None:
        statement = statement.where(FaqCandidate.status == status)

    total = await _count(session, statement)
    rows = (
        (
            await session.execute(
                statement.order_by(
                    FaqCandidate.freq.desc(), FaqCandidate.created_at.desc()
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def get_candidate(
    session: AsyncSession, candidate_id: UUID
) -> FaqCandidate | None:
    return await session.get(FaqCandidate, candidate_id)


async def list_faqs(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    keyword: str | None = None,
    status: str | None = None,
) -> tuple[list[Faq], int]:
    statement = select(Faq)
    if status is not None:
        statement = statement.where(Faq.status == status)
    if keyword:
        pattern = f"%{keyword}%"
        statement = statement.where(
            or_(Faq.question.ilike(pattern), Faq.answer.ilike(pattern))
        )

    total = await _count(session, statement)
    rows = (
        (
            await session.execute(
                statement.order_by(Faq.created_at.desc()).offset(offset).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def get_faq(session: AsyncSession, faq_id: UUID) -> Faq | None:
    return await session.get(Faq, faq_id)


async def published_faqs(session: AsyncSession) -> list[Faq]:
    """缓存重建的输入：所有已发布 FAQ，**含 `cache_enabled=false`**。

    带上禁用项是有意的——缓存里保留它们，切换 `cache_enabled` 才能立刻生效而不必重建。
    跳过匹配是缓存层的事（`engines/faq_cache`）。
    """

    rows = (
        (
            await session.execute(
                select(Faq)
                .where(Faq.status == FAQ_PUBLISHED)
                .order_by(Faq.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def create_faq(session: AsyncSession, **fields) -> Faq:
    faq = Faq(**fields)
    session.add(faq)
    await session.flush()
    return faq


async def recent_unanswered_questions(
    session: AsyncSession, *, limit: int
) -> list[QaAuditLog]:
    """挖掘输入：**已作答但没命中 FAQ** 的问题。

    只取 `answer_status='answered'`：`gap`（检索不到任何内容）属于知识缺口，"补文档"才是
    正解，做成 FAQ 会把缺文档的问题掩盖掉；`denied` 是权限问题，与知识无关。
    """

    rows = (
        (
            await session.execute(
                select(QaAuditLog)
                .where(
                    QaAuditLog.faq_hit.is_(False),
                    QaAuditLog.answer_status == "answered",
                )
                .order_by(QaAuditLog.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def pending_candidates(session: AsyncSession) -> list[FaqCandidate]:
    """待审候选（挖掘要更新它们的频次，所以给行而不是给问句）。"""

    return await candidates_by_status(session, (CANDIDATE_PENDING,))


async def candidates_by_status(
    session: AsyncSession, statuses: Sequence[str]
) -> list[FaqCandidate]:
    """按状态取候选。已定论的候选与已发布 FAQ 一起构成挖掘的判重集合。"""

    rows = (
        (
            await session.execute(
                select(FaqCandidate).where(FaqCandidate.status.in_(statuses))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def all_faqs(session: AsyncSession) -> list[Faq]:
    """全部 FAQ，**含已下线**。

    挖掘判重要看到下线项：一条 FAQ 被下线是运营的决定，不该因为「表里查不到已发布行」
    又被当成新问题推回审核列表。
    """

    rows = (
        (await session.execute(select(Faq).order_by(Faq.created_at.asc())))
        .scalars()
        .all()
    )
    return list(rows)


async def delete_faq(session: AsyncSession, faq: Faq) -> None:
    """删除 FAQ 行（维护脚本用；缓存条目由服务层同步）。"""

    await session.delete(faq)
    await session.flush()


async def gaps_outside(
    session: AsyncSession, questions: Sequence[str]
) -> list[KnowledgeGap]:
    """报告用：问句不在给定白名单里的缺口行（演示种子外的那些）。"""

    statement = select(KnowledgeGap)
    if questions:
        statement = statement.where(KnowledgeGap.question_text.not_in(list(questions)))
    statement = statement.order_by(KnowledgeGap.created_at.asc())
    rows = (await session.execute(statement)).scalars().all()
    return list(rows)


async def count_gaps(
    session: AsyncSession, *, questions: Sequence[str] | None = None
) -> int:
    """缺口行数；给了 `questions` 就只数这些问句的行（清场报告用）。"""

    statement = select(func.count()).select_from(KnowledgeGap)
    if questions is not None:
        statement = statement.where(KnowledgeGap.question_text.in_(list(questions)))
    return int((await session.execute(statement)).scalar_one())


async def delete_gaps(session: AsyncSession, ids: Sequence[UUID]) -> int:
    """按 id 删除缺口行并提交，返回删除行数（维护脚本用）。"""

    if not ids:
        return 0
    result = await session.execute(delete(KnowledgeGap).where(KnowledgeGap.id.in_(ids)))
    await session.commit()
    return int(result.rowcount or 0)


async def delete_candidate(session: AsyncSession, candidate: FaqCandidate) -> None:
    """删除候选行；**先把引用它的 FAQ 的 `source_candidate_id` 置空**。

    真库事务上实测过：直接删被引用的候选会被外键挡下（`faqs_source_candidate_id_fkey`，
    约束是 NO ACTION，不是 CASCADE）。选择置空而不是连 FAQ 一起删：一条 FAQ 可能已经发布并
    命中过真实问答，它不该因为候选被清场而消失。
    """

    await session.execute(
        update(Faq)
        .where(Faq.source_candidate_id == candidate.id)
        .values(source_candidate_id=None)
    )
    await session.delete(candidate)
    await session.flush()


async def create_candidate(session: AsyncSession, **fields) -> FaqCandidate:
    candidate = FaqCandidate(**fields)
    session.add(candidate)
    await session.flush()
    return candidate


async def record_gap(
    session: AsyncSession,
    *,
    question: str,
    department_id: UUID | None,
    max_similarity: float,
) -> KnowledgeGap:
    """原子记录或聚合同一部门的知识缺口。

    缺口不是逐请求日志：同一问题被反复问应累加到 `freq`，供运营按收益排序。表上没有
    `(question_text, department_id, status)` 唯一约束，不能安全地靠"先查再改"实现聚合；先取得
    PostgreSQL 事务级 advisory lock（同一键在同一事务内串行），再读写。因此多 WebSocket
    worker 并发问同一问题也只会更新一行。哈希碰撞最多让两个无关问题额外串行，不会混数据。
    """

    lock_key = f"gap:{department_id or '-'}:{question}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )

    row = (
        await session.execute(
            select(KnowledgeGap)
            .where(
                KnowledgeGap.question_text == question,
                KnowledgeGap.department_id == department_id,
                KnowledgeGap.status == GAP_OPEN,
            )
            .order_by(KnowledgeGap.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        row = KnowledgeGap(
            question_text=question,
            department_id=department_id,
            freq=1,
            max_similarity=max_similarity,
            last_asked_at=now,
            status=GAP_OPEN,
        )
        session.add(row)
    else:
        row.freq += 1
        previous = row.max_similarity
        row.max_similarity = (
            max(previous, max_similarity) if previous is not None else max_similarity
        )
        row.last_asked_at = now
    await session.flush()
    return row


# --- 知识缺口（tasklist 13.1） ---------------------------------------------


async def list_gaps(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    status: str | None = None,
    department_id: UUID | None = None,
) -> tuple[list[KnowledgeGap], int]:
    """缺口列表。按频次降序：同样补一篇文档，高频缺口先补收益最大。"""

    statement = select(KnowledgeGap)
    if status is not None:
        statement = statement.where(KnowledgeGap.status == status)
    if department_id is not None:
        statement = statement.where(KnowledgeGap.department_id == department_id)

    total = await _count(session, statement)
    rows = (
        (
            await session.execute(
                statement.order_by(
                    KnowledgeGap.freq.desc(), KnowledgeGap.created_at.desc()
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def get_gap(session: AsyncSession, gap_id: UUID) -> KnowledgeGap | None:
    return await session.get(KnowledgeGap, gap_id)


async def mark_gap_converted(session: AsyncSession, gap: KnowledgeGap) -> None:
    """标记为「已转建」。此时还没有文档，`filled_unit_id` 由导入受理时回填。"""

    gap.status = GAP_CONVERTED
    await session.flush()


async def link_gap_to_unit(
    session: AsyncSession, gap: KnowledgeGap, unit_id: UUID
) -> None:
    """把缺口指向转建出来的知识单元（导入受理时调用）。"""

    gap.filled_unit_id = unit_id
    gap.status = GAP_CONVERTED
    await session.flush()


async def mark_gap_filled_by_unit(session: AsyncSession, unit_id: UUID) -> int:
    """单元索引完成后回填缺口状态，返回受影响的缺口数。

    以 `filled_unit_id` 反查而不是让导入链记住 gap_id：那样导入链要多传一个参数，
    重试路径也得跟着传，容易漏。
    """

    result = await session.execute(
        update(KnowledgeGap)
        .where(
            KnowledgeGap.filled_unit_id == unit_id,
            KnowledgeGap.status == GAP_CONVERTED,
        )
        .values(status=GAP_FILLED)
    )
    return int(result.rowcount or 0)


async def reopen_gaps_of_unit(session: AsyncSession, unit_id: UUID) -> int:
    """单元被删除时把由它补上的缺口退回 `open`，返回受影响的缺口数。

    必须做两件事：清掉 `filled_unit_id`（否则外键让单元根本删不掉，管理员会拿到 500），
    并把状态退回 `open`——知识没了缺口就还开着，留在 `filled` 会让缺口清单骗人。
    """

    result = await session.execute(
        update(KnowledgeGap)
        .where(KnowledgeGap.filled_unit_id == unit_id)
        .values(status=GAP_OPEN, filled_unit_id=None)
    )
    return int(result.rowcount or 0)
