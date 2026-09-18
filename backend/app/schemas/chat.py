"""问答会话与消息的出入参。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ChatSessionOut(BaseModel):
    """会话行。`title` 由首个问题派生，可能为空（历史数据或尚未提问）。"""

    id: uuid.UUID
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    """单条消息。`citations` 存的就是当时下发的那份引用（含附图），回放不再查库。"""

    id: uuid.UUID
    role: str
    content: str
    citations: list[dict[str, Any]] | None = None
    created_at: datetime
