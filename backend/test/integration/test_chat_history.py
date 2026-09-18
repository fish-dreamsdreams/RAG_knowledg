"""问答会话与历史的数据库路径集成测试（tasklist 14.4）。

需要 PostgreSQL 与 Redis：`docker compose up -d postgres redis` 后
`pytest -m integration -k chat_history`。用演示种子账号（finance01 / admin）。

会话由用例自己造、末尾自己删：`chat_repo` 的写入是即时提交（前端要马上读到），
回滚型 fixture 兜不住这些行，留下垃圾会话会让侧栏在演示时不好看。
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import AppError
from app.models import QaAuditLog, User
from app.repositories import chat as chat_repo
from app.services import chat as chat_service

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "Demo@123456"
QUESTION = "出差住宿费怎么报销"
ANSWER = "依据《差旅费用标准》，一线城市住宿上限 600 元/天。"
CITATION = {"unit_id": str(uuid4()), "title": "差旅费用标准", "chunk_id": str(uuid4())}


async def _user_id(session: AsyncSession, username: str) -> UUID:
    return (
        await session.execute(select(User.id).where(User.username == username))
    ).scalar_one()


async def _token(client: AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _token(client, username)}"}


async def _drop(session: AsyncSession, *, user_id: UUID, session_id: UUID) -> None:
    """清掉本用例造的会话；已删或不属于该用户时静默跳过。"""

    try:
        await chat_service.delete_session(
            session, user_id=user_id, session_id=session_id
        )
    except AppError:
        pass


async def test_start_turn_creates_session_titled_by_the_question(
    session: AsyncSession,
) -> None:
    """没有会话就现建：标题取首个问题，用户消息同一次写入。"""

    finance = await _user_id(session, "finance01")

    session_id = await chat_service.start_turn(
        session, user_id=finance, session_id=None, question=QUESTION
    )

    try:
        rows, total = await chat_service.list_sessions(
            session, user_id=finance, offset=0, limit=20
        )
        created = next(row for row in rows if row.id == session_id)
        assert created.title == QUESTION
        assert total >= 1

        messages, _ = await chat_service.list_messages(
            session, user_id=finance, session_id=session_id, offset=0, limit=20
        )
        assert [(row.role, row.content) for row in messages] == [("user", QUESTION)]
    finally:
        await _drop(session, user_id=finance, session_id=session_id)


async def test_finish_turn_appends_answer_with_citations(session: AsyncSession) -> None:
    """答完落助手消息：引用（含附图）按当时下发的那份存，回放不再查库。"""

    finance = await _user_id(session, "finance01")
    session_id = await chat_service.start_turn(
        session, user_id=finance, session_id=None, question=QUESTION
    )

    try:
        await chat_service.finish_turn(
            session, session_id=session_id, answer=ANSWER, citations=[CITATION]
        )

        messages, total = await chat_service.list_messages(
            session, user_id=finance, session_id=session_id, offset=0, limit=20
        )
        assert total == 2
        assert [row.role for row in messages] == ["user", "assistant"]
        assert messages[1].content == ANSWER
        assert messages[1].citations == [CITATION]
        # 提问那条没有引用，不该写成空数组（前端少一个分支）
        assert messages[0].citations is None
    finally:
        await _drop(session, user_id=finance, session_id=session_id)


async def test_messages_keep_real_insert_time(session: AsyncSession) -> None:
    """时间来自库时钟，两条消息的 `created_at` 必须真的递增。

    回归用例：`chat_messages.created_at` 曾经是 `DEFAULT 'now()'`，被 PostgreSQL 在 DDL 时
    解析成固定时间，全表同一微秒——回放顺序变成随机的（实测出现过"先答后问"）。
    """

    finance = await _user_id(session, "finance01")
    session_id = await chat_service.start_turn(
        session, user_id=finance, session_id=None, question=QUESTION
    )

    try:
        await asyncio.sleep(0.05)
        await chat_service.finish_turn(
            session, session_id=session_id, answer=ANSWER, citations=[CITATION]
        )

        messages, _ = await chat_service.list_messages(
            session, user_id=finance, session_id=session_id, offset=0, limit=20
        )
        assert [row.role for row in messages] == ["user", "assistant"]
        assert messages[0].created_at < messages[1].created_at
    finally:
        await _drop(session, user_id=finance, session_id=session_id)


async def test_another_users_session_looks_nonexistent(session: AsyncSession) -> None:
    """别人的会话：读不到、删不掉，且报的是「不存在」而不是「无权限」。"""

    finance = await _user_id(session, "finance01")
    admin = await _user_id(session, "admin")
    session_id = await chat_service.start_turn(
        session, user_id=finance, session_id=None, question=QUESTION
    )

    try:
        with pytest.raises(AppError) as read_error:
            await chat_service.list_messages(
                session, user_id=admin, session_id=session_id, offset=0, limit=20
            )
        assert read_error.value.code == "AI_SESSION_NOT_FOUND"
        assert read_error.value.http_status == 404

        with pytest.raises(AppError) as delete_error:
            await chat_service.delete_session(
                session, user_id=admin, session_id=session_id
            )
        assert delete_error.value.code == "AI_SESSION_NOT_FOUND"

        # 别人的会话列表里也不该出现它
        rows, _ = await chat_service.list_sessions(
            session, user_id=admin, offset=0, limit=100
        )
        assert session_id not in {row.id for row in rows}

        # 删不掉就得真的还在
        messages, _ = await chat_service.list_messages(
            session, user_id=finance, session_id=session_id, offset=0, limit=20
        )
        assert [row.role for row in messages] == ["user"]
    finally:
        await _drop(session, user_id=finance, session_id=session_id)


async def test_delete_cascades_messages_but_keeps_the_audit(
    session: AsyncSession,
) -> None:
    """删会话带走消息（外键级联），不带走审计——留痕不因用户整理而消失。"""

    finance = await _user_id(session, "finance01")
    trace_id = f"integration-chat-{uuid4()}"
    session_id = await chat_service.start_turn(
        session, user_id=finance, session_id=None, question=QUESTION
    )
    await chat_service.finish_turn(
        session, session_id=session_id, answer=ANSWER, citations=[CITATION]
    )
    await chat_repo.add_audit_log(
        session,
        trace_id=trace_id,
        user_id=finance,
        session_id=session_id,
        question=QUESTION,
        faq_hit=False,
        allowed_unit_ids=[],
        denied_count=0,
        citation_ids=[CITATION["unit_id"]],
        answer_status="answered",
        latency_ms=12,
    )

    try:
        await chat_service.delete_session(
            session, user_id=finance, session_id=session_id
        )

        with pytest.raises(AppError):
            await chat_service.list_messages(
                session, user_id=finance, session_id=session_id, offset=0, limit=20
            )
        audit = (
            await session.execute(
                select(QaAuditLog.id).where(QaAuditLog.trace_id == trace_id)
            )
        ).scalar_one_or_none()
        assert audit is not None, "审计必须留下"
    finally:
        await session.execute(delete(QaAuditLog).where(QaAuditLog.trace_id == trace_id))
        await session.commit()
        await _drop(session, user_id=finance, session_id=session_id)


async def test_sessions_endpoint_is_scoped_to_the_caller(
    client: AsyncClient, session: AsyncSession
) -> None:
    """列表只返回自己的会话；别人的会话连 id 都不该出现。"""

    finance = await _user_id(session, "finance01")
    session_id = await chat_service.start_turn(
        session, user_id=finance, session_id=None, question=QUESTION
    )

    try:
        mine = await client.get("/api/v1/chat/sessions", headers=await _auth(client, "finance01"))
        assert mine.status_code == 200
        assert str(session_id) in {item["id"] for item in mine.json()["data"]["items"]}

        others = await client.get("/api/v1/chat/sessions", headers=await _auth(client, "admin"))
        assert others.status_code == 200
        assert str(session_id) not in {
            item["id"] for item in others.json()["data"]["items"]
        }
    finally:
        await _drop(session, user_id=finance, session_id=session_id)


async def test_messages_and_delete_endpoints_guard_ownership(
    client: AsyncClient, session: AsyncSession
) -> None:
    """HTTP 侧同样按归属收口：读别人的 404，删自己的 404 之后读也 404。"""

    finance = await _user_id(session, "finance01")
    session_id = await chat_service.start_turn(
        session, user_id=finance, session_id=None, question=QUESTION
    )
    await chat_service.finish_turn(
        session, session_id=session_id, answer=ANSWER, citations=[CITATION]
    )

    try:
        foreign = await client.get(
            f"/api/v1/chat/sessions/{session_id}/messages",
            headers=await _auth(client, "admin"),
        )
        assert foreign.status_code == 404
        assert foreign.json()["code"] == "AI_SESSION_NOT_FOUND"
        assert ANSWER not in foreign.text, "别人的会话正文一个字都不能回"

        mine = await client.get(
            f"/api/v1/chat/sessions/{session_id}/messages",
            headers=await _auth(client, "finance01"),
        )
        assert mine.status_code == 200
        items = mine.json()["data"]["items"]
        assert [item["role"] for item in items] == ["user", "assistant"]
        assert items[1]["citations"][0]["title"] == CITATION["title"]

        deleted = await client.delete(
            f"/api/v1/chat/sessions/{session_id}",
            headers=await _auth(client, "finance01"),
        )
        assert deleted.status_code == 200

        gone = await client.get(
            f"/api/v1/chat/sessions/{session_id}/messages",
            headers=await _auth(client, "finance01"),
        )
        assert gone.status_code == 404
    finally:
        await _drop(session, user_id=finance, session_id=session_id)
