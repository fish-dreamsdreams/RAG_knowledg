"""审计查询与看板聚合的数据访问（tasklist 13.2 / 13.3）。

看板的每个数字都由 `qa_audit_logs` 现算：这张表是问答的唯一全量事实（P12 每轮必写一条），
另建统计表就多一份可能对不上的副本，而演示规模下现算的代价可以忽略。

**时区**：`created_at` 是 timestamptz，库里存 UTC；「今日 / 近 7 天」与按天趋势一律按
`settings.report_timezone` 换算，否则北京时间上午 8 点前的提问会被算进前一天。

**口径**（PRD §8.3，服务层据此算比率，这里只回原始计数）：
PV = 提问轮次，UV = 独立 `user_id`，FAQ 命中率 = `faq_hit` 轮次 / 总轮次，
覆盖率 = 存在放行切片或 FAQ 直出的轮次 / 总轮次。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import QaAuditLog

# 榜单默认取前 10（PRD §6.6 的 TOP10）
DEFAULT_TOP_LIMIT = 10

# 覆盖率与 FAQ 命中率的分母：命中 FAQ 或至少有一条放行切片都算「答上了」
#
# `jsonb_typeof` 的 CASE 不能省：JSONB 列里的 Python `None` 默认落成 **JSON null** 而不是
# SQL NULL，而 `jsonb_array_length('null')` 在 PG 里是报错（不是回 NULL）。审计只增不删，
# 一条历史脏行就足以把整个看板打成 500。
_RATES_SQL = """
    SELECT
        count(*)                                                              AS pv,
        count(DISTINCT user_id)                                               AS uv,
        count(*) FILTER (WHERE faq_hit)                                       AS faq_hits,
        count(*) FILTER (
            WHERE faq_hit OR CASE
                WHEN jsonb_typeof(allowed_unit_ids) = 'array'
                THEN jsonb_array_length(allowed_unit_ids) ELSE 0 END > 0
        )                                                                     AS covered,
        coalesce(avg(latency_ms), 0)                                          AS avg_latency_ms,
        coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms), 0)   AS p50_latency_ms,
        coalesce(percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms), 0)   AS p90_latency_ms,
        coalesce(sum(prompt_tokens), 0)                                       AS prompt_tokens,
        coalesce(sum(completion_tokens), 0)                                   AS completion_tokens
    FROM qa_audit_logs
    WHERE created_at >= :start AND created_at < :end
"""

# 热门知识榜：`citation_ids` 是单元 id 数组，展开后按单元计数。
# 两层防护，审计历史不清理，脏值不该把整个看板打成 500：
#   1. 展开前先确认它真的是数组（JSON null / 标量直接换空数组），
#   2. 展开后用正则滤掉非 UUID 的元素。
_TOP_KNOWLEDGE_SQL = """
    SELECT elem.value::uuid AS unit_id, u.title AS title, count(*) AS hits
    FROM qa_audit_logs AS a
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(a.citation_ids) = 'array'
             THEN a.citation_ids ELSE '[]'::jsonb END
    ) AS elem(value)
    LEFT JOIN knowledge_units AS u ON u.id = elem.value::uuid
    WHERE a.created_at >= :start AND a.created_at < :end
      AND elem.value ~ '^[0-9a-fA-F-]{36}$'
    GROUP BY 1, 2
    ORDER BY hits DESC, unit_id
    LIMIT :limit
"""


async def _count(session: AsyncSession, statement: Select) -> int:
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    return int(total or 0)


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
) -> tuple[list[QaAuditLog], int]:
    """审计流水，时间倒序。筛选条件与 PRD §8.3 的字段一一对应。"""

    statement = select(QaAuditLog).where(
        QaAuditLog.created_at >= start, QaAuditLog.created_at < end
    )
    if user_id is not None:
        statement = statement.where(QaAuditLog.user_id == user_id)
    if faq_hit is not None:
        statement = statement.where(QaAuditLog.faq_hit.is_(faq_hit))
    if answer_status is not None:
        statement = statement.where(QaAuditLog.answer_status == answer_status)

    total = await _count(session, statement)
    rows = (
        (
            await session.execute(
                statement.order_by(QaAuditLog.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def rates(session: AsyncSession, *, start: datetime, end: datetime) -> dict[str, Any]:
    """PV/UV/命中/覆盖/时延/token 的原始计数。无数据时各项为 0。"""

    row = (
        await session.execute(text(_RATES_SQL), {"start": start, "end": end})
    ).mappings().one()
    return dict(row)


async def top_questions(
    session: AsyncSession, *, start: datetime, end: datetime, limit: int = DEFAULT_TOP_LIMIT
) -> list[tuple[str, int]]:
    """高频问题榜：同一问句按原文聚合（改写后的问句不算另一次提问）。"""

    rows = (
        await session.execute(
            text(
                """
                SELECT question, count(*) AS hits
                FROM qa_audit_logs
                WHERE created_at >= :start AND created_at < :end
                GROUP BY question
                ORDER BY hits DESC, question
                LIMIT :limit
                """
            ),
            {"start": start, "end": end, "limit": limit},
        )
    ).all()
    return [(row.question, int(row.hits)) for row in rows]


async def top_knowledge(
    session: AsyncSession, *, start: datetime, end: datetime, limit: int = DEFAULT_TOP_LIMIT
) -> list[tuple[UUID, str | None, int]]:
    """热门知识榜：按被引用次数排序，返回 (unit_id, 标题, 次数)。

    单元已删除时标题为 `None`——审计不清理，历史引用会留在榜上，这只是展示问题。
    """

    rows = (
        await session.execute(
            text(_TOP_KNOWLEDGE_SQL),
            {"start": start, "end": end, "limit": limit},
        )
    ).all()
    return [(row.unit_id, row.title, int(row.hits)) for row in rows]


async def daily_metrics(
    session: AsyncSession, *, start: datetime, end: datetime, tz: str
) -> list[dict[str, Any]]:
    """按天聚合 token 与时长（趋势图的数据源）。只回有数据的日期，补零在服务层做。"""

    rows = (
        await session.execute(
            text(
                """
        SELECT date_trunc('day', created_at AT TIME ZONE :tz)            AS day,
               count(*)                                                  AS pv,
               coalesce(sum(prompt_tokens), 0)                           AS prompt_tokens,
               coalesce(sum(completion_tokens), 0)                       AS completion_tokens,
               coalesce(avg(latency_ms), 0)                              AS avg_latency_ms,
               coalesce(percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY latency_ms), 0)                              AS p50_latency_ms,
               coalesce(percentile_cont(0.9) WITHIN GROUP (
                   ORDER BY latency_ms), 0)                              AS p90_latency_ms
        FROM qa_audit_logs
        WHERE created_at >= :start AND created_at < :end
        GROUP BY 1
        ORDER BY 1
                """
            ),
            {"start": start, "end": end, "tz": tz},
        )
    ).mappings().all()
    return [dict(row) for row in rows]
