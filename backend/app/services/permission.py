"""用户权限上下文：角色权限码并集 + 直属部门（数据权限不继承子部门，PRD 冻结项）。

`kb:perm:user:{id}` 缓存只加速，权威源永远是 PostgreSQL（TECH_SPEC §5.5）。
Redis 不可用时降级回库并打日志：缓存故障不应该把所有人挡在门外。
"""

import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.redis import get_redis
from app.models import User
from app.repositories import org as org_repo

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300


def cache_key(user_id: uuid.UUID | str) -> str:
    return f"kb:perm:user:{user_id}"


@dataclass(frozen=True)
class PermissionContext:
    department_id: uuid.UUID
    role_ids: list[uuid.UUID]
    permissions: list[str]
    # 角色码给前端用（登录后默认落点、身份展示），权限判定只认 permissions
    role_codes: list[str] = field(default_factory=list)

    def to_cache(self) -> str:
        return json.dumps(
            {
                "dept_id": str(self.department_id),
                "role_ids": [str(role_id) for role_id in self.role_ids],
                "perm_codes": self.permissions,
                "role_codes": self.role_codes,
            }
        )

    @classmethod
    def from_cache(cls, raw: str) -> "PermissionContext":
        payload = json.loads(raw)
        return cls(
            department_id=uuid.UUID(payload["dept_id"]),
            role_ids=[uuid.UUID(role_id) for role_id in payload["role_ids"]],
            permissions=list(payload["perm_codes"]),
            # 旧版本缓存没有这个键：缺就当成空，等 TTL 自然换新，不做全局失效
            role_codes=list(payload.get("role_codes") or []),
        )


async def build_context(session: AsyncSession, user: User) -> PermissionContext:
    """从数据库装载权限上下文。"""

    role_ids = await org_repo.list_role_ids_for_user(session, user.id)
    permissions = await org_repo.list_permission_codes_for_roles(session, role_ids)
    role_codes = await org_repo.list_role_codes_for_user(session, user.id)
    return PermissionContext(
        department_id=user.department_id,
        role_ids=role_ids,
        permissions=permissions,
        role_codes=role_codes,
    )


async def get_context(session: AsyncSession, user: User) -> PermissionContext:
    """读缓存，未命中回库并写回缓存。"""

    redis = get_redis()
    key = cache_key(user.id)
    try:
        cached = await redis.get(key)
        if cached:
            return PermissionContext.from_cache(cached)
    except Exception:  # noqa: BLE001 - 缓存故障不阻断鉴权
        logger.warning("权限缓存读取失败，回退数据库 user_id=%s", user.id)

    context = await build_context(session, user)
    try:
        await redis.set(key, context.to_cache(), ex=CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("权限缓存写入失败 user_id=%s", user.id)
    return context


async def invalidate_user(user_id: uuid.UUID) -> None:
    """改用户（部门/角色/停用）后主动删除该键（TECH_SPEC §5.5）。"""

    try:
        await get_redis().delete(cache_key(user_id))
    except Exception:  # noqa: BLE001
        logger.warning("权限缓存失效失败 user_id=%s", user_id)


async def invalidate_users(user_ids: list[uuid.UUID]) -> None:
    if not user_ids:
        return
    try:
        await get_redis().delete(*[cache_key(user_id) for user_id in user_ids])
    except Exception:  # noqa: BLE001
        logger.warning("权限缓存批量失效失败 count=%s", len(user_ids))
