"""Celery 应用（tasklist 6.2）。

broker 与 result backend 都用 Redis（`CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`）。

启动 worker（**必须用线程池**）：

```
celery -A app.engines.celery.celery_app:celery_app worker --pool=threads --concurrency=$INGEST_CONCURRENCY -l info
```

为什么是线程池：导入任务既等 MinerU（IO）又要跑本地 BGE-M3（GPU）。默认的 prefork
进程池会让每个子进程各自加载一份 M3 权重，显存翻 N 倍；线程池共享进程内的模型单例，
`Embedder` 内部已用锁串行化推理，不会并发抢显存。
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready

from app.common.config import settings

celery_app = Celery(
    "knowledge_base",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.engines.celery.ingest", "app.engines.celery.mine"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    # 长任务：worker 崩溃/重启时任务不会凭空消失
    task_acks_late=True,
    # 每个 worker 一次只取一个任务，避免长任务排在别人后面等
    worker_prefetch_multiplier=1,
    # 任务结果保留 1 小时，够前端轮询 import-status
    result_expires=3600,
    # acks_late + Redis broker：任务执行超过 visibility_timeout 会被重投（重复执行）。
    # 导入链最长约 15 min（MinerU 上限 900s）+ 编码时间，这里放宽到 2h。
    broker_transport_options={"visibility_timeout": 7200},
    # 注意：--pool=threads 时 Celery 的软/硬超时不会被强制中断，
    # 真正的超时由流水线内部保证（MinerU 轮询有总时限）。这里设值是为了将来切回
    # prefork 池时仍有一道兜底。
    task_soft_time_limit=1500,
    task_time_limit=1800,
    # 每日 03:00 挖掘 FAQ 候选（tasklist 11.4）。任务幂等，重复触发只会刷新频次。
    beat_schedule={
        "faq-mine-daily": {
            "task": "faq.mine_candidates",
            "schedule": crontab(hour=3, minute=0),
        },
    },
)


@worker_ready.connect
def _warmup_embedder(**_kwargs: object) -> None:
    """worker 起来就加载 M3：任务线程共享进程内单例，预热一次全体受益。

    导入放在函数内：`services.knowledge` 投递任务时会 import 本模块，若在模块顶部
    导入 embed，API 进程会因此带出 torch 与模型依赖（TECH_SPEC §8.0）。
    """

    if not settings.embed_warmup_enabled:
        return

    from app.engines.embed import get_embedder

    get_embedder().warmup()
