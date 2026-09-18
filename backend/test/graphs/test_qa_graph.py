"""问答图端到端路径测试（tasklist 12.5）。

外部依赖（Milvus、BGE-M3、重排模型、Chat 网关、Redis、PostgreSQL）全部换成确定假件，
保留真实的图拓扑与节点实现。这样能在不起一整套服务的前提下端到端断言几条不变量：

- P12：四条出口各写且仅写一条审计；
- P4：FAQ 命中时 Chat 调用次数为 0，token 也为 0；
- P5：allowed 为空时不调生成模型；
- P3：denied 单元的标题与正文不出现在任何出站内容里。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.engines.acl import AclSubject
from app.engines.retrieve import ParentContext, RecalledChunk
from app.graphs.context import GraphDeps
from app.graphs.nodes import access as access_node
from app.graphs.nodes import answer as answer_node
from app.graphs.nodes import faq as faq_node
from app.graphs.nodes import rank as rank_node
from app.graphs.nodes import retrieve as retrieve_node
from app.graphs.nodes import rewrite as rewrite_node
from app.graphs.nodes.prompts import DENIED_ANSWER, GAP_ANSWER
from app.graphs.qa_graph import qa_graph

QUESTION = "出差住宿费怎么报销"
ALLOWED_TEXT = "允许正文：出差住宿费按职级和出差城市类别执行。"
DENIED_TITLE = "2026 薪酬方案（机密）"
DENIED_TEXT = "机密正文：总经理年薪 180 万。"


class _Session:
    """只实现审计与缺口用到的方法。"""

    def __init__(self) -> None:
        self.rows: list[object] = []
        self.commits = 0

    def add(self, row) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        self.commits += 1


class _RewriteChat:
    async def ainvoke(self, _messages):
        return SimpleNamespace(
            content='{"rewritten": "出差住宿费报销标准", "keywords": ["差旅"], "hyde": ""}',
            usage_metadata={"input_tokens": 10, "output_tokens": 5},
        )


class _GenerateChat:
    async def astream(self, _messages):
        yield SimpleNamespace(content="依据制度，", usage_metadata=None)
        yield SimpleNamespace(
            content="需提交住宿发票。",
            usage_metadata={"input_tokens": 900, "output_tokens": 20},
        )


class _Reranker:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def score(self, _query: str, passages: list[str]) -> list[float]:
        return self._scores[: len(passages)]


@pytest.fixture
def env(monkeypatch):
    """装好假依赖，返回可配置的运行时。"""

    allowed_child = uuid4()
    denied_child = uuid4()
    parent = uuid4()

    state = SimpleNamespace(
        mode="allowed",
        allowed_unit=uuid4(),
        denied_unit=uuid4(),
        allowed_child=allowed_child,
        denied_child=denied_child,
        parent=parent,
        session=_Session(),
        rewrite_calls=0,
        generate_calls=0,
        gap_calls=0,
        faq_hit=None,
        max_similarity=0.72,
    )

    def _hit(child_id, unit_id) -> RecalledChunk:
        return RecalledChunk(str(child_id), str(unit_id), str(parent), 0.5)

    # --- FAQ ---
    async def _match(_redis, _question, _vector, *, threshold):
        return state.faq_hit

    monkeypatch.setattr(faq_node, "match_faq", _match)
    monkeypatch.setattr(faq_node, "_encode", lambda _question: [0.1])

    # --- 改写 / 生成：分别计数，才能分辨「谁没被调用」 ---
    def _rewrite_chat(*_args, **_kwargs):
        state.rewrite_calls += 1
        return _RewriteChat()

    def _generate_chat(*_args, **_kwargs):
        state.generate_calls += 1
        return _GenerateChat()

    monkeypatch.setattr(rewrite_node, "build_chat_model", _rewrite_chat)
    monkeypatch.setattr(answer_node, "build_chat_model", _generate_chat)

    # --- 召回 ---
    monkeypatch.setattr(retrieve_node, "encode_question", lambda _text: object())
    monkeypatch.setattr(
        retrieve_node,
        "hybrid_search",
        lambda *_a, **_k: (
            [_hit(allowed_child, state.allowed_unit)]
            if state.mode == "allowed"
            else [_hit(denied_child, state.denied_unit)]
            if state.mode == "denied"
            else []
        ),
    )
    monkeypatch.setattr(retrieve_node, "hyde_search", lambda *_a: [])
    monkeypatch.setattr(
        retrieve_node,
        "dense_similarity",
        lambda _embedding: state.max_similarity,
    )
    monkeypatch.setattr(
        retrieve_node, "rrf_merge", lambda rankings, *, limit: list(rankings[0])[:limit]
    )

    # --- 重排：child 正文只在这里被读，且不得进 State ---
    async def _chunks(_session, ids):
        rows = []
        for chunk_id in ids:
            if str(chunk_id) == str(allowed_child):
                rows.append(
                    SimpleNamespace(id=allowed_child, level="child", content=ALLOWED_TEXT)
                )
            elif str(chunk_id) == str(denied_child):
                rows.append(
                    SimpleNamespace(id=denied_child, level="child", content=DENIED_TEXT)
                )
        return rows

    monkeypatch.setattr(rank_node.knowledge_repo, "get_chunks_by_ids", _chunks)
    monkeypatch.setattr(
        rank_node, "get_reranker", lambda *_a, **_k: _Reranker([0.91, 0.88, 0.62])
    )

    # --- ACL ---
    class _Engine:
        def __init__(self, **_kwargs) -> None:
            pass

        async def filter(self, subject, unit_ids, *, session):
            if state.mode == "allowed":
                return list(unit_ids), []
            return [], list(unit_ids)

    monkeypatch.setattr(access_node, "AclEngine", _Engine)

    async def _expand(_session, _chunk_ids):
        return [
            ParentContext(state.allowed_unit, parent, ALLOWED_TEXT, [allowed_child])
        ]

    monkeypatch.setattr(access_node, "expand_parents", _expand)

    # --- 引用标题 ---
    async def _units(_session, unit_ids):
        return [
            SimpleNamespace(id=unit_id, title="差旅管理制度")
            for unit_id in unit_ids
            if unit_id == state.allowed_unit
        ]

    monkeypatch.setattr(answer_node.knowledge_repo, "get_units_by_ids", _units)

    # --- 缺口 ---
    async def _record_gap(_session, **_kwargs):
        state.gap_calls += 1

    monkeypatch.setattr(answer_node.faq_repo, "record_gap", _record_gap)

    return state


def _deps(state) -> GraphDeps:
    return GraphDeps(
        session=state.session,
        redis=object(),
        model_config=SimpleNamespace(
            faq_sim_threshold=0.88,
            gap_sim_threshold=0.35,
            rerank_model="test-reranker",
            temperature=0.2,
        ),
        subject=AclSubject(user_id=uuid4(), department_id=uuid4(), role_ids=[]),
    )


async def _run(state, question: str = QUESTION) -> dict:
    return await qa_graph.ainvoke(
        {"question": question, "user_id": str(uuid4())},
        config={"configurable": {"deps": _deps(state)}},
    )


# ---------- 拓扑 ----------


def test_every_exit_funnels_into_audit() -> None:
    """P12 的结构保证：出口漏连审计边时，这里会直接失败。"""

    edges = {(edge.source, edge.target) for edge in qa_graph.get_graph().edges}

    for exit_node in ("respond_faq", "generate", "respond_denied", "respond_gap"):
        assert (exit_node, "audit") in edges, f"{exit_node} 未汇入 audit"
    assert sum(1 for source, _ in edges if source == "audit") == 1


def test_acl_sits_between_cutoff_and_expand() -> None:
    """顺序固定，不得跳过 ACL 节点。"""

    edges = {(edge.source, edge.target) for edge in qa_graph.get_graph().edges}

    assert ("cutoff", "acl_filter") in edges
    assert ("acl_filter", "expand_parent") in edges
    assert ("expand_parent", "generate") in edges


# ---------- FAQ 路径（P4） ----------


async def test_faq_path_short_circuits_without_any_chat_call(env) -> None:
    env.faq_hit = SimpleNamespace(answer="满 1 年 5 天", faq_id="f1", matched_by="exact")

    result = await _run(env)

    assert env.rewrite_calls == 0, "P4：FAQ 命中不得调改写"
    assert env.generate_calls == 0
    assert result["answer"] == "满 1 年 5 天"
    assert result["citations"] == []
    assert result["faq_hit"] is True
    assert result["prompt_tokens"] == 0
    assert result["completion_tokens"] == 0
    assert len(env.session.rows) == 1, "P12：一次问答一条审计"
    assert env.session.rows[0].answer_status == "answered"
    assert env.session.rows[0].faq_hit is True


# ---------- 生成路径 ----------


async def test_answer_path_writes_one_audit_with_citations(env) -> None:
    result = await _run(env)

    assert result["answer"] == "依据制度，需提交住宿发票。"
    assert [item["title"] for item in result["citations"]] == ["差旅管理制度"]
    assert result["answer_status"] == "answered"
    # 改写 10/5 + 生成 900/20 必须累加，不能互相覆盖
    assert result["prompt_tokens"] == 910
    assert result["completion_tokens"] == 25

    assert len(env.session.rows) == 1
    row = env.session.rows[0]
    assert row.answer_status == "answered"
    assert row.denied_count == 0
    assert row.prompt_tokens == 910
    assert row.completion_tokens == 25
    assert row.max_similarity == 0.72
    assert row.rewritten == "出差住宿费报销标准"
    assert row.faq_hit is False


async def test_answer_path_never_exposes_denied_text(env) -> None:
    """P3：拒答单元的标题与正文不得出现在出站内容或 State 里。"""

    env.mode = "denied"

    result = await _run(env)

    serialized = repr(result)
    assert DENIED_TEXT not in serialized
    assert DENIED_TITLE not in serialized
    # denied 只保留 unit_id
    assert result["denied"] == [str(env.denied_unit)]
    assert result["allowed"] == []


# ---------- 拒答路径（P3 / P5） ----------


async def test_denied_path_uses_template_and_skips_generation(env) -> None:
    env.mode = "denied"

    result = await _run(env)

    assert result["answer"] == DENIED_ANSWER
    assert result["answer_status"] == "denied"
    assert env.generate_calls == 0, "P5：没有可读上下文时不得调生成模型"
    assert env.gap_calls == 0, "有权限问题时不能记成知识缺口"
    assert result["citations"] == []

    assert len(env.session.rows) == 1
    assert env.session.rows[0].answer_status == "denied"
    assert env.session.rows[0].denied_count == 1
    assert env.session.rows[0].citation_ids == []


# ---------- 缺口路径 ----------


async def test_gap_path_records_gap_and_skips_generation(env) -> None:
    env.mode = "gap"
    env.max_similarity = 0.11

    result = await _run(env)

    assert result["answer"] == GAP_ANSWER
    assert result["answer_status"] == "gap"
    assert env.generate_calls == 0, "P5：无上下文时不得调生成模型"
    assert env.gap_calls == 1, "缺口必须记入 knowledge_gaps"
    assert result["citations"] == []

    assert len(env.session.rows) == 1
    assert env.session.rows[0].answer_status == "gap"


async def test_empty_recall_also_goes_to_gap(env) -> None:
    """一路都没召回时同样走缺口，而不是让模型凭空作答。"""

    env.mode = "gap"
    env.max_similarity = 0.0

    result = await _run(env)

    assert result["answer"] == GAP_ANSWER
    assert env.gap_calls == 1


async def test_each_path_writes_exactly_one_audit(env) -> None:
    """P12 逐路径核对：四条出口各写且仅写一条。"""

    for mode, faq_hit, expected in (
        ("allowed", None, "answered"),
        ("denied", None, "denied"),
        ("gap", None, "gap"),
        (
            "allowed",
            SimpleNamespace(answer="已沉淀的答案", faq_id="f1", matched_by="exact"),
            "answered",
        ),
    ):
        env.mode = mode
        env.faq_hit = faq_hit
        env.session = _Session()

        result = await _run(env)

        assert len(env.session.rows) == 1, f"{mode}/faq={bool(faq_hit)} 审计条数不为 1"
        assert env.session.rows[0].answer_status == expected
        assert result["audit_id"]
