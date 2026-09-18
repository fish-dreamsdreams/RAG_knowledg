"""父子块切分（design.md §3.1：不调模型）。"""

from app.engines.chunking.chunker import (
    CHILD_MAX_CHARS,
    CHILD_OVERLAP_CHARS,
    CHILD_TARGET_CHARS,
    PARENT_MAX_CHARS,
    PARENT_OVERLAP_CHARS,
    PARENT_TARGET_CHARS,
    Child,
    Chunker,
    Parent,
    asset_names,
    content_hash,
)

__all__ = [
    "CHILD_MAX_CHARS",
    "CHILD_OVERLAP_CHARS",
    "CHILD_TARGET_CHARS",
    "PARENT_MAX_CHARS",
    "PARENT_OVERLAP_CHARS",
    "PARENT_TARGET_CHARS",
    "Child",
    "Chunker",
    "Parent",
    "asset_names",
    "content_hash",
]
