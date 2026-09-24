"""演示库清场（tasklist 18）的报告与删除口径。

真库用例，自建自清：问句都带随机后缀，只碰自己建的行。**不拿整份报告调 `apply_report`**——
那会把演示库里其他待清行一起删掉；验证 apply 的地方用手工构造的 `BlockReport`，只指向自己的行。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.common.db import SessionLocal
from app.models import Faq, FaqCandidate, KnowledgeGap
from app.models.chat import QaAuditLog
from app.repositories import faq as faq_repo
from app.services import demo_logs, demo_state
from app.services import faq as faq_service

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


@pytest.fixture
async def audit_rows():
    ids: list[UUID] = []
    yield ids
    async with SessionLocal() as session:
        await session.execute(delete(QaAuditLog).where(QaAuditLog.id.in_(ids)))
        await session.commit()


@pytest.fixture
async def gaps():
    ids: list[UUID] = []
    yield ids
    async with SessionLocal() as session:
        await session.execute(delete(KnowledgeGap).where(KnowledgeGap.id.in_(ids)))
        await session.commit()


@pytest.fixture
async def candidates():
    ids: list[UUID] = []
    yield ids
    async with SessionLocal() as session:
        await session.execute(delete(FaqCandidate).where(FaqCandidate.id.in_(ids)))
        await session.commit()


@pytest.fixture
async def faqs():
    ids: list[UUID] = []
    yield ids
    async with SessionLocal() as session:
        await session.execute(delete(Faq).where(Faq.id.in_(ids)))
        await session.commit()


# ---------- 建行 ----------


async def _add_audit_row(trace_id: str, question: str) -> UUID:
    row = QaAuditLog(
        id=uuid4(),
        trace_id=trace_id,
        question=question,
        faq_hit=False,
        answer_status="answered",
    )
    async with SessionLocal() as session:
        session.add(row)
        await session.commit()
    return row.id


async def _add_gap(question: str) -> UUID:
    row = KnowledgeGap(id=uuid4(), question_text=question, freq=1, status="open")
    async with SessionLocal() as session:
        session.add(row)
        await session.commit()
    return row.id


async def _add_candidate(question: str, *, status: str, created_at: datetime) -> UUID:
    row = FaqCandidate(
        id=uuid4(),
        representative_question=question,
        similar_questions=[question],
        freq=3,
        status=status,
        created_at=created_at,
    )
    async with SessionLocal() as session:
        session.add(row)
        await session.commit()
    return row.id


async def _add_faq(
    question: str, *, created_at: datetime, source_candidate_id: UUID | None = None
) -> UUID:
    row = Faq(
        id=uuid4(),
        question=question,
        answer=f"{question} 的答复",
        status=faq_repo.FAQ_PUBLISHED,
        cache_enabled=True,
        source_candidate_id=source_candidate_id,
        created_at=created_at,
    )
    async with SessionLocal() as session:
        session.add(row)
        await session.commit()
    return row.id


async def _doomed_ids(session, block: str) -> set[str]:
    report = (await demo_state.build_report(session, blocks=(block,)))[0]
    return {row["id"] for row in report.doomed}


# ---------- 审计行 ----------


async def test_audit_block_keeps_seed_prefix_rows(audit_rows) -> None:
    """种子行（`demo-` 前缀）不算痕迹；其余进待删清单。"""

    seed = await _add_audit_row(f"{demo_logs.DEMO_TRACE_PREFIX}{uuid4().hex}", "清场测试-种子行")
    junk = await _add_audit_row(uuid4().hex, "清场测试-验收残留")
    audit_rows.extend([seed, junk])

    async with SessionLocal() as session:
        doomed = await _doomed_ids(session, demo_state.AUDIT)
        report = (await demo_state.build_report(session, blocks=(demo_state.AUDIT,)))[0]

    assert str(junk) in doomed
    assert str(seed) not in doomed
    assert report.kept >= 1, "带前缀的行应计入保留数"


async def test_apply_deletes_only_the_rows_in_the_report(audit_rows) -> None:
    """`apply_report` 只动报告里点名的行：手工构造报告，删一条、留一条。"""

    seed = await _add_audit_row(f"{demo_logs.DEMO_TRACE_PREFIX}{uuid4().hex}", "清场测试-种子行2")
    junk = await _add_audit_row(uuid4().hex, "清场测试-待删行")
    audit_rows.extend([seed, junk])

    scoped = demo_state.BlockReport(
        block=demo_state.AUDIT, doomed=[{"id": str(junk)}], kept=1
    )
    async with SessionLocal() as session:
        deleted = await demo_state.apply_report(session, [scoped])
        left = (await session.execute(select(QaAuditLog.id))).scalars().all()

    assert deleted == {demo_state.AUDIT: 1}
    assert junk not in left
    assert seed in left


# ---------- 知识缺口 ----------


async def test_gaps_block_keeps_whitelist_questions(gaps) -> None:
    """白名单（种子缺口簇）里的问句留下；白名单外的进待删清单并真的被删。"""

    whitelisted = demo_logs.demo_gap_questions()[0]
    mine_kept = await _add_gap(whitelisted)
    junk = await _add_gap(f"清场测试-缺口-{uuid4().hex[:8]}")
    gaps.extend([mine_kept, junk])

    async with SessionLocal() as session:
        doomed = await _doomed_ids(session, demo_state.GAPS)

    assert str(junk) in doomed
    assert str(mine_kept) not in doomed

    scoped = demo_state.BlockReport(
        block=demo_state.GAPS, doomed=[{"id": str(junk)}], kept=0
    )
    async with SessionLocal() as session:
        deleted = await demo_state.apply_report(session, [scoped])
        left = (await session.execute(select(KnowledgeGap.id))).scalars().all()

    assert deleted == {demo_state.GAPS: 1}
    assert junk not in left
    assert mine_kept in left


# ---------- 保留口径 ----------


async def test_duplicate_candidates_keep_the_published_row(candidates) -> None:
    """重复候选保留 **published** 那条，而不是单纯按时间最早：它是 FAQ 的来源记录。"""

    question = f"清场测试候选-{uuid4().hex[:8]}是否可以补开"
    published = await _add_candidate(
        question, status=faq_repo.CANDIDATE_PUBLISHED, created_at=NOW - timedelta(minutes=1)
    )
    pending = await _add_candidate(
        question, status=faq_repo.CANDIDATE_PENDING, created_at=NOW
    )
    candidates.extend([published, pending])

    async with SessionLocal() as session:
        doomed = await _doomed_ids(session, demo_state.CANDIDATES)

    assert str(pending) in doomed
    assert str(published) not in doomed


async def test_deleting_a_candidate_keeps_its_faq_and_clears_the_reference(
    candidates, faqs
) -> None:
    """删候选前把 FAQ 的 `source_candidate_id` 置空：真库外键是 NO ACTION，不置空会报错。"""

    question = f"清场测试候选-{uuid4().hex[:8]}怎么报销"
    candidate = await _add_candidate(
        question, status=faq_repo.CANDIDATE_PUBLISHED, created_at=NOW - timedelta(minutes=1)
    )
    faq = await _add_faq(question, created_at=NOW, source_candidate_id=candidate)
    candidates.append(candidate)
    faqs.append(faq)

    async with SessionLocal() as session:
        await faq_service.delete_candidate(session, candidate)
        row = (await session.execute(select(Faq).where(Faq.id == faq))).scalar_one()

    assert row.source_candidate_id is None
    assert row.question == question


async def test_duplicate_faqs_keep_the_earliest(faqs) -> None:
    """重复 FAQ 保留 `created_at` 最早那条（与 `scripts/clean_duplicate_faqs.py` 同一实现）。"""

    question = f"清场测试FAQ-{uuid4().hex[:8]}几天内可退"
    early = await _add_faq(question, created_at=NOW - timedelta(hours=1))
    later = await _add_faq(question, created_at=NOW)
    faqs.extend([early, later])

    async with SessionLocal() as session:
        doomed = await _doomed_ids(session, demo_state.FAQS)
        groups = [
            group
            for group in await faq_service.find_duplicate_faqs(session)
            if group.keep.question == question
        ]

    assert str(later) in doomed
    assert str(early) not in doomed
    assert len(groups) == 1
    assert [item.faq_id for item in groups[0].drop] == [later]
