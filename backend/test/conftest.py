"""测试公共 fixture。测试代码统一放在 backend/test/ 下（TECH_SPEC §9）。"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_auth
from app.common.db import SessionLocal, engine
from app.common.redis import get_redis
from app.common.security import create_access_token
from app.main import app


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests() -> AsyncIterator[None]:
    """pytest-asyncio 每个用例新建事件循环，而连接池会把连接绑在旧循环上。

    用例结束释放连接池，避免下一个用例复用到已关闭循环的连接
    （'Event loop is closed'）。单元测试不连库，这里是空操作。
    """

    yield
    await engine.dispose()
    await get_redis().aclose()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """不提交、结束回滚，避免污染演示库。

    注意：seed 与 demo_logs 这类加载器内部自行提交，相关用例会留下数据。
    """

    async with SessionLocal() as db_session:
        try:
            yield db_session
        finally:
            await db_session.rollback()


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
def user_id() -> str:
    return str(uuid4())


@pytest.fixture
def access_token(user_id: str) -> str:
    return create_access_token(
        subject=user_id,
        department_id=str(uuid4()),
        role_ids=[str(uuid4())],
        permissions=["chat", "ai:chat"],
    )


@pytest.fixture
def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
def fake_identity():
    """把 `require_auth` 换成假身份，让单元测试不碰数据库与 Redis。

    用法：`current = fake_identity(permissions=["org:dept"])`
    """

    def _make(
        *,
        permissions: list[str] | None = None,
        role_ids: list[str] | None = None,
        role_codes: list[str] | None = None,
        is_active: bool = True,
        current_user=None,
    ) -> CurrentUser:
        current = CurrentUser(
            user_id=uuid4(),
            username="fake-user",
            display_name="假用户",
            department_id=uuid4(),
            role_ids=[uuid4() for _ in role_ids] if role_ids is not None else [uuid4()],
            role_codes=list(role_codes or []),
            permissions=list(permissions or []),
            is_active=is_active,
        )
        if current_user is not None:
            current.user_id = current_user
        app.dependency_overrides[require_auth] = lambda: current
        return current

    yield _make
    app.dependency_overrides.pop(require_auth, None)
