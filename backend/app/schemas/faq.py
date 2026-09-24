"""FAQ 审核、发布与维护的请求/响应模型（TECH_SPEC §4.2）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CandidateItem(BaseModel):
    candidate_id: UUID
    question: str
    similar_questions: list[str] = Field(default_factory=list)
    freq: int
    confidence: float | None = None
    suggested_answer: str | None = None
    source_unit_ids: list[UUID] = Field(default_factory=list)
    status: str
    reject_reason: str | None = None
    created_at: datetime


class PublishPayload(BaseModel):
    """发布候选。省略 `answer` 时回退到候选自带的 `suggested_answer`。"""

    answer: str | None = Field(default=None, min_length=1)


class RejectPayload(BaseModel):
    """驳回原因必填：候选会被永久标记，没有原因就无法复盘为什么没采纳。"""

    reason: str = Field(min_length=1, max_length=500)


class FaqItem(BaseModel):
    faq_id: UUID
    question: str
    answer: str
    cache_enabled: bool
    status: str
    source_candidate_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class FaqUpdate(BaseModel):
    """只允许改答案、`cache_enabled` 与状态。

    **不支持改问句**：问句是缓存里向量的编码源，改它必须重新编码并重建缓存，出错面大而
    收益低（旧向量会与新问句不匹配）。需要别的问法就再发一条 FAQ，也让匹配更精确。
    """

    answer: str | None = Field(default=None, min_length=1)
    cache_enabled: bool | None = None
    status: str | None = Field(default=None, pattern="^(published|offline)$")
