"""系统配置的请求/响应模型（tasklist 13.4）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ModelConfigView(BaseModel):
    """模型配置视图。**永远不回明文密钥**：只给掩码与「是否已配置」。"""

    base_url: str
    chat_model: str
    temperature: float
    max_tokens: int
    rerank_model: str
    faq_sim_threshold: float
    gap_sim_threshold: float
    faq_cluster_min_freq: int
    api_key_configured: bool
    api_key_masked: str | None = None


class ModelConfigUpdate(BaseModel):
    """局部更新：**只改显式传了的字段**（服务层用 `exclude_unset`）。

    `api_key` 的三种语义：不传 = 不动；传非空串 = 加密覆盖；传空串 = 清除（回到 env 兜底）。
    """

    base_url: str | None = Field(default=None, max_length=512)
    chat_model: str | None = Field(default=None, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    rerank_model: str | None = Field(default=None, max_length=128)
    faq_sim_threshold: float | None = Field(default=None, ge=0, le=1)
    gap_sim_threshold: float | None = Field(default=None, ge=0, le=1)
    faq_cluster_min_freq: int | None = Field(default=None, ge=1, le=100)
    api_key: str | None = Field(default=None, max_length=512)
