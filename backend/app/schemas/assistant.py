"""3D 助手闲聊请求/响应。不走知识库检索，只调 Chat 网关。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AssistantTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=2000)


class AssistantChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    history: list[AssistantTurn] = Field(default_factory=list, max_length=12)


class AssistantChatView(BaseModel):
    reply: str
