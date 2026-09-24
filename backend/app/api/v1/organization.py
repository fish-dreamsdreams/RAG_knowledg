"""组织架构、用户、角色与权限码接口。"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, PageParams, pagination, require_perm
from app.common.db import get_session
from app.common.errors import AppError
from app.models import Department, Permission, Role, User
from app.repositories import org as repo
from app.schemas.common import page, success
from app.schemas.org import (
    DepartmentCreate,
    DepartmentNode,
    DepartmentUpdate,
    PermissionOut,
    RoleBrief,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services import org as org_service

router = APIRouter(tags=["organization"])


def _department_tree(departments: list[Department]) -> list[DepartmentNode]:
    nodes = {
        department.id: DepartmentNode(
            id=department.id,
            name=department.name,
            parent_id=department.parent_id,
            is_active=department.is_active,
            children=[],
        )
        for department in departments
    }
    roots: list[DepartmentNode] = []
    for department in departments:
        node = nodes[department.id]
        if department.parent_id and department.parent_id in nodes:
            nodes[department.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


def _department_data(department: Department) -> dict:
    return DepartmentNode(
        id=department.id,
        name=department.name,
        parent_id=department.parent_id,
        is_active=department.is_active,
        children=[],
    ).model_dump()


async def _role_ids(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    return await repo.list_role_ids_for_user(session, user_id)


async def _user_data(
    session: AsyncSession, user: User, role_map: dict[uuid.UUID, list[Role]] | None = None
) -> dict:
    names = await repo.department_names(session, {user.department_id})
    role_map = role_map if role_map is not None else await repo.roles_by_user_ids(session, [user.id])
    roles = role_map.get(user.id, [])
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        department_id=user.department_id,
        department_name=names.get(user.department_id),
        is_active=user.is_active,
        role_ids=[role.id for role in roles],
        roles=[RoleBrief(id=role.id, code=role.code, name=role.name) for role in roles],
    ).model_dump()


async def _role_data(session: AsyncSession, role: Role) -> dict:
    permission_map = await repo.permission_ids_by_role(session, [role.id])
    return RoleOut(
        id=role.id,
        code=role.code,
        name=role.name,
        description=role.description,
        permission_ids=permission_map.get(role.id, []),
    ).model_dump()


@router.get("/departments", dependencies=[Depends(require_perm("org:dept"))])
async def list_departments(session: AsyncSession = Depends(get_session)) -> dict:
    return success([node.model_dump() for node in _department_tree(await repo.list_departments(session))])


@router.post("/departments", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_perm("org:dept"))])
async def create_department(
    payload: DepartmentCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    department = await org_service.create_department(
        session, name=payload.name, parent_id=payload.parent_id, is_active=payload.is_active
    )
    return success(_department_data(department))


@router.put("/departments/{department_id}", dependencies=[Depends(require_perm("org:dept"))])
async def update_department(
    department_id: uuid.UUID,
    payload: DepartmentUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    department = await repo.get_department(session, department_id)
    if department is None:
        raise AppError.not_found("部门不存在")
    department = await org_service.update_department(
        session, department, payload.model_dump(exclude_unset=True)
    )
    return success(_department_data(department))


@router.delete("/departments/{department_id}", dependencies=[Depends(require_perm("org:dept"))])
async def delete_department(
    department_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    department = await repo.get_department(session, department_id)
    if department is None:
        raise AppError.not_found("部门不存在")
    await org_service.delete_department(session, department)
    return success(message="部门已删除")


@router.get("/users", dependencies=[Depends(require_perm("org:user"))])
async def list_users(
    params: PageParams = Depends(pagination),
    keyword: str | None = Query(None, max_length=64),
    department_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    users, total = await repo.list_users(
        session,
        offset=params.offset,
        limit=params.page_size,
        keyword=keyword,
        department_id=department_id,
    )
    roles = await repo.roles_by_user_ids(session, [user.id for user in users])
    return page(
        [await _user_data(session, user, roles) for user in users],
        total,
        params.page,
        params.page_size,
    )


@router.post("/users", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_perm("org:user"))])
async def create_user(payload: UserCreate, session: AsyncSession = Depends(get_session)) -> dict:
    user = await org_service.create_user(
        session,
        username=payload.username,
        display_name=payload.display_name,
        password=payload.password,
        department_id=payload.department_id,
        role_ids=payload.role_ids,
        is_active=payload.is_active,
    )
    return success(await _user_data(session, user))


@router.get("/users/{user_id}", dependencies=[Depends(require_perm("org:user"))])
async def get_user(user_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    user = await repo.get_user(session, user_id)
    if user is None:
        raise AppError.not_found("用户不存在")
    return success(await _user_data(session, user))


@router.put("/users/{user_id}", dependencies=[Depends(require_perm("org:user"))])
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await repo.get_user(session, user_id)
    if user is None:
        raise AppError.not_found("用户不存在")
    values = payload.model_dump(exclude_unset=True)
    role_ids = values.pop("role_ids", None)
    user = await org_service.update_user(session, user, values, role_ids)
    return success(await _user_data(session, user))


@router.delete("/users/{user_id}", dependencies=[Depends(require_perm("org:user"))])
async def delete_user(user_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    user = await repo.get_user(session, user_id)
    if user is None:
        raise AppError.not_found("用户不存在")
    await org_service.delete_user(session, user)
    return success(message="用户已删除")


@router.get("/roles", dependencies=[Depends(require_perm("org:role"))])
async def list_roles(session: AsyncSession = Depends(get_session)) -> dict:
    return success([await _role_data(session, role) for role in await repo.list_roles(session)])


@router.post("/roles", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_perm("org:role"))])
async def create_role(payload: RoleCreate, session: AsyncSession = Depends(get_session)) -> dict:
    role = await org_service.create_role(
        session,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        permission_ids=payload.permission_ids,
    )
    return success(await _role_data(session, role))


@router.get("/roles/{role_id}", dependencies=[Depends(require_perm("org:role"))])
async def get_role(role_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    role = await repo.get_role(session, role_id)
    if role is None:
        raise AppError.not_found("角色不存在")
    return success(await _role_data(session, role))


@router.put("/roles/{role_id}", dependencies=[Depends(require_perm("org:role"))])
async def update_role(
    role_id: uuid.UUID,
    payload: RoleUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    role = await repo.get_role(session, role_id)
    if role is None:
        raise AppError.not_found("角色不存在")
    values = payload.model_dump(exclude_unset=True)
    permission_ids = values.pop("permission_ids", None)
    role = await org_service.update_role(session, role, values, permission_ids)
    return success(await _role_data(session, role))


@router.delete("/roles/{role_id}", dependencies=[Depends(require_perm("org:role"))])
async def delete_role(role_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    role = await repo.get_role(session, role_id)
    if role is None:
        raise AppError.not_found("角色不存在")
    await org_service.delete_role(session, role)
    return success(message="角色已删除")


@router.get("/permissions", dependencies=[Depends(require_perm("org:role"))])
async def list_permissions(session: AsyncSession = Depends(get_session)) -> dict:
    items = [
        PermissionOut(
            id=permission.id,
            code=permission.code,
            name=permission.name,
            type=permission.type,
            parent_id=permission.parent_id,
            sort=permission.sort,
        ).model_dump()
        for permission in await repo.list_permissions(session)
    ]
    return success(items)
