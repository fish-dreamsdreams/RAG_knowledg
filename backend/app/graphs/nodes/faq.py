"""FAQ 命中与应答节点（tasklist 12.2，TECH_SPEC §8.1）。

`faq_match` 命中即短路：**不检索、不改写、不调 Chat**（P4）。这是 FAQ 缓存存在的全部意义
——省掉一次改写调用、三路召回与一次重排。

`respond_faq` 的 `citations` 恒为空：FAQ 是审核过的独立答案，不指向任何知识单元。硬造一条
引用反而会让用户以为答案出自某份文档，而实际上它出自人工审核后的沉淀。
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from starlette.concurrency import run_in_threadpool

from app.engines.faq_cache import match as match_faq
from app.engines.embed import get_embedder
from app.graphs.context import deps_of
from app.graphs.state import QAState

logger = logging.getLogger(__name__)


def _encode(question: str) -> list[float]:
    """同步编码。**必须**经线程池调用：M3 前向是阻塞的 GPU 推理。"""

    embedding = get_embedder().encode([question], return_sparse=False)
    return [float(value) for value in embedding.dense[0]]


async def faq_match(state: QAState, config: RunnableConfig) -> dict:
    """查 FAQ 缓存。Redis 不可用时 `match` 返回未命中，问答照常走检索链。"""

    deps = deps_of(config)
    question = state["question"]
    vector = await run_in_threadpool(_encode, question)

    hit = await match_faq(
        deps.redis,
        question,
        vector,
        threshold=deps.model_config.faq_sim_threshold,
    )
    if hit is None:
        return {"faq_hit": False}

    logger.info("FAQ 命中：faq_id=%s matched_by=%s", hit.faq_id, hit.matched_by)
    return {
        "faq_hit": True,
        "faq_answer": hit.answer,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


async def respond_faq(state: QAState, config: RunnableConfig) -> dict:
    """命中 FAQ 的出口。不调 Chat，因此不产生 token 消耗。"""

    del config  # 出口不需要外部依赖
    return {"answer": state.get("faq_answer") or "", "citations": [], "answer_status": "answered"}
