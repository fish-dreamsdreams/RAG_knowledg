"""四维权限弹窗的候选项接口测试（tasklist 14.5 的后端补充）。

`kb:acl` 是唯一门槛：知识管理员要能选部门/角色/人员，但不该为了选人拿到 `org:*`。
需要 PostgreSQL（用例走真实登录，依赖已跑过种子）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "Demo@123456"
URL = "/api/v1/knowledge-units/acl-options"


async def login_headers(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_acl_options_requires_kb_acl(client: AsyncClient) -> None:
    """普通员工（无 `kb:acl`）拿不到候选项：选人权不能靠「能登录」白送。"""

    headers = await login_headers(client, "sales01")

    response = await client.get(URL, headers=headers)

    assert response.status_code == 403


async def test_acl_options_returns_departments_roles_users(client: AsyncClient) -> None:
    headers = await login_headers(client, "kbadm")

    response = await client.get(URL, headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    department_names = {item["name"] for item in data["departments"]}
    assert {"财务部", "销售部"} <= department_names
    # 扁平结构带 parent_id，前端自己拼树（P2 不级联由前端保证）
    assert all("parent_id" in item for item in data["departments"])
    assert any(role["code"] == "kb_admin" for role in data["roles"])
    cs = next(user for user in data["users"] if user["username"] == "cs01")
    assert cs["display_name"] == "客服专员"
    assert cs["department_name"] == "客服部"


async def test_acl_options_filters_users_by_keyword(client: AsyncClient) -> None:
    headers = await login_headers(client, "kbadm")

    response = await client.get(URL, params={"keyword": "cs"}, headers=headers)

    assert response.status_code == 200, response.text
    usernames = {item["username"] for item in response.json()["data"]["users"]}
    assert "cs01" in usernames
    assert "sales01" not in usernames
