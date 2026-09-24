"""Chat 网关客户端构造（TECH_SPEC §8.1：Chat 走网关，Embedding 走本地 BGE-M3）。"""

from app.engines.chat.client import build_chat_model

__all__ = ["build_chat_model"]
