"""3D 助手闲聊服务：不连真实网关。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.common.errors import AppError, ErrorCode
from app.services import assistant as assistant_service
from app.schemas.assistant import AssistantTurn


class _FakeModel:
    def __init__(self, parts: list[str] | None = None) -> None:
        self.parts = parts or ["小小", "在呢！"]
        self.seen = None

    async def astream(self, messages):
        self.seen = messages
        for part in self.parts:
            yield SimpleNamespace(content=part)


@pytest.fixture
def fake_model(monkeypatch):
    model = _FakeModel()

    async def _load(_session):
        return SimpleNamespace()

    monkeypatch.setattr(assistant_service.system_repo, "load_model_config", _load)
    monkeypatch.setattr(assistant_service, "build_chat_model", lambda *_args, **_kwargs: model)
    return model


async def test_iter_tokens_yields_pieces(fake_model) -> None:
    model = await assistant_service.prepare_chat(object())
    messages = assistant_service.build_messages("你好", [])
    pieces = [piece async for piece in assistant_service.iter_tokens(model, messages)]
    assert pieces == ["小小", "在呢！"]
    assert fake_model.seen[-1].content == "你好"


async def test_build_messages_keeps_recent_history() -> None:
    history = [
        AssistantTurn(role="user", content="我是谁"),
        AssistantTurn(role="assistant", content="你是朋友呀"),
    ]
    texts = [item.content for item in assistant_service.build_messages("还记得吗", history)]
    assert "我是谁" in texts
    assert "你是朋友呀" in texts
    assert texts[-1] == "还记得吗"


async def test_prepare_chat_maps_missing_config(monkeypatch) -> None:
    async def _load(_session):
        return SimpleNamespace()

    monkeypatch.setattr(assistant_service.system_repo, "load_model_config", _load)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("Chat 网关配置不完整，缺少：LLM_API_KEY")

    monkeypatch.setattr(assistant_service, "build_chat_model", _boom)

    with pytest.raises(AppError) as exc:
        await assistant_service.prepare_chat(object())
    assert exc.value.code == ErrorCode.SYS_VALIDATION


async def test_iter_tokens_maps_upstream_failure() -> None:
    class Broken:
        async def astream(self, _messages):
            raise RuntimeError("timeout")
            yield  # pragma: no cover — make this an async generator

    with pytest.raises(AppError) as exc:
        async for _ in assistant_service.iter_tokens(Broken(), []):
            pass
    assert exc.value.code == ErrorCode.AI_UPSTREAM_ERROR


async def test_sse_bytes_emits_delta_then_done(fake_model) -> None:
    model = await assistant_service.prepare_chat(object())
    chunks = [
        chunk.decode("utf-8")
        async for chunk in assistant_service.sse_bytes(model, assistant_service.build_messages("hi", []))
    ]
    joined = "".join(chunks)
    assert '"delta": "小小"' in joined
    assert '"done": true' in joined
