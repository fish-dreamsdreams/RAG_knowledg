"""本地 Cross-Encoder 重排与断崖切分（tasklist 10，TECH_SPEC §1）。

`cutoff` / `cliff_index` 是纯函数，可脱离模型单测；`Reranker` 持有进程内单例权重。
"""

from app.engines.rerank.reranker import (
    DEFAULT_BATCH_SIZE,
    RERANK_MAX_LENGTH,
    Reranker,
    cliff_index,
    cutoff,
    get_reranker,
    rank,
    reset_rerankers,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "RERANK_MAX_LENGTH",
    "Reranker",
    "cliff_index",
    "cutoff",
    "get_reranker",
    "rank",
    "reset_rerankers",
]
