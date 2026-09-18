"""认证的数据库路径集成测试（tasklist 3.2、3.3）。

需要 PostgreSQL 与 Redis：`docker compose up -d postgres redis` 后
`pytest -m integration -k auth`。用演示种子账号，会写权限缓存。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.security import create_access_token
from app.models import User
from app.services.permission import cache_key, invalidate_user

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "Demo@123456"


async def _user(session: AsyncSession, username: str) -> User:
    return (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one()


async def test_login_success_returns_identity_and_token(
    client: AsyncClient, session: AsyncSession
) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": DEMO_PASSWORD}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0
    assert data["user"]["username"] == "admin"
    assert data["user"]["department_name"] == "总公司"
    assert data["user"]["permissions"], "系统管理员必须有权限码"
    # 角色码给前端选默认落点用（PRD §4：sys_admin → 运营看板）
    assert data["user"]["role_codes"] == ["system_admin"]

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["data"]["user_id"] == data["user"]["user_id"]


async def test_login_failures_are_indistinguishable(client: AsyncClient) -> None:
    wrong_password = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"}
    )
    unknown_user = await client.post(
        "/api/v1/auth/login", json={"username": "no-such-user", "password": DEMO_PASSWORD}
    )

    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    assert wrong_password.json()["code"] == "AUTH_INVALID_TOKEN"
    assert wrong_password.json()["message"] == unknown_user.json()["message"]


async def test_disabled_user_token_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    """停用账号即使 Token 未过期也必须 401（tasklist 3.3）。"""

    user = await _user(session, "cs01")
    token = create_access_token(subject=str(user.id))

    user.is_active = False
    await session.commit()
    try:
        await invalidate_user(user.id)
        response = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_INVALID_TOKEN"

        login = await client.post(
            "/api/v1/auth/login", json={"username": "cs01", "password": DEMO_PASSWORD}
        )
        assert login.status_code == 401
    finally:
        user.is_active = True
        await session.commit()
        await invalidate_user(user.id)


async def test_permission_context_is_cached_then_invalidated(
    client: AsyncClient, session: AsyncSession
) -> None:
    """缓存只加速：写入 TTL 键，失效后回数据库重算（TECH_SPEC §5.5）。"""

    from app.common.redis import get_redis

    user = await _user(session, "cs01")
    await invalidate_user(user.id)

    redis = get_redis()
    assert await redis.get(cache_key(user.id)) is None

    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {create_access_token(subject=str(user.id))}"},
    )
    assert response.status_code == 200

    cached = await redis.get(cache_key(user.id))
    assert cached is not None
    assert 0 < await redis.ttl(cache_key(user.id)) <= 300

    await invalidate_user(user.id)
    assert await redis.get(cache_key(user.id)) is None
