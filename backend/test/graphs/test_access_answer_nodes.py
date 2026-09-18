"""ACL、父块展开与答案出口节点测试（tasklist 12.4）。

这些用例刻意把 P3 / P5 的安全属性放在节点层锁死：无权正文不得进入 State 或 Prompt；没有
`parent_contexts` 时不许调用 Chat；固定出口也不许调用 Chat。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.common.errors import AppError, ErrorCode
from app.engines.acl import AclSubject
from app.engines.retrieve import ParentContext, RecalledChunk
from app.engines.storage import object_store
from app.graphs.context import GraphDeps
from app.graphs.nodes import access as access_node
from app.graphs.nodes import answer as answer_node
from app.graphs.nodes.access import acl_filter, expand_parent
from app.graphs.nodes.answer import generate, respond_denied, respond_gap
from app.graphs.nodes.prompts import DENIED_ANSWER, GAP_ANSWER


@pytest.fixture
def ids():
    return SimpleNamespace(
        allowed_unit=uuid4(), denied_unit=uuid4(), user=uuid4(), department=uuid4()
    )


@pytest.fixture
def config(ids):
    def _make(session=object()) -> dict:
        return {
            "configurable": {
                "deps": GraphDeps(
                    session=session,
                    redis=object(),
                    model_config=SimpleNamespace(gap_sim_threshold=0.62),
                    subject=AclSubject(
                        user_id=ids.user, department_id=ids.department, role_ids=[]
                    ),
                )
            }
        }

    return _make


def _hit(chunk_id: UUID, unit_id: UUID) -> RecalledChunk:
    return RecalledChunk(str(chunk_id), str(unit_id), str(uuid4()), 0.9)


# ---------- acl_filter / expand_parent ----------


async def test_acl_filter_keeps_allowed_and_retains_denied_ids_only(
    monkeypatch, config, ids
) -> None:
    allowed = _hit(uuid4(), ids.allowed_unit)
    denied = _hit(uuid4(), ids.denied_unit)
    captured: dict = {}

    class _Engine:
        def __init__(self, *, redis_factory):
            captured["redis"] = redis_factory()

        async def filter(self, subject, unit_ids, *, session):
            captured["subject"] = subject
            captured["ids"] = unit_ids
            captured["session"] = session
            return [ids.allowed_unit], [ids.denied_unit]

    monkeypatch.setattr(access_node, "AclEngine", _Engine)

    result = await acl_filter(
        {"reranked": [allowed, denied], "max_similarity": 0.75}, config()
    )

    assert result == {
        "allowed": [allowed],
        "denied": [str(ids.denied_unit)],
        # 相关度达标才提示「部分资料无权」：整轮判缺口时提示它自相矛盾
        "acl_notice": True,
        # 阈值必须随 ACL 节点落地，不能指望调用方记得注入
        "gap_threshold": 0.62,
    }
    assert captured["ids"] == [ids.allowed_unit, ids.denied_unit]
    assert captured["subject"].user_id == ids.user
    # 回传结构中只有 RecalledChunk 的 id/分数，没有可泄露的标题和正文。
    assert "机密薪酬" not in repr(result)


async def test_acl_filter_low_similarity_has_no_notice(monkeypatch, config, ids) -> None:
    """相似度不达标时整轮会判缺口，此时不得再提示「检测到相关制度但无权查阅」。"""

    allowed = _hit(uuid4(), ids.allowed_unit)
    denied = _hit(uuid4(), ids.denied_unit)

    class _Engine:
        def __init__(self, **_kwargs):
            pass

        async def filter(self, *_args, **_kwargs):
            return [ids.allowed_unit], [ids.denied_unit]

    monkeypatch.setattr(access_node, "AclEngine", _Engine)

    result = await acl_filter(
        {"reranked": [allowed, denied], "max_similarity": 0.51}, config()
    )

    assert result["acl_notice"] is False


async def test_acl_filter_only_denied_has_no_notice(monkeypatch, config, ids) -> None:
    denied = _hit(uuid4(), ids.denied_unit)

    class _Engine:
        def __init__(self, **_kwargs):
            pass

        async def filter(self, *_args, **_kwargs):
            return [], [ids.denied_unit]

    monkeypatch.setattr(access_node, "AclEngine", _Engine)

    result = await acl_filter({"reranked": [denied], "max_similarity": 0.75}, config())

    assert result["allowed"] == []
    assert result["denied"] == [str(ids.denied_unit)]
    assert result["acl_notice"] is False
    assert result["gap_threshold"] == 0.62


async def test_expand_parent_only_passes_allowed_child_ids(monkeypatch, config, ids) -> None:
    allowed_id = uuid4()
    seen: dict = {}
    contexts = [
        ParentContext(ids.allowed_unit, uuid4(), "允许的父块正文", [allowed_id])
    ]

    async def _expand(session, chunk_ids):
        seen["session"] = session
        seen["chunk_ids"] = chunk_ids
        return contexts

    monkeypatch.setattr(access_node, "expand_parents", _expand)

    result = await expand_parent(
        {"allowed": [_hit(allowed_id, ids.allowed_unit)]}, config()
    )

    assert result == {"parent_contexts": contexts}
    assert seen["chunk_ids"] == [allowed_id]


# ---------- generate ----------


class _Chat:
    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks
        self.messages = None

    async def astream(self, messages):
        self.messages = messages
        for chunk in self.chunks:
            yield chunk


def _delta(text: str, usage: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=text, usage_metadata=usage)


async def test_generate_streams_allowed_context_and_citations_only(
    monkeypatch, config, ids
) -> None:
    allowed_child = uuid4()
    contexts = [
        ParentContext(
            ids.allowed_unit,
            uuid4(),
            "允许正文：出差住宿费按职级和城市标准执行。",
            [allowed_child],
        )
    ]
    chat = _Chat([_delta("住宿费"), _delta("按制度报销。")])
    stream: list[dict] = []

    monkeypatch.setattr(answer_node, "build_chat_model", lambda *_a, **_k: chat)
    monkeypatch.setattr(answer_node, "_stream_writer", lambda: stream.append)

    async def _units(_session, unit_ids):
        assert unit_ids == [ids.allowed_unit]
        return [SimpleNamespace(id=ids.allowed_unit, title="差旅管理制度")]

    monkeypatch.setattr(answer_node.knowledge_repo, "get_units_by_ids", _units)

    result = await generate(
        {
            "question": "出差住宿费怎么报销",
            "parent_contexts": contexts,
            # 即便 State 带有 denied id，生成也不能读取它。
            "denied": [str(ids.denied_unit)],
        },
        config(),
    )

    assert result == {
        "answer": "住宿费按制度报销。",
        "citations": [
            {
                "unit_id": str(ids.allowed_unit),
                "title": "差旅管理制度",
                "chunk_id": str(allowed_child),
                "snippet": "允许正文：出差住宿费按职级和城市标准执行。",
            }
        ],
        "answer_status": "answered",
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    assert stream == [{"type": "token", "delta": "住宿费"}, {"type": "token", "delta": "按制度报销。"}]
    prompt = "\n".join(str(message.content) for message in chat.messages)
    assert "允许正文" in prompt
    assert "denied" not in prompt
    assert "机密薪酬" not in prompt


async def test_generate_accumulates_rewrite_and_stream_usage(monkeypatch, config, ids) -> None:
    """生成阶段的 usage 要与改写阶段累加，不能覆盖。"""

    context = ParentContext(ids.allowed_unit, uuid4(), "允许正文", [uuid4()])
    # 只有末个 chunk 带 usage，前几块没有
    chat = _Chat(
        [
            _delta("答"),
            _delta("案"),
            _delta("", usage={"input_tokens": 900, "output_tokens": 40}),
        ]
    )

    monkeypatch.setattr(answer_node, "build_chat_model", lambda *_a, **_k: chat)
    monkeypatch.setattr(answer_node, "_stream_writer", lambda: None)

    async def _units(_session, _ids):
        return [SimpleNamespace(id=ids.allowed_unit, title="制度")]

    monkeypatch.setattr(answer_node.knowledge_repo, "get_units_by_ids", _units)

    result = await generate(
        {
            "question": "问题",
            "parent_contexts": [context],
            "prompt_tokens": 156,
            "completion_tokens": 322,
        },
        config(),
    )

    assert result["prompt_tokens"] == 156 + 900
    assert result["completion_tokens"] == 322 + 40


async def test_generate_without_context_never_calls_chat(monkeypatch, config) -> None:
    monkeypatch.setattr(
        answer_node,
        "build_chat_model",
        lambda *_a, **_k: pytest.fail("P5：allowed 为空时不得调生成模型"),
    )

    result = await generate({"question": "没有上下文", "parent_contexts": []}, config())

    assert result == {
        "answer": GAP_ANSWER,
        "citations": [],
        "answer_status": "gap",
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


async def test_generate_wraps_gateway_failure(monkeypatch, config, ids) -> None:
    context = ParentContext(ids.allowed_unit, uuid4(), "允许正文", [uuid4()])

    class _BrokenChat:
        async def astream(self, _messages):
            raise RuntimeError("gateway timeout")
            yield  # pragma: no cover - 让 Python 将它识别成 async generator

    monkeypatch.setattr(answer_node, "build_chat_model", lambda *_a, **_k: _BrokenChat())

    with pytest.raises(AppError) as excinfo:
        await generate({"question": "问题", "parent_contexts": [context]}, config())

    assert excinfo.value.code == ErrorCode.AI_UPSTREAM_ERROR
    assert excinfo.value.http_status == 502


# ---------- citations[].assets（tasklist 17.5） ----------


def _patch_chat(monkeypatch, ids, titles: dict) -> None:
    """让 generate 走假模型与假标题查询，只留 citation 组装这段逻辑可断言。"""

    monkeypatch.setattr(
        answer_node, "build_chat_model", lambda *_a, **_k: _Chat([_delta("答案。")])
    )
    monkeypatch.setattr(answer_node, "_stream_writer", lambda: None)

    async def _units(_session, unit_ids):
        return [
            SimpleNamespace(id=unit_id, title=titles[unit_id]) for unit_id in unit_ids
        ]

    monkeypatch.setattr(answer_node.knowledge_repo, "get_units_by_ids", _units)


async def test_citations_carry_asset_proxy_urls_not_storage_keys(
    monkeypatch, config, ids
) -> None:
    """附图给的是后端代理地址，不是对象存储地址（P17 的同一条底线）。"""

    content = (
        "流程如下。\n\n![](assets/img_1.jpg)\n\n图注：三级审批流程。\n\n"
        "![](assets/img_2.png)\n"
    )
    contexts = [ParentContext(ids.allowed_unit, uuid4(), content, [uuid4()])]
    _patch_chat(monkeypatch, ids, {ids.allowed_unit: "审批流程"})

    result = await generate({"question": "审批流程", "parent_contexts": contexts}, config())

    assets = result["citations"][0]["assets"]
    assert [item["name"] for item in assets] == ["img_1.jpg", "img_2.png"]
    assert assets[0]["url"] == (
        f"/api/v1/knowledge-units/{ids.allowed_unit}/assets/img_1.jpg"
    )
    for item in assets:
        # 前端不直连对象存储：地址里不许出现桶名、endpoint 或对象 key
        assert object_store.UNIT_PREFIX not in item["url"]
        assert "minio" not in item["url"].lower()


async def test_answer_image_budget_is_shared_across_citations(
    monkeypatch, config, ids
) -> None:
    """上限是**单条答案**的：按引用共享，否则引用一多附图就能刷成几十张。"""

    other_unit = uuid4()
    monkeypatch.setattr(answer_node, "settings", SimpleNamespace(answer_max_images=2))
    contexts = [
        ParentContext(
            ids.allowed_unit,
            uuid4(),
            "![](assets/a.png)\n\n![](assets/b.png)",
            [uuid4()],
        ),
        ParentContext(other_unit, uuid4(), "![](assets/c.png)", [uuid4()]),
    ]
    _patch_chat(monkeypatch, ids, {ids.allowed_unit: "甲", other_unit: "乙"})

    result = await generate({"question": "问题", "parent_contexts": contexts}, config())

    citations = result["citations"]
    assert [item["name"] for item in citations[0]["assets"]] == ["a.png", "b.png"]
    # 配额用尽后，后面的引用不再附图
    assert "assets" not in citations[1]


async def test_zero_budget_disables_assets(monkeypatch, config, ids) -> None:
    context = ParentContext(ids.allowed_unit, uuid4(), "![](assets/a.png)", [uuid4()])
    monkeypatch.setattr(answer_node, "settings", SimpleNamespace(answer_max_images=0))
    _patch_chat(monkeypatch, ids, {ids.allowed_unit: "甲"})

    result = await generate({"question": "问题", "parent_contexts": [context]}, config())

    assert "assets" not in result["citations"][0]


async def test_external_image_urls_do_not_become_citation_assets(
    monkeypatch, config, ids
) -> None:
    """手写文档里的外链图片不在本单元里，附图只收 `assets/` 相对 key。"""

    content = "![外链](https://cdn.example.com/assets/logo.png)\n\n正文。"
    context = ParentContext(ids.allowed_unit, uuid4(), content, [uuid4()])
    _patch_chat(monkeypatch, ids, {ids.allowed_unit: "甲"})

    result = await generate({"question": "问题", "parent_contexts": [context]}, config())

    assert "assets" not in result["citations"][0]


# ---------- 固定出口 ----------


async def test_respond_denied_is_fixed_and_never_calls_chat(monkeypatch, config) -> None:
    monkeypatch.setattr(
        answer_node,
        "build_chat_model",
        lambda *_a, **_k: pytest.fail("拒答出口不许调用 Chat"),
    )

    result = await respond_denied(
        {"denied": [str(uuid4())], "question": "机密薪酬是多少"}, config()
    )

    assert result == {
        "answer": DENIED_ANSWER,
        "citations": [],
        "answer_status": "denied",
    }
    assert "机密薪酬" not in result["answer"]


async def test_respond_gap_records_gap_without_chat(monkeypatch, config, ids) -> None:
    session = SimpleNamespace(commits=0)

    async def _commit():
        session.commits += 1

    session.commit = _commit
    captured: dict = {}

    async def _record(_session, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(answer_node.faq_repo, "record_gap", _record)
    monkeypatch.setattr(
        answer_node,
        "build_chat_model",
        lambda *_a, **_k: pytest.fail("缺口出口不许调用 Chat"),
    )

    result = await respond_gap(
        {"question": "清关单据需要哪些", "max_similarity": 0.12}, config(session)
    )

    assert result == {
        "answer": GAP_ANSWER,
        "citations": [],
        "answer_status": "gap",
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    assert captured == {
        "question": "清关单据需要哪些",
        "department_id": ids.department,
        "max_similarity": 0.12,
    }
    assert session.commits == 1
