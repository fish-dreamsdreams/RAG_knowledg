"""图运行期的外部依赖（tasklist 12.2+）。

节点不自己开 session、不自己 new Redis 客户端：这些一律从 `RunnableConfig.configurable`
取。两个理由：

1. State 会被 LangGraph 增量合并、并随 `astream` 推进前端，绝不能混入连接对象。
2. 单测可以把假 session、假 Redis、假配置塞进 `configurable`，节点逻辑因此完全可测——
   不必起数据库，也不会加载模型。

`AclSubject` 也在这里：一次问答的权限判定主体在整张图里是固定的，随依赖传入比每个节点
各拼一次更不容易出错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter

from langchain_core.runnables import RunnableConfig
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines.acl import AclSubject
from app.engines.faq_cache import FaqHit  # noqa: F401 - 供节点注解用
from app.models import ModelConfig

DEPS_KEY = "deps"


@dataclass
class GraphDeps:
    session: AsyncSession
    redis: Redis
    # 模型名与三个阈值都来自这张单行配置表，节点不重复查库
    model_config: ModelConfig
    subject: AclSubject
    session_id: str | None = None
    # 由 WebSocket/HTTP 入口构建依赖时记录；audit 用它计算端到端延迟
    started_at: float = field(default_factory=perf_counter)


def deps_of(config: RunnableConfig) -> GraphDeps:
    """取依赖。缺失即编程错误，直接抛而不是给默认值——默认值会让"忘了注入"变成静默错误。"""

    configurable = config.get("configurable") or {}
    deps = configurable.get(DEPS_KEY)
    if not isinstance(deps, GraphDeps):
        raise RuntimeError("qa_graph 缺少运行期依赖：请在 config.configurable 注入 GraphDeps")
    return deps


__all__ = ["DEPS_KEY", "GraphDeps", "deps_of"]
