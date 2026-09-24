"""组织接口与权限缓存集成测试（tasklist 4.5）。"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4

from app.common.security import create_access_token
from app.models import Department, Role, User
from app.repositories import org as repo
from app.services.permission import invalidate_user

pytestmark = pytest.mark.integration


async def _login(client: AsyncClient, username: str = "admin") -> dict:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": "Demo@123456"}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _headers(client: AsyncClient, username: str = "admin") -> dict[str, str]:
    data = await _login(client, username)
    return {"Authorization": f"Bearer {data['access_token']}"}


async def test_department_tree_and_non_empty_delete_409(
    client: AsyncClient, session: AsyncSession
) -> None:
    headers = await _headers(client)
    tree = await client.get("/api/v1/departments", headers=headers)
    assert tree.status_code == 200
    assert tree.json()["data"]

    finance = (
        await session.execute(select(Department).where(Department.name == "财务部"))
    ).scalar_one()
    response = await client.delete(f"/api/v1/departments/{finance.id}", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "ORG_DEPT_NOT_EMPTY"


async def test_employee_cannot_access_org_routes(client: AsyncClient) -> None:
    headers = await _headers(client, "cs01")
    response = await client.get("/api/v1/departments", headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_FORBIDDEN"


async def test_role_permission_change_is_immediate_and_union_is_exact(
    client: AsyncClient, session: AsyncSession
) -> None:
    admin_headers = await _headers(client)
    cs01 = (
        await session.execute(select(User).where(User.username == "cs01"))
    ).scalar_one()
    employee = (
        await session.execute(select(Role).where(Role.code == "employee"))
    ).scalar_one()
    kb_admin = (
        await session.execute(select(Role).where(Role.code == "kb_admin"))
    ).scalar_one()

    # 当前普通员工权限上下文先进入缓存。
    cs_headers = await _headers(client, "cs01")
    before = await client.get("/api/v1/auth/me", headers=cs_headers)
    assert before.status_code == 200
    assert before.json()["data"]["permissions"] == ["ai:chat", "chat"]

    # 通过接口把 employee 的权限替换为唯一的 ai:chat，再确认受影响用户缓存被主动失效。
    permissions = await client.get("/api/v1/permissions", headers=admin_headers)
    assert permissions.status_code == 200
    ai_chat_id = next(
        item["id"] for item in permissions.json()["data"] if item["code"] == "ai:chat"
    )
    role_response = await client.put(
        f"/api/v1/roles/{employee.id}",
        headers=admin_headers,
        json={"permission_ids": [ai_chat_id]},
    )
    assert role_response.status_code == 200
    assert role_response.json()["data"]["permission_ids"] == [ai_chat_id]

    after = await client.get("/api/v1/auth/me", headers=cs_headers)
    assert after.status_code == 200
    assert after.json()["data"]["permissions"] == ["ai:chat"]

    # 恢复演示种子角色，避免影响后续集成测试。
    permission_rows = await repo.list_permissions(session)
    permission_by_code = {permission.code: permission.id for permission in permission_rows}
    restore_ids = [permission_by_code[code] for code in ("chat", "ai:chat")]
    role_response = await client.put(
        f"/api/v1/roles/{employee.id}",
        headers=admin_headers,
        json={"permission_ids": [str(permission_id) for permission_id in restore_ids]},
    )
    assert role_response.status_code == 200
    await invalidate_user(cs01.id)


async def test_role_crud_and_user_crud_are_wired(
    client: AsyncClient, session: AsyncSession
) -> None:
    headers = await _headers(client)
    departments = await client.get("/api/v1/departments", headers=headers)
    hq_id = next(item["id"] for item in departments.json()["data"] if item["name"] == "总公司")

    suffix = uuid4().hex[:10]
    role_code = f"integration_{suffix}"
    username = f"integration_{suffix}"
    created_role = await client.post(
        "/api/v1/roles",
        headers=headers,
        json={"code": role_code, "name": "集成临时角色", "permission_ids": []},
    )
    assert created_role.status_code == 201
    role_id = created_role.json()["data"]["id"]
    created_user = await client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "username": username,
            "display_name": "集成临时用户",
            "password": "Temp@123456",
            "department_id": hq_id,
            "role_ids": [role_id],
        },
    )
    assert created_user.status_code == 201
    user_id = created_user.json()["data"]["id"]
    assert created_user.json()["data"]["role_ids"] == [role_id]

    assert (await client.delete(f"/api/v1/users/{user_id}", headers=headers)).status_code == 200
    assert (await client.delete(f"/api/v1/roles/{role_id}", headers=headers)).status_code == 200
