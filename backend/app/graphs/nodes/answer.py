"""答案生成、固定拒答与知识缺口出口（tasklist 12.4）。

安全边界：

- `generate` 只读 ACL 后的 `parent_contexts`，不接触 `reranked` / `denied`；
- citation 仅从 `parent_contexts` 建造，所以天然是 allowed 的子集；
- `respond_denied` / `respond_gap` 都不调用 Chat。没有上下文时让模型"礼貌地回答"就是幻觉源头。

LangGraph 的 custom stream writer 在图运行时逐 token 向 WebSocket 层输出；直接单测节点时
没有图运行上下文，writer 安全退化为空操作。最终答案仍累计写 State，审计与断线恢复都有
确定的完整内容可用。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import aclosing
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.common.config import settings
from app.common.errors import AppError
from app.engines.chat import build_chat_model
from app.engines.chunking import asset_names
from app.engines.retrieve import ParentContext
from app.engines.storage import object_store
from app.graphs.context import deps_of
from app.graphs.nodes.prompts import ANSWER_SYSTEM, ANSWER_USER, DENIED_ANSWER, GAP_ANSWER
from app.graphs.state import Citation, CitationAsset, QAState
from app.repositories import faq as faq_repo
from app.repositories import knowledge as knowledge_repo

logger = logging.getLogger(__name__)
SNIPPET_CHARS = 240


def _stream_writer() -> Callable[[dict[str, str]], None] | None:
    """取 LangGraph custom writer；图外直接调用节点时返回 None。"""

    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except (ImportError, RuntimeError):
        return None


def _chunk_text(chunk: Any) -> str:
    """提取 Chat 流 chunk 的文本，非文本块（tool call 等）一律忽略。"""

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


def _usage_counts(chunk: object) -> tuple[int, int]:
    """从流式 chunk 读取 usage；兼容 input/output 与 prompt/completion 两种命名。

    流式调用只有末个 chunk 带 usage，所以调用方取最大值累加而不是逐块相加。
    """

    usage = getattr(chunk, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        return 0, 0
    prompt = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    completion = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return (
        int(prompt) if isinstance(prompt, int | float) else 0,
        int(completion) if isinstance(completion, int | float) else 0,
    )


def _context_text(contexts: Sequence[ParentContext]) -> str:
    """不写入单元标题或 id，只给模型允许的正文与确定边界。"""

    return "\n\n---\n\n".join(context.content for context in contexts)


def _citation_assets(context: ParentContext, budget: int) -> tuple[list[CitationAsset], int]:
    """从父块正文里回收图片，返回 `(该条引用附图, 剩余配额)`（tasklist 17.5）。

    只从**已授权**的 `parent_contexts` 里取图：候选范围天然是 allowed 子块所属父块的正文，
    与正文同口径，不必再查一次 `knowledge_unit_assets`（TECH_SPEC §8.0）。

    配额跨引用共享：一条答案最多 `ANSWER_MAX_IMAGES` 张，按引用顺序先到先得。不做正文内联
    ——流式输出下内联占位符会被逐字吐出来，用户会看到 `[图` 这种半截文本。
    """

    if budget <= 0:
        return [], 0
    names = asset_names(context.content)[:budget]
    assets: list[CitationAsset] = [
        CitationAsset(name=name, url=object_store.asset_proxy_path(context.unit_id, name))
        for name in names
    ]
    return assets, budget - len(assets)


async def _citations(
    contexts: Sequence[ParentContext], config: RunnableConfig
) -> list[Citation]:
    """构造每个父块一条引用；标题只查询已经 ACL 放行的 unit。"""

    deps = deps_of(config)
    units = await knowledge_repo.get_units_by_ids(
        deps.session, [context.unit_id for context in contexts]
    )
    titles = {row.id: row.title for row in units}

    citations: list[Citation] = []
    budget = max(0, settings.answer_max_images)
    for context in contexts:
        title = titles.get(context.unit_id)
        if title is None or not context.child_ids:
            # 已删单元或数据不一致时宁可少一条 citation，不猜标题、更不查 denied 单元补齐。
            continue
        citation = Citation(
            unit_id=str(context.unit_id),
            title=title,
            chunk_id=str(context.child_ids[0]),
            snippet=context.content[:SNIPPET_CHARS],
        )
        assets, budget = _citation_assets(context, budget)
        if assets:
            # 无图的引用不带空数组：前端少一个分支，审计也不多存噪音
            citation["assets"] = assets
        citations.append(citation)
    return citations


async def generate(state: QAState, config: RunnableConfig) -> dict:
    """基于已授权父块流式生成答案，并在结束后返回完整答案和 citation。"""

    contexts = state.get("parent_contexts") or []
    if not contexts:
        # 图的 ACL 分支正常不会走到这里；这是防御式兜底，保证被误调用时也绝不调模型（P5）。
        logger.error("generate 在无 parent_contexts 时被调用，已安全拒绝")
        return {
            "answer": GAP_ANSWER,
            "citations": [],
            "answer_status": "gap",
            "prompt_tokens": state.get("prompt_tokens", 0),
            "completion_tokens": state.get("completion_tokens", 0),
        }

    deps = deps_of(config)
    chat = build_chat_model(deps.model_config, streaming=True)
    writer = _stream_writer()
    fragments: list[str] = []
    generated_prompt_tokens = generated_completion_tokens = 0

    try:
        # aclosing：流中途抛错（网关断流、超时）时也确定关闭底层响应，
        # 否则连接要等 GC 才释放，长跑的服务会积住连接
        async with aclosing(
            chat.astream(
                [
                    SystemMessage(content=ANSWER_SYSTEM),
                    HumanMessage(
                        content=ANSWER_USER.format(
                            context=_context_text(contexts), question=state["question"]
                        )
                    ),
                ]
            )
        ) as stream:
            async for chunk in stream:
                usage_prompt, usage_completion = _usage_counts(chunk)
                generated_prompt_tokens = max(generated_prompt_tokens, usage_prompt)
                generated_completion_tokens = max(
                    generated_completion_tokens, usage_completion
                )
                text = _chunk_text(chunk)
                if not text:
                    continue
                fragments.append(text)
                if writer is not None:
                    writer({"type": "token", "delta": text})
    except Exception as exc:  # noqa: BLE001 - 统一为前端可处理的上游错误
        logger.warning("答案生成调用失败：%s", exc)
        raise AppError.ai_upstream() from exc

    return {
        "answer": "".join(fragments),
        "citations": await _citations(contexts, config),
        "answer_status": "answered",
        "prompt_tokens": state.get("prompt_tokens", 0) + generated_prompt_tokens,
        "completion_tokens": state.get("completion_tokens", 0) + generated_completion_tokens,
    }


async def respond_denied(state: QAState, config: RunnableConfig) -> dict:
    """仅命中无权资料时的固定拒答。严禁读取 denied 标题/正文或调用 Chat。"""

    del state, config
    return {"answer": DENIED_ANSWER, "citations": [], "answer_status": "denied"}


async def respond_gap(state: QAState, config: RunnableConfig) -> dict:
    """无可用上下文时记录知识缺口并返回固定文案，不调用 Chat。"""

    deps = deps_of(config)
    await faq_repo.record_gap(
        deps.session,
        question=state["question"],
        department_id=deps.subject.department_id,
        max_similarity=state.get("max_similarity", 0.0),
    )
    # Gap 是独立的运营信号，不能等审计节点才提交；问答随后失败也不应丢掉用户的补库需求。
    await deps.session.commit()
    return {
        "answer": GAP_ANSWER,
        "citations": [],
        "answer_status": "gap",
        "prompt_tokens": state.get("prompt_tokens", 0),
        "completion_tokens": state.get("completion_tokens", 0),
    }
