"""断崖切分的单测与属性测试（tasklist 10.3）。

覆盖 P6 的三类边界（无断崖、落差明显、条数越界），断言保留条数始终落在
`[min(3, n), min(10, n)]` 内。切分是纯函数，不需要加载模型。
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from app.engines.rerank import Reranker, cliff_index, cutoff, rank

MIN_KEEP = 3
MAX_KEEP = 10
# 归一化落差比阈值（落差 / 最高分），不是绝对落差
MIN_RATIO = 0.13


def test_no_cliff_keeps_max_keep() -> None:
    """分数缓降、落差全在阈值之下 → 判定无断崖 → 保留 10。"""

    scores = [1.0 - index * 0.01 for index in range(20)]

    assert cutoff(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO) == 10


def test_equal_scores_keep_max_keep() -> None:
    """分数完全相同（没有任何落差）也必须给出确定结果。"""

    assert (
        cutoff([0.9] * 50, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO)
        == MAX_KEEP
    )


def test_obvious_cliff_cuts_there() -> None:
    """第 4 名之后骤降（落差 0.45）→ 切在 4，保留 4 条。"""

    scores = [0.9, 0.85, 0.8, 0.75, 0.3, 0.28, 0.25]

    assert cliff_index(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP) == 4
    assert cutoff(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO) == 4


def test_cliff_before_min_keep_is_ignored() -> None:
    """前几名之间的落差再大也不能切：至少要保留 `min_keep` 条。"""

    scores = [0.9, 0.2, 0.19, 0.18, 0.17]

    # 检测范围从下标 3 起，头部的 0.7 落差根本不参与
    assert cutoff(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO) == 5


def test_cliff_beyond_max_keep_is_ignored() -> None:
    """真断崖在第 11 名之后：切点上限是 `max_keep`，因此保留 10。"""

    scores = [0.9 - index * 0.001 for index in range(10)] + [0.05] * 10

    assert cutoff(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO) == 10


def test_short_candidate_list_keeps_all() -> None:
    """不足 `min_keep` 条时不可能凭空凑数，只能全保留。"""

    assert cutoff([0.9, 0.1], min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO) == 2
    assert cutoff([], min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO) == 0


def test_keep_count_stays_within_bounds() -> None:
    """边界性质：无论输入多长，`k` 都在 `[min(3, n), min(10, n)]` 内。"""

    rng = random.Random(11)
    for size in range(0, 40):
        for _ in range(20):
            scores = sorted((rng.random() for _ in range(size)), reverse=True)
            keep = cutoff(
                scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO
            )
            assert min(MIN_KEEP, size) <= keep <= min(MAX_KEEP, size)


def test_cutoff_is_deterministic() -> None:
    scores = [0.9, 0.85, 0.8, 0.75, 0.3]

    results = {
        cutoff(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO)
        for _ in range(20)
    }

    assert results == {4}


def test_reranker_exposes_pure_helpers() -> None:
    """tasklist 10.2 要求能按 `Reranker.cutoff(scores, ...)` 直接调用。"""

    scores = [0.9, 0.85, 0.8, 0.75, 0.3]

    assert (
        Reranker.cutoff(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO)
        == 4
    )
    assert Reranker.cliff_index(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP) == 4


def test_rank_orders_desc_and_breaks_ties_by_original_index() -> None:
    class FixedReranker:
        def score(self, query: str, passages: Sequence[str]) -> list[float]:
            return [0.5, 0.95, 0.5, 0.9, 0.2]

    ranked = rank(
        "问句",
        ["a", "b", "c", "d", "e"],
        reranker=FixedReranker(),  # type: ignore[arg-type]
        min_keep=MIN_KEEP,
        max_keep=MAX_KEEP,
        min_drop_ratio=MIN_RATIO,
    )

    # 降序 b(0.95) d(0.9) a(0.5) c(0.5) e(0.2)；同分的 a/c 按原下标，a 在前
    assert [index for index, _ in ranked] == [1, 3, 0, 2]


def test_rank_on_empty_input_skips_scoring() -> None:
    class ExplodingReranker:
        def score(self, query: str, passages: Sequence[str]) -> list[float]:
            raise AssertionError("空候选不该调用打分")

    assert rank("问句", [], reranker=ExplodingReranker()) == []  # type: ignore[arg-type]


# ---------- 低分区：落差比存在的理由 ----------


def test_low_score_cliff_is_detected_by_ratio() -> None:
    """低分区的断崖必须靠归一化落差比才切得动。

    绝对落差只有 0.043，任何绝对阈值（旧值 0.15）都会判成「无断崖」、硬留 10 条；
    但它是最高分 0.05 的 86%，比例上是不折不扣的断崖。这正是改用落差比的原因。
    """

    scores = [0.05, 0.048, 0.046, 0.044, 0.001, 0.0009, 0.0008, 0.0007, 0.0006, 0.0005]

    # 落差比：0.04 0.04 0.04 0.86 0.002 ...
    assert (
        cutoff(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP, min_drop_ratio=MIN_RATIO)
        == 4
    )


# ---------- 属性测试 ----------


def test_scaling_scores_does_not_move_the_cliff() -> None:
    """属性：整列分数乘正常数后，最大落差位置不变——切点只看落差的相对大小。"""

    rng = random.Random(20260915)

    for _ in range(300):
        size = rng.randint(1, 20)
        scores = sorted((rng.random() for _ in range(size)), reverse=True)
        factor = rng.choice([0.5, 2.0, 7.5, 100.0])

        assert cliff_index(scores, min_keep=MIN_KEEP, max_keep=MAX_KEEP) == cliff_index(
            [score * factor for score in scores],
            min_keep=MIN_KEEP,
            max_keep=MAX_KEEP,
        )
