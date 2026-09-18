"""问答会话、消息与审计写入。

写入一律**立即提交**：会话与消息要马上可见（前端刷新即读取，WS 层更是没有请求事务可等），
审计更是 P12 的凭据，不能跟着失败请求一起回滚。
"""

import uuid
from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatMessage, ChatSession, QaAuditLog


async def add_audit_log(session: AsyncSession, **fields) -> QaAuditLog:
    """插入一条审计行并提交。

    构造与提交集中在一处：图的 `audit` 节点与 WebSocket 层的「中断」出口都要写同一张表，
    字段口径不一致会让看板把两类记录算成不同的东西。

    这里直接提交而不是等请求事务收尾：审计是 P12 的凭据，不能跟着失败请求一起回滚。
    """

    # 主键显式生成：`UUIDPkMixin` 的默认值要等 flush 才落到实例上，而调用方拿到行之后
    # 立刻就要把 id 回给前端。显式赋值让「已入库的 id」在提交后一定可读。
    row = QaAuditLog(id=uuid4(), **fields)
    session.add(row)
    await session.commit()
    return row


async def audit_logs_excluding_prefix(
    session: AsyncSession, *, keep_prefix: str
) -> list[QaAuditLog]:
    """报告用：`trace_id` 不以 `keep_prefix` 开头的审计行。

    清场工具的「目标集合」——演示种子写的行带固定前缀（`demo_logs.DEMO_TRACE_PREFIX`），
    其余都是真跑出来的（验收脚本、手工演示、真实使用）。
    """

    rows = (
        (
            await session.execute(
                select(QaAuditLog)
                .where(~QaAuditLog.trace_id.startswith(keep_prefix))
                .order_by(QaAuditLog.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def count_audit_logs(
    session: AsyncSession, *, keep_prefix: str | None = None
) -> int:
    """审计行数；给了 `keep_prefix` 就只数该前缀的行（清场报告用）。"""

    statement = select(func.count()).select_from(QaAuditLog)
    if keep_prefix is not None:
        statement = statement.where(QaAuditLog.trace_id.startswith(keep_prefix))
    return int((await session.execute(statement)).scalar_one())


async def delete_audit_logs(session: AsyncSession, ids: Sequence[uuid.UUID]) -> int:
    """按 id 删除审计行并提交，返回删除行数。

    **只有维护脚本会调它。** `QaAuditLog` 的文档串写着「每次问答必写一条（P12）。不清理历史」
    ——「审计不删」是产品层的不变量，这里开删除口子只为演示库复位，`rg` 不到任何 API 或控制台
    路径调用它；要接进产品就不是加个接口的事，而是要先推翻那条决定。
    """

    if not ids:
        return 0
    result = await session.execute(delete(QaAuditLog).where(QaAuditLog.id.in_(ids)))
    await session.commit()
    return int(result.rowcount or 0)


async def create_session(
    session: AsyncSession, *, user_id: uuid.UUID, title: str | None
) -> ChatSession:
    """开一个会话。`user_id` 由服务端从登录态取，不接受客户端传入。"""

    # 与审计同理：id 显式生成，调用方要立刻拿到它（WS 首轮问答就要把 id 回给前端）
    row = ChatSession(id=uuid4(), user_id=user_id, title=title)
    session.add(row)
    await session.commit()
    return row


async def get_owned_session(
    session: AsyncSession, *, session_id: uuid.UUID, user_id: uuid.UUID
) -> ChatSession | None:
    """按「会话 id + 归属用户」查询，查不到就是查不到（所有权判定只在库里做）。"""

    rows = await session.execute(
        select(ChatSession).where(
            ChatSession.id == session_id, ChatSession.user_id == user_id
        )
    )
    return rows.scalar_one_or_none()


async def list_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, offset: int, limit: int
) -> tuple[list[ChatSession], int]:
    """当前用户的会话，最近活跃在前。"""

    conditions = ChatSession.user_id == user_id
    total = (
        await session.execute(select(func.count()).select_from(ChatSession).where(conditions))
    ).scalar_one()
    rows = await session.execute(
        select(ChatSession)
        .where(conditions)
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars()), total


async def delete_session(session: AsyncSession, row: ChatSession) -> None:
    """物理删除；消息靠外键 `ON DELETE CASCADE` 一起走。

    审计行里的 `session_id` 是普通字段（没有外键），删会话不会带走历史审计——
    审计是留痕，不该被用户的整理动作抹掉。
    """

    await session.delete(row)
    await session.commit()


async def add_message(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: str,
    content: str,
    citations: list[dict] | None = None,
) -> ChatMessage:
    row = ChatMessage(
        id=uuid4(),
        session_id=session_id,
        role=role,
        content=content,
        citations=citations,
    )
    session.add(row)
    await session.commit()
    return row


async def list_messages(
    session: AsyncSession, *, session_id: uuid.UUID, offset: int, limit: int
) -> tuple[list[ChatMessage], int]:
    """按时间正序：会话回放必须与当时的阅读顺序一致。"""

    conditions = ChatMessage.session_id == session_id
    total = (
        await session.execute(select(func.count()).select_from(ChatMessage).where(conditions))
    ).scalar_one()
    rows = await session.execute(
        select(ChatMessage)
        .where(conditions)
        # created_at 精度到微秒，同一轮的两条消息理论上可能撞上，用 id 兜底保证顺序稳定
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars()), total


async def touch_session(session: AsyncSession, *, session_id: uuid.UUID) -> None:
    """把 `updated_at` 推到当前时间，让会话在列表里冒到最前。"""

    await session.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id)
        .values(updated_at=func.now())
    )
    await session.commit()
