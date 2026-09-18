"""子块去重与父块回溯（tasklist 9.3，TECH_SPEC §8.1）。

召回命中的是子块，生成要的却是它所属**父块**的完整正文：子块为了向量质量切得短，
父块约 1200 字，拼起来才有上下文可用。

三条规则：

- 按 `chunk_id` 去重：融合已在同一轮内去重，这里再兜一次，挡住多轮调用拼在一起的重复。
- 同一父块的多个命中子块**只取一次正文**——父块本身完整，不需要合并它的子块。
- 总长按父块**首次出现顺序**截断到 `MAX_CONTEXT_CHARS` 汉字，不按分数插队：顺序一变，
  上下文与重排名次就对不上，citation 也会错位。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import NamedTuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import knowledge as knowledge_repo

logger = logging.getLogger(__name__)

# 送进生成的上下文字数上限（汉字）
MAX_CONTEXT_CHARS = 6000


class ParentContext(NamedTuple):
    """一个父块，以及本轮命中它的子块 id（citation 用）。"""

    unit_id: UUID
    parent_id: UUID
    content: str
    child_ids: list[UUID]


async def expand_parents(
    session: AsyncSession,
    chunk_ids: Sequence[UUID],
    *,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> list[ParentContext]:
    """把命中的子块 id 回溯成父块正文，按序截断到 `max_chars`。"""

    unique = list(dict.fromkeys(chunk_ids))
    if not unique:
        return []

    children = await knowledge_repo.get_chunks_by_ids(session, unique)
    hit_children = {row.id: row for row in children}
    # 父块顺序来自入参顺序，而入参顺序承载的是融合名次
    ordered_parent_ids = list(
        dict.fromkeys(row.parent_id for row in children if row.parent_id is not None)
    )
    if not ordered_parent_ids:
        return []

    parents = {
        row.id: row
        for row in await knowledge_repo.get_chunks_by_ids(session, ordered_parent_ids)
    }

    hit_child_ids: dict[UUID, list[UUID]] = {}
    for child_id in unique:
        row = hit_children.get(child_id)
        if row is not None and row.parent_id is not None:
            hit_child_ids.setdefault(row.parent_id, []).append(child_id)

    contexts: list[ParentContext] = []
    used = 0
    for parent_id in ordered_parent_ids:
        if used >= max_chars:
            logger.debug("上下文已达 %s 字上限，丢弃剩余父块", max_chars)
            break
        parent = parents.get(parent_id)
        if parent is None:
            # 数据不一致（父块被删）：跳过，别在上下文里留空段
            logger.warning("命中子块的父块不存在：%s", parent_id)
            continue
        remaining = max_chars - used
        content = (
            parent.content
            if len(parent.content) <= remaining
            else parent.content[:remaining]
        )
        used += len(content)
        contexts.append(
            ParentContext(
                unit_id=parent.unit_id,
                parent_id=parent_id,
                content=content,
                child_ids=hit_child_ids.get(parent_id, []),
            )
        )
    return contexts
