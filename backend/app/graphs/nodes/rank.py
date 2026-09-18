"""重排与断崖截断节点（tasklist 12.3）。

Milvus 召回只知道子块 id 和近似分，Cross-Encoder 重排必须读 PostgreSQL 中的 child 正文。
正文只在节点栈变量中存在，**不写 State**：下一步 ACL 会决定哪些结果能带进 `parent_contexts`，
提前把正文塞 State 会让无权内容随流式更新泄露（P3）。

`rerank` 与 `cutoff` 分成两个节点，和技术设计一致：前者只打分/排序，后者是纯确定的断崖
算术。这让阈值标定与排序模型的行为能独立测试。
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from starlette.concurrency import run_in_threadpool

from app.common.ids import parse_uuid
from app.engines.rerank import cutoff as choose_cutoff
from app.engines.rerank import get_reranker
from app.engines.retrieve import RecalledChunk
from app.graphs.context import deps_of
from app.graphs.state import QAState
from app.repositories import knowledge as knowledge_repo


async def rerank(state: QAState, config: RunnableConfig) -> dict:
    """取 child 正文，经本地 Cross-Encoder 打分并降序排列。

    Milvus 命中可能在索引删除和本轮查询之间被清掉；取不到正文的条目直接丢弃，不能用空串给
    它打分并让它靠模型偏置混进结果。
    """

    deps = deps_of(config)
    hits = state.get("child_hits") or []
    ids = [chunk_id for hit in hits if (chunk_id := parse_uuid(hit.chunk_id)) is not None]
    rows = await knowledge_repo.get_chunks_by_ids(deps.session, ids)
    contents = {str(row.id): row.content for row in rows if row.level == "child"}
    candidates = [hit for hit in hits if hit.chunk_id in contents]
    if not candidates:
        return {"rerank_scores": [], "reranked": []}

    passages = [contents[hit.chunk_id] for hit in candidates]
    reranker = get_reranker(deps.model_config.rerank_model)
    scores = await run_in_threadpool(reranker.score, state["rewritten"], passages)
    ordered = sorted(enumerate(scores), key=lambda item: (-item[1], item[0]))

    return {
        "rerank_scores": [score for _, score in ordered],
        "reranked": [candidates[index] for index, _ in ordered],
    }


async def cutoff(state: QAState, config: RunnableConfig) -> dict:
    """按归一化落差比截断为 3～10 条（候选本身不足 3 时全保留）。"""

    del config
    scores = state.get("rerank_scores") or []
    reranked = state.get("reranked") or []
    keep = choose_cutoff(scores)
    return {"reranked": reranked[:keep]}
