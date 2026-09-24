"""3D 助手闲聊：用系统 Chat 网关流式对话，不检索知识库。"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import AppError, ErrorCode
from app.engines.chat import build_chat_model
from app.repositories import system as system_repo
from app.schemas.assistant import AssistantTurn

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是知识库管理平台的 3D 助手「小小」，自称小小，性格活泼像萌新。"
    "回答亲切清楚，可以多说几句，不要使用 Markdown、列表或代码块。"
    "不知道的事就老实承认，不要编造公司制度或知识库内容。"
)


def _chunk_text(chunk: object) -> str:
    content = getattr(chunk, "content", chunk)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def build_messages(content: str, history: list[AssistantTurn]) -> list[Any]:
    messages: list[Any] = [SystemMessage(content=SYSTEM_PROMPT)]
    for turn in history[-10:]:
        if turn.role == "assistant":
            messages.append(AIMessage(content=turn.content))
        else:
            messages.append(HumanMessage(content=turn.content))
    messages.append(HumanMessage(content=content.strip()))
    return messages


async def prepare_chat(session: AsyncSession):
    """读取网关配置并构造流式客户端。配置不全时抛校验错误。"""

    model_config = await system_repo.load_model_config(session)
    try:
        return build_chat_model(model_config, streaming=True)
    except RuntimeError as exc:
        raise AppError.validation(str(exc)) from exc


async def iter_tokens(model: Any, messages: list[Any]) -> AsyncIterator[str]:
    """逐片产出模型增量文本。"""

    try:
        async with aclosing(model.astream(messages)) as stream:
            async for chunk in stream:
                text = _chunk_text(chunk)
                if text:
                    yield text
    except AppError:
        raise
    except Exception:
        logger.exception("3D 助手调用 Chat 网关失败")
        raise AppError.ai_upstream() from None


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def sse_bytes(model: Any, messages: list[Any]) -> AsyncIterator[bytes]:
    """把 token 流包装成 SSE。出错也走 data 事件，避免半截 HTTP 失败。"""

    try:
        async for piece in iter_tokens(model, messages):
            yield _sse({"delta": piece})
        yield _sse({"done": True})
    except AppError as exc:
        yield _sse({"error": exc.message, "code": exc.code})
    except Exception:
        logger.exception("3D 助手流式输出失败")
        yield _sse(
            {
                "error": "模型服务暂不可用，请稍后重试",
                "code": ErrorCode.AI_UPSTREAM_ERROR,
            }
        )
