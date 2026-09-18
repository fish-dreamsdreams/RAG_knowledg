"""演示库清场：把验收与手工演示留下的痕迹清回「导入脚本刚跑完」的样子。

**为什么需要**（本轮真踩到的）：验收脚本每跑一次往 `qa_audit_logs` 留 5 行——看板 PV 跟着涨，
反复问同一句还会被挖掘成候选（演示库里真长出过第 5 条候选）；缺口列表与候选列表同理。

**口径：按来源认，不按时间猜。**

- 审计行：`trace_id` 不以 `demo_logs.DEMO_TRACE_PREFIX` 开头的全部删掉——种子写的行带固定前缀，
  其余都是真跑出来的
- 缺口：`data/seeds/qa_demo_logs.json` 的 `gap_clusters` 代表问句是白名单，白名单外删掉
- 候选：同问句重复的（按 `faq_cache.normalize`）保留 `created_at` 最早一条，其余删掉
- FAQ：同问句重复的同样保留最早（与 `scripts/clean_duplicate_faqs.py` 共用一处实现，见
  `services.faq.find_duplicate_faqs`）

候选与 FAQ 都**只做「重复」这一确定性判据**，不用启发式猜「哪条脏」：报告里把候选频次列出来
供人判断，机器不替人做这个决定。

**删除只属于维护脚本**：`backend/tool/clean_demo_state.py` 是唯一调用方。尤其是审计行——
`QaAuditLog` 写着「每次问答必写一条（P12）。不清理历史」，那是产品层的不变量；清场工具是
演示库复位，不构成给它开口子的理由。**不要把本模块接进 API 或控制台。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines import faq_cache
from app.repositories import chat as chat_repo
from app.repositories import faq as faq_repo
from app.services import demo_logs
from app.services import faq as faq_service

AUDIT = "audit"
GAPS = "gaps"
CANDIDATES = "candidates"
FAQS = "faqs"
BLOCKS: tuple[str, ...] = (AUDIT, GAPS, FAQS, CANDIDATES)

BLOCK_LABELS = {
    AUDIT: "审计行（非 demo- 前缀）",
    GAPS: "知识缺口（演示缺口簇之外）",
    FAQS: "FAQ（同问句重复，保留最早一条）",
    CANDIDATES: "候选（同问句重复，published 优先，其次保留最早）",
}


@dataclass(frozen=True)
class BlockReport:
    """一个块的只读报告；`doomed` 兼作导出内容（纯 JSON 友好值）。"""

    block: str
    doomed: list[dict[str, Any]]
    kept: int
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return BLOCK_LABELS[self.block]

    @property
    def count(self) -> int:
        return len(self.doomed)


async def build_report(
    session: AsyncSession, *, blocks: tuple[str, ...] = BLOCKS
) -> list[BlockReport]:
    """只读：算出每个块要删什么、留多少。**不写库。**"""

    builders = {
        AUDIT: _audit_block,
        GAPS: _gaps_block,
        FAQS: _faqs_block,
        CANDIDATES: _candidates_block,
    }
    return [await builders[block](session) for block in blocks]


async def apply_report(
    session: AsyncSession,
    reports: list[BlockReport],
    *,
    redis: Redis | None = None,
) -> dict[str, int]:
    """按报告执行删除，返回各块实际删除行数。"""

    deleted: dict[str, int] = {}
    for report in reports:
        ids = [UUID(str(row["id"])) for row in report.doomed]
        if report.block == AUDIT:
            deleted[AUDIT] = await chat_repo.delete_audit_logs(session, ids)
        elif report.block == GAPS:
            deleted[GAPS] = await faq_repo.delete_gaps(session, ids)
        elif report.block == FAQS:
            await faq_service.delete_faqs(session, ids, redis=redis)
            deleted[FAQS] = len(ids)
        elif report.block == CANDIDATES:
            for candidate_id in ids:
                await faq_service.delete_candidate(session, candidate_id)
            deleted[CANDIDATES] = len(ids)
        else:  # pragma: no cover - 调用方只传 BLOCKS 里的名字
            raise ValueError(f"未知清场块：{report.block}")
    return deleted


# ---------- 各块的报告 ----------


async def _audit_block(session: AsyncSession) -> BlockReport:
    prefix = demo_logs.DEMO_TRACE_PREFIX
    rows = await chat_repo.audit_logs_excluding_prefix(session, keep_prefix=prefix)
    doomed = [
        {
            "id": str(row.id),
            "trace_id": row.trace_id,
            "question": row.question,
            "answer_status": row.answer_status,
            "faq_hit": row.faq_hit,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]
    return BlockReport(
        block=AUDIT,
        doomed=doomed,
        kept=await chat_repo.count_audit_logs(session, keep_prefix=prefix),
        notes=[
            "这些行来自验收脚本、手工演示与真实使用，不是种子数据；"
            "删掉后看板 PV / FAQ 命中率会掉回导入脚本刚跑完时的数（这正是目的）",
            _span_note(rows),
        ],
    )


async def _gaps_block(session: AsyncSession) -> BlockReport:
    whitelist = demo_logs.demo_gap_questions()
    rows = await faq_repo.gaps_outside(session, whitelist)
    doomed = [
        {
            "id": str(row.id),
            "question_text": row.question_text,
            "freq": row.freq,
            "status": row.status,
            "max_similarity": row.max_similarity,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]
    return BlockReport(
        block=GAPS,
        doomed=doomed,
        kept=await faq_repo.count_gaps(session, questions=whitelist),
        notes=["白名单来自 data/seeds/qa_demo_logs.json 的 gap_clusters 代表问句"],
    )


async def _faqs_block(session: AsyncSession) -> BlockReport:
    groups = await faq_service.find_duplicate_faqs(session)
    total = len(await faq_repo.all_faqs(session))
    doomed = [_faq_row(item) for group in groups for item in group.drop]

    notes: list[str] = []
    for group in groups:
        if group.answers_differ:
            notes.append(
                f"「{group.keep.question}」组内答案不一致：保留的是最早那条（{group.keep.faq_id}），"
                "动手前先确认它是要留的答复"
            )
    return BlockReport(
        block=FAQS, doomed=doomed, kept=total - len(doomed), notes=notes
    )


async def _candidates_block(session: AsyncSession) -> BlockReport:
    rows = await faq_repo.candidates_by_status(
        session,
        (
            faq_repo.CANDIDATE_PENDING,
            faq_repo.CANDIDATE_REJECTED,
            faq_repo.CANDIDATE_PUBLISHED,
        ),
    )

    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(faq_cache.normalize(row.representative_question), []).append(row)

    doomed: list[dict[str, Any]] = []
    pending_freqs: list[str] = []
    for group in grouped.values():
        # 保留优先级：published 先（它是 FAQ 的来源记录，删了就没法回答「这条 FAQ 从哪来」），
        # 同档再按 created_at 最早。只按最早排的话，「先挖掘出 pending、再发布」那对里
        # 会留着 pending、删掉 published，FAQ 的 source_candidate_id 就被无辜置空了。
        group.sort(
            key=lambda row: (
                row.status != faq_repo.CANDIDATE_PUBLISHED,
                row.created_at,
            )
        )
        if len(group) == 1:
            if group[0].status == faq_repo.CANDIDATE_PENDING:
                pending_freqs.append(f"{group[0].representative_question}={group[0].freq}")
            continue
        doomed.extend(_candidate_row(row) for row in group[1:])

    notes = []
    if pending_freqs:
        notes.append(
            "重复组之外的待审候选按频次列出，机器不替你判断要不要清："
            + "、".join(pending_freqs)
        )
    if doomed:
        notes.append(
            "删除候选前会把引用它的 FAQ 的 source_candidate_id 置空（外键实测需要），FAQ 本身不动"
        )
        # 与挖掘去重的交互：这些问句已被 FAQ 覆盖时，删掉候选后挖掘任务就不会再提议它们（11.4 的
        # 口径），「挖掘 → 审核 → 发布」这条演示路径就重演不了了。不说清楚，下一个人会在现场才发现。
        settled = {
            faq_cache.normalize(row.question) for row in await faq_repo.all_faqs(session)
        }
        covered = sum(
            1 for row in doomed if faq_cache.normalize(row["question"]) in settled
        )
        if covered:
            notes.append(
                f"其中 {covered} 条的问句已被已发布 FAQ 覆盖：清掉后挖掘不会再提议它们，"
                "要重演「挖掘 → 审核 → 发布」得先下线或删除对应 FAQ"
            )
    return BlockReport(
        block=CANDIDATES,
        doomed=doomed,
        kept=len(rows) - len(doomed),
        notes=notes,
    )


# ---------- 行的 JSON 形态 ----------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _span_note(rows: list[Any]) -> str:
    """把删除的时间跨度写进报告：这是操作者判断「删下去会不会误伤」最直接的依据。

    非种子行不区分「验收脚本的」与「演示现场手点的」——两者形状完全一样，都算痕迹。
    """

    if not rows:
        return "没有非种子行"
    first, last = rows[0].created_at, rows[-1].created_at
    return f"时间跨度 {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M}（含更早的演示走查）"


def _faq_row(item: Any) -> dict[str, Any]:
    return {
        "id": str(item.faq_id),
        "question": item.question,
        "answer": item.answer,
        "status": item.status,
        "cache_enabled": item.cache_enabled,
        "source_candidate_id": str(item.source_candidate_id)
        if item.source_candidate_id
        else None,
        "created_at": _iso(item.created_at),
    }


def _candidate_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "question": row.representative_question,
        "freq": row.freq,
        "status": row.status,
        "reject_reason": row.reject_reason,
        "created_at": _iso(row.created_at),
    }
