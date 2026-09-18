"""v1 路由汇总。"""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    chat,
    faq,
    health,
    knowledge_units,
    operations,
    organization,
    system,
)
from app.websocket import chat as chat_ws

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(organization.router)
api_router.include_router(knowledge_units.router)
api_router.include_router(faq.router)
api_router.include_router(operations.router)
api_router.include_router(system.router)
# 会话历史（14.4）：与 WS 问答共用 ai:chat，但走 HTTP 以便刷新后回放
api_router.include_router(chat.router)
# WS 路由也挂在这里，`/api/v1` 前缀只在这一处拼，避免两套前缀来源
api_router.include_router(chat_ws.router)
