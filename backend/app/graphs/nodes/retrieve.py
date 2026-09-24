"""召回节点（tasklist 12.3，TECH_SPEC §5.3 / §8.1）。

本节点做三件事：

1. dense 编码只用 `rewritten`，确保语义向量不被关键词堆叠扭曲；
2. sparse 编码用「改写句 + keywords」，保住制度号、专名等词面信号；
3. 问句 hybrid 结果与可选 HyDE dense 结果再走应用层 RRF，交给重排的候选恒为 Top-20。

Milvus 的 `hybrid_search` 在服务端已经把 dense / sparse 融成一张列表，API 不返回两个子
排名。因此 `dense_hits` / `sparse_hits` 这两个历史 State 字段固定留空，不能把同一份融合
结果伪装成两条原始路径；真正可用且有意义的中间结果是 `child_hits`。这避免为"可观测字段"
额外做两次 Milvus 查询、又不污染字段语义。

Milvus、M3 编码都是同步阻塞调用，必须经线程池运行，否则一轮问答的召回会卡住事件循环。
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from starlette.concurrency import run_in_threadpool

from app.engines.retrieve import (
    RECALL_LIMIT,
    dense_similarity,
    encode_question,
    hybrid_search,
    hyde_search,
    rrf_merge,
)
from app.graphs.state import QAState


def _sparse_text(rewritten: str, keywords: list[str]) -> str:
    """稀疏编码输入。无关键词时不追加任何分隔符，保证与旧查询字节一致。"""

    return rewritten if not keywords else f"{rewritten}\n{' '.join(keywords)}"


def _retrieve(
    rewritten: str, keywords: list[str], hyde: str | None
) -> tuple[list, list, list, float]:
    """同步三路召回。返回 hybrid、HyDE、问句 dense 的最高相似度。"""

    dense_embedding = encode_question(rewritten)
    sparse_embedding = (
        encode_question(_sparse_text(rewritten, keywords)) if keywords else None
    )
    hybrid_hits = hybrid_search(dense_embedding, sparse_embedding=sparse_embedding)
    max_similarity = dense_similarity(dense_embedding)

    hyde_hits = []
    if hyde:
        hyde_hits = hyde_search(encode_question(hyde))
    child_hits = rrf_merge([hybrid_hits, hyde_hits], limit=RECALL_LIMIT)
    return hybrid_hits, hyde_hits, child_hits, max_similarity


async def retrieve_hybrid(state: QAState, config: RunnableConfig) -> dict:
    """召回并 RRF 融合。`config` 保留节点统一调用形状，本节点不需要外部依赖。"""

    del config
    rewritten = state["rewritten"]
    keywords = state.get("keywords") or []
    hyde = state.get("hyde")

    hybrid_hits, hyde_hits, child_hits, max_similarity = await run_in_threadpool(
        _retrieve, rewritten, keywords, hyde
    )

    return {
        # 服务端 hybrid RRF 不回传 dense / sparse 原始子排名，不能凭空伪造
        "dense_hits": [],
        "sparse_hits": [],
        "hyde_hits": hyde_hits,
        "child_hits": child_hits,
        "max_similarity": max_similarity,
    }
