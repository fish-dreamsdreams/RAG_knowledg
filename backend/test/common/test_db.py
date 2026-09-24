"""worker 会话与事件循环的绑定关系（真机自检抓到的并发导入缺陷）。

`test_ingest_pipeline.py` 那条「连续跑两次 Celery 任务」的用例抓不到这个缺陷：模块级引擎的
连接池只在**第一次**建连时用 exec-once 锁，顺序任务第二次根本碰不到锁。一批导入并发投递时，
两个新事件循环会同时撞上那把锁，后到的一方报
`RuntimeError: <asyncio.locks.Lock ...> is bound to a different event loop`，整批任务失败。

这里不碰业务表，只验证机制：每个任务必须拿到**自己的引擎**，且并发也各连各的。
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.common.db import (
    is_transient_conflict,
    run_with_conflict_retry,
    worker_session,
)


class _FakeSession:
    """只记帐 `rollback()`：冲突后事务已被服务端中止，不回滚就不能继续用这条连接。"""

    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


def _db_error(orig: BaseException) -> DBAPIError:
    return DBAPIError("DELETE FROM knowledge_units", None, orig)


class _DeadlockWithoutSqlstate(Exception):
    """有些包装会把 SQLSTATE 吃掉，此时只能按类名认。"""


_DeadlockWithoutSqlstate.__name__ = "DeadlockDetectedError"


class _SerializationFailureBySqlstate(Exception):
    sqlstate = "40001"


@pytest.mark.parametrize(
    "error",
    [
        _db_error(asyncpg.exceptions.DeadlockDetectedError("deadlock detected")),
        _db_error(_DeadlockWithoutSqlstate("deadlock detected")),
        _db_error(_SerializationFailureBySqlstate("could not serialize access")),
    ],
)
def test_is_transient_conflict_accepts_deadlock_and_serialization(error) -> None:
    assert is_transient_conflict(error) is True


def test_is_transient_conflict_rejects_other_errors() -> None:
    """约束冲突、连接断开、普通异常重试没有意义，必须原样抛出去。"""

    class _UniqueViolation(Exception):
        sqlstate = "23505"

    assert is_transient_conflict(_db_error(_UniqueViolation("duplicate key"))) is False
    assert is_transient_conflict(ValueError("not a db error")) is False
    assert is_transient_conflict(_db_error(Exception("boom"))) is False


async def test_run_with_conflict_retry_replays_after_deadlock() -> None:
    """死锁回滚后重放：第三次成功，前面两次各回滚一次。"""

    session = _FakeSession()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _db_error(asyncpg.exceptions.DeadlockDetectedError("deadlock detected"))
        return "deleted"

    result = await run_with_conflict_retry(
        session, operation, attempts=3, base_delay=0  # type: ignore[arg-type]
    )

    assert result == "deleted"
    assert calls == 3
    assert session.rollbacks == 2


async def test_run_with_conflict_retry_gives_up_after_attempts() -> None:
    """一直冲突就把最后一次异常抛出去，不无限转圈。"""

    session = _FakeSession()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise _db_error(asyncpg.exceptions.DeadlockDetectedError("deadlock detected"))

    with pytest.raises(DBAPIError):
        await run_with_conflict_retry(
            session, operation, attempts=2, base_delay=0  # type: ignore[arg-type]
        )

    assert calls == 2
    assert session.rollbacks == 1


async def test_run_with_conflict_retry_does_not_retry_other_errors() -> None:
    """非瞬时错误一次都不重试（重试只会把同一个错误再犯一遍）。"""

    session = _FakeSession()

    async def operation() -> None:
        raise IntegrityError("INSERT INTO knowledge_units", None, Exception("duplicate"))

    with pytest.raises(IntegrityError):
        await run_with_conflict_retry(session, operation, base_delay=0)  # type: ignore[arg-type]

    assert session.rollbacks == 0


def _run_task_in_new_loop(bucket: list[Any]) -> None:
    """在一个干净的事件循环里跑一次任务会话，等价于 Celery 任务里的 `asyncio.run`。"""

    async def _run() -> None:
        async with worker_session() as session:
            await session.execute(text("select 1"))
            bucket.append(session.get_bind())

    asyncio.run(_run())


async def test_worker_session_binds_a_fresh_engine_per_task() -> None:
    """一任务一引擎：两个任务不能共用引擎（共用就共用连接池里的 exec-once 锁）。"""

    buckets: list[list[Any]] = [[], []]
    for bucket in buckets:
        await asyncio.to_thread(_run_task_in_new_loop, bucket)

    assert buckets[0][0] is not buckets[1][0]


async def test_worker_session_survives_parallel_tasks() -> None:
    """并发（worker 默认 `--concurrency=4`）时每个任务各自建连，互不干扰。"""

    buckets: list[list[Any]] = [[], [], [], []]

    await asyncio.gather(
        *(asyncio.to_thread(_run_task_in_new_loop, bucket) for bucket in buckets)
    )

    binds = [bucket[0] for bucket in buckets]
    assert len(set(map(id, binds))) == len(buckets)
