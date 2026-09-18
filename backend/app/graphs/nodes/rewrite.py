"""查询改写节点（tasklist 12.2，TECH_SPEC §1）。

一次 Chat 调用同时产出 `rewritten` / `keywords` / `hyde`：改写与假设文档依赖对问句的同一个
理解，拆成两次调用只是多一次往返和一次失败点。

两类失败要分开处理，这是本节点最要紧的取舍：

- **解析失败**（网关答了，但不是合法 JSON）：回退到原问句、`keywords=[]`、`hyde=None`。
  模型返回多余文字、把 JSON 包进 Markdown 代码块都是常态，为此让整轮问答失败不可接受；
  少了 HyDE 只是少一路召回，检索链仍然完整。
- **调用失败**（网络 / 鉴权 / 超时）：抛 `AppError.ai_upstream`。生成节点同样依赖网关，
  此处静默降级只会把「网关卡了」变成「答案质量莫名变差」，后者难查得多。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.common.config import settings
from app.common.errors import AppError
from app.engines.chat import build_chat_model
from app.graphs.context import deps_of
from app.graphs.nodes.prompts import REWRITE_SYSTEM, REWRITE_USER
from app.graphs.state import QAState

logger = logging.getLogger(__name__)

_CODE_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass(frozen=True)
class Rewrite:
    rewritten: str
    keywords: list[str]
    hyde: str | None


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象。

    依次尝试：整体解析 → 剥掉代码块围栏 → 截取第一个 `{` 到最后一个 `}`。三段都失败才
    放弃。顺序即优先级：越靠前的候选越接近"模型老实输出了 JSON"的理想情况。
    """

    candidates = [text]
    fenced = _CODE_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _keywords(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def to_rewrite(question: str, parsed: dict[str, Any] | None) -> Rewrite:
    """把解析结果收敛成确定的三元组，任何缺失都退到可用值。

    `rewritten` 缺失时退回原问句而不是空串：空串会让后续检索查一个空向量。
    HyDE 关闭时在**这里**丢弃而非在检索节点丢弃——State 里不放用不到的数据，
    下游就不必各自记得判断开关。
    """

    if parsed is None:
        return Rewrite(rewritten=question, keywords=[], hyde=None)

    hyde = _text(parsed.get("hyde")) or None
    return Rewrite(
        rewritten=_text(parsed.get("rewritten")) or question,
        keywords=_keywords(parsed.get("keywords")),
        hyde=hyde if settings.hyde_enabled else None,
    )


def _usage_counts(response: object) -> tuple[int, int]:
    """兼容 LangChain `usage_metadata` 的 input/output 与 prompt/completion 两种命名。"""

    usage = getattr(response, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        return 0, 0
    prompt = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    completion = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return (
        int(prompt) if isinstance(prompt, int | float) else 0,
        int(completion) if isinstance(completion, int | float) else 0,
    )


async def rewrite(state: QAState, config: RunnableConfig) -> dict:
    deps = deps_of(config)
    question = state["question"]
    chat = build_chat_model(deps.model_config)

    try:
        response = await chat.ainvoke(
            [
                SystemMessage(content=REWRITE_SYSTEM),
                HumanMessage(content=REWRITE_USER.format(question=question)),
            ]
        )
    except Exception as exc:  # noqa: BLE001 - 统一收敛为可直接展示的上游错误
        logger.warning("改写调用失败：%s", exc)
        raise AppError.ai_upstream() from exc

    content = response.content
    text = content if isinstance(content, str) else str(content)
    result = to_rewrite(question, _extract_json(text))

    if result.rewritten == question and not result.keywords and result.hyde is None:
        logger.warning("改写结果不可用，已回退原问句：%r", text[:200])

    prompt_tokens, completion_tokens = _usage_counts(response)
    return {
        "rewritten": result.rewritten,
        "keywords": result.keywords,
        "hyde": result.hyde,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
