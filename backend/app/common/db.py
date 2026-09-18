"""异步数据库引擎与会话依赖。"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.common.config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# 死锁与序列化失败是**瞬时**冲突：Postgres 会挑一方回滚，换个时机重放就能成，重试是官方
# 推荐的应对。按 SQLSTATE 判定（40P01 死锁 / 40001 序列化失败），不认驱动的异常类名。
_TRANSIENT_CONFLICT_SQLSTATES = frozenset({"40P01", "40001"})
# 少数包装会把 SQLSTATE 吃掉，此时退化成按类名认（asyncpg 之外还有 psycopg 等驱动）
_TRANSIENT_CONFLICT_NAMES = frozenset(
    {"DeadlockDetectedError", "SerializationError", "SerializationFailure"}
)

_T = TypeVar("_T")


def is_transient_conflict(exc: BaseException) -> bool:
    """异常是不是「重放一次就能成」的并发冲突（死锁 / 序列化失败）。

    只认 SQLSTATE 与驱动异常类名：约束冲突、连接断开这类错误重试没有意义，必须原样抛出去。
    """

    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate in _TRANSIENT_CONFLICT_SQLSTATES:
        return True
    return type(orig).__name__ in _TRANSIENT_CONFLICT_NAMES


async def run_with_conflict_retry(
    session: AsyncSession,
    operation: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.05,
) -> _T:
    """跑一段自带事务边界的写操作，遇死锁/序列化失败就回滚重放。

    `operation` 必须自己 `commit()`：重试是「整段回滚后重放」，半提交的状态没法续写，
    所以它内部的每一步都要能在干净事务里重头再来（幂等或纯写）。

    重试次数用尽仍冲突就把异常原样抛出，由调用方决定对外怎么说（大多应该转成 503）。
    """

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except DBAPIError as exc:
            if attempt >= attempts or not is_transient_conflict(exc):
                raise
            logger.warning(
                "写库撞上并发冲突（第 %s/%s 次），回滚重试：%s",
                attempt,
                attempts,
                type(getattr(exc, "orig", exc)).__name__,
            )
            # 冲突后事务已被服务端中止，必须回滚才能继续用这条连接
            await session.rollback()
            await asyncio.sleep(base_delay * attempt)
    raise AssertionError("重试循环不可达")  # pragma: no cover


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """Celery 任务专用会话：**一次任务一个引擎**，用完 `dispose()`。

    模块级引擎在 worker 里不能用。`asyncio.run` 每个任务新建事件循环，而引擎内部有绑在
    「首次使用它的事件循环」上的 asyncio 原语：连接池的 exec-once 锁（`AsyncAdaptedLock`）
    与每次新建的连接。NullPool 只挡住了连接复用，挡不住那把锁——同一批导入并发跑两个任务时
    两者会争用同一把锁，后到的一方直接失败：
    `RuntimeError: <asyncio.locks.Lock ...> is bound to a different event loop`
    （真机现象：一批导入里同时投递的任务全挂，事后单条重投却成功）。所以引擎也必须一任务一个。
    导入是分钟级长任务，建连与 dispose 的开销可忽略。
    """

    task_engine = create_async_engine(
        settings.database_url, poolclass=NullPool, pool_pre_ping=True, future=True
    )
    try:
        session_factory = async_sessionmaker(
            task_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with session_factory() as session:
            yield session
    finally:
        await task_engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
