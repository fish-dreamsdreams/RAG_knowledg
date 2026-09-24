"""审计节点（tasklist 12.5，不变量 P12）。

四条出口（FAQ 命中、生成、拒答、缺口）都汇入本节点，每次问答写且仅写一条记录。

把审计放在图**最后**而不是各出口各自写，是让 P12 从"记得写"变成结构性质：只要出口连到
`audit` 就不可能漏；将来新增出口若忘了连边，图编译后它没有出边，一眼就能看出来。

`answer_status` 由各出口显式写入，不靠比较中文文案反推——文案是要反复改的东西，拿它当
判据迟早会错。
"""

from __future__ import annotations

import logging
from time import perf_counter
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig

from app.common.ids import parse_uuid
from app.common.logging import get_trace_id
from app.graphs.context import deps_of
from app.graphs.state import QAState
from app.repositories import chat as chat_repo

logger = logging.getLogger(__name__)


def _dedup(values: list[UUID]) -> list[UUID]:
    return list(dict.fromkeys(values))


async def audit(state: QAState, config: RunnableConfig) -> dict:
    deps = deps_of(config)

    trace_id = get_trace_id()
    if not trace_id or trace_id == "-":
        # 直接调图（测试、脚本）时没有中间件写 trace_id；这里补一个，
        # 否则所有这类审计行会共用 "-"，反而失去可追溯性
        trace_id = str(uuid4())

    # allowed / citation 都只取 unit_id：审计要能回答"这次答的是哪些单元"，
    # 不需要（也不该有）正文与标题
    allowed_units = _dedup(
        [unit_id for hit in state.get("allowed") or [] if (unit_id := parse_uuid(hit.unit_id))]
    )
    citation_units = _dedup(
        [
            unit_id
            for item in state.get("citations") or []
            if (unit_id := parse_uuid(item.get("unit_id")))
        ]
    )

    row = await chat_repo.add_audit_log(
        deps.session,
        trace_id=trace_id,
        user_id=deps.subject.user_id,
        session_id=parse_uuid(deps.session_id),
        question=state["question"],
        # FAQ 命中时没有改写，字段保持 NULL 而不是空串：空串会与"改写过但结果为空"混淆
        rewritten=state.get("rewritten"),
        faq_hit=bool(state.get("faq_hit")),
        allowed_unit_ids=[str(item) for item in allowed_units],
        denied_count=len(state.get("denied") or []),
        citation_ids=[str(item) for item in citation_units],
        answer_status=state.get("answer_status") or "answered",
        max_similarity=state.get("max_similarity"),
        prompt_tokens=state.get("prompt_tokens", 0),
        completion_tokens=state.get("completion_tokens", 0),
        latency_ms=max(0, int((perf_counter() - deps.started_at) * 1000)),
    )
    return {"audit_id": str(row.id)}
