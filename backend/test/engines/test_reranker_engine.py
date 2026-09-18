"""`Reranker` 的行为测试：激活函数与实例缓存（tasklist 10.1）。

不加载真实权重，用假模型把两条关键约束锁死在单元测试里：

1. 必须**显式**过 Sigmoid——两个候选模型的 `config.json` 都没有默认激活函数，
   `predict` 返回的原始 logits 量纲会让断崖阈值失效
2. 实例按模型名缓存——否则系统配置里换了 reranker 也拿不到新模型
"""

from __future__ import annotations

import pytest

from app.common.config import settings
from app.engines.rerank import Reranker, get_reranker, reset_rerankers


class _FakeModel:
    """记录 `predict` 收到的参数，返回一组原始 logits。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list[tuple[str, str]], object]] = []

    def predict(self, pairs, batch_size, activation_fn=None):  # noqa: ANN001
        self.calls.append((list(pairs), activation_fn))
        return [7.5, -6.25]


def test_score_passes_explicit_sigmoid(monkeypatch: pytest.MonkeyPatch) -> None:
    """`activation_fn` 不能省：省了拿到的就是 logits（相邻落差动辄 5~14）。"""

    fake = _FakeModel()
    reranker = Reranker("fake/model")
    monkeypatch.setattr(reranker, "_load", lambda: fake)

    scores = reranker.score("问句", ["甲", "乙"])

    pairs, activation = fake.calls[0]
    assert pairs == [("问句", "甲"), ("问句", "乙")]
    assert activation is not None, "必须显式传 activation_fn，不能依赖模型默认值"
    assert type(activation).__name__ == "Sigmoid"
    assert scores == [7.5, -6.25], "取值原样返回，激活由模型侧负责"


def test_score_on_empty_input_does_not_load(monkeypatch: pytest.MonkeyPatch) -> None:
    reranker = Reranker("fake/model")

    def explode():  # noqa: ANN202
        raise AssertionError("空候选不该加载权重")

    monkeypatch.setattr(reranker, "_load", explode)

    assert reranker.score("问句", []) == []


def test_get_reranker_caches_per_model_name() -> None:
    """同名复用实例，异名必须是另一个实例。"""

    reset_rerankers()
    first = get_reranker("fake/one")
    again = get_reranker("fake/one")
    other = get_reranker("fake/two")

    assert first is again
    assert first is not other
    assert other.model_name == "fake/two"

    reset_rerankers()


def test_reset_rerankers_drops_instances() -> None:
    reset_rerankers()
    instance = get_reranker("fake/x")

    reset_rerankers()

    assert instance is not get_reranker("fake/x")
    reset_rerankers()


def test_rerank_device_falls_back_to_embed_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RERANK_DEVICE` 留空时跟 `EMBED_DEVICE`，填了就独立生效。

    重排不写向量库，所以单独放 cpu 不会造成「索引侧与查询侧不同设备」那种问题。
    """

    monkeypatch.setattr(settings, "rerank_device", "")
    monkeypatch.setattr(settings, "embed_device", "cuda:0")
    assert Reranker("fake/m").device == "cuda:0"

    monkeypatch.setattr(settings, "rerank_device", "cpu")
    assert Reranker("fake/m").device == "cpu"
