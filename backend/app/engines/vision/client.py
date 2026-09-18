"""视觉模型客户端：图片 → 图注（tasklist 17.3，TECH_SPEC §8.0）。

只做一次 OpenAI 兼容的 `/chat/completions` 调用，图片以 base64 data URL 内联。
本项目只做 API 调用、不部署模型，endpoint 由部署方通过 `VISION_*` 配置决定。

设计约束：

- 开关 `ASSETS_VISION_ENABLED` 默认关闭；关闭或未配置完整时直接返回 `None`。
- 失败一律降级为 `None`：调用方保留无图注的图片标记，**不阻断导入**。
- 并发受 `VISION_CONCURRENCY` 限制，避免打爆上游与撑大本地内存。
- 信息量不足时模型回 `INSUFFICIENT`，这里转成 `None`——敷衍的图注（“这是一张流程图”）
  会比周围原文更难命中，宁可不注入。
- 调用失败只记 warning，不把上游响应正文写进日志（可能含模型回显的图片内容）。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx

from app.common.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 60.0
# 图注长度上限：只作为检索文本，过长会挤占块预算
MAX_CAPTION_CHARS = 300
MAX_CAPTION_TOKENS = 256
# 内联图片上限：base64 会放大 1/3，过大既超上游限制也吃内存
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024

# 正文里图注行的前缀，前端据此区分“原文”与“模型生成的说明”
CAPTION_PREFIX = "图注："
# 模型认为图中信息不足以支撑检索时的约定回复
INSUFFICIENT = "INSUFFICIENT"

SYSTEM_PROMPT = (
    "你是文档配图的摘要助手。用一到三句中文描述图中与文档检索相关的关键要素、"
    "结构、流程、数据或结论，不要复述图片文件名或格式，不要输出 Markdown。"
    f"如果图中没有可用于检索的实质信息（例如纯装饰、空白、纯 logo），"
    f"只回复 {INSUFFICIENT}。"
)
USER_PROMPT = "请为这张文档配图生成检索用摘要。"


class VisionClient:
    """OpenAI 兼容的视觉客户端。

    `transport` 仅用于测试注入（httpx.MockTransport），生产环境不要传。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        enabled: bool | None = None,
    ) -> None:
        resolved_base = settings.vision_base_url if base_url is None else base_url
        self.base_url = resolved_base.rstrip("/")
        self.api_key = settings.vision_api_key if api_key is None else api_key
        self.model = settings.vision_model if model is None else model
        self.concurrency = max(
            1, settings.vision_concurrency if concurrency is None else concurrency
        )
        self.timeout = REQUEST_TIMEOUT_SECONDS if timeout is None else timeout
        self._transport = transport
        # 显式开关：None 时跟随配置，测试可直接传入而不依赖全局设置
        self._enabled_override = enabled
        # 3.10+ 的信号量不绑定事件循环，可安全跨用例复用
        self._semaphore = asyncio.Semaphore(self.concurrency)

    @property
    def enabled(self) -> bool:
        """三项配置齐全且开关打开时才真正调用。"""

        flag = (
            settings.assets_vision_enabled
            if self._enabled_override is None
            else self._enabled_override
        )
        return bool(flag and self.base_url and self.api_key and self.model)

    @asynccontextmanager
    async def _client_scope(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._transport is not None:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self.timeout
            ) as client:
                yield client
            return
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            yield client

    async def caption_images(
        self, images: Sequence[tuple[bytes, str]]
    ) -> list[str | None]:
        """并发为多张图生成图注，顺序与入参一致；单张失败只该张为 `None`。"""

        if not images:
            return []
        if not self.enabled:
            return [None] * len(images)
        return list(await asyncio.gather(*(self.caption_image(*item) for item in images)))

    async def caption_image(self, content: bytes, media_type: str) -> str | None:
        """为单张图生成图注；不可用、超限或失败都返回 `None`。"""

        if not self.enabled:
            return None
        if len(content) > MAX_INLINE_IMAGE_BYTES:
            logger.warning("图片超过视觉调用上限，跳过图注：%s", media_type)
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": USER_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": _data_url(content, media_type)},
                        },
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": MAX_CAPTION_TOKENS,
        }

        async with self._semaphore:
            try:
                async with self._client_scope() as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    response.raise_for_status()
                    data = response.json()
            except Exception:  # noqa: BLE001 - 视觉失败绝不阻断导入
                logger.warning("视觉调用失败，该图退回无图注：%s", media_type, exc_info=True)
                return None

        raw = _extract_content(data)
        if not raw.strip():
            # 思考型模型（响应里带 reasoning_content）可能把 max_tokens 全花在思考上，
            # 正文为空。静默丢图注会让人以为"这张图没内容"，留一条线索便于排查。
            logger.warning("视觉响应没有正文，该图退回无图注：%s", media_type)
        return _clean_caption(raw)


def _data_url(content: bytes, media_type: str) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _extract_content(data: object) -> str:
    """从 OpenAI 兼容响应里取出正文；结构异常返回空串。"""

    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _clean_caption(raw: str) -> str | None:
    """规整模型输出：去掉自带前缀与空白，过滤“信息量不足”，截断到上限。"""

    text = " ".join(raw.split())
    if not text:
        return None
    if text.strip().strip("。.!！：:").upper() == INSUFFICIENT:
        return None
    for prefix in (CAPTION_PREFIX, "图注:", "图注 :"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    if not text:
        return None
    if len(text) > MAX_CAPTION_CHARS:
        text = f"{text[:MAX_CAPTION_CHARS].rstrip()}…"
    return text
