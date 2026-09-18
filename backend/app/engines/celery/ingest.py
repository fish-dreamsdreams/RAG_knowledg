"""导入任务入口（tasklist 6.2 / 6.3）。

Celery 任务是**同步函数**（worker 用 `--pool=threads`），内部用 `asyncio.run` 驱动异步
流水线；会话用 `worker_session()`（一任务一引擎 + NullPool），避免连接与连接池的锁被绑在
已关闭或别人的事件循环上（`app/common/db.py` 里解释了为什么要一任务一引擎）。

任务函数不向 broker 抛异常：失败已在流水线里收敛为 `parse_status=failed` + 清理，
重试是管理员的手工动作（任务 7.4），不做自动重试——重复投递对长任务代价过高。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from app.common.db import worker_session
from app.engines.celery.celery_app import celery_app
from app.services.ingest import run_ingest

logger = logging.getLogger(__name__)


@celery_app.task(name="ingest.ingest_unit")
def ingest_unit(unit_id: str, task_id: str | None = None) -> dict:
    """解析 → 切块 → 编码 → 写 Milvus 的完整流水线。"""

    try:
        return asyncio.run(_ingest_unit(unit_id, task_id))
    except Exception:  # noqa: BLE001 - 兜底：任务函数不应把异常抛回 broker
        logger.exception("导入任务异常退出：unit=%s task=%s", unit_id, task_id)
        return {"unit_id": unit_id, "task_id": task_id, "status": "failed"}


async def _ingest_unit(unit_id: str, task_id: str | None) -> dict:
    async with worker_session() as session:
        outcome = await run_ingest(session, UUID(unit_id), task_id=task_id)

    logger.info(
        "导入结束：unit=%s status=%s chunks=%s", outcome.unit_id, outcome.status, outcome.chunks
    )
    return {
        "unit_id": str(outcome.unit_id),
        "task_id": task_id,
        "status": outcome.status,
        "chunks": outcome.chunks,
        "vectors": outcome.vectors,
        "error": outcome.error,
    }
