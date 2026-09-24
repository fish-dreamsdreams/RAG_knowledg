"""FAQ 审核、发布与维护接口测试（tasklist 11.2 / 11.3）。

需要 PostgreSQL + Redis（发布要写行并同步缓存）。用例自建自清：问句带随机后缀，缓存条目
只删自己建的那些，不动演示数据。

编码被替换成假向量：`_encode_many` 会加载 BGE-M3，测试没必要为「缓存是否同步」付出加载权重
的代价。向量本身的行为在 `test_embedder_utils.py` 与集成测试里覆盖。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.common.db import SessionLocal
from app.common.errors import AppError
from app.common.redis import get_redis
from app.engines import faq_cache
from app.models import Faq, FaqCandidate, KnowledgeGap
from app.models.chat import QaAuditLog
from app.repositories import faq as faq_repo
from app.services import faq as faq_service
from app.services import faq_mining
from app.services.faq_mining import run_mining

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "Demo@123456"
DIM = 4


async def login_headers(client: AsyncClient, username: str = "admin") -> dict[str, str]:
    """发布要写 `faqs.created_by`（外键指向 users），必须走真实登录，不能用假身份。"""

    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def fake_vectors(monkeypatch) -> None:
    monkeypatch.setattr(
        faq_service,
        "_encode_many",
        lambda questions: [[0.1] * DIM for _ in questions],
    )


@pytest.fixture
async def candidates():
    """登记用例创建的候选，结束时删掉。"""

    ids: list[UUID] = []
    yield ids
    async with SessionLocal() as session:
        await session.execute(delete(FaqCandidate).where(FaqCandidate.id.in_(ids)))
        await session.commit()


@pytest.fixture
async def faqs():
    """登记用例创建的 FAQ，结束时连缓存条目一起清掉。"""

    ids: list[UUID] = []
    redis = get_redis()
    yield ids
    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(Faq).where(Faq.id.in_(ids)))).scalars().all()
        )
        await session.execute(delete(Faq).where(Faq.id.in_(ids)))
        await session.commit()
    for row in rows:
        await faq_cache.remove(redis, str(row.id))


async def make_candidate(
    *,
    freq: int = 1,
    suggested_answer: str | None = "满 1 年不满 10 年 5 天。",
) -> UUID:
    question = f"测试FAQ-{uuid4().hex[:8]}年假几天"
    async with SessionLocal() as session:
        candidate = FaqCandidate(
            representative_question=question,
            similar_questions=[question],
            freq=freq,
            suggested_answer=suggested_answer,
        )
        session.add(candidate)
        await session.commit()
        return candidate.id


# ---------- 候选列表 ----------


async def test_list_candidates_orders_by_freq_desc(
    client: AsyncClient, candidates
) -> None:
    low = await make_candidate(freq=1)
    high = await make_candidate(freq=9)
    candidates.extend([low, high])

    response = await client.get(
        "/api/v1/faq/candidates",
        params={"status": "pending"},
        headers=await login_headers(client),
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    ours = [item for item in items if UUID(item["candidate_id"]) in {low, high}]
    assert [UUID(item["candidate_id"]) for item in ours] == [high, low]


async def test_list_candidates_requires_review_permission(
    client: AsyncClient, fake_identity
) -> None:
    fake_identity(permissions=[])

    response = await client.get("/api/v1/faq/candidates")

    assert response.status_code == 403


# ---------- 发布 ----------


async def test_publish_candidate_writes_faq_and_cache(
    client: AsyncClient, candidates, faqs
) -> None:
    candidate_id = await make_candidate()
    candidates.append(candidate_id)
    headers = await login_headers(client)

    response = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/publish", headers=headers, json={}
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    faq_id = UUID(data["faq_id"])
    faqs.append(faq_id)
    assert data["answer"] == "满 1 年不满 10 年 5 天。"
    assert data["status"] == "published"
    assert UUID(data["source_candidate_id"]) == candidate_id

    # 缓存必须同步：Hash 有这条，且向量键已写入
    redis = get_redis()
    assert await redis.hget(faq_cache.PUBLISHED_KEY, str(faq_id)) is not None
    assert await redis.get(f"kb:faq:emb:{faq_id}") is not None

    # 候选被关闭
    async with SessionLocal() as session:
        row = await session.get(FaqCandidate, candidate_id)
        assert row is not None
        assert row.status == "published"
        assert row.reviewed_by is not None


async def test_publish_uses_explicit_answer_over_suggestion(
    client: AsyncClient, candidates, faqs
) -> None:
    candidate_id = await make_candidate()
    candidates.append(candidate_id)

    response = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/publish",
        headers=await login_headers(client),
        json={"answer": "审核后改写的答案。"},
    )

    assert response.status_code == 200
    faqs.append(UUID(response.json()["data"]["faq_id"]))
    assert response.json()["data"]["answer"] == "审核后改写的答案。"


async def test_publish_without_any_answer_is_rejected(
    client: AsyncClient, candidates
) -> None:
    """没有答案的 FAQ 会命中却答不出内容，比不命中更糟。"""

    candidate_id = await make_candidate(suggested_answer=None)
    candidates.append(candidate_id)

    response = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/publish",
        headers=await login_headers(client),
        json={},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "SYS_VALIDATION"


async def test_publish_twice_conflicts(
    client: AsyncClient, candidates, faqs
) -> None:
    candidate_id = await make_candidate()
    candidates.append(candidate_id)
    headers = await login_headers(client)

    first = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/publish", headers=headers, json={}
    )
    faqs.append(UUID(first.json()["data"]["faq_id"]))

    second = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/publish", headers=headers, json={}
    )

    assert second.status_code == 409
    assert second.json()["code"] == "FAQ_CANDIDATE_CLOSED"


async def test_publish_missing_candidate_is_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/api/v1/faq/candidates/{uuid4()}/publish",
        headers=await login_headers(client),
        json={},
    )

    assert response.status_code == 404


# ---------- 驳回 ----------


async def test_reject_marks_candidate_and_requires_reason(
    client: AsyncClient, candidates
) -> None:
    candidate_id = await make_candidate()
    candidates.append(candidate_id)
    headers = await login_headers(client)

    missing = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/reject", headers=headers, json={}
    )
    assert missing.status_code == 400

    response = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/reject",
        headers=headers,
        json={"reason": "已有制度文件覆盖，不必单独成条"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "rejected"
    assert response.json()["data"]["reject_reason"] == "已有制度文件覆盖，不必单独成条"


async def test_rejected_candidate_cannot_be_published(
    client: AsyncClient, candidates
) -> None:
    candidate_id = await make_candidate()
    candidates.append(candidate_id)
    headers = await login_headers(client)

    await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/reject",
        headers=headers,
        json={"reason": "重复"},
    )
    response = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/publish", headers=headers, json={}
    )

    assert response.status_code == 409


# ---------- FAQ 维护 ----------


@pytest.fixture
async def published_faq(client: AsyncClient, candidates, faqs) -> UUID:
    candidate_id = await make_candidate()
    candidates.append(candidate_id)
    response = await client.post(
        f"/api/v1/faq/candidates/{candidate_id}/publish",
        headers=await login_headers(client),
        json={},
    )
    faq_id = UUID(response.json()["data"]["faq_id"])
    faqs.append(faq_id)
    return faq_id


async def test_update_answer_syncs_cache(
    client: AsyncClient, published_faq: UUID
) -> None:
    response = await client.put(
        f"/api/v1/faqs/{published_faq}",
        headers=await login_headers(client),
        json={"answer": "修订后的答案：满 1 年为 5 天。"},
    )

    assert response.status_code == 200
    raw = await get_redis().hget(faq_cache.PUBLISHED_KEY, str(published_faq))
    assert raw is not None
    assert "修订后的答案" in raw


async def test_disable_cache_keeps_entry_but_stops_matching(
    client: AsyncClient, published_faq: UUID
) -> None:
    """切 `cache_enabled=false` 后条目仍在缓存里，但不再参与匹配（无需重建）。"""

    response = await client.put(
        f"/api/v1/faqs/{published_faq}",
        headers=await login_headers(client),
        json={"cache_enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["data"]["cache_enabled"] is False

    async with SessionLocal() as session:
        row = await session.get(Faq, published_faq)
        assert row is not None
        question = row.question

    hit = await faq_cache.match(get_redis(), question, [0.1] * DIM, threshold=0.0)
    assert hit is None or hit.faq_id != str(published_faq)


async def test_offline_faq_is_removed_from_cache(
    client: AsyncClient, published_faq: UUID
) -> None:
    response = await client.put(
        f"/api/v1/faqs/{published_faq}",
        headers=await login_headers(client),
        json={"status": "offline"},
    )

    assert response.status_code == 200
    redis = get_redis()
    assert await redis.hget(faq_cache.PUBLISHED_KEY, str(published_faq)) is None
    assert await redis.get(f"kb:faq:emb:{published_faq}") is None


async def test_list_faqs_filters_by_keyword(
    client: AsyncClient, published_faq: UUID
) -> None:
    async with SessionLocal() as session:
        row = await session.get(Faq, published_faq)
        assert row is not None
        keyword = row.question

    response = await client.get(
        "/api/v1/faqs",
        params={"keyword": keyword},
        headers=await login_headers(client),
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [UUID(item["faq_id"]) for item in items] == [published_faq]


async def test_update_missing_faq_is_404(client: AsyncClient) -> None:
    response = await client.put(
        f"/api/v1/faqs/{uuid4()}",
        headers=await login_headers(client),
        json={"answer": "x"},
    )

    assert response.status_code == 404


# ---------- 缓存重建 ----------


async def test_rebuild_cache_projects_published_faqs(
    client: AsyncClient, published_faq: UUID
) -> None:
    async with SessionLocal() as session:
        written = await faq_service.rebuild_cache(session)

    redis = get_redis()
    assert written >= 1
    assert await redis.hget(faq_cache.PUBLISHED_KEY, str(published_faq)) is not None


async def test_rebuild_cache_excludes_offline_faqs(
    client: AsyncClient, published_faq: UUID
) -> None:
    await client.put(
        f"/api/v1/faqs/{published_faq}",
        headers=await login_headers(client),
        json={"status": "offline"},
    )

    async with SessionLocal() as session:
        await faq_service.rebuild_cache(session)

    assert await get_redis().hget(faq_cache.PUBLISHED_KEY, str(published_faq)) is None


async def test_delete_faq_removes_row_and_closes_cache(published_faq: UUID) -> None:
    """删除必须连缓存一起清：只删库的话，被删的问句还能命中并答出内容。"""

    async with SessionLocal() as session:
        row = await session.get(Faq, published_faq)
        assert row is not None
        question = row.question
        await faq_service.delete_faq(session, published_faq)

    async with SessionLocal() as session:
        assert await session.get(Faq, published_faq) is None

    redis = get_redis()
    assert await redis.hget(faq_cache.PUBLISHED_KEY, str(published_faq)) is None
    assert await redis.get(f"kb:faq:emb:{published_faq}") is None

    # 阈值放到 0，排除相似度因素：它已经不可能再被命中了
    hit = await faq_cache.match(redis, question, [0.1] * DIM, threshold=0.0)
    assert hit is None or hit.faq_id != str(published_faq)


async def test_delete_missing_faq_is_not_found() -> None:
    async with SessionLocal() as session:
        with pytest.raises(AppError) as excinfo:
            await faq_service.delete_faq(session, uuid4())

    assert excinfo.value.http_status == 404
    assert excinfo.value.code == "KB_NOT_FOUND"


async def test_mining_skips_question_already_published_as_faq(
    published_faq: UUID, monkeypatch
) -> None:
    """已发布的问句不再被挖成候选。

    只比对「待审候选」时，一条已发布 FAQ 的问句会被日志反复推成新候选，再发布一次就得到
    两条同问句的 FAQ——演示库里真长出过 5 行 / 4 个问句。这里让挖掘真的看到 3 次同样提问
    （达到频次门槛），断言它不再为这个问句建第二条。
    """

    async with SessionLocal() as session:
        row = await session.get(Faq, published_faq)
        assert row is not None
        question = row.question

    trace = f"mine-dup-{uuid4().hex[:8]}"
    async with SessionLocal() as session:
        session.add_all(
            [
                QaAuditLog(
                    trace_id=trace,
                    question=question,
                    faq_hit=False,
                    answer_status="answered",
                )
                for _ in range(3)
            ]
        )
        await session.commit()

    # 假编码要让**目标问句自己成一簇**（三条同一问句 ✓），其余问句各占一维：本用例跑的是真库，
    # 把它们合成一簇会真的往演示库里写一条无关候选（第一版就写过一条 freq=47 的）。
    # 各占一维 ⇒ 其余问句都成单元素簇 ⇒ 达不到频次门槛 ⇒ 什么也不会写。
    def _fake_encode(items):
        width = len(items) + 1
        vectors = []
        for index, text in enumerate(items):
            vector = [0.0] * width
            vector[0 if text == question else index + 1] = 1.0
            vectors.append(vector)
        return vectors

    monkeypatch.setattr(faq_mining, "_encode_many", _fake_encode)

    async def _min_freq(_session) -> int:
        return 3

    monkeypatch.setattr(faq_mining, "_min_freq", _min_freq)

    try:
        async with SessionLocal() as session:
            result = await run_mining(session, limit=50)
            pending = await faq_repo.candidates_by_status(
                session, (faq_repo.CANDIDATE_PENDING,)
            )
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(QaAuditLog).where(QaAuditLog.trace_id == trace))
            await session.commit()

    assert result.skipped_settled >= 1
    # 其余问句都成了单元素簇，所以整轮不该新建任何候选（也保证本用例不留脏数据）
    assert result.created == 0
    key = faq_cache.normalize(question)
    assert [
        item.representative_question
        for item in pending
        if faq_cache.normalize(item.representative_question) == key
    ] == []


# ---------- 挖掘 ----------


async def test_trigger_mine_dispatches_task(
    client: AsyncClient, monkeypatch
) -> None:
    """接口只负责投递：一次挖掘要编码上百条问句，不能在请求线程里跑。"""

    from app.engines.celery import mine as mine_module

    dispatched: list[str] = []

    class _Task:
        id = "task-123"

    def _delay() -> _Task:
        dispatched.append("called")
        return _Task()

    monkeypatch.setattr(mine_module.mine_candidates, "delay", _delay)

    response = await client.post("/api/v1/faq/mine", headers=await login_headers(client))

    assert response.status_code == 200
    assert response.json()["data"]["task_id"] == "task-123"
    assert dispatched == ["called"]


async def test_trigger_mine_requires_review_permission(
    client: AsyncClient, fake_identity
) -> None:
    fake_identity(permissions=[])

    response = await client.post("/api/v1/faq/mine")

    assert response.status_code == 403


async def test_mining_input_excludes_hits_and_gaps() -> None:
    """挖掘只看「答出来了但没命中 FAQ」的问题。

    `gap` 属于知识缺口（缺文档，补文档才是正解），`denied` 是权限问题，命中 FAQ 的早已
    沉淀过。把这三类混进来会掩盖真正该补的东西。
    """

    marker = uuid4().hex[:8]
    trace = f"mine-{marker}"
    wanted = f"{marker}-答出来但没命中"

    async with SessionLocal() as session:
        session.add_all(
            [
                QaAuditLog(
                    trace_id=trace,
                    question=wanted,
                    faq_hit=False,
                    answer_status="answered",
                ),
                QaAuditLog(
                    trace_id=trace,
                    question=f"{marker}-命中FAQ",
                    faq_hit=True,
                    answer_status="answered",
                ),
                QaAuditLog(
                    trace_id=trace,
                    question=f"{marker}-知识缺口",
                    faq_hit=False,
                    answer_status="gap",
                ),
                QaAuditLog(
                    trace_id=trace,
                    question=f"{marker}-权限拒绝",
                    faq_hit=False,
                    answer_status="denied",
                ),
            ]
        )
        await session.commit()

    try:
        async with SessionLocal() as session:
            rows = await faq_repo.recent_unanswered_questions(session, limit=50)

        assert [row.question for row in rows if marker in row.question] == [wanted]
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(QaAuditLog).where(QaAuditLog.trace_id == trace)
            )
            await session.commit()


async def test_record_gap_aggregates_same_question_in_one_row() -> None:
    """重复提问必须累加 freq，而不是让缺口列表被同一问题刷屏。"""

    question = f"gap-{uuid4().hex}"
    try:
        async with SessionLocal() as session:
            first = await faq_repo.record_gap(
                session, question=question, department_id=None, max_similarity=-0.2
            )
            await session.commit()
            first_id = first.id

        async with SessionLocal() as session:
            second = await faq_repo.record_gap(
                session, question=question, department_id=None, max_similarity=0.12
            )
            await session.commit()
            second_id = second.id

        assert second_id == first_id
        async with SessionLocal() as session:
            row = await session.get(KnowledgeGap, first_id)
            assert row is not None
            assert row.freq == 2
            assert row.max_similarity == 0.12
            assert row.last_asked_at is not None
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(KnowledgeGap).where(KnowledgeGap.question_text == question)
            )
            await session.commit()
