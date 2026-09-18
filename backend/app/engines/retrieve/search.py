"""三路召回（tasklist 9.1 / 9.2，TECH_SPEC §5.3）。

① `hybrid_search`：问句 dense Top-20 + 问句 sparse Top-20，服务端 `RRFRanker(k=60)` 融合
② `hyde_search` ：HyDE 文本的 dense Top-20

再在应用层把 ①② 的名次融合成 Top-20（`fusion.rrf_merge`）。两段融合不能并成一次调用：
Milvus 每个 `AnnSearchRequest` 只接受一个查询向量，单次 `hybrid_search` 只能有一个 ranker。

**两路都过滤 `enabled == true`，都不前置过滤 ACL**：检索要召回无权项，才能在生成前给出
「部分内容无权访问」的提示（TECH_SPEC §5.3）；裁剪由 `AclEngine` 在 `acl_filter` 节点做（P9）。

`max_similarity` 只取问句 dense 路的余弦最大值，**不含 HyDE 路**：假设文本与语料天然相近，
用它会拉高相似度、把「无答案」误判成「有答案」（P8）。
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from pymilvus import AnnSearchRequest, RRFRanker

from app.common.config import settings
from app.engines.embed import Embedding, get_embedder
from app.engines.retrieve.fusion import RRF_K, RecalledChunk, rrf_merge
from app.engines.retrieve.milvus_store import (
    COLLECTION_NAME,
    ensure_collection,
    get_client,
    milvus_call,
)

logger = logging.getLogger(__name__)

# 每一路的候选数（TECH_SPEC §5.3）
DENSE_TOP_K = 20
SPARSE_TOP_K = 20
# 融合后交给重排的条数
RECALL_LIMIT = 20

# 停用切片不得进候选（P9）；这里**不**过滤 ACL，见模块 docstring
ENABLED_FILTER = "enabled == true"
# HNSW 检索宽度
_HNSW_EF = 64
_OUTPUT_FIELDS = ["chunk_id", "unit_id", "parent_id"]


class RecallResult(NamedTuple):
    """一次问句召回的产物。"""

    chunks: list[RecalledChunk]
    # 只来自问句 dense 路（P8）
    max_similarity: float


def _vector(embedding: Embedding) -> list[float]:
    if embedding.dense.shape[0] != 1:
        raise ValueError(f"查询向量必须恰好一条，实际 {embedding.dense.shape[0]} 条")
    return [float(item) for item in embedding.dense[0]]


def _sparse(embedding: Embedding) -> dict[int, float]:
    if len(embedding.sparse) != 1:
        raise ValueError(f"稀疏查询向量必须恰好一条，实际 {len(embedding.sparse)} 条")
    return {int(index): float(weight) for index, weight in embedding.sparse[0].items()}


def encode_question(text: str) -> Embedding:
    """问句编码。索引侧与查询侧必须同一个模型，否则向量空间对不上（TECH_SPEC §5.4）。"""

    return get_embedder().encode([text])


def _to_chunks(raw) -> list[RecalledChunk]:
    """Milvus 返回 `[[hit, ...]]`，hit 形如 `{"id": …, "distance": …, "entity": {…}}`。"""

    hits = raw[0] if raw else []
    chunks: list[RecalledChunk] = []
    for hit in hits:
        entity = hit.get("entity") or {}
        chunk_id = entity.get("chunk_id") or hit.get("id")
        if not chunk_id:
            continue
        chunks.append(
            RecalledChunk(
                chunk_id=str(chunk_id),
                unit_id=str(entity.get("unit_id", "")),
                parent_id=str(entity.get("parent_id", "")),
                score=float(hit.get("distance", 0.0)),
            )
        )
    return chunks


def hybrid_search(
    embedding: Embedding,
    *,
    sparse_embedding: Embedding | None = None,
    limit: int = RECALL_LIMIT,
) -> list[RecalledChunk]:
    """第①路：问句 dense + 问句 sparse，由服务端 RRF 融合。

    `sparse_embedding` 允许把改写句追加 `keywords` 后单独编码（TECH_SPEC §1）：dense 仍取
    纯改写句，避免关键词堆叠改变语义向量；sparse 取带关键词文本，保住制度号、专名等词面
    信号。不传时保留旧行为，两路共用同一份 embedding。
    """

    sparse_source = sparse_embedding or embedding
    ensure_collection()
    client = get_client()
    requests = [
        AnnSearchRequest(
            data=[_vector(embedding)],
            anns_field="dense",
            param={"metric_type": "COSINE", "params": {"ef": _HNSW_EF}},
            limit=DENSE_TOP_K,
            expr=ENABLED_FILTER,
        ),
        AnnSearchRequest(
            data=[_sparse(sparse_source)],
            anns_field="sparse",
            param={"metric_type": "IP"},
            limit=SPARSE_TOP_K,
            expr=ENABLED_FILTER,
        ),
    ]
    with milvus_call("hybrid_search"):
        raw = client.hybrid_search(
            collection_name=COLLECTION_NAME,
            reqs=requests,
            ranker=RRFRanker(RRF_K),
            limit=limit,
            output_fields=_OUTPUT_FIELDS,
        )
    return _to_chunks(raw)


def hyde_search(
    embedding: Embedding, *, limit: int = RECALL_LIMIT
) -> list[RecalledChunk]:
    """第②路：HyDE 文本的 dense 检索（与第①路共用 `dense` 字段，不额外占存储）。"""

    ensure_collection()
    client = get_client()
    with milvus_call("hyde search"):
        raw = client.search(
            collection_name=COLLECTION_NAME,
            data=[_vector(embedding)],
            anns_field="dense",
            search_params={"metric_type": "COSINE", "params": {"ef": _HNSW_EF}},
            limit=limit,
            filter=ENABLED_FILTER,
            output_fields=_OUTPUT_FIELDS,
        )
    return _to_chunks(raw)


def dense_similarity(embedding: Embedding) -> float:
    """问句 dense 路的最高余弦相似度；无命中返回 0（P8 的 `max_similarity` 就取它）。

    单独查一次而不是从第①路结果里取：`hybrid_search` 只回融合后的名次，拿不到 dense 子
    请求的原始余弦分。多一次 query 换口径准确，演示规模下划算。
    """

    ensure_collection()
    client = get_client()
    with milvus_call("dense similarity"):
        raw = client.search(
            collection_name=COLLECTION_NAME,
            data=[_vector(embedding)],
            anns_field="dense",
            search_params={"metric_type": "COSINE", "params": {"ef": _HNSW_EF}},
            limit=1,
            filter=ENABLED_FILTER,
            output_fields=["chunk_id"],
        )
    hits = raw[0] if raw else []
    return float(hits[0].get("distance", 0.0)) if hits else 0.0


def recall(question: str, *, hyde: str | None = None) -> RecallResult:
    """三路召回并融合为 Top-20（TECH_SPEC §5.3）。

    `hyde` 为空或 `HYDE_ENABLED=false` 时跳过第②路，只返回第①路的融合结果，不报错。
    """

    embedding = encode_question(question)
    max_similarity = dense_similarity(embedding)

    rankings = [hybrid_search(embedding)]
    if hyde and settings.hyde_enabled:
        rankings.append(hyde_search(encode_question(hyde)))
    else:
        logger.debug(
            "跳过 HyDE 路：hyde=%s HYDE_ENABLED=%s", bool(hyde), settings.hyde_enabled
        )

    return RecallResult(
        chunks=rrf_merge(rankings, k=RRF_K, limit=RECALL_LIMIT),
        max_similarity=max_similarity,
    )
