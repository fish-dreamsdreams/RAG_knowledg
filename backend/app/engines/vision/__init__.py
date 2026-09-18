"""视觉模型客户端（design.md §3.1：只调用外部 API，不做业务编排）。

`ASSETS_VISION_ENABLED` 默认关闭；关闭时所有调用直接返回 `None`，导入链路照常走完。
"""

from app.engines.vision.client import (
    CAPTION_PREFIX,
    INSUFFICIENT,
    MAX_CAPTION_CHARS,
    VisionClient,
)

__all__ = [
    "CAPTION_PREFIX",
    "INSUFFICIENT",
    "MAX_CAPTION_CHARS",
    "VisionClient",
]
