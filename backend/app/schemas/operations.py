"""运营闭环的请求/响应模型：知识缺口、审计流水、看板（tasklist 13.1 ~ 13.3）。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class GapItem(BaseModel):
    """缺口列表项。字段名与 design.md §4.2 的表列一致（PRD §8.3 把 `freq` 写成 `frequency`）。"""

    gap_id: UUID
    question_text: str
    department_name: str | None = None
    freq: int
    max_similarity: float | None = None
    last_asked_at: datetime | None = None
    # open | converted | filled
    status: str
    filled_unit_id: UUID | None = None


class GapConvertPrefill(BaseModel):
    """转建后的预填信息：前端据此打开导入抽屉并带上 `from_gap_id`。

    `suggested_category` 恒为 `None`：仓库里还没有分类词表（导入时的 `category` 是自由文本），
    编一套分类只会让运营看到凭空的建议。前端把它做成可留空的下拉/输入框即可。
    """

    gap_id: UUID
    status: str
    suggested_title: str
    suggested_category: str | None = None


class AuditItem(BaseModel):
    """PRD §8.3 的审计字段全量回传，供运营排查单轮问答。"""

    audit_id: UUID
    trace_id: str
    user_id: UUID | None = None
    session_id: UUID | None = None
    question: str
    rewritten: str | None = None
    faq_hit: bool
    allowed_unit_ids: list[str] = Field(default_factory=list)
    denied_count: int
    citation_ids: list[str] = Field(default_factory=list)
    # answered | denied | gap | interrupted
    answer_status: str
    max_similarity: float | None = None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    created_at: datetime


class DashboardSummary(BaseModel):
    """看板顶部指标卡（PRD §6.6）。比率是 0~1 的小数，由前端决定展示成百分比。"""

    range: str
    start: datetime
    end: datetime
    pv: int
    uv: int
    knowledge_count: int
    faq_hit_rate: float
    kb_coverage_rate: float
    avg_latency_ms: float
    p50_latency_ms: float
    p90_latency_ms: float
    total_tokens: int


class TopQuestionItem(BaseModel):
    question: str
    hits: int


class TopKnowledgeItem(BaseModel):
    unit_id: UUID
    # 单元被删除后历史引用仍在榜上，标题为 null
    title: str | None = None
    hits: int


class TrendPoint(BaseModel):
    """按天的一个点。没有问答的日期补零，否则折线会把空缺日子直接连过去。"""

    day: date
    pv: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: float
    p50_latency_ms: float
    p90_latency_ms: float


class TrendSeries(BaseModel):
    range: str
    points: list[TrendPoint] = Field(default_factory=list)
