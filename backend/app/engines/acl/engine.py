"""四维权限引擎（tasklist 8.1 / 8.2，design.md §2.5）。

判定入口是 `filter`：拿一批 `unit_ids`，返回 `(allowed, denied)`。检索链在排序之后、
拼上下文之前调用它，把无权单元从候选里摘掉（P9：未授权不可检索、也不可作答）。

读取路径是**缓存优先**：`kb:acl:unit:{id}`（TTL 10min）→ 未命中回 PostgreSQL → 回填。
保存 ACL 的接口必须调用 `invalidate`，靠 TTL 过期等于权限变更延迟生效（P11）。

`loader` 与 `redis_factory` 可注入，测试能完全脱离 PostgreSQL 与 Redis。默认 Redis 取
`get_redis()`（API / WS 进程的长生命周期循环）；Celery 任务若要用本引擎，请注入
`new_redis` 并自行关闭，理由见 `app/common/redis.py`。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.redis import get_redis
from app.engines.acl import cache
from app.engines.acl.types import (
    PRINCIPAL_DEPARTMENT,
    PRINCIPAL_ROLE,
    PRINCIPAL_TYPES,
    PRINCIPAL_USER,
    AclSubject,
    UnitAcl,
    allows,
)
from app.models.knowledge import KnowledgeUnit, KnowledgeUnitAcl

logger = logging.getLogger(__name__)

# 注入用的加载器：给出 unit_ids，返回它们的权限快照（查不到的单元不出现在结果里）
Loader = Callable[
    [AsyncSession | None, Sequence[UUID]], Awaitable[dict[UUID, UnitAcl]]
]
RedisFactory = Callable[[], Redis]


class AclEngine:
    """四维权限判定的门面。"""

    def __init__(
        self,
        *,
        loader: Loader | None = None,
        redis_factory: RedisFactory | None = None,
    ) -> None:
        self._loader = loader
        self._redis_factory = redis_factory

    # ---------- 判定 ----------

    async def check(
        self, subject: AclSubject, unit_id: UUID, *, session: AsyncSession | None = None
    ) -> bool:
        """单单元判定。"""

        allowed, _ = await self.filter(subject, [unit_id], session=session)
        return bool(allowed)

    async def filter(
        self,
        subject: AclSubject,
        unit_ids: Sequence[UUID],
        *,
        session: AsyncSession | None = None,
    ) -> tuple[list[UUID], list[UUID]]:
        """批量判定，返回 `(allowed, denied)`。

        两者互斥，并集等于**去重后**的入参（保持首次出现顺序）。查不到快照的 unit_id
        归入 denied：宁可少给，也不能因为查不到而放行（fail-closed）。
        """

        unique = list(dict.fromkeys(unit_ids))
        snapshot = await self.snapshot(unique, session=session)

        allowed: list[UUID] = []
        denied: list[UUID] = []
        for unit_id in unique:
            if allows(snapshot.get(unit_id), subject):
                allowed.append(unit_id)
            else:
                denied.append(unit_id)
        return allowed, denied

    # ---------- 快照 ----------

    async def snapshot(
        self, unit_ids: Sequence[UUID], *, session: AsyncSession | None = None
    ) -> dict[UUID, UnitAcl]:
        """取多个单元的权限快照：缓存优先，未命中的回源数据库并回填。"""

        unique = list(dict.fromkeys(unit_ids))
        if not unique:
            return {}

        redis = self._redis()
        cached: dict[UUID, UnitAcl] = {}
        missing: list[UUID] = list(unique)

        if redis is not None:
            raw_values = await self._mget(redis, unique)
            missing = []
            for unit_id, raw in zip(unique, raw_values, strict=True):
                parsed = cache.load(raw) if raw else None
                if parsed is None:
                    missing.append(unit_id)
                else:
                    cached[unit_id] = parsed

        if not missing:
            return cached

        loaded = await self._load(session, missing)
        if redis is not None and loaded:
            await self._store(redis, loaded)
        cached.update(loaded)
        return cached

    # ---------- 失效（P11） ----------

    async def invalidate(self, unit_id: UUID) -> None:
        """删掉一个单元的快照；保存它的 ACL 之后必须调用。"""

        await self.invalidate_many([unit_id])

    async def invalidate_many(self, unit_ids: Sequence[UUID]) -> None:
        """批量删快照（例如批量调整权限、删除知识单元时）。"""

        unique = list(dict.fromkeys(unit_ids))
        if not unique:
            return
        redis = self._redis()
        if redis is None:
            return
        try:
            await redis.delete(*[cache.unit_key(item) for item in unique])
        except Exception:  # noqa: BLE001 - 缓存删不掉不该让保存权限失败
            logger.warning("ACL 快照失效失败：%s", unique, exc_info=True)

    # ---------- 内部 ----------

    def _redis(self) -> Redis | None:
        if self._redis_factory is not None:
            return self._redis_factory()
        return get_redis()

    async def _mget(self, redis: Redis, unit_ids: Sequence[UUID]) -> list[str | None]:
        """读缓存；Redis 不可用时按全部未命中处理，绝不让问答失败。"""

        try:
            values = await redis.mget([cache.unit_key(item) for item in unit_ids])
        except Exception:  # noqa: BLE001
            logger.warning("ACL 缓存读取失败，全部回源数据库", exc_info=True)
            return [None] * len(unit_ids)
        return list(values)

    async def _store(self, redis: Redis, snapshot: dict[UUID, UnitAcl]) -> None:
        """回填缓存；不缓存"不存在的单元"，避免把穿透结果固化下来。"""

        try:
            async with redis.pipeline(transaction=False) as pipe:
                for unit_id, acl in snapshot.items():
                    pipe.set(
                        cache.unit_key(unit_id),
                        cache.dump(acl),
                        ex=cache.ACL_TTL_SECONDS,
                    )
                await pipe.execute()
        except Exception:  # noqa: BLE001 - 回填失败只影响下次性能
            logger.warning("ACL 快照回填失败", exc_info=True)

    async def _load(
        self, session: AsyncSession | None, unit_ids: Sequence[UUID]
    ) -> dict[UUID, UnitAcl]:
        """回源数据库：一次查询取回 `acl_global` 与全部 principal。"""

        if self._loader is not None:
            return await self._loader(session, unit_ids)
        if session is None:
            raise ValueError("未注入 loader 时必须提供 session 才能回源数据库")

        rows = await session.execute(
            select(
                KnowledgeUnit.id,
                KnowledgeUnit.acl_global,
                KnowledgeUnitAcl.principal_type,
                KnowledgeUnitAcl.principal_id,
            )
            .select_from(KnowledgeUnit)
            .outerjoin(KnowledgeUnitAcl, KnowledgeUnitAcl.unit_id == KnowledgeUnit.id)
            .where(KnowledgeUnit.id.in_(unit_ids))
        )

        flags: dict[UUID, bool] = {}
        principal_ids: dict[UUID, dict[str, set[UUID]]] = {}
        for unit_id, acl_global, principal_type, principal_id in rows:
            flags[unit_id] = bool(acl_global)
            if principal_type not in PRINCIPAL_TYPES or principal_id is None:
                continue
            bucket = principal_ids.setdefault(
                unit_id, {item: set() for item in PRINCIPAL_TYPES}
            )
            bucket[principal_type].add(principal_id)

        return {
            unit_id: UnitAcl(
                acl_global=acl_global,
                departments=frozenset(
                    principal_ids.get(unit_id, {}).get(PRINCIPAL_DEPARTMENT, ())
                ),
                roles=frozenset(
                    principal_ids.get(unit_id, {}).get(PRINCIPAL_ROLE, ())
                ),
                users=frozenset(
                    principal_ids.get(unit_id, {}).get(PRINCIPAL_USER, ())
                ),
            )
            for unit_id, acl_global in flags.items()
        }
