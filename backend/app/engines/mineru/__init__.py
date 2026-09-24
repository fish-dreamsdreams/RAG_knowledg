"""MinerU 在线 API 客户端（design.md §3.1：不做权限判定与业务编排）。"""

from app.engines.mineru.assets import (
    SUPPORTED_IMAGE_TYPES,
    ExtractedAsset,
    extract_assets,
)
from app.engines.mineru.client import (
    SUPPORTED_EXTENSIONS,
    TEXT_EXTENSIONS,
    MinerUClient,
    MinerUError,
    ParsedDocument,
    read_text_file,
)

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "SUPPORTED_IMAGE_TYPES",
    "TEXT_EXTENSIONS",
    "ExtractedAsset",
    "MinerUClient",
    "MinerUError",
    "ParsedDocument",
    "extract_assets",
    "read_text_file",
]
