"""召回与重排节点测试（tasklist 12.3）。

所有引擎调用都替换为确定假件：这里验证的是图节点的拼接、线程池边界、State 输出与「正文绝不
进 State」；Milvus 的实际检索与 Cross-Encoder 分数已在引擎与集成测试里覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.engines.retrieve import RecalledChunk
from app.graphs.context import GraphDeps
from app.graphs.nodes import rank as rank_node
from app.graphs.nodes import retrieve as retrieve_node
from app.graphs.nodes.rank import cutoff, rerank
from app.graphs.nodes.retrieve import retrieve_hybrid


def _hit(name: str, *, score: float = 0.0) -> RecalledChunk:
    return RecalledChunk(name, str(uuid4()), str(uuid4()), score)


@pytest.fixture
def threadpool(monkeypatch):
    """让同步工作在当前协程内直接执行，并记录它确实是经线程池入口调的。"""

    calls: list[tuple] = []

    async def _run(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(retrieve_node, "run_in_threadpool", _run)
    monkeypatch.setattr(rank_node, "run_in_threadpool", _run)
    return calls


# ---------- retrieve_hybrid ----------


async def test_retrieve_uses_keywords_only_for_sparse_encoding(monkeypatch, threadpool) -> None:
    """Dense 取纯改写句；sparse 取「改写句 + keywords」（TECH_SPEC §1）。"""

    encoded: list[str] = []
    dense = object()
    sparse = object()
    hybrid = [_hit("hybrid")]
    hyde = [_hit("hyde")]

    def _encode(text: str):
        encoded.append(text)
        return dense if len(encoded) == 1 else sparse

    seen: dict = {}
    monkeypatch.setattr(retrieve_node, "encode_question", _encode)
    monkeypatch.setattr(
        retrieve_node,
        "hybrid_search",
        lambda embedding, *, sparse_embedding=None: seen.update(
            dense=embedding, sparse=sparse_embedding
        ) or hybrid,
    )
    monkeypatch.setattr(retrieve_node, "hyde_search", lambda embedding: hyde)
    monkeypatch.setattr(retrieve_node, "dense_similarity", lambda embedding: 0.77)
    monkeypatch.setattr(
        retrieve_node, "rrf_merge", lambda rankings, *, limit: hybrid + hyde
    )

    result = await retrieve_hybrid(
        {
            "rewritten": "员工差旅住宿费报销标准",
            "keywords": ["差旅", "住宿费", "制度2026"],
            "hyde": "住宿费报销按员工职级和城市确定。",
        },
        {},
    )

    assert encoded == [
        "员工差旅住宿费报销标准",
        "员工差旅住宿费报销标准\n差旅 住宿费 制度2026",
        "住宿费报销按员工职级和城市确定。",
    ]
    assert seen == {"dense": dense, "sparse": sparse}
    assert result == {
        "dense_hits": [],
        "sparse_hits": [],
        "hyde_hits": hyde,
        "child_hits": hybrid + hyde,
        "max_similarity": 0.77,
    }
    assert len(threadpool) == 1


async def test_retrieve_without_keywords_or_hyde_skips_extra_encodes(monkeypatch, threadpool) -> None:
    encoded: list[str] = []
    embedding = object()
    hybrid = [_hit("hybrid")]

    monkeypatch.setattr(
        retrieve_node,
        "encode_question",
        lambda text: encoded.append(text) or embedding,
    )
    monkeypatch.setattr(
        retrieve_node, "hybrid_search", lambda *_a, **_k: hybrid
    )
    monkeypatch.setattr(
        retrieve_node,
        "hyde_search",
        lambda *_a: pytest.fail("hyde 为空时不能跑第②路"),
    )
    monkeypatch.setattr(retrieve_node, "dense_similarity", lambda _embedding: 0.31)
    monkeypatch.setattr(
        retrieve_node, "rrf_merge", lambda rankings, *, limit: list(rankings[0])
    )

    result = await retrieve_hybrid(
        {"rewritten": "年假几天", "keywords": [], "hyde": None}, {}
    )

    assert encoded == ["年假几天"]
    assert result["hyde_hits"] == []
    assert result["child_hits"] == hybrid
    assert result["max_similarity"] == 0.31
    assert len(threadpool) == 1


async def test_retrieve_max_similarity_comes_from_original_dense_route(monkeypatch) -> None:
    """HyDE 分数不得参与 gap 判定，否则假设文本会把无答案误判成有答案。"""

    dense = object()
    hyde_embedding = object()
    monkeypatch.setattr(
        retrieve_node,
        "encode_question",
        lambda text: dense if text == "原问题" else hyde_embedding,
    )
    monkeypatch.setattr(retrieve_node, "hybrid_search", lambda *_a, **_k: [])
    monkeypatch.setattr(retrieve_node, "hyde_search", lambda *_a: [])
    monkeypatch.setattr(retrieve_node, "dense_similarity", lambda embedding: 0.12)
    monkeypatch.setattr(retrieve_node, "rrf_merge", lambda *_a, **_k: [])

    result = await retrieve_hybrid(
        {"rewritten": "原问题", "hyde": "很像答案的假设文字"}, {}
    )

    assert result["max_similarity"] == 0.12


# ---------- rerank / cutoff ----------


def _config(session=object()) -> dict:
    return {
        "configurable": {
            "deps": GraphDeps(
                session=session,
                redis=object(),
                model_config=SimpleNamespace(rerank_model="test-reranker"),
                subject=object(),
            )
        }
    }


class _Reranker:
    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, passages))
        return self.scores


async def test_rerank_reads_child_bodies_and_orders_by_cross_encoder_score(
    monkeypatch, threadpool
) -> None:
    first, second, missing = _hit(""), _hit(""), _hit("")
    # 用合法 UUID 字符串替换显示名，模拟 Milvus 的真正返回
    first = first._replace(chunk_id=str(uuid4()))
    second = second._replace(chunk_id=str(uuid4()))
    missing = missing._replace(chunk_id=str(uuid4()))

    async def _chunks(_session, ids):
        assert set(ids) == {
            UUID(first.chunk_id),
            UUID(second.chunk_id),
            UUID(missing.chunk_id),
        }
        return [
            SimpleNamespace(id=UUID(first.chunk_id), level="child", content="第一段"),
            SimpleNamespace(id=UUID(second.chunk_id), level="child", content="第二段"),
            # 父块不允许进 Cross-Encoder：候选只该是 child
            SimpleNamespace(id=UUID(missing.chunk_id), level="parent", content="父块"),
        ]

    fake = _Reranker([0.2, 0.9])
    model_names: list[str] = []
    monkeypatch.setattr(rank_node.knowledge_repo, "get_chunks_by_ids", _chunks)
    monkeypatch.setattr(
        rank_node, "get_reranker", lambda name: model_names.append(name) or fake
    )

    result = await rerank(
        {"rewritten": "测试问题", "child_hits": [first, second, missing]}, _config()
    )

    assert result["rerank_scores"] == [0.9, 0.2]
    assert result["reranked"] == [second, first]
    assert fake.calls == [("测试问题", ["第一段", "第二段"])]
    assert model_names == ["test-reranker"]
    # State 只有 id / 分数结构，正文从未被节点写出
    assert "第一段" not in repr(result)
    assert "父块" not in repr(result)
    assert len(threadpool) == 1


async def test_rerank_empty_when_all_candidates_are_missing(monkeypatch) -> None:
    hit = _hit(str(uuid4()))
    hit = hit._replace(chunk_id=str(uuid4()))

    async def _chunks(_session, _ids):
        return []

    monkeypatch.setattr(rank_node.knowledge_repo, "get_chunks_by_ids", _chunks)
    monkeypatch.setattr(
        rank_node, "get_reranker", lambda *_a: pytest.fail("没有正文时不能加载重排模型")
    )

    assert await rerank({"rewritten": "问题", "child_hits": [hit]}, _config()) == {
        "rerank_scores": [],
        "reranked": [],
    }


async def test_cutoff_uses_engine_result_to_truncate(monkeypatch) -> None:
    hits = [_hit(str(index)) for index in range(6)]
    seen: list[list[float]] = []

    def _cut(scores):
        seen.append(list(scores))
        return 3

    monkeypatch.setattr(rank_node, "choose_cutoff", _cut)

    result = await cutoff(
        {"rerank_scores": [0.9, 0.8, 0.7, 0.2, 0.1, 0.0], "reranked": hits},
        {},
    )

    assert seen == [[0.9, 0.8, 0.7, 0.2, 0.1, 0.0]]
    assert result == {"reranked": hits[:3]}


async def test_cutoff_empty_input_is_safe() -> None:
    assert await cutoff({}, {}) == {"reranked": []}
