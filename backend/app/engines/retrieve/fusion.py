"""应用层 RRF 融合（tasklist 9.2，TECH_SPEC §5.3）。

第①路（问句 dense + 问句 sparse）由 Milvus 的 `RRFRanker` 在服务端融合；第②路（HyDE
dense）必须单独 `search` 后在应用层与第①路再融合一次——Milvus 每个 `AnnSearchRequest`
只接受一个查询向量，单次 `hybrid_search` 只能有一个 ranker，两段融合无法合并成一次调用。

融合只用**名次**，不用分数绝对值：`score = Σ 1/(k + rank)`。好处是 dense 的余弦与 sparse
的内积量纲不同也不影响结果，单路分数整体加常数同样不改变排序。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

# 与 Milvus 侧 `RRFRanker` 取同一个 k，两段融合口径一致
RRF_K = 60


class RecalledChunk(NamedTuple):
    """一路召回命中的子块。融合后 `score` 是 RRF 分，不再是向量相似度。"""

    chunk_id: str
    unit_id: str
    parent_id: str
    score: float


def rrf_merge(
    rankings: Sequence[Sequence[RecalledChunk]],
    *,
    k: int = RRF_K,
    limit: int = 20,
) -> list[RecalledChunk]:
    """按名次融合多路召回，返回 Top-`limit`。

    同一 `chunk_id` 在多路出现时累加得分，并保留**首次出现**那条记录的内容字段
    （`unit_id` / `parent_id` 各路必然一致）。同分时按 `chunk_id` 升序，保证结果可复现。
    """

    totals: dict[str, float] = {}
    first_seen: dict[str, RecalledChunk] = {}

    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            totals[chunk.chunk_id] = totals.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(chunk.chunk_id, chunk)

    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return [
        first_seen[chunk_id]._replace(score=score) for chunk_id, score in ordered[:limit]
    ]
