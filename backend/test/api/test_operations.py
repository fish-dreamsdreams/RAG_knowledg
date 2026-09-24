"""知识缺口、审计查询与看板接口测试（tasklist 13.1 ~ 13.3）。

需要 PostgreSQL：缺口转建、审计筛选与看板聚合都直接读库。用例自建自清——问句带随机后缀、
审计行记录 id 后逐个删除，不动 15.2 预置的演示数据（缺口池里的 3 条演示缺口、demo_logs 的审计行）。

看板聚合的时间窗用真实 `resolve_range` 的话，断言就得跟演示数据纠缠（"pv 至少 N"这种弱断言）。
这里 monkeypatch 成一段只含本用例数据的时间窗，把 PV/UV/命中率/覆盖率算得对错真正锁住。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text

from app.common.db import SessionLocal
from app.models import User
from app.models.chat import QaAuditLog
from app.models.faq import KnowledgeGap
from app.models.knowledge import KnowledgeUnit
from app.repositories import faq as faq_repo
from app.services import knowledge as knowledge_service
from app.services import operations as operations_service
from app.services.operations import RangeWindow

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "Demo@123456"


async def login_headers(client: AsyncClient, username: str = "admin") -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
async def gap_factory():
    """建知识缺口；问句带随机后缀，避免与演示缺口或断言的"最高频"打架。"""

    created: list[UUID] = []

    async def _make(
        *,
        question: str | None = None,
        freq: int = 1,
        status: str = faq_repo.GAP_OPEN,
        department_id: UUID | None = None,
        filled_unit_id: UUID | None = None,
    ) -> UUID:
        gap_id = uuid4()
        async with SessionLocal() as session:
            session.add(
                KnowledgeGap(
                    id=gap_id,
                    question_text=question or f"测试缺口 {uuid4().hex[:8]}",
                    department_id=department_id,
                    freq=freq,
                    max_similarity=0.21,
                    last_asked_at=datetime.now(UTC),
                    status=status,
                    filled_unit_id=filled_unit_id,
                )
            )
            await session.commit()
        created.append(gap_id)
        return gap_id

    yield _make

    async with SessionLocal() as session:
        await session.execute(delete(KnowledgeGap).where(KnowledgeGap.id.in_(created)))
        await session.commit()


@pytest.fixture
async def audit_factory():
    """建审计行，可指定时间与各口径字段。"""

    created: list[UUID] = []

    async def _make(
        *,
        created_at: datetime | None = None,
        user_id: UUID | None = None,
        question: str | None = None,
        faq_hit: bool = False,
        allowed_unit_ids: list[str] | None = None,
        citation_ids: list[str] | None = None,
        answer_status: str = "answered",
        latency_ms: int = 1000,
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
    ) -> UUID:
        audit_id = uuid4()
        async with SessionLocal() as session:
            session.add(
                QaAuditLog(
                    id=audit_id,
                    trace_id=uuid4().hex,
                    user_id=user_id,
                    session_id=uuid4(),
                    question=question or f"测试问题 {uuid4().hex[:8]}",
                    faq_hit=faq_hit,
                    allowed_unit_ids=allowed_unit_ids or [],
                    denied_count=0,
                    citation_ids=citation_ids or [],
                    answer_status=answer_status,
                    max_similarity=0.5,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                    created_at=created_at or datetime.now(UTC),
                )
            )
            await session.commit()
        created.append(audit_id)
        return audit_id

    yield _make

    async with SessionLocal() as session:
        await session.execute(delete(QaAuditLog).where(QaAuditLog.id.in_(created)))
        await session.commit()


@pytest.fixture
async def unit_factory():
    """建知识单元行（看板热门榜要按 citation 反查标题）。"""

    created: list[UUID] = []

    async def _make(title: str | None = None) -> UUID:
        unit_id = uuid4()
        async with SessionLocal() as session:
            session.add(
                KnowledgeUnit(
                    id=unit_id,
                    title=title or f"看板测试单元 {uuid4().hex[:8]}",
                    format="md",
                    source_path="",
                    source_filename="看板测试.md",
                    status="disabled",
                    parse_status="indexed",
                )
            )
            await session.commit()
        created.append(unit_id)
        return unit_id

    yield _make

    async with SessionLocal() as session:
        await session.execute(
            delete(KnowledgeUnit).where(KnowledgeUnit.id.in_(created))
        )
        await session.commit()


@pytest.fixture
async def user_ids() -> list[UUID]:
    """两个真实用户 id：`qa_audit_logs.user_id` 有外键指向 users，编一个 id 插不进去。"""

    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(User.id).order_by(User.created_at).limit(2)))
            .scalars()
            .all()
        )
    assert len(rows) >= 2, "演示数据里应至少有两个用户"
    return list(rows)


@pytest.fixture
async def created_units():
    """导入接口建出来的单元在用例结束时走真实删除路径（也就顺带覆盖缺口回退）。"""

    ids: list[UUID] = []
    yield ids

    for unit_id in ids:
        async with SessionLocal() as session:
            if await session.get(KnowledgeUnit, unit_id) is None:
                continue
            await knowledge_service.delete_unit(session, unit_id)


@pytest.fixture
def dispatched(monkeypatch) -> list[tuple[UUID, str]]:
    """替换真实投递器，避免测试连 broker。"""

    calls: list[tuple[UUID, str]] = []
    monkeypatch.setattr(
        knowledge_service, "dispatch_ingest", lambda pairs: calls.extend(pairs)
    )
    return calls


@pytest.fixture
def narrow_window(monkeypatch):
    """把看板时间窗收成"最近 5 分钟"，让聚合断言只受本用例数据影响。"""

    def _apply() -> RangeWindow:
        now = datetime.now(UTC)
        window = RangeWindow(
            key=operations_service.RANGE_7D,
            start=now - timedelta(minutes=5),
            end=now + timedelta(minutes=5),
            days=[now.date()],
        )
        monkeypatch.setattr(operations_service, "resolve_range", lambda _key: window)
        return window

    return _apply


def upload(name: str = "制度.md", content: str = "# 制度\n\n正文。\n") -> tuple:
    return ("files", (name, content.encode("utf-8"), "application/octet-stream"))


# --- 13.1 知识缺口 ---------------------------------------------------------


async def test_gap_list_orders_by_freq_and_filters_status(
    client: AsyncClient, gap_factory
) -> None:
    """高频在前、状态可筛，部门名要一起回（列表里要显示是谁问的）。"""

    headers = await login_headers(client)
    departments = await client.get("/api/v1/departments", headers=headers)
    assert departments.status_code == 200, departments.text
    department = departments.json()["data"][0]

    low = await gap_factory(question="测试缺口 低频", freq=9001)
    high = await gap_factory(
        question="测试缺口 高频", freq=9002, department_id=UUID(department["id"])
    )
    await gap_factory(question="测试缺口 已补全", freq=9003, status=faq_repo.GAP_FILLED)

    response = await client.get(
        "/api/v1/knowledge-gaps", params={"status": "open", "page_size": 100}, headers=headers
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    questions = [item["question_text"] for item in items]

    assert questions[:2] == ["测试缺口 高频", "测试缺口 低频"]
    assert "测试缺口 已补全" not in questions
    by_question = {item["question_text"]: item for item in items}
    assert by_question["测试缺口 高频"]["department_name"] == department["name"]
    assert by_question["测试缺口 低频"]["department_name"] is None
    assert by_question["测试缺口 高频"]["gap_id"] == str(high)
    assert by_question["测试缺口 低频"]["gap_id"] == str(low)


async def test_convert_gap_marks_converted_and_returns_prefill(
    client: AsyncClient, gap_factory
) -> None:
    """转建只改状态并给出预填标题：单元由导入接口带 from_gap_id 建。"""

    headers = await login_headers(client)
    question = "测试缺口 转建预填"
    gap_id = await gap_factory(question=question)

    response = await client.post(
        f"/api/v1/knowledge-gaps/{gap_id}/convert", headers=headers
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == faq_repo.GAP_CONVERTED
    assert data["suggested_title"] == question

    async with SessionLocal() as session:
        gap = await session.get(KnowledgeGap, gap_id)
        assert gap is not None and gap.status == faq_repo.GAP_CONVERTED


async def test_convert_filled_gap_conflicts(client: AsyncClient, gap_factory) -> None:
    headers = await login_headers(client)
    gap_id = await gap_factory(status=faq_repo.GAP_FILLED)

    response = await client.post(
        f"/api/v1/knowledge-gaps/{gap_id}/convert", headers=headers
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "GAP_NOT_CONVERTIBLE"


async def test_convert_missing_gap_is_404(client: AsyncClient) -> None:
    headers = await login_headers(client)
    response = await client.post(
        f"/api/v1/knowledge-gaps/{uuid4()}/convert", headers=headers
    )
    assert response.status_code == 404, response.text


async def test_convert_requires_permission(client: AsyncClient, fake_identity, gap_factory) -> None:
    """只有查看权限的人不能转建。"""

    gap_id = await gap_factory()
    fake_identity(permissions=["gap:view"])

    response = await client.post(f"/api/v1/knowledge-gaps/{gap_id}/convert")
    assert response.status_code == 403, response.text


async def test_import_with_from_gap_id_links_gap(
    client: AsyncClient, gap_factory, dispatched, created_units
) -> None:
    """转建落地的关键一跳：导入带 from_gap_id，缺口钉到新建的单元上。"""

    headers = await login_headers(client)
    gap_id = await gap_factory(status=faq_repo.GAP_CONVERTED)

    response = await client.post(
        "/api/v1/knowledge-units/import",
        files=[upload()],
        data={"from_gap_id": str(gap_id)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    unit_id = UUID(response.json()["data"]["items"][0]["unit_id"])
    created_units.append(unit_id)

    async with SessionLocal() as session:
        gap = await session.get(KnowledgeGap, gap_id)
        assert gap is not None
        assert gap.filled_unit_id == unit_id
        # 索引还没跑，所以仍是 converted：置 filled 是导入链走到 indexed 之后的事
        assert gap.status == faq_repo.GAP_CONVERTED


async def test_import_from_gap_rejects_multiple_files(
    client: AsyncClient, gap_factory, dispatched, created_units
) -> None:
    """"这个缺口由哪个单元补上"必须唯一，多文件直接拒绝而不是任选一个。"""

    headers = await login_headers(client)
    gap_id = await gap_factory(status=faq_repo.GAP_CONVERTED)

    response = await client.post(
        "/api/v1/knowledge-units/import",
        files=[upload("a.md"), upload("b.md")],
        data={"from_gap_id": str(gap_id)},
        headers=headers,
    )
    assert response.status_code == 400, response.text
    assert not dispatched


async def test_import_from_gap_rejects_filled_gap(
    client: AsyncClient, gap_factory, dispatched, created_units
) -> None:
    headers = await login_headers(client)
    gap_id = await gap_factory(status=faq_repo.GAP_FILLED)

    response = await client.post(
        "/api/v1/knowledge-units/import",
        files=[upload()],
        data={"from_gap_id": str(gap_id)},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert not dispatched


async def test_deleting_unit_reopens_its_gap(
    client: AsyncClient, gap_factory, unit_factory
) -> None:
    """删掉用来补缺口的单元：缺口退回 `open`。

    顺带锁住外键：`knowledge_gaps.filled_unit_id` 指向单元，若删除前不摘掉这个引用，
    单元就删不掉（接口 500），管理员会卡在"文档删不了"这一层。
    """

    headers = await login_headers(client)
    unit_id = await unit_factory()
    gap_id = await gap_factory(status=faq_repo.GAP_FILLED, filled_unit_id=unit_id)

    response = await client.delete(f"/api/v1/knowledge-units/{unit_id}", headers=headers)
    assert response.status_code == 200, response.text

    async with SessionLocal() as session:
        gap = await session.get(KnowledgeGap, gap_id)
        assert gap is not None
        assert gap.status == faq_repo.GAP_OPEN
        assert gap.filled_unit_id is None


# --- 13.2 审计查询 ---------------------------------------------------------


async def test_audit_logs_filter_by_user_and_status(
    client: AsyncClient, audit_factory, user_ids
) -> None:
    headers = await login_headers(client)
    mine = user_ids[0]
    keep = await audit_factory(
        user_id=mine, answer_status="denied", question="测试问题 审计筛选"
    )
    await audit_factory(user_id=mine, answer_status="answered")
    await audit_factory(user_id=user_ids[1], answer_status="denied")

    response = await client.get(
        "/api/v1/audit-logs",
        params={"user_id": str(mine), "answer_status": "denied"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["total"] == 1
    item = body["items"][0]
    assert item["audit_id"] == str(keep)
    assert item["question"] == "测试问题 审计筛选"
    assert item["answer_status"] == "denied"
    assert set(item) >= {
        "trace_id",
        "user_id",
        "question",
        "faq_hit",
        "allowed_unit_ids",
        "denied_count",
        "citation_ids",
        "max_similarity",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "created_at",
    }


async def test_audit_logs_filter_by_faq_hit(
    client: AsyncClient, audit_factory, user_ids
) -> None:
    headers = await login_headers(client)
    user_id = user_ids[0]
    await audit_factory(user_id=user_id, faq_hit=True)
    await audit_factory(user_id=user_id, faq_hit=False)

    response = await client.get(
        "/api/v1/audit-logs", params={"user_id": str(user_id), "faq_hit": True}, headers=headers
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1 and items[0]["faq_hit"] is True


async def test_audit_logs_day_range_includes_both_ends(
    client: AsyncClient, audit_factory
) -> None:
    """日期区间含首含尾，且按业务时区算——按 UTC 切会把当天 00:00~08:00 的提问漏掉。"""

    headers = await login_headers(client)
    target = (datetime.now(UTC) - timedelta(days=3)).date()
    # 当日 00:30（+08:00）= 前一日 16:30 UTC：UTC 口径下这天就会被漏掉
    inside = await audit_factory(
        created_at=datetime.combine(target, time(0, 30), tzinfo=ZoneInfo("Asia/Shanghai")),
        question="测试问题 边界当天",
    )
    outside = await audit_factory(
        created_at=datetime.combine(
            target - timedelta(days=1), time(23, 0), tzinfo=ZoneInfo("Asia/Shanghai")
        ),
        question="测试问题 边界前一日",
    )

    response = await client.get(
        "/api/v1/audit-logs",
        params={
            "start": target.isoformat(),
            "end": target.isoformat(),
            "page_size": 100,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    ids = {item["audit_id"] for item in response.json()["data"]["items"]}
    assert str(inside) in ids
    assert str(outside) not in ids


async def test_audit_logs_rejects_reversed_range(client: AsyncClient) -> None:
    headers = await login_headers(client)
    response = await client.get(
        "/api/v1/audit-logs",
        params={"start": "2026-09-15", "end": "2026-09-01"},
        headers=headers,
    )
    assert response.status_code == 400, response.text


async def test_audit_logs_require_dashboard_permission(client: AsyncClient, fake_identity) -> None:
    fake_identity(permissions=["gap:view"])
    response = await client.get("/api/v1/audit-logs")
    assert response.status_code == 403, response.text


# --- 13.3 看板 -------------------------------------------------------------


async def test_summary_computes_rates_from_audit(
    client: AsyncClient, audit_factory, user_ids, narrow_window
) -> None:
    """口径（PRD §8.3）：FAQ 命中率 = faq_hit 轮次/总轮次，覆盖率 = 有放行切片或直出的轮次/总轮次。"""

    narrow_window()
    headers = await login_headers(client)
    user_id, other_user = user_ids[0], user_ids[1]
    unit_id = str(uuid4())
    await audit_factory(
        user_id=user_id,
        faq_hit=True,
        latency_ms=1000,
        prompt_tokens=100,
        completion_tokens=50,
    )
    await audit_factory(
        user_id=user_id,
        allowed_unit_ids=[unit_id],
        latency_ms=3000,
        prompt_tokens=200,
        completion_tokens=100,
    )
    await audit_factory(
        user_id=user_id,
        allowed_unit_ids=[],
        answer_status="denied",
        latency_ms=2000,
        prompt_tokens=0,
        completion_tokens=0,
    )
    await audit_factory(
        user_id=other_user,
        allowed_unit_ids=[],
        answer_status="gap",
        latency_ms=4000,
        prompt_tokens=0,
        completion_tokens=0,
    )

    response = await client.get(
        "/api/v1/dashboard/summary", params={"range": "7d"}, headers=headers
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["pv"] == 4
    assert data["uv"] == 2
    assert data["faq_hit_rate"] == 0.25
    # 覆盖：faq 直出 1 条 + 有放行切片 1 条 = 2/4；denied 与 gap 都不算
    assert data["kb_coverage_rate"] == 0.5
    assert data["avg_latency_ms"] == 2500.0
    assert data["p50_latency_ms"] == 2500.0
    assert data["total_tokens"] == 450
    assert data["knowledge_count"] >= 0


async def test_summary_is_zero_on_empty_window(
    client: AsyncClient, monkeypatch
) -> None:
    """空态不能 500：比率为 0 的分母要落到 0。

    时间窗取 2020 年（当天不可能有其它数据），否则"空窗"会依赖别的用例是否刚好跑在同一时刻。
    """

    empty = RangeWindow(
        key=operations_service.RANGE_TODAY,
        start=datetime(2020, 1, 1, tzinfo=UTC),
        end=datetime(2020, 1, 2, tzinfo=UTC),
        days=[date(2020, 1, 1)],
    )
    monkeypatch.setattr(operations_service, "resolve_range", lambda _key: empty)
    headers = await login_headers(client)
    response = await client.get(
        "/api/v1/dashboard/summary", params={"range": "today"}, headers=headers
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["pv"] == 0 and data["uv"] == 0
    assert data["faq_hit_rate"] == 0.0 and data["kb_coverage_rate"] == 0.0


async def test_top_questions_orders_by_hits(
    client: AsyncClient, audit_factory, narrow_window
) -> None:
    narrow_window()
    headers = await login_headers(client)
    await audit_factory(question="测试问题 高频榜")
    await audit_factory(question="测试问题 高频榜")
    await audit_factory(question="测试问题 低频榜")

    response = await client.get(
        "/api/v1/dashboard/top-questions", params={"range": "7d"}, headers=headers
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert items[0]["question"] == "测试问题 高频榜"
    assert items[0]["hits"] == 2
    assert items[1]["hits"] == 1


async def test_top_knowledge_resolves_title_and_tolerates_deleted_unit(
    client: AsyncClient, audit_factory, unit_factory, narrow_window
) -> None:
    """热门榜要给出标题；单元被删掉后引用仍在（审计不清理），标题回 null 而不是 500。"""

    narrow_window()
    headers = await login_headers(client)
    unit_id = await unit_factory(title="看板测试单元 差旅报销标准")
    deleted = uuid4()
    await audit_factory(citation_ids=[str(unit_id), str(deleted)])

    response = await client.get(
        "/api/v1/dashboard/top-knowledge", params={"range": "7d"}, headers=headers
    )
    assert response.status_code == 200, response.text
    by_unit = {item["unit_id"]: item for item in response.json()["data"]}
    assert by_unit[str(unit_id)]["title"] == "看板测试单元 差旅报销标准"
    assert by_unit[str(deleted)]["title"] is None
    assert all(item["hits"] == 1 for item in by_unit.values())


async def test_dashboard_tolerates_json_null_arrays(
    client: AsyncClient, audit_factory, unit_factory, narrow_window
) -> None:
    """历史行的 JSON null 不能把看板打成 500。

    `allowed_unit_ids`/`citation_ids` 是 JSONB，早期写入路径把 Python `None` 落成了 **JSON null**
    （`'null'::jsonb`）；PG 对 JSON null 调 `jsonb_array_length` / `jsonb_array_elements_text`
    是直接报错、不是回 NULL——而审计只增不删，一行「没有命中」的历史记录就够把整个看板 500。

    顺带锁住根因：新写入的行里 `None` 落成 SQL NULL，不再产生 JSON null。
    """

    narrow_window()
    headers = await login_headers(client)
    unit_id = await unit_factory(title="看板测试单元 JSON null")

    # 1) 旧路径写下的行：JSON null
    legacy_id = await audit_factory(question="测试问题 历史 JSON null", citation_ids=[str(unit_id)])
    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE qa_audit_logs SET allowed_unit_ids = 'null'::jsonb, "
                "citation_ids = 'null'::jsonb WHERE id = :id"
            ),
            {"id": legacy_id},
        )
        await session.commit()

    # 2) 新路径写下的行：None → SQL NULL
    fresh_id = uuid4()
    async with SessionLocal() as session:
        session.add(
            QaAuditLog(
                id=fresh_id,
                trace_id=uuid4().hex,
                question="测试问题 新写入空引用",
                allowed_unit_ids=None,
                citation_ids=None,
                denied_count=0,
                answer_status="gap",
                latency_ms=1000,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
        typeof = await session.scalar(
            text("SELECT jsonb_typeof(citation_ids) FROM qa_audit_logs WHERE id = :id"),
            {"id": fresh_id},
        )
    assert typeof is None, "新写入的 None 应当是 SQL NULL，而不是 JSON null"

    try:
        for path in ("summary", "top-knowledge", "top-questions", "token-trend"):
            response = await client.get(
                f"/api/v1/dashboard/{path}", params={"range": "7d"}, headers=headers
            )
            assert response.status_code == 200, f"{path}: {response.text}"

        # 榜单里不该出现这条脏行（它没有引用任何单元）
        response = await client.get(
            "/api/v1/dashboard/top-knowledge", params={"range": "7d"}, headers=headers
        )
        rows = {item["unit_id"] for item in response.json()["data"]}
        assert str(unit_id) not in rows
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(QaAuditLog).where(QaAuditLog.id.in_([legacy_id, fresh_id]))
            )
            await session.commit()


async def test_token_trend_fills_gap_days(
    client: AsyncClient, audit_factory, monkeypatch
) -> None:
    """没有问答的日子要补零；缺了这一天，折线会把空缺直接连过去，看不出停摆。

    窗口取 2020 年：演示数据与其它用例的数据都不可能在那个区间，补零与分桶才算得准。
    """

    days = [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]
    tz = ZoneInfo("Asia/Shanghai")
    window = RangeWindow(
        key=operations_service.RANGE_7D,
        start=datetime.combine(days[0], time.min, tzinfo=tz).astimezone(UTC),
        end=datetime.combine(
            days[-1] + timedelta(days=1), time.min, tzinfo=tz
        ).astimezone(UTC),
        days=days,
    )
    monkeypatch.setattr(operations_service, "resolve_range", lambda _key: window)
    headers = await login_headers(client)
    await audit_factory(
        created_at=datetime.combine(days[-1], time(12, 0), tzinfo=ZoneInfo("Asia/Shanghai")),
        prompt_tokens=30,
        completion_tokens=20,
        latency_ms=1500,
    )

    response = await client.get(
        "/api/v1/dashboard/token-trend", params={"range": "7d"}, headers=headers
    )
    assert response.status_code == 200, response.text
    points = response.json()["data"]["points"]
    assert [point["day"] for point in points] == [day.isoformat() for day in days]
    assert points[0] == {
        "day": days[0].isoformat(),
        "pv": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "avg_latency_ms": 0.0,
        "p50_latency_ms": 0.0,
        "p90_latency_ms": 0.0,
    }
    assert points[-1]["pv"] == 1
    assert points[-1]["total_tokens"] == 50
    assert points[-1]["p90_latency_ms"] == 1500.0
