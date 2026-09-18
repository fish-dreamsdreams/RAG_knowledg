"""问答会话与消息（tasklist 14.4）。

会话**由第一句话定义**：不留「新建会话」接口。空会话是纯噪音——侧栏会堆一排没有内容的
「新会话」，还得靠过滤条件假装它们不存在。所以会话在第一轮问答开始时开、标题取首个问题。

所有权一律在库里判：客户端传来的 `session_id` 只是候选，`get_owned_session` 查不到就按
「不存在」处理——不存在与不属于当前用户必须给出同一个结果（`AppError.session_not_found`）。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import AppError
from app.models import ChatMessage, ChatSession
from app.repositories import chat as chat_repo

# 会话标题长度：侧栏一行放得下，再长也没人读
TITLE_MAX_CHARS = 30


def derive_title(question: str) -> str:
    """标题取首个问题，压平空白后截断。

    不按标点切：中文问句常常不带句号，按标点切等于不切。超长加省略号，让侧栏能看出被截过。
    """

    flat = " ".join(question.split())
    if len(flat) <= TITLE_MAX_CHARS:
        return flat
    return f"{flat[:TITLE_MAX_CHARS]}…"


async def list_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, offset: int, limit: int
) -> tuple[list[ChatSession], int]:
    return await chat_repo.list_sessions(
        session, user_id=user_id, offset=offset, limit=limit
    )


async def list_messages(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    offset: int,
    limit: int,
) -> tuple[list[ChatMessage], int]:
    """会话回放。非本人的会话按「不存在」处理，不区分 404 与 403。"""

    await _require_owned(session, session_id=session_id, user_id=user_id)
    return await chat_repo.list_messages(
        session, session_id=session_id, offset=offset, limit=limit
    )


async def delete_session(
    session: AsyncSession, *, user_id: uuid.UUID, session_id: uuid.UUID
) -> None:
    row = await _require_owned(session, session_id=session_id, user_id=user_id)
    await chat_repo.delete_session(session, row)


async def start_turn(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    question: str,
) -> uuid.UUID:
    """开一轮问答：校验会话归属（或新建），先把用户消息落库。

    先落用户消息再跑图：中途中断的那一轮，用户的提问也该留在历史里——否则刷新后只剩
    一条没有问题的回答更像是产品坏了。返回的 `session_id` 是本轮唯一可信的会话标识，
    后续写入都用它，不再回看客户端传来的值。
    """

    if session_id is None:
        row = await chat_repo.create_session(
            session, user_id=user_id, title=derive_title(question)
        )
    else:
        row = await _require_owned(session, session_id=session_id, user_id=user_id)

    await chat_repo.add_message(
        session, session_id=row.id, role="user", content=question
    )
    await chat_repo.touch_session(session, session_id=row.id)
    return row.id


async def finish_turn(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    answer: str,
    citations: list[dict] | None,
) -> None:
    """答完落助手消息（含引用）。

    `session_id` 只接受 `start_turn` 的返回值，不接受客户端字符串，因此这里不重复判归属。
    中断与生成失败不会走到这里：没有答案就没有助手消息，审计里已有 `interrupted` 记录。
    """

    await chat_repo.add_message(
        session,
        session_id=session_id,
        role="assistant",
        content=answer,
        citations=citations or None,
    )
    await chat_repo.touch_session(session, session_id=session_id)


async def _require_owned(
    session: AsyncSession, *, session_id: uuid.UUID, user_id: uuid.UUID
) -> ChatSession:
    row = await chat_repo.get_owned_session(
        session, session_id=session_id, user_id=user_id
    )
    if row is None:
        raise AppError.session_not_found()
    return row
