"""脚手架冒烟测试：健康检查、401 与 trace_id（tasklist 1.5、不变量 P14）。"""

from httpx import AsyncClient


async def test_root_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "OK"
    assert body["data"]["status"] == "ok"
    assert response.headers.get("X-Trace-Id")


async def test_api_v1_health_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"


async def test_json_responses_declare_utf8_charset(client: AsyncClient) -> None:
    """缺 charset 时 PowerShell 5.1 会按 Latin-1 解码，中文变乱码。"""

    response = await client.get("/api/v1/health")
    assert "charset=utf-8" in response.headers["content-type"].lower()


async def test_me_without_token_returns_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "AUTH_INVALID_TOKEN"
    assert body["data"] is None
    assert response.headers.get("X-Trace-Id")


async def test_me_with_invalid_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_TOKEN"


async def test_trace_id_is_echoed_when_provided(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Trace-Id": "trace-from-client"})
    assert response.headers["X-Trace-Id"] == "trace-from-client"
