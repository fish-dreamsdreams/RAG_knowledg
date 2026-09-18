"""RRF 融合的单测与属性测试（tasklist 9.4）。

核心性质：融合只依赖名次排序，对单路分数整体加常数不敏感。
"""

from __future__ import annotations

import random

from app.engines.retrieve.fusion import RRF_K, RecalledChunk, rrf_merge


def chunk(name: str, score: float = 0.0) -> RecalledChunk:
    return RecalledChunk(
        chunk_id=name, unit_id=f"unit-{name}", parent_id=f"parent-{name}", score=score
    )


def test_single_ranking_keeps_order() -> None:
    merged = rrf_merge([[chunk("a"), chunk("b"), chunk("c")]], limit=10)

    assert [item.chunk_id for item in merged] == ["a", "b", "c"]


def test_raw_scores_do_not_matter() -> None:
    """单路分数只是摆设，名次才决定结果。"""

    left = [chunk("a", 0.9), chunk("b", 0.1)]
    right = [chunk("a", 0.01), chunk("b", 0.99)]

    assert rrf_merge([left], limit=10) == rrf_merge([right], limit=10)


def test_two_rankings_reward_agreement() -> None:
    """两路都靠前的排在只有一路靠前的之前。"""

    merged = rrf_merge([[chunk("a"), chunk("b")], [chunk("b"), chunk("c")]], limit=10)

    assert [item.chunk_id for item in merged] == ["b", "a", "c"]


def test_score_is_sum_over_routes() -> None:
    merged = rrf_merge([[chunk("a"), chunk("x")], [chunk("a")]], limit=10)

    assert merged[0].chunk_id == "a"
    assert merged[0].score == 1 / (RRF_K + 1) + 1 / (RRF_K + 1)


def test_duplicate_across_routes_is_merged_once() -> None:
    merged = rrf_merge([[chunk("a")], [chunk("a")]], limit=10)

    assert len(merged) == 1
    assert merged[0].score == 2 * (1 / (RRF_K + 1))


def test_keeps_content_fields_from_first_seen() -> None:
    first = RecalledChunk("a", "unit-1", "parent-1", 0.0)
    second = RecalledChunk("a", "unit-1", "parent-1", 5.0)

    merged = rrf_merge([[first], [second]], limit=10)

    assert merged[0].unit_id == "unit-1"
    assert merged[0].parent_id == "parent-1"
    assert merged[0].score != 5.0, "融合后 score 是 RRF 分，不再是向量相似度"


def test_limit_truncates() -> None:
    ranking = [chunk(f"c{index}") for index in range(30)]

    assert len(rrf_merge([ranking], limit=20)) == 20


def test_empty_input() -> None:
    assert rrf_merge([], limit=10) == []
    assert rrf_merge([[]], limit=10) == []


def test_ties_break_deterministically() -> None:
    """不同路的同分项按 chunk_id 升序，保证同输入同输出。"""

    merged = rrf_merge([[chunk("b")], [chunk("a")]], limit=10)

    assert [item.chunk_id for item in merged] == ["a", "b"]


# ---------- 属性测试 ----------


def _random_ranking(
    rng: random.Random, pool: list[str], size: int
) -> list[RecalledChunk]:
    return [chunk(name, rng.random()) for name in rng.sample(pool, min(size, len(pool)))]


def test_adding_constant_to_a_route_changes_nothing() -> None:
    """属性：某一路的分数整体加常数（量纲漂移的极端情形）不改变融合结果。"""

    rng = random.Random(20260915)
    pool = [f"c{index}" for index in range(12)]

    for _ in range(300):
        rankings = [
            _random_ranking(rng, pool, rng.randint(0, 12)) for _ in range(3)
        ]
        shifted = [
            [
                item._replace(score=item.score + rng.choice([1e3, 1e9]))
                for item in ranking
            ]
            for ranking in rankings
        ]

        assert rrf_merge(rankings, limit=20) == rrf_merge(shifted, limit=20)


def test_result_is_deduped_subset_of_input() -> None:
    """属性：结果去重、只含输入项、不超过 limit、分数单调不增。"""

    rng = random.Random(7)
    pool = [f"c{index}" for index in range(10)]

    for _ in range(300):
        rankings = [
            _random_ranking(rng, pool, rng.randint(0, 10)) for _ in range(4)
        ]
        limit = rng.randint(1, 20)
        merged = rrf_merge(rankings, limit=limit)

        ids = [item.chunk_id for item in merged]
        present = {item.chunk_id for ranking in rankings for item in ranking}

        assert len(ids) == len(set(ids))
        assert set(ids) <= present
        assert len(ids) <= limit
        scores = [item.score for item in merged]
        assert scores == sorted(scores, reverse=True)
