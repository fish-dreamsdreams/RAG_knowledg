"""认证与权限单元测试（tasklist 3.4）：不依赖数据库与 Redis。

真实数据库路径（登录成功、停用账号、角色变更即时生效）在
`test/integration/test_auth.py` 中覆盖。
"""

import re
from uuid import uuid4

from httpx import AsyncClient

from app.common.security import create_access_token
from app.main import app

# 只要求登录、不要求功能权限码的路由
ROUTES_WITHOUT_PERMISSION = {
    ("get", "/health"),
    ("get", "/api/v1/health"),
    ("post", "/api/v1/auth/login"),
    ("get", "/api/v1/auth/me"),
}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


async def test_login_rejects_missing_fields(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/login", json={"username": "admin"})
    assert response.status_code == 400
    assert response.json()["code"] == "SYS_VALIDATION"


async def test_me_without_token_returns_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_TOKEN"


async def test_me_with_garbage_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_TOKEN"


async def test_me_with_expired_token_returns_401(client: AsyncClient) -> None:
    expired = create_access_token(subject=str(uuid4()), expires_minutes=-1)
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_TOKEN"


async def test_me_with_token_signed_by_other_key_returns_401(client: AsyncClient) -> None:
    """用错密钥签发的 Token 必须被拒（防止伪造身份）。"""

    from jose import jwt

    forged = jwt.encode({"sub": str(uuid4())}, "another-secret", algorithm="HS256")
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_TOKEN"


async def test_me_returns_identity_with_permissions(client: AsyncClient, fake_identity) -> None:
    current = fake_identity(permissions=["kb:import", "ai:chat"], role_codes=["kb_admin"])
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["user_id"] == str(current.user_id)
    assert set(data["permissions"]) == {"kb:import", "ai:chat"}
    # 前端据此决定登录后默认落点（PRD §4），不是鉴权依据
    assert data["role_codes"] == ["kb_admin"]
    assert len(data["role_ids"]) == len(current.role_ids)


async def test_no_protected_route_returns_2xx_without_permission(
    client: AsyncClient, fake_identity
) -> None:
    """遍历全部受保护路由：零权限身份访问一律不得 2xx。

    从 OpenAPI schema 枚举（`app.routes` 会把 include 的子路由包成不透明对象，
    摊不平）；新增路由忘记挂 `require_perm` 会直接在这条用例上暴露。
    """

    fake_identity(permissions=[])
    violations: list[str] = []
    checked = 0

    for path, operations in app.openapi()["paths"].items():
        for method in sorted(set(operations) & HTTP_METHODS):
            if (method, path) in ROUTES_WITHOUT_PERMISSION:
                continue
            checked += 1
            concrete = re.sub(r"\{[^}]+\}", str(uuid4()), path)
            response = await client.request(method.upper(), concrete, json={})
            if 200 <= response.status_code < 300:
                violations.append(f"{method.upper()} {path} -> {response.status_code}")

    assert checked >= 8, f"路由枚举异常，只扫到 {checked} 条，用例失去意义"
    assert not violations, f"以下路由缺少权限码守卫：{violations}"


async def test_missing_permission_returns_403_with_code(
    client: AsyncClient, fake_identity
) -> None:
    """反向对照：守卫确实在早于处理函数的位置拦住请求。"""

    fake_identity(permissions=["kb:view"])
    response = await client.get("/api/v1/departments")

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_FORBIDDEN"
    assert response.json()["data"] is None
