"""问答会话与历史接口（tasklist 14.4，TECH_SPEC §4.5）。

三个接口都是「我的会话」：权限码只作入口（`ai:chat`），真正决定能看到哪些数据的是
`chat_sessions.user_id`——把它交给权限码或部门数据权限都会让同事之间互相看见提问记录。

没有「新建会话」接口：会话在第一轮问答开始时创建（见 `services/chat.py`）。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, PageParams, pagination, require_perm
from app.common.db import get_session
from app.schemas.chat import ChatMessageOut, ChatSessionOut
from app.schemas.common import page, success
from app.services import chat as chat_service

router = APIRouter(tags=["chat"])


@router.get("/chat/sessions")
async def list_chat_sessions(
    params: PageParams = Depends(pagination),
    current: CurrentUser = Depends(require_perm("ai:chat")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """我的会话列表，最近活跃在前（侧栏按这个顺序渲染）。"""

    rows, total = await chat_service.list_sessions(
        session, user_id=current.user_id, offset=params.offset, limit=params.page_size
    )
    # 逐字段构造而不是 from_attributes：出参对前端是一份契约，多一个字段都得先改 schema
    items = [
        ChatSessionOut(
            id=row.id, title=row.title, created_at=row.created_at, updated_at=row.updated_at
        ).model_dump(mode="json")
        for row in rows
    ]
    return page(items, total, params.page, params.page_size)


@router.get("/chat/sessions/{session_id}/messages")
async def list_chat_messages(
    session_id: UUID,
    params: PageParams = Depends(pagination),
    current: CurrentUser = Depends(require_perm("ai:chat")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """回放一个会话：按时间正序，引用与附图随消息一起下发。"""

    rows, total = await chat_service.list_messages(
        session,
        user_id=current.user_id,
        session_id=session_id,
        offset=params.offset,
        limit=params.page_size,
    )
    items = [
        ChatMessageOut(
            id=row.id,
            role=row.role,
            content=row.content,
            citations=row.citations,
            created_at=row.created_at,
        ).model_dump(mode="json")
        for row in rows
    ]
    return page(items, total, params.page, params.page_size)


@router.delete("/chat/sessions/{session_id}")
async def delete_chat_session(
    session_id: UUID,
    current: CurrentUser = Depends(require_perm("ai:chat")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """删除会话与其中的消息；审计行不受影响（留痕不随用户整理而消失）。"""

    await chat_service.delete_session(
        session, user_id=current.user_id, session_id=session_id
    )
    return success(None)
