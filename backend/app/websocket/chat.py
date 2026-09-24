"""WebSocket 问答通道（tasklist 12.6，TECH_SPEC §4.6）。

职责只有三件：握手鉴权、心跳与空闲回收、把 `qa_graph.astream` 的事件翻成前端消息。
**不编排检索流程**——节点顺序属于图，这里不允许出现业务分支。

协议：

| 方向 | 消息 |
|------|------|
| 收 | `{"type": "ask", "session_id": "...", "content": "..."}`、`{"type": "ping"}` |
| 发 | `status` / `token` / `citation` / `acl_notice` / `done` / `error` / `pong` / `heartbeat` |

三个需要解释的实现决定：

1. **先 `accept` 再按鉴权结果关闭**。Starlette 在未 accept 时关闭，uvicorn 只会回一个 HTTP
   403，拿不到自定义关闭码；而规范要求前端能凭 `4401` 弹登录并保留草稿。代价是握手能完成，
   换到的是协议要求的可区分信号。
2. **答案文本一律以 `token` 下发**。FAQ 命中、拒答、缺口三个出口是固定文案、不流式产出；
   若只转发 `generate` 的 token，这三个出口在前端会得到一个空气泡。所以本轮一个 token 都
   没发过时，把最终答案作为单个 token 补齐，协议对前端保持单一形状。
3. **一次只处理一个 `ask`**。上一问未结束时新的 `ask` 只回错误、不另开一轮——并发会让两轮
   token 交错写进同一个气泡。问答进行中仍读 `ping` 并回 `pong`，否则开发反代会把长时间
   静默的连接掐掉，前端就显示「生成中断」。
4. **答完之前中断都补一条 `interrupted` 审计**。图的 `audit` 节点是唯一写审计的地方，
   而客户端断开与上游报错都会让它跑不到；不补记，P12（每次问答必写一条）就在最常见的
   失败路径上失效。已经拿到 `audit_id` 的轮次不补记，保证恰好一条。
5. **会话历史在这里落库**（14.4）：提问先落、答案答完再落，因此中断的那一轮在历史里
   只留一个问题——这正是当时发生的事。历史写入失败绝不阻断问答：拿不到历史比答不出来轻。

`acl_notice` 只带布尔量、不含标题：`denied` 在 State 里也只有 unit_id（P3）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import partial
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.api.deps import CurrentUser
from app.common.db import SessionLocal
from app.common.errors import AppError, ErrorCode
from app.common.logging import set_trace_id
from app.common.redis import get_redis
from app.common.security import decode_token
from app.engines.acl import AclSubject
from app.graphs.context import DEPS_KEY, GraphDeps
from app.graphs.qa_graph import qa_graph
from app.repositories import chat as chat_repo
from app.repositories import org as org_repo
from app.repositories import system as system_repo
from app.services import chat as chat_service
from app.services import permission as permission_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

CHAT_PERMISSION = "ai:chat"
# 客户端每 30s 发 ping；空闲 10 分钟没有任何消息才断开（TECH_SPEC §4.6）
IDLE_TIMEOUT_SECONDS = 600
# 跑图期间若下行长时间无帧，Vite 反代 / 协议 ping 会把连接掐掉。15s 发一帧 heartbeat。
HEARTBEAT_SECONDS = 15
# 单测里关掉：假 socket 把「收件箱空」当成断开，跑图时再读会把正常问答误判成中断。
ACK_CLIENT_DURING_ASK = True
# 自定义关闭码：4401 令牌失效（规范指定）／4403 无 ai:chat 权限／4408 空闲超时
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_IDLE_TIMEOUT = 4408

# 节点名 → 对外 status。规范只登记了这几个阶段，`expand_parent` / `audit` 与各出口节点
# 刻意不出现在这里：把内部节点名全盘透出去只会让前端跟着后端重构一起改。
STATUS_BY_NODE = {
    "faq_match": "faq_hit",
    "rewrite": "rewrite",
    "retrieve_hybrid": "retrieve",
    "rerank": "rerank",
    "cutoff": "cutoff",
    "acl_filter": "acl",
    "generate": "generate",
}


class ClientGone(Exception):
    """客户端已断开：用于从推流中途退出来。"""


@dataclass
class _Turn:
    """一轮问答的转发状态。中断时也靠它写出可用的审计行。"""

    streamed: bool = False
    answer: str = ""
    faq_hit: bool = False
    audit_id: str | None = None
    allowed_unit_ids: list[str] = field(default_factory=list)
    denied_count: int = 0
    citation_ids: list[str] = field(default_factory=list)
    # 下发给前端的完整引用（含附图）：历史回放要还原成一样的引用卡片，只存 unit_id 不够
    citations: list[dict[str, Any]] = field(default_factory=list)


# ---------- 发送与关闭 ----------


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """发送一条消息；连接已断时抛 `ClientGone`，由调用方决定如何收尾。"""

    if websocket.client_state is not WebSocketState.CONNECTED:
        raise ClientGone
    try:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError) as exc:
        raise ClientGone from exc


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await _send_json(websocket, {"type": "error", "code": code, "message": message})


async def _send_error_quietly(websocket: WebSocket, code: str, message: str) -> None:
    """发不出去就算了：客户端已经走了，此时唯一还有意义的动作是收尾。"""

    try:
        await _send_error(websocket, code, message)
    except ClientGone:
        pass


async def _close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:  # 已经关过了
        pass


# ---------- 鉴权 ----------


async def _authenticate(websocket: WebSocket) -> CurrentUser | None:
    """按 `?access_token=` 校验 JWT 并装载权限上下文。

    Token 走查询参数而不是请求头：浏览器的 WebSocket 构造函数不支持自定义请求头。参数名沿用
    图片代理接口的 `access_token`，避免同一个项目里出现两套叫法。
    """

    token = websocket.query_params.get("access_token") or ""
    if not token:
        return None
    try:
        payload = decode_token(token)
        user_id = UUID(str(payload["sub"]))
    except (AppError, KeyError, ValueError):
        return None

    async with SessionLocal() as session:
        user = await org_repo.get_user(session, user_id)
        if user is None or not user.is_active:
            # 停用或软删用户，Token 未过期也不放行（TECH_SPEC §4.5）
            return None
        context = await permission_service.get_context(session, user)
        return CurrentUser(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            department_id=context.department_id,
            role_ids=context.role_ids,
            permissions=context.permissions,
        )


def _has_chat_permission(current: CurrentUser) -> bool:
    return CHAT_PERMISSION in current.permissions or "*" in current.permissions


# ---------- 推流 ----------


async def _heartbeat_loop(websocket: WebSocket, stop: asyncio.Event) -> None:
    """跑图期间定时发一帧，避免鉴权后到首 token 的静默窗口被反代掐线。"""

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
            return
        except TimeoutError:
            await _send_json(websocket, {"type": "heartbeat"})


async def _ack_client_during_ask(websocket: WebSocket, pump: asyncio.Task[None]) -> None:
    """问答进行中继续读连接：pong 保活，多余 ask 拒绝；对端断开则取消跑图。"""

    while not pump.done():
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect as exc:
            if not pump.done():
                pump.cancel()
            raise ClientGone from exc
        except RuntimeError as exc:
            if "not connected" not in str(exc).lower():
                raise
            if not pump.done():
                pump.cancel()
            raise ClientGone from exc

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await _send_error(websocket, ErrorCode.SYS_VALIDATION, "消息不是合法 JSON")
            continue
        if not isinstance(message, dict):
            await _send_error(websocket, ErrorCode.SYS_VALIDATION, "消息必须是对象")
            continue

        kind = message.get("type")
        if kind == "ping":
            await _send_json(websocket, {"type": "pong"})
            continue
        if kind == "ask":
            await _send_error(websocket, ErrorCode.SYS_VALIDATION, "上一问尚未结束")
            continue
        await _send_error(
            websocket, ErrorCode.SYS_VALIDATION, f"未知消息类型：{kind}"
        )


async def _pump(
    websocket: WebSocket,
    question: str,
    current: CurrentUser,
    session_id: str | None,
    turn: _Turn,
) -> None:
    """跑一遍问答图，把节点更新与 token 转成前端消息，并把终态字段记进 `turn`。"""

    async with SessionLocal() as session:
        deps = GraphDeps(
            session=session,
            redis=get_redis(),
            model_config=await system_repo.load_model_config(session),
            subject=AclSubject(
                user_id=current.user_id,
                department_id=current.department_id,
                role_ids=current.role_ids,
            ),
            session_id=session_id,
        )
        config = {"configurable": {DEPS_KEY: deps}}

        # aclosing：客户端中途消失时立即关掉图，而不是等 GC。关得晚会让已经没人要的
        # 那一轮继续留在事件循环里，也把「恰好一条审计」交给时机去保证。
        async with aclosing(
            qa_graph.astream(
                {"question": question, "user_id": str(current.user_id)},
                config=config,
                # updates 给节点进度与终态字段；custom 给 generate 逐字产出的 token
                stream_mode=["updates", "custom"],
            )
        ) as stream:
            stop = asyncio.Event()
            beat = asyncio.create_task(
                _heartbeat_loop(websocket, stop), name="qa-ws-heartbeat"
            )
            try:
                async for mode, payload in stream:
                    if beat.done():
                        await beat
                    if mode == "custom":
                        await _forward_custom(websocket, payload, turn)
                        continue
                    await _forward_updates(websocket, payload, turn, session)
            finally:
                stop.set()
                beat.cancel()
                try:
                    await beat
                except asyncio.CancelledError:
                    pass
                except ClientGone:
                    pass


async def _forward_custom(websocket: WebSocket, payload: Any, turn: _Turn) -> None:
    if not isinstance(payload, dict):
        return
    kind = payload.get("type")
    if kind == "heartbeat":
        await _send_json(websocket, {"type": "heartbeat"})
        return
    if kind != "token":
        return
    delta = payload.get("delta")
    if not isinstance(delta, str) or not delta:
        return
    turn.streamed = True
    await _send_json(websocket, {"type": "token", "delta": delta})


async def _forward_updates(
    websocket: WebSocket,
    payload: Any,
    turn: _Turn,
    session: Any | None = None,
) -> None:
    if not isinstance(payload, dict):
        return

    for node, delta in payload.items():
        if not isinstance(delta, dict):
            continue

        status = STATUS_BY_NODE.get(node)
        if status is not None:
            await _send_json(websocket, {"type": "status", "stage": status})

        if node == "faq_match" and delta.get("faq_hit"):
            turn.faq_hit = True

        if node == "acl_filter":
            turn.allowed_unit_ids = [
                str(hit.unit_id) for hit in delta.get("allowed") or []
            ]
            turn.denied_count = len(delta.get("denied") or [])
            if delta.get("acl_notice"):
                # 只说"涉及无权内容"，说不出是哪一份（P3）
                await _send_json(websocket, {"type": "acl_notice", "has_denied": True})

        if node == "expand_parent" and session is not None:
            # 生成可能空等几十秒。这里提交，避免整轮占用连接处于 idle-in-transaction。
            await session.commit()

        for citation in delta.get("citations") or []:
            turn.citation_ids.append(str(citation.get("unit_id", "")))
            turn.citations.append(citation)
            await _send_json(websocket, {"type": "citation", **citation})

        if isinstance(delta.get("answer"), str):
            turn.answer = delta["answer"]
        if delta.get("audit_id"):
            turn.audit_id = str(delta["audit_id"])


async def _handle_ask(
    websocket: WebSocket, current: CurrentUser, message: dict[str, Any]
) -> None:
    question = str(message.get("content") or "").strip()
    if not question:
        await _send_error(websocket, ErrorCode.SYS_VALIDATION, "问题内容不能为空")
        return

    trace_id = str(uuid4())
    set_trace_id(trace_id)
    started = perf_counter()
    turn = _Turn()

    session_id = await _open_history_turn(websocket, current, message, question)
    if session_id is _REJECTED:
        return

    incomplete = partial(
        _record_interrupted, current, session_id, question, trace_id, started, turn
    )

    pump = asyncio.create_task(
        _pump(
            websocket, question, current, str(session_id) if session_id else None, turn
        ),
        name="qa-ws-pump",
    )
    ack = (
        asyncio.create_task(_ack_client_during_ask(websocket, pump), name="qa-ws-ack")
        if ACK_CLIENT_DURING_ASK
        else None
    )
    try:
        # 图的 deps 里会话 id 是字符串（审计列由 SQLAlchemy 转 UUID），历史写入直接用 UUID
        await pump
    except asyncio.CancelledError:
        await incomplete()
        return
    except ClientGone:
        await incomplete()
        return
    except AppError as exc:
        # 图在 audit 之前就失败了；对用户和对审计都是「生成中断」
        await incomplete()
        await _send_error_quietly(websocket, exc.code, exc.message)
        return
    except Exception:  # noqa: BLE001 - 兜底，避免一条坏问答关掉整条连接
        logger.exception("问答执行失败 user_id=%s", current.user_id)
        await incomplete()
        await _send_error_quietly(websocket, ErrorCode.SYS_INTERNAL, "服务器内部错误")
        return
    finally:
        if ack is not None:
            ack.cancel()
            try:
                await ack
            except asyncio.CancelledError:
                pass
            except ClientGone:
                pass

    await _close_history_turn(session_id, turn)

    try:
        if not turn.streamed and turn.answer:
            # 固定文案出口不流式产出，补一个 token 让前端只需处理一种形状
            await _send_json(websocket, {"type": "token", "delta": turn.answer})
        await _send_json(
            websocket,
            {
                "type": "done",
                "audit_id": turn.audit_id,
                "faq_hit": turn.faq_hit,
                # 首轮问答时会话才被创建，前端要靠它认领这次会话（否则刷新后找不到）
                "session_id": str(session_id) if session_id else None,
            },
        )
    except ClientGone:
        # 审计行已由 audit 节点写入，这里不能再补一条（P12：每次问答恰好一条）
        logger.info("done 下发时客户端已断开 trace_id=%s", trace_id)


# ---------- 会话历史（tasklist 14.4） ----------


class _Rejected:
    """占位哨兵：区分「没有会话」（`None`，正常）与「会话被拒」（必须终止本轮）。"""


_REJECTED = _Rejected()


async def _open_history_turn(
    websocket: WebSocket,
    current: CurrentUser,
    message: dict[str, Any],
    question: str,
) -> UUID | None | _Rejected:
    """校验/创建会话并落用户消息。

    客户端给的 `session_id` 只是候选：归属校验在库里做，不属于当前用户就按「不存在」拒绝，
    否则用户能把消息写进别人的会话（P4 的同一条底线）。返回 `_REJECTED` 表示已经回过错误帧。
    """

    candidate = _uuid_or_none(message.get("session_id"))
    try:
        async with SessionLocal() as session:
            return await chat_service.start_turn(
                session,
                user_id=current.user_id,
                session_id=candidate,
                question=question,
            )
    except AppError as exc:
        await _send_error_quietly(websocket, exc.code, exc.message)
        return _REJECTED
    except Exception:  # noqa: BLE001 - 存量历史写不进去不该让这一轮问答答不出来
        logger.exception("会话历史写入失败 user_id=%s", current.user_id)
        return None


async def _close_history_turn(session_id: UUID | None, turn: _Turn) -> None:
    """落助手消息；失败只记日志——答案已经吐给用户了，此时再报错只会让人以为白问了一次。"""

    if session_id is None or not turn.answer:
        return
    try:
        async with SessionLocal() as session:
            await chat_service.finish_turn(
                session,
                session_id=session_id,
                answer=turn.answer,
                citations=turn.citations,
            )
    except Exception:  # noqa: BLE001
        logger.exception("助手消息写入失败 session_id=%s", session_id)


async def _record_interrupted(
    current: CurrentUser,
    session_id: UUID | None,
    question: str,
    trace_id: str,
    started: float,
    turn: _Turn,
) -> None:
    """补一条 `interrupted` 审计，补齐那些没跑到 `audit` 节点的问答。

    两种情形：客户端在答完之前断开，或图在上游报错。两者对用户都是「生成中断，请重试」，
    也都不含任何图表里已有的结论；不补记，P12 就在最常见的两条失败路径上失效。
    `answer_status` 的 `interrupted` 取值本就在表结构里预留。

    已经拿到 `audit_id` 说明 `audit` 节点写过行了：此时绝不能补，否则一次问答两条记录。
    """

    if turn.audit_id is not None:
        return

    logger.info("问答未完成，补记 interrupted 审计 trace_id=%s", trace_id)
    try:
        async with SessionLocal() as session:
            await chat_repo.add_audit_log(
                session,
                trace_id=trace_id,
                user_id=current.user_id,
                session_id=session_id,
                question=question,
                faq_hit=turn.faq_hit,
                allowed_unit_ids=turn.allowed_unit_ids,
                denied_count=turn.denied_count,
                citation_ids=turn.citation_ids,
                answer_status="interrupted",
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
            )
    except Exception:  # noqa: BLE001 - 补记失败不该再抛给已经断开的连接
        logger.exception("interrupted 审计写入失败 trace_id=%s", trace_id)


def _uuid_or_none(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


# ---------- 连接主循环 ----------


async def _serve(websocket: WebSocket, current: CurrentUser) -> None:
    while True:
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=IDLE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.info("WS 空闲超时 user_id=%s", current.user_id)
            await _close(websocket, CLOSE_IDLE_TIMEOUT)
            return

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await _send_error(websocket, ErrorCode.SYS_VALIDATION, "消息不是合法 JSON")
            continue
        if not isinstance(message, dict):
            await _send_error(websocket, ErrorCode.SYS_VALIDATION, "消息必须是对象")
            continue

        kind = message.get("type")
        if kind == "ping":
            await _send_json(websocket, {"type": "pong"})
            continue
        if kind != "ask":
            await _send_error(
                websocket, ErrorCode.SYS_VALIDATION, f"未知消息类型：{kind}"
            )
            continue

        await _handle_ask(websocket, current, message)


@router.websocket("/ws/chat")
async def chat_socket(websocket: WebSocket) -> None:
    """`WS /api/v1/ws/chat`（TECH_SPEC §4.5 登记为 `ai:chat`）。"""

    await websocket.accept()

    current = await _authenticate(websocket)
    if current is None:
        await _close(websocket, CLOSE_UNAUTHORIZED)
        return
    if not _has_chat_permission(current):
        await _close(websocket, CLOSE_FORBIDDEN)
        return

    logger.info("WS 已连接 user_id=%s", current.user_id)
    try:
        await _serve(websocket, current)
    except WebSocketDisconnect:
        logger.info("WS 已断开 user_id=%s", current.user_id)
    except ClientGone:
        logger.info("WS 发送失败，连接已失效 user_id=%s", current.user_id)
    except RuntimeError as exc:
        # 对端先断时，随后 receive_text 会变成「未连接」而不是 WebSocketDisconnect
        if "not connected" not in str(exc).lower():
            raise
        logger.info("WS 已失效 user_id=%s", current.user_id)
