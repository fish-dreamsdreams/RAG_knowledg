"""FAQ 挖掘：把反复被问、又没命中 FAQ 的问题归并成候选（tasklist 11.4）。

输入取自 `qa_audit_logs` 中 `faq_hit=false` 且 `answer_status='answered'` 的问题——被问过
且系统当时答得出来，说明语料里有答案，只是没被沉淀成一条固定问答。

**幂等是硬要求**：Beat 每日触发，同一批问题会被反复扫到。判重按代表问句的归一化文本，命中
已有待审候选时只更新频次与同义问法，不新建第二条——否则一周之后候选列表会被同一问题刷屏，
审核者要不了几天就会开始无脑点通过。

判重还要覆盖**已定论的问句**（`faq_candidates` 里已发布/已驳回的行、`faqs` 表里全部行含已下线）：
只看待审候选时，一条已发布的 FAQ 会被日志重新推成新候选，再发布一次就得到两条同问句的 FAQ
（演示库里真出现过 5 行 / 4 个问句）。已驳回的也一样：驳回是审核决定，第二天又推回同一条，审核者
只会开始无视列表。代价是「驳回后永久不再提议」——若日后需要「驳回后允许重新提议」，应加一个
时间窗口，而不是拿掉这条判重。

`source_unit_ids` 与 `suggested_answer` 留空：前者需要知道哪个单元真正回答了该问题（未命中
FAQ 的问题并不直接携带这个信息，硬填会误导审核），后者需要生成模型，属审核环节的产物。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.common.config import settings
from app.engines.faq_cache import normalize
from app.models import ModelConfig
from app.repositories import faq as faq_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cluster:
    """一簇语义相近的问题。`representative` 是离簇心最近的那条，最像"标准问法"。"""

    member_indices: list[int]
    representative: str
    confidence: float


@dataclass(frozen=True)
class MiningResult:
    scanned: int
    clusters: int
    created: int
    updated: int
    skipped_settled: int
    below_min_freq: int


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = norm_left = norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


def _mean_vector(vectors: Sequence[Sequence[float]]) -> list[float]:
    if not vectors:
        return []
    width = len(vectors[0])
    totals = [0.0] * width
    for vector in vectors:
        for index, value in enumerate(vector):
            totals[index] += value
    return [value / len(vectors) for value in totals]


def cluster_questions(
    questions: Sequence[str],
    vectors: Sequence[Sequence[float]],
    *,
    threshold: float,
) -> list[Cluster]:
    """贪心聚类：顺序扫描，与某个簇心余弦 ≥ `threshold` 就并入，否则新建簇。

    不用 KMeans 是因为这里没有"簇数"先验——谁也不知道这周的问题会分成几类，而贪心只需要
    一个可解释的相似度阈值。代价是对输入顺序敏感，同一类可能被拆成两簇；后果只是候选多出
    一条，审核时合并即可，比"每次跑出不同结果"可接受得多。

    归入时就地更新簇心（增量平均），使簇心逐步逼近簇的真实中心，代表问句因此更稳。
    """

    members: list[list[int]] = []
    centroids: list[list[float]] = []

    for index, vector in enumerate(vectors):
        best_cluster = -1
        best_score = -1.0
        for slot, centroid in enumerate(centroids):
            score = _cosine(vector, centroid)
            if score > best_score:
                best_cluster, best_score = slot, score

        if best_cluster >= 0 and best_score >= threshold:
            group = members[best_cluster]
            group.append(index)
            centroids[best_cluster] = _mean_vector([vectors[i] for i in group])
        else:
            members.append([index])
            centroids.append(list(vector))

    clusters: list[Cluster] = []
    for group, centroid in zip(members, centroids):
        distances = [(_cosine(vectors[i], centroid), i) for i in group]
        best_score, best_index = max(distances)
        clusters.append(
            Cluster(
                member_indices=list(group),
                representative=questions[best_index],
                confidence=round(best_score, 4),
            )
        )
    return clusters


def _encode_many(questions: Sequence[str]) -> list[list[float]]:
    # 延迟导入：本模块会被 API 进程经 `engines/celery/mine.py` 间接导入，在模块顶部导入
    # embed 会把 torch 与模型依赖带进 API 进程（TECH_SPEC §8.0）
    from app.engines.embed import get_embedder

    if not questions:
        return []
    embedding = get_embedder().encode(list(questions), return_sparse=False)
    return [[float(value) for value in row] for row in embedding.dense]


async def _min_freq(session: AsyncSession) -> int:
    """频次门槛取系统配置（TECH_SPEC §4.5 允许系统配置覆盖 `faq_cluster_min_freq`）。"""

    row = (await session.execute(select(ModelConfig).limit(1))).scalar_one_or_none()
    return row.faq_cluster_min_freq if row is not None else 3


async def _settled_questions(session: AsyncSession) -> set[str]:
    """已定论问句的归一化集合：FAQ 全体（含已下线）+ 已发布 / 已驳回的候选。

    归一化口径与命中判定同一套（`faq_cache.normalize`），否则「年假可以休几天？」与
    「年假可以休几天」会被当成两个问题，判重就白做了。
    """

    settled = {normalize(row.question) for row in await faq_repo.all_faqs(session)}
    settled |= {
        normalize(row.representative_question)
        for row in await faq_repo.candidates_by_status(
            session, (faq_repo.CANDIDATE_PUBLISHED, faq_repo.CANDIDATE_REJECTED)
        )
    }
    return settled


async def run_mining(
    session: AsyncSession, *, limit: int = 200, redis: Redis | None = None
) -> MiningResult:
    """扫描 → 编码 → 归并 → 写候选。`redis` 参数暂无用途，保留以对齐服务层签名。"""

    del redis  # 挖掘只写 PostgreSQL；缓存由发布动作同步

    min_freq = await _min_freq(session)
    rows = await faq_repo.recent_unanswered_questions(session, limit=limit)
    if not rows:
        return MiningResult(
            scanned=0, clusters=0, created=0, updated=0, skipped_settled=0, below_min_freq=0
        )

    questions = [row.question for row in rows]
    vectors = await run_in_threadpool(_encode_many, questions)
    clusters = cluster_questions(
        questions, vectors, threshold=settings.faq_cluster_sim
    )

    existing = {
        normalize(row.representative_question): row
        for row in await faq_repo.pending_candidates(session)
    }
    settled = await _settled_questions(session)

    created = updated = below = skipped = 0
    for cluster in clusters:
        if len(cluster.member_indices) < min_freq:
            below += 1
            continue

        key = normalize(cluster.representative)
        others = [
            questions[index]
            for index in cluster.member_indices
            if questions[index] != cluster.representative
        ]

        current = existing.get(key)
        if current is not None:
            current.freq = len(cluster.member_indices)
            current.similar_questions = others
            current.confidence = cluster.confidence
            updated += 1
            continue

        if key in settled:
            skipped += 1
            continue

        candidate = await faq_repo.create_candidate(
            session,
            representative_question=cluster.representative,
            similar_questions=others,
            freq=len(cluster.member_indices),
            confidence=cluster.confidence,
        )
        existing[key] = candidate
        created += 1

    await session.commit()
    logger.info(
        "FAQ 挖掘完成：扫描 %s 条，%s 簇，新建 %s，更新 %s，已定论跳过 %s，未达频次 %s",
        len(rows),
        len(clusters),
        created,
        updated,
        skipped,
        below,
    )
    return MiningResult(
        scanned=len(rows),
        clusters=len(clusters),
        created=created,
        updated=updated,
        skipped_settled=skipped,
        below_min_freq=below,
    )
