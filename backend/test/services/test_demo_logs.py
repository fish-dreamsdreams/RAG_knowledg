"""场景 B 演示日志的数据一致性测试（任务 15.2 数据部分）。

需要 PostgreSQL：`docker compose up -d postgres` 后跑 `pytest -m integration -k demo_logs`。
测试会调用加载器（幂等），因此会把演示数据留在开发库里 —— 这正是演示所需的状态。
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeGap, QaAuditLog, User
from app.services.demo_logs import DEMO_TRACE_PREFIX, _load, run_demo_logs

pytestmark = pytest.mark.integration


async def _cs01(session: AsyncSession) -> User:
    return (
        await session.execute(select(User).where(User.username == "cs01"))
    ).scalar_one()


async def test_volume_satisfies_prd(session: AsyncSession) -> None:
    """PRD §3.1 要求场景 B 至少 50 条相似客服日志。"""

    summary = await run_demo_logs()

    assert summary["answered_logs"] >= 50, "退款类相似日志不足 50 条"
    assert summary["gap_logs"] >= 10, "缺口问法过少，缺口列表看不出频次"
    assert summary["gaps"] == len(_load()["gap_clusters"])


async def test_loader_is_idempotent(session: AsyncSession) -> None:
    first = await run_demo_logs()
    second = await run_demo_logs()

    assert first == second
    total = (
        await session.execute(
            select(func.count())
            .select_from(QaAuditLog)
            .where(QaAuditLog.trace_id.like(f"{DEMO_TRACE_PREFIX}%"))
        )
    ).scalar_one()
    assert total == first["total_logs"], "重复加载产生了重复审计行"


async def test_every_cluster_is_large_enough_for_mining(session: AsyncSession) -> None:
    """任务 11.4 的归并输入：每簇条数必须 ≥ faq_cluster_min_freq，否则挖不出候选。"""

    data = _load()
    min_freq = data["_thresholds"]["faq_cluster_min_freq"]
    await run_demo_logs()

    for cluster in data["answered_clusters"]:
        assert len(cluster["questions"]) >= min_freq, f"簇 {cluster['key']} 条数不足"
        rows = (
            await session.execute(
                select(QaAuditLog).where(
                    QaAuditLog.question.in_(cluster["questions"]),
                    # 只数演示行：这张表同时装着真实问答的审计（看板就读它），
                    # 真人问过同一句话就会把条数顶多，与「演示数据能不能挖」无关
                    QaAuditLog.trace_id.like(f"{DEMO_TRACE_PREFIX}%"),
                )
            )
        ).scalars().all()
        assert len(rows) == len(cluster["questions"])
        for row in rows:
            assert row.faq_hit is False, "已走 FAQ 的问题不该进入挖掘输入"
            assert row.answer_status == "answered"


async def test_similarity_sits_between_the_two_thresholds(session: AsyncSession) -> None:
    """演示数据的自洽性：answered 落在 [gap, faq) 之间，gap 必须低于 gap 阈值。

    只统计**演示行**（`trace_id` 前缀）：这张表同时装着真实问答的审计（看板就读它），
    真跑过的提问会把最大相似度拉上去，不按来源过滤就会莫名其妙地红。
    """

    data = _load()
    gap_threshold = data["_thresholds"]["gap_sim_threshold"]
    faq_threshold = data["_thresholds"]["faq_sim_threshold"]
    await run_demo_logs()
    demo_only = QaAuditLog.trace_id.like(f"{DEMO_TRACE_PREFIX}%")

    answered = (
        await session.execute(
            select(func.min(QaAuditLog.max_similarity), func.max(QaAuditLog.max_similarity)).where(
                QaAuditLog.answer_status == "answered", demo_only
            )
        )
    ).one()
    assert gap_threshold <= answered[0] and answered[1] < faq_threshold, answered

    gaps = (
        await session.execute(
            select(func.max(QaAuditLog.max_similarity)).where(
                QaAuditLog.answer_status == "gap", demo_only
            )
        )
    ).scalar_one()
    assert gaps < gap_threshold, f"缺口最高相似 {gaps} 不应达到 gap 阈值 {gap_threshold}"


async def test_gaps_are_aggregated_not_per_question(session: AsyncSession) -> None:
    data = _load()
    await run_demo_logs()
    cs01 = await _cs01(session)

    for cluster in data["gap_clusters"]:
        gap = (
            await session.execute(
                select(KnowledgeGap).where(
                    KnowledgeGap.question_text == cluster["representative"]
                )
            )
        ).scalar_one()
        assert gap.freq == len(cluster["questions"]), "频次应等于同簇问法条数"
        assert gap.status == "open"
        assert gap.department_id == cs01.department_id, "缺口应归属提问人部门"
        assert gap.last_asked_at is not None
        assert gap.filled_unit_id is None

    representatives = [cluster["representative"] for cluster in data["gap_clusters"]]
    count = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeGap)
            .where(KnowledgeGap.question_text.in_(representatives))
        )
    ).scalar_one()
    assert count == len(representatives), "缺口被逐条问题拆成了多行"
