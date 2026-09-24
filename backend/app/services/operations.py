"""运营闭环服务：知识缺口、审计查询与看板（tasklist 13.1 ~ 13.3）。

三块共用一套「时间窗」解析：看板的「今日 / 近 7 天」与审计查询的起止都要先换算到
`settings.report_timezone`（审计时间戳存 UTC），把边界收在这一处，免得每个接口各算一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.config import settings
from app.common.errors import AppError
from app.models.chat import QaAuditLog
from app.repositories import faq as faq_repo
from app.repositories import knowledge as knowledge_repo
from app.repositories import operations as operations_repo
from app.repositories import org as org_repo
from app.schemas.operations import (
    AuditItem,
    DashboardSummary,
    GapConvertPrefill,
    GapItem,
    TopKnowledgeItem,
    TopQuestionItem,
    TrendPoint,
    TrendSeries,
)

RANGE_TODAY = "today"
RANGE_7D = "7d"
# 转建预填标题的长度上限：够看清是什么问题，又不至于把整段追问塞进标题
SUGGESTED_TITLE_CHARS = 60


@dataclass(frozen=True)
class RangeWindow:
    """业务时区下的时间窗。`start` / `end` 已换算成 UTC，可直接用于 SQL 比较。"""

    key: str
    start: datetime
    end: datetime
    # 覆盖到的本地日期（含首含尾），趋势图按它补零
    days: list[date]


def resolve_range(range_key: str) -> RangeWindow:
    """`today` 或 `7d` → 时间窗；`7d` 指含今天在内的 7 个自然日。"""

    tz = ZoneInfo(settings.report_timezone)
    now_local = datetime.now(tz)
    today = now_local.date()
    first = today if range_key == RANGE_TODAY else today - timedelta(days=6)
    start_local = datetime.combine(first, time.min, tzinfo=tz)
    days = [first + timedelta(days=offset) for offset in range((today - first).days + 1)]
    return RangeWindow(
        key=range_key,
        start=start_local.astimezone(UTC),
        end=now_local.astimezone(UTC),
        days=days,
    )


def resolve_day_range(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    """日期区间（含首含尾）→ UTC 边界；两端都缺省时取最近 7 天。

    结束日期按「次日 00:00」换算：用户说「到 9 月 15 日」包含当天，写成 `< 15 日 00:00`
    会把当天的记录整段漏掉。
    """

    tz = ZoneInfo(settings.report_timezone)
    today = datetime.now(tz).date()
    last = end or today
    first = start or (last - timedelta(days=6))
    if first > last:
        raise AppError.validation("开始日期不能晚于结束日期")

    return (
        datetime.combine(first, time.min, tzinfo=tz).astimezone(UTC),
        datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC),
    )


def _rate(numerator: int, denominator: int) -> float:
    """比率为 0 的分母给 0：看板空态显示 0% 而不是让整个接口 500。"""

    return round(numerator / denominator, 4) if denominator else 0.0


def _suggested_title(question: str) -> str:
    text = question.strip()
    if len(text) <= SUGGESTED_TITLE_CHARS:
        return text
    return f"{text[:SUGGESTED_TITLE_CHARS]}…"


# --- 知识缺口（13.1） -------------------------------------------------------


async def list_gaps(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    status: str | None = None,
    department_id: UUID | None = None,
) -> tuple[list[GapItem], int]:
    rows, total = await faq_repo.list_gaps(
        session, offset=offset, limit=limit, status=status, department_id=department_id
    )
    names = await org_repo.department_names(
        session, {row.department_id for row in rows if row.department_id is not None}
    )
    items = [
        GapItem(
            gap_id=row.id,
            question_text=row.question_text,
            department_name=names.get(row.department_id),
            freq=row.freq,
            max_similarity=row.max_similarity,
            last_asked_at=row.last_asked_at,
            status=row.status,
            filled_unit_id=row.filled_unit_id,
        )
        for row in rows
    ]
    return items, total


async def convert_gap(session: AsyncSession, gap_id: UUID) -> GapConvertPrefill:
    """转建：把缺口钉在 `converted`，返回导入抽屉要的预填信息。

    这一步**不创建知识单元**——转建时还没有文档，单元由随后的导入接口带 `from_gap_id` 建，
    索引完成后由导入链把它置为 `filled`（P16）。
    """

    gap = await faq_repo.get_gap(session, gap_id)
    if gap is None:
        raise AppError.not_found("知识缺口不存在")
    if gap.status == faq_repo.GAP_FILLED:
        raise AppError.gap_not_convertible()

    await faq_repo.mark_gap_converted(session, gap)
    await session.commit()
    return GapConvertPrefill(
        gap_id=gap.id,
        status=gap.status,
        suggested_title=_suggested_title(gap.question_text),
    )


# --- 审计查询（13.2） -------------------------------------------------------


async def list_audit_logs(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    start: datetime,
    end: datetime,
    user_id: UUID | None = None,
    faq_hit: bool | None = None,
    answer_status: str | None = None,
) -> tuple[list[AuditItem], int]:
    rows, total = await operations_repo.list_audit_logs(
        session,
        offset=offset,
        limit=limit,
        start=start,
        end=end,
        user_id=user_id,
        faq_hit=faq_hit,
        answer_status=answer_status,
    )
    return [_audit_item(row) for row in rows], total


def _audit_item(row: QaAuditLog) -> AuditItem:
    return AuditItem(
        audit_id=row.id,
        trace_id=row.trace_id,
        user_id=row.user_id,
        session_id=row.session_id,
        question=row.question,
        rewritten=row.rewritten,
        faq_hit=row.faq_hit,
        allowed_unit_ids=[str(value) for value in (row.allowed_unit_ids or [])],
        denied_count=row.denied_count,
        citation_ids=[str(value) for value in (row.citation_ids or [])],
        answer_status=row.answer_status,
        max_similarity=row.max_similarity,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        latency_ms=row.latency_ms,
        created_at=row.created_at,
    )


# --- 看板（13.3） -----------------------------------------------------------


async def dashboard_summary(
    session: AsyncSession, *, range_key: str
) -> DashboardSummary:
    window = resolve_range(range_key)
    counts = await operations_repo.rates(session, start=window.start, end=window.end)
    knowledge_count = await knowledge_repo.count_units(session)

    pv = int(counts["pv"])
    return DashboardSummary(
        range=window.key,
        start=window.start,
        end=window.end,
        pv=pv,
        uv=int(counts["uv"]),
        knowledge_count=knowledge_count,
        faq_hit_rate=_rate(int(counts["faq_hits"]), pv),
        kb_coverage_rate=_rate(int(counts["covered"]), pv),
        avg_latency_ms=round(float(counts["avg_latency_ms"]), 1),
        p50_latency_ms=round(float(counts["p50_latency_ms"]), 1),
        p90_latency_ms=round(float(counts["p90_latency_ms"]), 1),
        total_tokens=int(counts["prompt_tokens"]) + int(counts["completion_tokens"]),
    )


async def top_questions(
    session: AsyncSession, *, range_key: str, limit: int
) -> list[TopQuestionItem]:
    window = resolve_range(range_key)
    rows = await operations_repo.top_questions(
        session, start=window.start, end=window.end, limit=limit
    )
    return [TopQuestionItem(question=question, hits=hits) for question, hits in rows]


async def top_knowledge(
    session: AsyncSession, *, range_key: str, limit: int
) -> list[TopKnowledgeItem]:
    window = resolve_range(range_key)
    rows = await operations_repo.top_knowledge(
        session, start=window.start, end=window.end, limit=limit
    )
    return [
        TopKnowledgeItem(unit_id=unit_id, title=title, hits=hits)
        for unit_id, title, hits in rows
    ]


async def token_trend(session: AsyncSession, *, range_key: str) -> TrendSeries:
    window = resolve_range(range_key)
    rows = await operations_repo.daily_metrics(
        session, start=window.start, end=window.end, tz=settings.report_timezone
    )
    by_day = {row["day"].date(): row for row in rows}

    points: list[TrendPoint] = []
    for day in window.days:
        row = by_day.get(day)
        if row is None:
            points.append(
                TrendPoint(
                    day=day,
                    pv=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    avg_latency_ms=0.0,
                    p50_latency_ms=0.0,
                    p90_latency_ms=0.0,
                )
            )
            continue
        prompt_tokens = int(row["prompt_tokens"])
        completion_tokens = int(row["completion_tokens"])
        points.append(
            TrendPoint(
                day=day,
                pv=int(row["pv"]),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                avg_latency_ms=round(float(row["avg_latency_ms"]), 1),
                p50_latency_ms=round(float(row["p50_latency_ms"]), 1),
                p90_latency_ms=round(float(row["p90_latency_ms"]), 1),
            )
        )
    return TrendSeries(range=window.key, points=points)
