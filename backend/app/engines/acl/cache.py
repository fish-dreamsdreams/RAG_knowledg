"""ACL 快照的 Redis 缓存（tasklist 8.2，P11）。

key：`kb:acl:unit:{unit_id}`，value 是权限快照的 JSON，TTL 10 分钟。

保存 ACL 时必须主动调用 `invalidate` / `invalidate_many`，不能只靠过期——否则刚收回的
权限在 TTL 内仍然放行，等于权限变更延迟生效（P11 要求主动删）。
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from app.engines.acl.types import UnitAcl

logger = logging.getLogger(__name__)

ACL_KEY_PREFIX = "kb:acl:unit:"
# 快照 TTL：10 分钟。调权限后靠主动删失效，TTL 只是兜底
ACL_TTL_SECONDS = 600


def unit_key(unit_id: UUID | str) -> str:
    return f"{ACL_KEY_PREFIX}{unit_id}"


def dump(acl: UnitAcl) -> str:
    """序列化快照；集合排序后输出，保证同一份权限产生同一个字符串。"""

    return json.dumps(
        {
            "acl_global": acl.acl_global,
            "departments": sorted(str(item) for item in acl.departments),
            "roles": sorted(str(item) for item in acl.roles),
            "users": sorted(str(item) for item in acl.users),
        },
        ensure_ascii=False,
    )


def load(raw: str) -> UnitAcl | None:
    """反序列化；内容损坏或格式不符时返回 None，由调用方回源数据库。"""

    try:
        data = json.loads(raw)
        return UnitAcl(
            acl_global=bool(data["acl_global"]),
            departments=frozenset(UUID(item) for item in data["departments"]),
            roles=frozenset(UUID(item) for item in data["roles"]),
            users=frozenset(UUID(item) for item in data["users"]),
        )
    except (ValueError, KeyError, TypeError):
        logger.warning("ACL 缓存内容不可解析，按未命中处理", exc_info=True)
        return None
