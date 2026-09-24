"""FAQ 挖掘任务入口（tasklist 11.4）。

Celery 任务是**同步函数**（worker 用 `--pool=threads`），内部用 `asyncio.run` 驱动异步用例；
会话用 `worker_session()`（一任务一引擎 + NullPool），理由见 `app/common/db.py`。核心逻辑在
`services/faq_mining.py`，本文件只做调度与结果收敛（与 `ingest.py` 同一分工）。

触发方式：Beat 每日 03:00 自动跑（`celery_app.beat_schedule`），或 `POST /api/v1/faq/mine`
手动触发。任务幂等，重复执行只会刷新候选的频次与同义问法。
"""

from __future__ import annotations

import asyncio
import logging

from app.common.db import worker_session
from app.engines.celery.celery_app import celery_app
from app.services.faq_mining import run_mining

logger = logging.getLogger(__name__)


@celery_app.task(name="faq.mine_candidates")
def mine_candidates(limit: int = 200) -> dict:
    """扫描未命中 FAQ 的问题并归并出候选。"""

    try:
        return asyncio.run(_mine_candidates(limit))
    except Exception:  # noqa: BLE001 - 任务函数不应把异常抛回 broker
        logger.exception("FAQ 挖掘任务异常退出：limit=%s", limit)
        return {"status": "failed", "limit": limit}


async def _mine_candidates(limit: int) -> dict:
    async with worker_session() as session:
        result = await run_mining(session, limit=limit)

    return {
        "status": "ok",
        "limit": limit,
        "scanned": result.scanned,
        "clusters": result.clusters,
        "created": result.created,
        "updated": result.updated,
        "skipped_settled": result.skipped_settled,
        "below_min_freq": result.below_min_freq,
    }
