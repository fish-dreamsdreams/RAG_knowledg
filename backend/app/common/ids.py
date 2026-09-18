"""UUID 解析小工具。

Milvus 返回的 id、WebSocket 与 HTTP 传入的字符串都要转成 UUID 才能查库。解析失败返回
`None` 而不是抛异常：调用方（检索链、审计）的语义都是「这条记录不可用就跳过」，让一个坏
id 把整轮问答打挂没有道理。
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID


def parse_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def parse_uuids(values: Iterable[object]) -> list[UUID]:
    """逐个解析并丢弃失败项，保持原顺序、不去重（去重由调用方按语义决定）。"""

    return [parsed for value in values if (parsed := parse_uuid(value)) is not None]
