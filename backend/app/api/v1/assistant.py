"""3D 助手闲聊接口：登录即可，SSE 流式输出。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_auth
from app.common.db import get_session
from app.schemas.assistant import AssistantChatRequest
from app.services import assistant as assistant_service

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post("/chat")
async def assistant_chat(
    payload: AssistantChatRequest,
    current: CurrentUser = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """用系统 Chat 网关流式生成助手回复。"""

    del current
    model = await assistant_service.prepare_chat(session)
    messages = assistant_service.build_messages(payload.content, payload.history)
    return StreamingResponse(
        assistant_service.sse_bytes(model, messages),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
