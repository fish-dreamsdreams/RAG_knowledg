"""FAQ 命中与缓存维护测试（tasklist 11.5，覆盖 TECH_SPEC §5.5）。

不引 `fakeredis`，也不连真 Redis：命中判定与缓存维护的正确性是纯逻辑，不该依赖一个跑着的
服务。`FakeRedis` 只实现本模块实际用到的命令，超出范围的用法会直接 AttributeError 暴露出来。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from redis.exceptions import RedisError

from app.engines.faq_cache import (
    PUBLISHED_KEY,
    FaqEntry,
    match,
    normalize,
    rebuild,
    remove,
    set_enabled,
    upsert,
)

THRESHOLD = 0.88


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple]] = []

    def hset(self, *args) -> _FakePipeline:
        self._ops.append(("hset", args))
        return self

    def delete(self, *args) -> _FakePipeline:
        self._ops.append(("delete", args))
        return self

    def hdel(self, *args) -> _FakePipeline:
        self._ops.append(("hdel", args))
        return self

    def set(self, *args) -> _FakePipeline:
        self._ops.append(("set", args))
        return self

    def get(self, *args) -> _FakePipeline:
        self._ops.append(("get", args))
        return self

    async def execute(self) -> list:
        self._redis.guard()
        results = []
        for name, args in self._ops:
            results.append(await getattr(self._redis, name)(*args))
        self._ops.clear()
        return results


class FakeRedis:
    """够用的内存替身。`fail=True` 用来验证缓存故障时的降级行为。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.fail = False

    def guard(self) -> None:
        if self.fail:
            raise RedisError("模拟 Redis 故障")

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def hset(self, key: str, field: str, value: str) -> int:
        self.guard()
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hget(self, key: str, field: str) -> str | None:
        self.guard()
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> dict[str, str]:
        self.guard()
        return dict(self.hashes.get(key, {}))

    async def hkeys(self, key: str) -> list[str]:
        self.guard()
        return list(self.hashes.get(key, {}))

    async def hdel(self, key: str, field: str) -> int:
        self.guard()
        return 1 if self.hashes.get(key, {}).pop(field, None) is not None else 0

    async def delete(self, *keys: str) -> int:
        self.guard()
        removed = 0
        for key in keys:
            removed += 1 if self.hashes.pop(key, None) is not None else 0
            removed += 1 if self.strings.pop(key, None) is not None else 0
        return removed

    async def get(self, key: str) -> str | None:
        self.guard()
        return self.strings.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.guard()
        self.strings[key] = value
        return True


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


def _entry(
    faq_id: str,
    question: str,
    *,
    enabled: bool = True,
    vector: Sequence[float] | None = None,
) -> FaqEntry:
    return FaqEntry(
        faq_id=faq_id,
        question=question,
        answer=f"{question}的答案",
        enabled=enabled,
        vector=vector,
    )


# ---------- 文本匹配 ----------


async def test_exact_match_ignores_punctuation_and_spaces(redis: FakeRedis) -> None:
    """「年假可以休几天？」与「年假 可以休几天」应当命中同一条。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天？")])

    hit = await match(redis, "年假 可以休几天", threshold=THRESHOLD)

    assert hit is not None
    assert hit.faq_id == "f1"
    assert hit.matched_by == "exact"
    assert hit.score == 1.0


async def test_contains_match_accepts_true_substring(redis: FakeRedis) -> None:
    """「可以休几天」是「年假可以休几天」的真子串，占 5/7，文本层就该命中。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天")])

    hit = await match(redis, "可以休几天", threshold=THRESHOLD)

    assert hit is not None
    assert hit.matched_by == "contains"


async def test_paraphrase_is_not_caught_by_text_match(redis: FakeRedis) -> None:
    """改写问法不是子串，文本层抓不到——这正是向量匹配存在的理由。

    「年假几天」对「年假可以休几天」是常见改写，但两个字面量没有包含关系。如果今后有人把
    这里改成模糊匹配来“修”它，就是在用文本近似重复实现向量召回。
    """

    await rebuild(redis, [_entry("f1", "年假可以休几天", vector=[1.0, 0.0, 0.0, 0.0])])

    # 没有查询向量时抓不到
    assert await match(redis, "年假几天", threshold=THRESHOLD) is None
    # 给了向量才命中
    hit = await match(redis, "年假几天", [1.0, 0.0, 0.0, 0.0], threshold=THRESHOLD)
    assert hit is not None
    assert hit.matched_by == "vector"


async def test_contains_match_rejects_too_short_fragment(redis: FakeRedis) -> None:
    """「年假」只占 2/7，太泛，不该被文本吃掉——交给向量判定。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天")])

    assert await match(redis, "年假", threshold=THRESHOLD) is None


async def test_empty_cache_misses(redis: FakeRedis) -> None:
    assert await match(redis, "年假可以休几天", threshold=THRESHOLD) is None


# ---------- 向量匹配 ----------


async def test_vector_match_above_threshold_hits(redis: FakeRedis) -> None:
    await rebuild(
        redis,
        [
            _entry("f1", "年假可以休几天", vector=[1.0, 0.0, 0.0, 0.0]),
            _entry("f2", "报销需要哪些材料", vector=[0.0, 0.0, 1.0, 0.0]),
        ],
    )

    hit = await match(redis, "休假天数怎么算", [0.99, 0.1, 0.0, 0.0], threshold=THRESHOLD)

    assert hit is not None
    assert hit.faq_id == "f1"
    assert hit.matched_by == "vector"
    assert hit.score > THRESHOLD


async def test_vector_match_below_threshold_misses(redis: FakeRedis) -> None:
    """低于阈值必须返回 None：这是「FAQ 未命中 → 走检索」的分支依据。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天", vector=[1.0, 0.0, 0.0, 0.0])])

    assert await match(redis, "报销怎么走", [0.0, 0.0, 1.0, 0.0], threshold=THRESHOLD) is None


async def test_vector_match_without_query_vector_only_does_text(redis: FakeRedis) -> None:
    await rebuild(redis, [_entry("f1", "年假可以休几天", vector=[1.0, 0.0, 0.0, 0.0])])

    assert await match(redis, "完全不相关的问题", None, threshold=THRESHOLD) is None


async def test_dimension_mismatch_is_not_a_hit(redis: FakeRedis) -> None:
    """换过 Embedding 模型的旧缓存维度对不上时应判不命中，而不是抛异常。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天", vector=[1.0, 0.0])])

    assert await match(redis, "别的问题", [1.0, 0.0, 0.0], threshold=THRESHOLD) is None


# ---------- cache_enabled ----------


async def test_disabled_entry_is_excluded_from_text_match(redis: FakeRedis) -> None:
    """`cache_enabled=false` 的 FAQ 不参与匹配（tasklist 11.5 明确要求）。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天", enabled=False)])

    assert await match(redis, "年假可以休几天", threshold=THRESHOLD) is None


async def test_disabled_entry_is_excluded_from_vector_match(redis: FakeRedis) -> None:
    await rebuild(
        redis,
        [_entry("f1", "年假可以休几天", enabled=False, vector=[1.0, 0.0, 0.0, 0.0])],
    )

    assert await match(redis, "休假天数", [1.0, 0.0, 0.0, 0.0], threshold=THRESHOLD) is None


async def test_set_enabled_takes_effect_without_rebuild(redis: FakeRedis) -> None:
    """切开关必须立刻生效，不必等全量重建。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天")])
    assert await match(redis, "年假可以休几天", threshold=THRESHOLD) is not None

    assert await set_enabled(redis, "f1", False) is True
    assert await match(redis, "年假可以休几天", threshold=THRESHOLD) is None

    assert await set_enabled(redis, "f1", True) is True
    assert await match(redis, "年假可以休几天", threshold=THRESHOLD) is not None


async def test_set_enabled_on_missing_entry_reports_false(redis: FakeRedis) -> None:
    assert await set_enabled(redis, "不存在", False) is False


# ---------- 缓存维护 ----------


async def test_rebuild_removes_stale_vector_keys(redis: FakeRedis) -> None:
    """旧向量键不在 Hash 里，整体重建必须显式清掉，否则泄漏且可能被后续匹配捞出来。"""

    await rebuild(redis, [_entry("old", "旧问题", vector=[1.0, 0.0])])
    assert "kb:faq:emb:old" in redis.strings

    await rebuild(redis, [_entry("new", "新问题", vector=[0.0, 1.0])])

    assert "kb:faq:emb:old" not in redis.strings
    assert "kb:faq:emb:new" in redis.strings
    assert set(redis.hashes[PUBLISHED_KEY]) == {"new"}


async def test_rebuild_with_empty_entries_clears_cache(redis: FakeRedis) -> None:
    await rebuild(redis, [_entry("f1", "年假可以休几天", vector=[1.0, 0.0])])

    assert await rebuild(redis, []) == 0

    assert redis.hashes.get(PUBLISHED_KEY, {}) == {}
    assert "kb:faq:emb:f1" not in redis.strings


async def test_upsert_updates_single_entry(redis: FakeRedis) -> None:
    await rebuild(redis, [_entry("f1", "年假可以休几天")])

    assert await upsert(redis, _entry("f1", "年假可以休几天", enabled=False)) is True

    assert await match(redis, "年假可以休几天", threshold=THRESHOLD) is None


async def test_remove_deletes_both_hash_and_vector(redis: FakeRedis) -> None:
    await rebuild(redis, [_entry("f1", "年假可以休几天", vector=[1.0, 0.0])])

    assert await remove(redis, "f1") is True

    assert "f1" not in redis.hashes.get(PUBLISHED_KEY, {})
    assert "kb:faq:emb:f1" not in redis.strings


# ---------- 故障降级 ----------


async def test_redis_failure_returns_miss_instead_of_raising(redis: FakeRedis) -> None:
    """Redis 挂了只应导致未命中、回退正常检索，不该让问答失败。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天")])
    redis.fail = True

    assert await match(redis, "年假可以休几天", threshold=THRESHOLD) is None
    assert await rebuild(redis, [_entry("f1", "x")]) == 0
    assert await upsert(redis, _entry("f1", "x")) is False
    assert await remove(redis, "f1") is False
    assert await set_enabled(redis, "f1", False) is False


async def test_corrupted_payload_is_skipped(redis: FakeRedis) -> None:
    """单条 JSON 损坏只跳过该条，其它条目仍可命中。"""

    await rebuild(redis, [_entry("f1", "年假可以休几天")])
    redis.hashes[PUBLISHED_KEY]["bad"] = "{ 不是 JSON"

    hit = await match(redis, "年假可以休几天", threshold=THRESHOLD)

    assert hit is not None
    assert hit.faq_id == "f1"


# ---------- normalize ----------


def test_normalize_strips_punctuation_and_case() -> None:
    assert normalize("年假可以休几天？") == "年假可以休几天"
    assert normalize("  Annual　Leave ") == "annualleave"
    assert normalize("报销，需要什么材料！") == "报销需要什么材料"
