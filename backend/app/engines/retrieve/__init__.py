"""三路召回与 RRF 融合（design.md §3.1：本模块不做权限判定）。

- 问句编码与 Milvus 检索：`search`
- RRF 融合：`fusion`
- 子块去重与父块回溯：`expand`
- collection 维护（写入 / 启停 / 删除）：`milvus_store`

权限裁剪在 `engines/acl`：检索按 TECH_SPEC §5.3 有意不过滤 ACL，好让生成前能区分
「没检索到」与「检索到了但无权」。
"""

from app.engines.retrieve.expand import (
    MAX_CONTEXT_CHARS,
    ParentContext,
    expand_parents,
)
from app.engines.retrieve.fusion import RRF_K, RecalledChunk, rrf_merge
from app.engines.retrieve.milvus_store import (
    COLLECTION_NAME,
    ChunkVector,
    count_unit_chunks,
    delete_by_unit,
    ensure_collection,
    get_client,
    set_unit_enabled,
    upsert_chunks,
)
from app.engines.retrieve.search import (
    DENSE_TOP_K,
    ENABLED_FILTER,
    RECALL_LIMIT,
    SPARSE_TOP_K,
    RecallResult,
    dense_similarity,
    encode_question,
    hybrid_search,
    hyde_search,
    recall,
)

__all__ = [
    "COLLECTION_NAME",
    "DENSE_TOP_K",
    "ENABLED_FILTER",
    "MAX_CONTEXT_CHARS",
    "RECALL_LIMIT",
    "RRF_K",
    "SPARSE_TOP_K",
    "ChunkVector",
    "ParentContext",
    "RecallResult",
    "RecalledChunk",
    "count_unit_chunks",
    "delete_by_unit",
    "dense_similarity",
    "encode_question",
    "ensure_collection",
    "expand_parents",
    "get_client",
    "hybrid_search",
    "hyde_search",
    "recall",
    "rrf_merge",
    "set_unit_enabled",
    "upsert_chunks",
]
