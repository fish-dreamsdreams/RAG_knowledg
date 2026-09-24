"""种子数据加载器：部门、权限码、角色、演示账号与模型配置。

运行：在 backend/ 目录执行 `python -m app.services.seed`
幂等：按自然键（部门名、权限码、角色码、用户名）更新或创建。
演示账号口令写在 data/seeds/users.json，仅用于演示，加载时转为 bcrypt 哈希入库。
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.db import SessionLocal
from app.common.security import hash_password
from app.models import (
    Department,
    ModelConfig,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parents[3] / "data" / "seeds"


def _load(name: str) -> list[dict[str, Any]]:
    return json.loads((SEED_DIR / name).read_text(encoding="utf-8"))


async def _seed_departments(session: AsyncSession) -> dict[str, Department]:
    departments: dict[str, Department] = {}
    for row in _load("departments.json"):
        existing = (
            await session.execute(select(Department).where(Department.name == row["name"]))
        ).scalar_one_or_none()
        if existing is None:
            existing = Department(name=row["name"])
            session.add(existing)
            await session.flush()
        departments[row["key"]] = existing

    # 父节点必须在拿到 id 之后再设置
    for row in _load("departments.json"):
        parent_key = row.get("parent_key")
        parent = departments.get(parent_key) if parent_key else None
        department = departments[row["key"]]
        if department.parent_id != (parent.id if parent else None):
            department.parent_id = parent.id if parent else None
    await session.flush()
    return departments


async def _seed_permissions(session: AsyncSession) -> dict[str, Permission]:
    permissions: dict[str, Permission] = {}
    for row in _load("permissions.json"):
        existing = (
            await session.execute(select(Permission).where(Permission.code == row["code"]))
        ).scalar_one_or_none()
        if existing is None:
            existing = Permission(code=row["code"])
            session.add(existing)
        existing.name = row["name"]
        existing.type = row["type"]
        existing.sort = row["sort"]
        await session.flush()
        permissions[row["code"]] = existing

    for row in _load("permissions.json"):
        parent_code = row.get("parent_code")
        permission = permissions[row["code"]]
        permission.parent_id = (
            permissions[parent_code].id if parent_code else None
        )
    await session.flush()
    return permissions


async def _seed_roles(
    session: AsyncSession, permissions: dict[str, Permission]
) -> dict[str, Role]:
    all_codes = list(permissions.keys())
    roles: dict[str, Role] = {}
    for row in _load("roles.json"):
        existing = (
            await session.execute(select(Role).where(Role.code == row["code"]))
        ).scalar_one_or_none()
        if existing is None:
            existing = Role(code=row["code"])
            session.add(existing)
        existing.name = row["name"]
        existing.description = row.get("description")
        await session.flush()
        roles[row["code"]] = existing

        wanted = all_codes if "*" in row["permissions"] else row["permissions"]
        await session.execute(
            RolePermission.__table__.delete().where(
                RolePermission.role_id == existing.id
            )
        )
        for code in wanted:
            session.add(RolePermission(role_id=existing.id, permission_id=permissions[code].id))
    await session.flush()
    return roles


async def _seed_users(
    session: AsyncSession,
    departments: dict[str, Department],
    roles: dict[str, Role],
) -> int:
    created = 0
    for row in _load("users.json"):
        existing = (
            await session.execute(select(User).where(User.username == row["username"]))
        ).scalar_one_or_none()
        if existing is None:
            existing = User(
                username=row["username"],
                password_hash=hash_password(row["password"]),
            )
            session.add(existing)
            created += 1
        existing.display_name = row["display_name"]
        existing.department_id = departments[row["department_key"]].id
        existing.is_active = True
        existing.is_deleted = False
        existing.deleted_at = None
        await session.flush()

        await session.execute(
            UserRole.__table__.delete().where(UserRole.user_id == existing.id)
        )
        for role_code in row["roles"]:
            session.add(UserRole(user_id=existing.id, role_id=roles[role_code].id))
    await session.flush()
    return created


async def _seed_model_config(session: AsyncSession) -> bool:
    existing = (await session.execute(select(ModelConfig).limit(1))).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(
        ModelConfig(
            base_url="",
            chat_model="",
            rerank_model="BAAI/bge-reranker-v2-m3",
            faq_sim_threshold=0.88,
            gap_sim_threshold=0.35,
            faq_cluster_min_freq=3,
        )
    )
    await session.flush()
    return True


async def run_seed() -> dict[str, int]:
    async with SessionLocal() as session:
        departments = await _seed_departments(session)
        permissions = await _seed_permissions(session)
        roles = await _seed_roles(session, permissions)
        users_created = await _seed_users(session, departments, roles)
        config_created = await _seed_model_config(session)
        await session.commit()

    summary = {
        "departments": len(departments),
        "permissions": len(permissions),
        "roles": len(roles),
        "users_created": users_created,
        "model_config_created": int(config_created),
    }
    logger.info("种子加载完成: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(run_seed()))
