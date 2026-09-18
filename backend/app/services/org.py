"""组织架构用例：事务编排、校验与权限缓存失效。"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import AppError
from app.common.security import hash_password
from app.models import Department, Role, User
from app.repositories import org as repo
from app.services.permission import invalidate_user, invalidate_users


async def ensure_department(session: AsyncSession, department_id: uuid.UUID) -> Department:
    department = await repo.get_department(session, department_id)
    if department is None:
        raise AppError.not_found("部门不存在")
    return department


async def ensure_role_ids(session: AsyncSession, role_ids: list[uuid.UUID]) -> None:
    for role_id in set(role_ids):
        if await repo.get_role(session, role_id) is None:
            raise AppError.not_found("角色不存在")


async def ensure_permission_ids(session: AsyncSession, permission_ids: list[uuid.UUID]) -> None:
    if len(set(permission_ids)) != len(permission_ids):
        raise AppError.validation("权限码不能重复")
    existing = await repo.existing_permission_ids(session, permission_ids)
    if existing != set(permission_ids):
        raise AppError.not_found("权限码不存在")


async def ensure_parent_not_cycle(
    session: AsyncSession, department_id: uuid.UUID, parent_id: uuid.UUID | None
) -> None:
    if parent_id is None:
        return
    if department_id == parent_id:
        raise AppError.validation("部门不能将自身设为父部门")
    await ensure_department(session, parent_id)

    current = parent_id
    visited: set[uuid.UUID] = set()
    while current is not None:
        if current in visited:
            raise AppError.validation("部门层级存在循环")
        visited.add(current)
        parent = await repo.get_department(session, current)
        if parent is None:
            break
        if parent.parent_id == department_id:
            raise AppError.validation("不能将部门移动到自己的子部门下")
        current = parent.parent_id


async def create_department(
    session: AsyncSession, *, name: str, parent_id: uuid.UUID | None, is_active: bool
) -> Department:
    if parent_id is not None:
        await ensure_department(session, parent_id)
    department = Department(name=name, parent_id=parent_id, is_active=is_active)
    session.add(department)
    await session.commit()
    await session.refresh(department)
    return department


async def update_department(
    session: AsyncSession,
    department: Department,
    values: dict,
) -> Department:
    if "parent_id" in values:
        await ensure_parent_not_cycle(session, department.id, values["parent_id"])
    for key, value in values.items():
        setattr(department, key, value)
    await session.commit()
    await session.refresh(department)
    return department


async def delete_department(session: AsyncSession, department: Department) -> None:
    if await repo.count_child_departments(session, department.id) or await repo.count_department_users(
        session, department.id
    ):
        raise AppError.dept_not_empty()
    department.is_deleted = True
    department.deleted_at = datetime.now(UTC)
    department.is_active = False
    await session.commit()


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    display_name: str,
    password: str,
    department_id: uuid.UUID,
    role_ids: list[uuid.UUID],
    is_active: bool,
) -> User:
    await ensure_department(session, department_id)
    await ensure_role_ids(session, role_ids)
    if await repo.username_exists(session, username):
        raise AppError.username_taken()
    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        department_id=department_id,
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    await repo.replace_user_roles(session, user.id, role_ids)
    await session.commit()
    await session.refresh(user)
    return user


async def update_user(
    session: AsyncSession, user: User, values: dict, role_ids: list[uuid.UUID] | None
) -> User:
    if "department_id" in values:
        await ensure_department(session, values["department_id"])
    if role_ids is not None:
        await ensure_role_ids(session, role_ids)
    if "password" in values:
        values["password_hash"] = hash_password(values.pop("password"))
    for key, value in values.items():
        setattr(user, key, value)
    if role_ids is not None:
        await repo.replace_user_roles(session, user.id, role_ids)
    await session.commit()
    await session.refresh(user)
    await invalidate_user(user.id)
    return user


async def delete_user(session: AsyncSession, user: User) -> None:
    user.is_deleted = True
    user.deleted_at = datetime.now(UTC)
    user.is_active = False
    await session.commit()
    await invalidate_user(user.id)


async def create_role(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    description: str | None,
    permission_ids: list[uuid.UUID],
) -> Role:
    if await repo.role_code_exists(session, code):
        raise AppError.role_code_taken()
    await ensure_permission_ids(session, permission_ids)
    role = Role(code=code, name=name, description=description)
    session.add(role)
    await session.flush()
    await repo.replace_role_permissions(session, role.id, permission_ids)
    await session.commit()
    await session.refresh(role)
    return role


async def update_role(
    session: AsyncSession, role: Role, values: dict, permission_ids: list[uuid.UUID] | None
) -> Role:
    if permission_ids is not None:
        await ensure_permission_ids(session, permission_ids)
    for key, value in values.items():
        setattr(role, key, value)
    if permission_ids is not None:
        await repo.replace_role_permissions(session, role.id, permission_ids)
    affected = await repo.list_user_ids_with_role(session, role.id)
    await session.commit()
    await session.refresh(role)
    await invalidate_users(affected)
    return role


async def delete_role(session: AsyncSession, role: Role) -> None:
    if await repo.count_users_with_role(session, role.id):
        raise AppError.role_in_use()
    role.is_deleted = True
    role.deleted_at = datetime.now(UTC)
    await session.commit()
