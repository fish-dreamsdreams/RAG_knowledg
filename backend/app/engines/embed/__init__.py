"""本地 BGE-M3 编码单例（design.md §3.1：不联网，权重本地缓存）。"""

from app.engines.embed.embedder import (
    MAX_LENGTH,
    Embedder,
    Embedding,
    get_embedder,
    is_oom_error,
    normalize_dense,
    to_sparse_weights,
)

__all__ = [
    "MAX_LENGTH",
    "Embedder",
    "Embedding",
    "get_embedder",
    "is_oom_error",
    "normalize_dense",
    "to_sparse_weights",
]
