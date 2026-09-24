"""视觉客户端单测（tasklist 17.3）：走 MockTransport，不连真实模型。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.engines.vision import INSUFFICIENT, MAX_CAPTION_CHARS, VisionClient

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
VISION_URL = "https://vision.example.com/v1"


def _client(
    reply: str = "图中为三级审批流程。",
    *,
    status: int = 200,
    calls: list[str] | None = None,
    enabled: bool = True,
) -> VisionClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.content.decode("utf-8"))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return VisionClient(
        base_url=VISION_URL,
        api_key="test-key",
        model="test-vision",
        transport=httpx.MockTransport(handler),
        enabled=enabled,
    )


async def test_disabled_client_returns_none_without_request() -> None:
    calls: list[str] = []
    client = _client(calls=calls, enabled=False)

    assert await client.caption_image(PNG, "image/png") is None
    assert calls == []


async def test_missing_config_disables_client() -> None:
    client = VisionClient(base_url="", api_key="", model="", enabled=True)

    assert client.enabled is False
    assert await client.caption_image(PNG, "image/png") is None


async def test_caption_is_returned_and_request_is_openai_shaped() -> None:
    calls: list[str] = []
    client = _client("图中为三级审批流程。", calls=calls)

    caption = await client.caption_image(PNG, "image/png")

    assert caption == "图中为三级审批流程。"
    body = json.loads(calls[0])
    assert body["model"] == "test-vision"
    assert body["temperature"] == 0
    parts = body["messages"][1]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("reply", [INSUFFICIENT, "insufficient", " INSUFFICIENT。 "])
async def test_insufficient_reply_is_dropped(reply: str) -> None:
    assert await _client(reply).caption_image(PNG, "image/png") is None


async def test_self_prefixed_caption_is_stripped() -> None:
    caption = await _client("图注：图中为三级审批流程。").caption_image(PNG, "image/png")

    assert caption == "图中为三级审批流程。"


async def test_overlong_caption_is_truncated() -> None:
    caption = await _client("审查" * 400).caption_image(PNG, "image/png")

    assert caption is not None
    assert len(caption) <= MAX_CAPTION_CHARS + 1


async def test_upstream_error_degrades_to_none() -> None:
    assert await _client(status=500).caption_image(PNG, "image/png") is None


async def test_malformed_response_degrades_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = VisionClient(
        base_url=VISION_URL,
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
        enabled=True,
    )

    assert await client.caption_image(PNG, "image/png") is None


async def test_empty_content_degrades_to_none_with_a_log(caplog) -> None:
    """思考型模型把预算花在 reasoning 上时正文为空：结果是 None，但必须留下线索。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "让我先想想……"},
                    }
                ]
            },
        )

    client = VisionClient(
        base_url=VISION_URL,
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
        enabled=True,
    )

    with caplog.at_level("WARNING"):
        assert await client.caption_image(PNG, "image/png") is None

    assert "没有正文" in caplog.text


async def test_transport_error_degrades_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = VisionClient(
        base_url=VISION_URL,
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
        enabled=True,
    )

    assert await client.caption_image(PNG, "image/png") is None


async def test_oversize_image_is_skipped_without_request() -> None:
    calls: list[str] = []
    client = _client(calls=calls)

    oversize = b"0" * (5 * 1024 * 1024 + 1)

    assert await client.caption_image(oversize, "image/png") is None
    assert calls == []


async def test_batch_keeps_order_and_isolates_failures() -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] == 2:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "摘要"}}]})

    client = VisionClient(
        base_url=VISION_URL,
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
        enabled=True,
    )

    captions = await client.caption_images([(PNG, "image/png")] * 3)

    assert len(captions) == 3
    assert sum(1 for item in captions if item is None) == 1
    assert [item for item in captions if item is not None] == ["摘要", "摘要"]


async def test_empty_batch_needs_no_request() -> None:
    assert await _client().caption_images([]) == []
