"""faq / respond_faq 节点测试（tasklist 12.2）。

缓存命中判定本身已在 `test/engines/test_faq_cache.py` 覆盖，这里只测节点：命中时回什么、
未命中时留什么，以及**不做什么**——P4 要求命中 FAQ 时不检索、不调 Chat。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.graphs.context import GraphDeps
from app.graphs.nodes import faq as faq_node
from app.graphs.nodes.faq import faq_match, respond_faq

QUESTION = "年假可以休几天"


@pytest.fixture
def config():
    """节点只用到 `faq_sim_threshold`，其余依赖给最简替身。

    必须是真 `GraphDeps`：`deps_of` 会做类型检查，用 SimpleNamespace 会被它拦下——那个
    检查就是为了让「忘了注入依赖」立刻炸掉而不是静默走到默认值。
    """

    def _make() -> dict:
        deps = GraphDeps(
            session=object(),
            redis=object(),
            model_config=SimpleNamespace(faq_sim_threshold=0.88, gap_sim_threshold=0.35),
            subject=object(),
        )
        return {"configurable": {"deps": deps}}

    return _make


@pytest.fixture(autouse=True)
def stub_encoder(monkeypatch) -> None:
    monkeypatch.setattr(faq_node, "_encode", lambda question: [0.1, 0.2])


def _hit(answer: str = "满 1 年不满 10 年 5 天"):
    return SimpleNamespace(
        faq_id="f1", question=QUESTION, answer=answer, score=1.0, matched_by="exact"
    )


async def test_match_hit_fills_answer(monkeypatch, config) -> None:
    async def _match(*_args, **_kwargs):
        return _hit()

    monkeypatch.setattr(faq_node, "match_faq", _match)

    result = await faq_match({"question": QUESTION}, config())

    assert result == {
        "faq_hit": True,
        "faq_answer": "满 1 年不满 10 年 5 天",
        # P4：命中 FAQ 不调 Chat，本次问答的 token 消耗必须为 0
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


async def test_match_miss_only_sets_flag(monkeypatch, config) -> None:
    """未命中时不得写入 `faq_answer`：回头 `respond_faq` 被误触发也不能吐半截答案。"""

    async def _match(*_args, **_kwargs):
        return None

    monkeypatch.setattr(faq_node, "match_faq", _match)

    result = await faq_match({"question": QUESTION}, config())

    assert result == {"faq_hit": False}


async def test_match_passes_threshold_from_config(monkeypatch, config) -> None:
    """阈值必须来自系统配置，不能吃引擎默认值。"""

    seen: dict = {}

    async def _match(_redis, _question, _vector, *, threshold):
        seen["threshold"] = threshold
        return None

    monkeypatch.setattr(faq_node, "match_faq", _match)
    runtime = config()
    runtime["configurable"]["deps"].model_config.faq_sim_threshold = 0.91

    await faq_match({"question": QUESTION}, runtime)

    assert seen["threshold"] == 0.91


async def test_respond_faq_returns_answer_without_citations(config) -> None:
    """FAQ 是人工审核过的独立答案，不指向任何单元，因此 citation 恒为空。"""

    result = await respond_faq({"faq_answer": "满 1 年 5 天"}, config())

    assert result == {"answer": "满 1 年 5 天", "citations": [], "answer_status": "answered"}


async def test_respond_faq_tolerates_missing_answer(config) -> None:
    """`respond_faq` 被误触发时返回空串，而不是抛异常把整轮问答打挂。"""

    assert await respond_faq({}, config()) == {
        "answer": "",
        "citations": [],
        "answer_status": "answered",
    }
