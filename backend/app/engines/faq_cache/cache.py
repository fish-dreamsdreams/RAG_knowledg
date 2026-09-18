"""FAQ 命中判定与 Redis 缓存维护（tasklist 11.1，TECH_SPEC §5.5）。

分层（design.md）：这里只做**命中判定 + Redis 维护**，不查库、不写 FAQ 业务——FAQ 数据由
`services` 从 PostgreSQL 查出后传进来（`repositories` 是唯一 SQL 出口）。

缓存是**纯加速层**：Redis 不可用时一律返回「未命中」，让调用方回退到正常检索链路。命中 FAQ
只是省掉检索与生成（P4 要求「命中时不检索、不调 Chat」），不是正确性的前提，所以缓存故障
不该让问答失败。

Redis 结构（键名遵循 `kb:{域}:{id}` 约定）：

- `kb:faq:published`：Hash，field 是 `faq_id`，value 是 `{"q", "a", "enabled"}` 的 JSON
- `kb:faq:emb:{faq_id}`：String，该 FAQ 问句的 dense 向量（JSON 数组）

`enabled=false` 的条目**留在缓存里但跳过匹配**：这样切换 `cache_enabled` 之后无需等全量重建
就能立刻生效，也让「下线」与「禁用缓存」在命中路径上表现一致。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

PUBLISHED_KEY = "kb:faq:published"
_EMB_PREFIX = "kb:faq:emb:"

# 文本归一化要抹掉的噪声：空白、ASCII 标点、CJK 标点与全角标点。
# 目标是让「年假可以休几天？」「年假可以休几天」「年假 可以休几天」等价。
_NOISE = re.compile(
    r"[\s\u3000!-/:-@\[-`{-~"
    r"\u3001-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65]"
)
# 包含匹配的占位下限：短问句占长问句的比例低于此值就不算命中。
# 「年假几天」(4) ⊂「年假可以休几天」(7) 比例 0.57，是要命中的；「年假」(2) 比例 0.29 太泛，
# 应当交给向量判定，否则任何含「年假」两字的问句都会被一条 FAQ 吃掉。
_CONTAINS_MIN_RATIO = 0.5
_CONTAINS_MIN_CHARS = 2


@dataclass(frozen=True)
class FaqEntry:
    """重建 / 更新缓存所需的 FAQ 快照，由 `services` 组装。"""

    faq_id: str
    question: str
    answer: str
    enabled: bool = True
    vector: Sequence[float] | None = None


@dataclass(frozen=True)
class FaqHit:
    """命中结果。`matched_by` 记录判定路径，便于排查「为什么命中了这条」。"""

    faq_id: str
    question: str
    answer: str
    score: float
    matched_by: str  # exact | contains | vector


def normalize(text: str) -> str:
    """抹掉空白与标点并统一大小写，用于文本匹配。"""

    return _NOISE.sub("", text).lower()


def _emb_key(faq_id: str) -> str:
    return f"{_EMB_PREFIX}{faq_id}"


def _dump(entry: FaqEntry) -> str:
    return json.dumps(
        {"q": entry.question, "a": entry.answer, "enabled": entry.enabled},
        ensure_ascii=False,
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度。两侧独立归一化，不假设上游向量已单位化。"""

    if not left or len(left) != len(right):
        return 0.0
    dot = norm_left = norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


async def rebuild(redis: Redis, entries: Sequence[FaqEntry]) -> int:
    """用 `entries` 整体替换缓存，返回写入条数。

    发布、下线、切换缓存开关之后由 `services` 调用。这里刻意**整体替换**而不是增量合并：
    权威数据在 PostgreSQL，缓存只是它的投影，全量重建最不容易积累偏差。

    旧向量键必须显式删除（`kb:faq:emb:{id}` 不在 Hash 里，删 Hash 带不走它）。所以先读出
    旧 field 列表，再在同一个 pipeline 里连同新数据一起提交——避免"先写后删"那种瞬时不一致。
    `entries` 为空是合法输入，等同于清空缓存。
    """

    try:
        stale_ids = await redis.hkeys(PUBLISHED_KEY)
    except RedisError:
        logger.warning("读取 FAQ 缓存失败，放弃重建", exc_info=True)
        return 0

    try:
        pipe = redis.pipeline()
        pipe.delete(PUBLISHED_KEY)
        for faq_id in stale_ids:
            pipe.delete(_emb_key(faq_id))
        for entry in entries:
            pipe.hset(PUBLISHED_KEY, entry.faq_id, _dump(entry))
            if entry.vector is not None:
                pipe.set(_emb_key(entry.faq_id), json.dumps(list(entry.vector)))
        await pipe.execute()
    except RedisError:
        logger.warning("重建 FAQ 缓存失败", exc_info=True)
        return 0

    return len(entries)


async def upsert(redis: Redis, entry: FaqEntry) -> bool:
    """写入或更新单条 FAQ（改答案、发布单条时用，避免全量重建）。"""

    try:
        pipe = redis.pipeline()
        pipe.hset(PUBLISHED_KEY, entry.faq_id, _dump(entry))
        if entry.vector is not None:
            pipe.set(_emb_key(entry.faq_id), json.dumps(list(entry.vector)))
        await pipe.execute()
    except RedisError:
        logger.warning("写入 FAQ 缓存失败：%s", entry.faq_id, exc_info=True)
        return False
    return True


async def remove(redis: Redis, faq_id: str) -> bool:
    """下线单条 FAQ：Hash 与向量键一起删，避免残留向量被后续匹配捞出来。"""

    try:
        pipe = redis.pipeline()
        pipe.hdel(PUBLISHED_KEY, faq_id)
        pipe.delete(_emb_key(faq_id))
        await pipe.execute()
    except RedisError:
        logger.warning("删除 FAQ 缓存失败：%s", faq_id, exc_info=True)
        return False
    return True


async def set_enabled(redis: Redis, faq_id: str, enabled: bool) -> bool:
    """切换单条的 `cache_enabled`。条目不在缓存里时返回 False，由调用方决定是否补建。"""

    try:
        raw = await redis.hget(PUBLISHED_KEY, faq_id)
        if raw is None:
            return False
        data = json.loads(raw)
        data["enabled"] = enabled
        await redis.hset(PUBLISHED_KEY, faq_id, json.dumps(data, ensure_ascii=False))
    except (RedisError, json.JSONDecodeError):
        logger.warning("切换 FAQ 缓存开关失败：%s", faq_id, exc_info=True)
        return False
    return True


async def _load_entries(redis: Redis) -> list[FaqEntry]:
    """读出所有启用中的条目。单条 JSON 损坏只跳过该条，不让整次匹配失败。"""

    try:
        payloads = await redis.hgetall(PUBLISHED_KEY)
    except RedisError:
        logger.warning("读取 FAQ 缓存失败，视为未命中", exc_info=True)
        return []

    entries: list[FaqEntry] = []
    for faq_id, payload in payloads.items():
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("FAQ 缓存条目损坏，跳过：%s", faq_id)
            continue
        if not data.get("enabled", True):
            continue
        entries.append(
            FaqEntry(
                faq_id=faq_id,
                question=data.get("q", ""),
                answer=data.get("a", ""),
                vector=None,
            )
        )
    return entries


async def _load_vectors(redis: Redis, faq_ids: Sequence[str]) -> list[str | None]:
    """批量取向量。逐条 GET 会产生 N 次往返，这里用 pipeline 压成一次。"""

    if not faq_ids:
        return []
    try:
        pipe = redis.pipeline()
        for faq_id in faq_ids:
            pipe.get(_emb_key(faq_id))
        result = await pipe.execute()
    except RedisError:
        logger.warning("读取 FAQ 向量失败，退化为纯文本匹配", exc_info=True)
        return [None] * len(faq_ids)
    return list(result)


def _match_text(entries: Sequence[FaqEntry], target: str) -> FaqHit | None:
    """先精确、再包含。包含匹配取"最贴近"的那条（占比最高），避免被长问句抢走。"""

    if not target:
        return None

    for entry in entries:
        if normalize(entry.question) == target:
            return FaqHit(
                faq_id=entry.faq_id,
                question=entry.question,
                answer=entry.answer,
                score=1.0,
                matched_by="exact",
            )

    best: tuple[float, FaqEntry] | None = None
    for entry in entries:
        other = normalize(entry.question)
        if len(target) < _CONTAINS_MIN_CHARS or len(other) < _CONTAINS_MIN_CHARS:
            continue
        shorter, longer = sorted((target, other), key=len)
        if shorter not in longer:
            continue
        ratio = len(shorter) / len(longer)
        if ratio < _CONTAINS_MIN_RATIO:
            continue
        if best is None or ratio > best[0]:
            best = (ratio, entry)

    if best is None:
        return None
    entry = best[1]
    return FaqHit(
        faq_id=entry.faq_id,
        question=entry.question,
        answer=entry.answer,
        score=best[0],
        matched_by="contains",
    )


async def match(
    redis: Redis,
    question: str,
    vector: Sequence[float] | None = None,
    *,
    threshold: float,
) -> FaqHit | None:
    """匹配 FAQ：规范化文本精确 / 包含优先，其次向量相似度 ≥ `threshold`。

    先文本后向量是有意的：文本命中是确定性的、零误判，而向量相似度是一个需要标定的连续
    判据（`faq_sim_threshold`，TECH_SPEC §1 要求换模型后重新标定）。能用文本判定就别动用
    阈值。

    `threshold` 必须由调用方从系统配置读出传入——引擎层不碰配置表，也不该猜默认值。

    `vector` 为 None 时只做文本匹配；Redis 不可用或没有向量时同样退化为文本匹配，而不是抛错。
    """

    entries = await _load_entries(redis)
    if not entries:
        return None

    hit = _match_text(entries, normalize(question))
    if hit is not None:
        return hit

    if vector is None or len(vector) == 0:
        return None

    raw_vectors = await _load_vectors(redis, [entry.faq_id for entry in entries])

    best_entry: FaqEntry | None = None
    best_score = -1.0
    for entry, raw in zip(entries, raw_vectors):
        if raw is None:
            continue
        try:
            stored = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("FAQ 向量损坏，跳过：%s", entry.faq_id)
            continue
        score = _cosine(vector, stored)
        if score > best_score:
            best_entry, best_score = entry, score

    if best_entry is None or best_score < threshold:
        return None

    return FaqHit(
        faq_id=best_entry.faq_id,
        question=best_entry.question,
        answer=best_entry.answer,
        score=best_score,
        matched_by="vector",
    )
