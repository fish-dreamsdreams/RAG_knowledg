"""组织与权限仓储：本层是唯一写 SQL 的地方（TECH_SPEC §1 分层）。

约定：用户/部门/角色一律过滤 `is_deleted=false`；软删除由服务层置位。
"""

import uuid

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Department, Permission, Role, RolePermission, User, UserRole

# ============================ 部门 ============================


def _departments() -> Select:
    return select(Department).where(Department.is_deleted.is_(False))


async def get_department(session: AsyncSession, department_id: uuid.UUID) -> Department | None:
    return (
        await session.execute(_departments().where(Department.id == department_id))
    ).scalar_one_or_none()


async def list_departments(session: AsyncSession) -> list[Department]:
    rows = await session.execute(_departments().order_by(Department.name))
    return list(rows.scalars())


async def count_child_departments(session: AsyncSession, department_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Department)
            .where(Department.parent_id == department_id, Department.is_deleted.is_(False))
        )
    ).scalar_one()


async def count_department_users(session: AsyncSession, department_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.department_id == department_id, User.is_deleted.is_(False))
        )
    ).scalar_one()


async def department_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Department.id, Department.name).where(Department.id.in_(ids))
    )
    return {department_id: name for department_id, name in rows.all()}


async def role_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """角色 id → 名称（含软删除角色：ACL 里残留的旧条目仍要能显示，否则标签静默消失）。"""

    if not ids:
        return {}
    rows = await session.execute(select(Role.id, Role.name).where(Role.id.in_(ids)))
    return {role_id: name for role_id, name in rows.all()}


async def user_display_names(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """用户 id → 显示名（同上，含已软删除用户）。"""

    if not ids:
        return {}
    rows = await session.execute(select(User.id, User.display_name).where(User.id.in_(ids)))
    return {user_id: name for user_id, name in rows.all()}


# ============================ 用户 ============================


def _users() -> Select:
    return select(User).where(User.is_deleted.is_(False))


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (await session.execute(_users().where(User.id == user_id))).scalar_one_or_none()


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    return (
        await session.execute(_users().where(User.username == username))
    ).scalar_one_or_none()


async def username_exists(session: AsyncSession, username: str) -> bool:
    """含软删用户：`username` 上有唯一约束，新建时必须连软删行一起挡。"""

    return (
        await session.execute(select(User.id).where(User.username == username))
    ).first() is not None


async def list_users(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
    keyword: str | None = None,
    department_id: uuid.UUID | None = None,
) -> tuple[list[User], int]:
    conditions = [User.is_deleted.is_(False)]
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(User.username.ilike(pattern), User.display_name.ilike(pattern)))
    if department_id is not None:
        conditions.append(User.department_id == department_id)

    total = (
        await session.execute(select(func.count()).select_from(User).where(*conditions))
    ).scalar_one()
    rows = await session.execute(
        select(User).where(*conditions).order_by(User.created_at).offset(offset).limit(limit)
    )
    return list(rows.scalars()), total


async def list_role_ids_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        select(UserRole.role_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user_id, Role.is_deleted.is_(False))
    )
    return [role_id for (role_id,) in rows.all()]


async def list_role_codes_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """角色码（去软删角色）。

    与 `list_role_ids_for_user` 分开：前端要知道的是**角色码**（决定登录后默认落点，PRD §4），
    而权限上下文里的 `role_ids` 是给数据权限与缓存失效用的，两者用途不同。
    """

    rows = await session.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, Role.is_deleted.is_(False))
        .order_by(Role.code)
    )
    return [code for (code,) in rows.all()]


async def roles_by_user_ids(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[Role]]:
    if not user_ids:
        return {}
    rows = await session.execute(
        select(UserRole.user_id, Role)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(user_ids), Role.is_deleted.is_(False))
        .order_by(Role.code)
    )
    grouped: dict[uuid.UUID, list[Role]] = {}
    for user_id, role in rows.all():
        grouped.setdefault(user_id, []).append(role)
    return grouped


async def replace_user_roles(
    session: AsyncSession, user_id: uuid.UUID, role_ids: list[uuid.UUID]
) -> None:
    """全量替换用户角色（空列表即清空）。"""

    current = set(await list_role_ids_for_user(session, user_id))
    wanted = set(role_ids)
    for role_id in current - wanted:
        link = await session.get(UserRole, {"user_id": user_id, "role_id": role_id})
        if link is not None:
            await session.delete(link)
    for role_id in wanted - current:
        session.add(UserRole(user_id=user_id, role_id=role_id))


async def list_user_ids_with_role(session: AsyncSession, role_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        select(UserRole.user_id).where(UserRole.role_id == role_id)
    )
    return [user_id for (user_id,) in rows.all()]


# ============================ 角色与权限码 ============================


def _roles() -> Select:
    return select(Role).where(Role.is_deleted.is_(False))


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return (await session.execute(_roles().where(Role.id == role_id))).scalar_one_or_none()


async def get_role_by_code(session: AsyncSession, code: str) -> Role | None:
    return (await session.execute(_roles().where(Role.code == code))).scalar_one_or_none()


async def role_code_exists(session: AsyncSession, code: str) -> bool:
    """含软删角色：`code` 上有唯一约束。"""

    return (
        await session.execute(select(Role.id).where(Role.code == code))
    ).first() is not None


async def list_roles(session: AsyncSession) -> list[Role]:
    rows = await session.execute(_roles().order_by(Role.code))
    return list(rows.scalars())


async def list_permissions(session: AsyncSession) -> list[Permission]:
    rows = await session.execute(
        select(Permission).order_by(Permission.sort, Permission.code)
    )
    return list(rows.scalars())


async def existing_permission_ids(
    session: AsyncSession, permission_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    if not permission_ids:
        return set()
    rows = await session.execute(
        select(Permission.id).where(Permission.id.in_(permission_ids))
    )
    return {permission_id for (permission_id,) in rows.all()}


async def permission_ids_by_role(
    session: AsyncSession, role_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not role_ids:
        return {}
    rows = await session.execute(
        select(RolePermission.role_id, RolePermission.permission_id).where(
            RolePermission.role_id.in_(role_ids)
        )
    )
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    for role_id, permission_id in rows.all():
        grouped.setdefault(role_id, []).append(permission_id)
    return grouped


async def replace_role_permissions(
    session: AsyncSession, role_id: uuid.UUID, permission_ids: list[uuid.UUID]
) -> None:
    """全量替换角色权限码（空列表即清空）。"""

    current = set((await permission_ids_by_role(session, [role_id])).get(role_id, []))
    wanted = set(permission_ids)
    for permission_id in current - wanted:
        link = await session.get(
            RolePermission, {"role_id": role_id, "permission_id": permission_id}
        )
        if link is not None:
            await session.delete(link)
    for permission_id in wanted - current:
        session.add(RolePermission(role_id=role_id, permission_id=permission_id))


async def list_permission_codes_for_roles(
    session: AsyncSession, role_ids: list[uuid.UUID]
) -> list[str]:
    """角色权限码并集（去重、去软删角色）。P11 的事实来源。"""

    if not role_ids:
        return []
    rows = await session.execute(
        select(RolePermission.role_id, Permission.code)
        .join(Permission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(RolePermission.role_id.in_(role_ids), Role.is_deleted.is_(False))
    )
    return merge_permission_codes(
        role_ids,
        [(role_id, code) for role_id, code in rows.all()],
    )


def merge_permission_codes(
    role_ids: list[uuid.UUID | str],
    role_permission_rows: list[tuple[uuid.UUID | str, str]],
) -> list[str]:
    """按指定角色求权限码并集；纯函数供 P11 属性测试。"""

    selected = set(role_ids)
    return sorted({code for role_id, code in role_permission_rows if role_id in selected})


async def count_users_with_role(session: AsyncSession, role_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(UserRole)
            .join(User, User.id == UserRole.user_id)
            .where(UserRole.role_id == role_id, User.is_deleted.is_(False))
        )
    ).scalar_one()
