"""Redis 客户端（缓存与 Celery 共用实例，不同 db）。"""

from functools import lru_cache

from redis.asyncio import Redis

from app.common.config import settings


@lru_cache
def get_redis() -> Redis:
    """进程级单例，**只能用在长生命周期的事件循环里**（API 进程、FastAPI 依赖）。

    它是 asyncio 客户端，连接池里的连接会绑在首次使用时的事件循环上。Celery 任务每次
    `asyncio.run` 都新建事件循环，复用这个单例会报
    `got Future attached to a different loop`（现象：worker 进程里只有第一次导入成功）。
    worker 侧请用 `new_redis()`。
    """

    return Redis.from_url(settings.redis_url, decode_responses=True)


def new_redis() -> Redis:
    """新建独立客户端，供「每个任务一个新事件循环」的 Celery 任务使用。

    调用方负责 `await client.aclose()`；导入任务是分钟级长任务，建连开销可忽略。
    """

    return Redis.from_url(settings.redis_url, decode_responses=True)
