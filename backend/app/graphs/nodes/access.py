"""权限过滤与父块展开节点（tasklist 12.4）。

这是整张图的安全闸门：重排后的候选可以同时有允许与拒绝的知识单元，但 ACL 之后只有
`allowed` 允许进入 `expand_parent`，进而进入 Chat Prompt。被拒项只留下 UUID 字符串供审计
计数和 WebSocket 的布尔提示使用，**标题、正文、父块 id 都不写 State**（P3）。

权限判定由 `AclEngine` 单点承担；节点既不重写 ACL SQL，也不通过"预先过滤检索结果"绕开
`denied` 的可解释提示。
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from app.common.ids import parse_uuid
from app.engines.acl import AclEngine
from app.engines.retrieve import expand_parents
from app.graphs.context import deps_of
from app.graphs.state import QAState


async def acl_filter(state: QAState, config: RunnableConfig) -> dict:
    """按 unit 粒度批量判定，保留原重排名次中的允许 child。"""

    deps = deps_of(config)
    reranked = state.get("reranked") or []
    unit_ids = [
        unit_id for hit in reranked if (unit_id := parse_uuid(hit.unit_id)) is not None
    ]

    # 引擎本身无状态，但 Redis 取本轮注入的客户端：用进程全局默认客户端，会让测试与
    # 不同事件循环下的调用隐式串线。
    acl_engine = AclEngine(redis_factory=lambda: deps.redis)
    allowed_ids, denied_ids = await acl_engine.filter(
        deps.subject, unit_ids, session=deps.session
    )
    allowed_set = set(allowed_ids)

    # 只携带 RecalledChunk（id + 分数）；任何单元标题或正文都不进 State。
    allowed = [
        hit
        for hit in reranked
        if (unit_id := parse_uuid(hit.unit_id)) is not None and unit_id in allowed_set
    ]
    denied = [str(unit_id) for unit_id in denied_ids]
    # 缺口阈值从系统配置就地写入。交给调用方注入的话，漏传不会报错，
    # 只会让每一轮问答都走缺口出口——这种失败模式太安静了。
    gap_threshold = deps.model_config.gap_sim_threshold
    max_similarity = state.get("max_similarity") or 0.0
    return {
        "allowed": allowed,
        "denied": denied,
        # 只有「确实答得出来、但部分相关资料无权」时才提示：相似度不达标时整轮按缺口
        # 出口（`after_acl`），再提示「检测到相关制度但无权查阅」就自相矛盾了。
        "acl_notice": bool(allowed and denied and max_similarity >= gap_threshold),
        "gap_threshold": gap_threshold,
    }


async def expand_parent(state: QAState, config: RunnableConfig) -> dict:
    """将允许的 child 按重排名次展开成父块上下文（总计最多 6000 字）。"""

    deps = deps_of(config)
    chunk_ids = [
        chunk_id
        for hit in state.get("allowed") or []
        if (chunk_id := parse_uuid(hit.chunk_id)) is not None
    ]
    contexts = await expand_parents(deps.session, chunk_ids)
    return {"parent_contexts": contexts}
