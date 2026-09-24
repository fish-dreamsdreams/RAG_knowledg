"""3D 助手闲聊接口：鉴权与 SSE 流，不连真实网关。"""

from __future__ import annotations

from httpx import AsyncClient

from app.common.db import get_session
from app.main import app
from app.services import assistant as assistant_service


async def test_assistant_chat_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/assistant/chat", json={"content": "你好"})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_TOKEN"


async def test_assistant_chat_streams_sse(client: AsyncClient, fake_identity, monkeypatch) -> None:
    fake_identity(permissions=["chat"])

    async def _prepare(_session):
        return object()

    async def _sse(_model, messages):
        assert messages[-1].content == "你好"
        yield 'data: {"delta": "小小"}\n\n'.encode("utf-8")
        yield b'data: {"done": true}\n\n'

    async def _session():
        yield object()

    monkeypatch.setattr(assistant_service, "prepare_chat", _prepare)
    monkeypatch.setattr(assistant_service, "sse_bytes", _sse)
    app.dependency_overrides[get_session] = _session
    try:
        response = await client.post(
            "/api/v1/assistant/chat",
            json={"content": "你好"},
            headers={"Authorization": "Bearer fake"},
        )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    body = response.text
    assert '"delta": "小小"' in body
    assert '"done": true' in body
