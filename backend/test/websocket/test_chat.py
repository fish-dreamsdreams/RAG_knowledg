"""WebSocket 问答通道测试（tasklist 12.6）。

分三层，各自证明不同的东西：

1. **转发层**：喂确定的事件流给假图，逐条核对出站消息。协议收窄（只发规范登记的字段、
   不把 State 原样透出去）在这里钉死。
2. **端到端**：接上**真实问答图**与图测试里的同一套假件，断言浏览器真会收到的东西。
   P3（denied 标题与正文不出现在任何出站消息）只有在这一层才成立得住。
3. **ASGI 层**：用 `TestClient` 真连一次，证明路由挂在 `/api/v1/ws/chat`、握手后的 4401
   能被前端拿到——这两件事都不是假 socket 能证明的。

假 socket 直接实现 `accept` / `send_text` / `close` / `receive_text`：Starlette 的
`WebSocket` 是个薄壳，替身比拉一整套 ASGI 中间件更好定位失败。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.common.security import create_access_token
from app.graphs.nodes.prompts import DENIED_ANSWER
from app.main import app
from app.models import ChatMessage, ChatSession, QaAuditLog
from app.websocket import chat as chat_ws

# 复用问答图端到端测试里的假件装配：这一层要验证的正是「真实图 → 真实消息」，
# 另造一套 fakes 只会让两边慢慢走偏。
from test.graphs.test_qa_graph import (  # noqa: F401 - pytest 靠导入注册 fixture
    ALLOWED_TEXT,
    DENIED_TEXT,
    DENIED_TITLE,
    QUESTION,
    env,
)

MODEL_CONFIG = SimpleNamespace(
    faq_sim_threshold=0.88,
    gap_sim_threshold=0.35,
    rerank_model="test-reranker",
    temperature=0.2,
)

# 规范登记的出站消息形状：任何多出来的字段都算把内部状态漏给了前端
MESSAGE_FIELDS = {
    "status": {"type", "stage"},
    "token": {"type", "delta"},
    "citation": {"type", "unit_id", "title", "chunk_id", "snippet"},
    "acl_notice": {"type", "has_denied"},
    "done": {"type", "audit_id", "faq_hit", "session_id"},
    "error": {"type", "code", "message"},
    "pong": {"type"},
    "heartbeat": {"type"},
}


class _Result:
    """只实现仓库层用到的取数方法。"""

    def __init__(self, row: Any = None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _Session:
    """只实现会话历史与审计写入用到的方法。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0
        # 归属查询的答案：默认「这条会话存在且属于当前用户」；用例可改成 None 模拟他人会话
        self.owned: Any = SimpleNamespace(id=uuid4())

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.owned)

    async def commit(self) -> None:
        self.commits += 1

    @property
    def rows(self) -> list[Any]:
        """审计行——本文件的 `rows` 断言说的都是 P12。"""

        return [row for row in self.added if isinstance(row, QaAuditLog)]

    @property
    def sessions(self) -> list[Any]:
        return [row for row in self.added if isinstance(row, ChatSession)]

    @property
    def messages(self) -> list[Any]:
        return [row for row in self.added if isinstance(row, ChatMessage)]


class _SessionFactory:
    """替代 `SessionLocal`：每次开「会话」都返回同一个假会话。"""

    def __init__(self, session: _Session) -> None:
        self._session = session

    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Socket:
    """假 WebSocket。

    `inbox` 用尽即视为对端断开，测试因此不会卡在 `receive_text` 上；需要「一直没消息」
    的场景（空闲超时）显式传 `block_forever=True`。
    """

    def __init__(
        self,
        inbox: list[str] | None = None,
        *,
        token: str | None = None,
        block_forever: bool = False,
        fail_on: set[str] | None = None,
        empty_wait: float = 0.0,
    ) -> None:
        self.query_params = {"access_token": token} if token else {}
        self.client_state = WebSocketState.CONNECTED
        self.accepted = False
        self.close_code: int | None = None
        self.sent: list[dict[str, Any]] = []
        self.raw: list[str] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        for item in inbox or []:
            self._inbox.put_nowait(item)
        self._block_forever = block_forever
        # 模拟"发送时客户端已经不见了"：命中这些类型即抛断开
        self._fail_on = fail_on or set()
        # 默认空收件箱立刻断开（让 chat_socket 能结束）。问答中途仍要读 ping 的用例把
        # 这个值调大，避免「空」比慢图更早被当成对端离开。
        self._empty_wait = empty_wait

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        assert self.client_state is WebSocketState.CONNECTED
        payload = json.loads(data)
        if payload["type"] in self._fail_on:
            self.client_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect(1006)
        self.raw.append(data)
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code
        self.client_state = WebSocketState.DISCONNECTED

    async def receive_text(self) -> str:
        if self.client_state is not WebSocketState.CONNECTED:
            raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')
        if self._block_forever:
            await asyncio.Event().wait()
        if self._empty_wait <= 0:
            try:
                return self._inbox.get_nowait()
            except asyncio.QueueEmpty as exc:
                raise WebSocketDisconnect(1000) from exc
        try:
            return await asyncio.wait_for(self._inbox.get(), timeout=self._empty_wait)
        except TimeoutError as exc:
            raise WebSocketDisconnect(1000) from exc

    @property
    def types(self) -> list[str]:
        return [message["type"] for message in self.sent]


class _Graph:
    """假问答图：只按给定顺序吐出事件。"""

    def __init__(self, events: list[tuple[str, Any]] | None = None) -> None:
        self.events = events or []
        self.asks: list[dict[str, Any]] = []

    async def astream(self, graph_input, config=None, stream_mode=None):
        self.asks.append(
            {"input": graph_input, "config": config, "stream_mode": stream_mode}
        )
        for event in self.events:
            yield event


class _FailingGraph(_Graph):
    """图在上游报错（网关 502、Milvus 不可用），此时它跑不到 `audit` 节点。"""

    async def astream(self, *args, **kwargs):
        raise chat_ws.AppError.ai_upstream()
        yield  # pragma: no cover - 让这是个异步生成器


class _BoomGraph(_Graph):
    async def astream(self, *args, **kwargs):
        raise RuntimeError("数据库连接串：postgresql://secret")
        yield  # pragma: no cover


class _MilvusDownGraph(_Graph):
    """检索层不可用：Milvus 连不上，`milvus_store` 把它转成 503 抛上来（P13）。"""

    async def astream(self, *args, **kwargs):
        raise chat_ws.AppError.dependency_unavailable()
        yield  # pragma: no cover - 让这是个异步生成器


def _ask(content: str = "出差住宿费怎么报销") -> str:
    return json.dumps(
        {"type": "ask", "session_id": str(uuid4()), "content": content},
        ensure_ascii=False,
    )


def _allowed_run() -> list[tuple[str, Any]]:
    """一次「允许 → 生成 → 审计」的真实事件顺序。"""

    citation = {
        "unit_id": str(uuid4()),
        "title": "差旅管理制度",
        "chunk_id": str(uuid4()),
        "snippet": ALLOWED_TEXT,
    }
    return [
        ("updates", {"faq_match": {"faq_hit": False}}),
        ("updates", {"rewrite": {"rewritten": "出差住宿费报销标准"}}),
        ("updates", {"retrieve_hybrid": {"allowed": []}}),
        ("updates", {"rerank": {"reranked": []}}),
        ("updates", {"cutoff": {"allowed": []}}),
        ("updates", {"acl_filter": {"allowed": [], "denied": [], "acl_notice": False}}),
        ("updates", {"expand_parent": {"parent_contexts": []}}),
        ("custom", {"type": "token", "delta": "依据制度，"}),
        ("custom", {"type": "token", "delta": "需提交住宿发票。"}),
        ("updates", {"generate": {"answer": "依据制度，需提交住宿发票。", "citations": [citation]}}),
        ("updates", {"audit": {"audit_id": str(uuid4())}}),
    ]


@pytest.fixture
def ws(monkeypatch) -> _Session:
    """把 WS 层的外部依赖换成假件，返回记录审计行的假会话。"""

    session = _Session()
    monkeypatch.setattr(chat_ws, "SessionLocal", _SessionFactory(session))
    monkeypatch.setattr(chat_ws, "get_redis", lambda: object())
    # 假 socket 把空收件箱当成断开；跑图期间再读会把正常问答误判成中断
    monkeypatch.setattr(chat_ws, "ACK_CLIENT_DURING_ASK", False)

    async def _load(_session):
        return MODEL_CONFIG

    monkeypatch.setattr(chat_ws.system_repo, "load_model_config", _load)
    return session


def _authenticate_as(
    monkeypatch, *, permissions: list[str] | None = None, user_id: UUID | None = None
) -> str:
    """装一个能走通鉴权的用户（绕开库与 Redis），返回可用的 `access_token`。

    `user_id` 只在断言「会话归谁」时需要指定；其余用例用随机 id 即可。
    """

    user_id = user_id or uuid4()
    department_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        username="zhangsan",
        display_name="张三",
        is_active=True,
    )

    async def _get_user(_session, _user_id):
        return user

    async def _get_context(_session, _user):
        return SimpleNamespace(
            department_id=department_id,
            # 权限只从角色解析，Token 里带什么都不作数（P11）
            role_ids=[uuid4()],
            permissions=["ai:chat"] if permissions is None else permissions,
        )

    monkeypatch.setattr(chat_ws.org_repo, "get_user", _get_user)
    monkeypatch.setattr(chat_ws.permission_service, "get_context", _get_context)
    return create_access_token(subject=str(user_id))


def _assert_protocol(socket: _Socket) -> None:
    """每条出站消息都必须严格是规范登记的形状。"""

    for message in socket.sent:
        expected = MESSAGE_FIELDS.get(message["type"])
        assert expected is not None, f"未登记的消息类型：{message['type']}"
        assert set(message) == expected, f"{message['type']} 字段不符：{sorted(message)}"


# ---------- 握手鉴权 ----------


async def test_missing_token_closes_with_4401(ws) -> None:
    socket = _Socket()

    await chat_ws.chat_socket(socket)

    assert socket.accepted, "必须先 accept，否则拿不到自定义关闭码"
    assert socket.close_code == 4401
    assert socket.sent == []


async def test_garbage_token_closes_with_4401(ws) -> None:
    socket = _Socket(token="not-a-jwt")

    await chat_ws.chat_socket(socket)

    assert socket.close_code == 4401


async def test_expired_token_closes_with_4401(ws) -> None:
    expired = create_access_token(subject=str(uuid4()), expires_minutes=-1)
    socket = _Socket(token=expired)

    await chat_ws.chat_socket(socket)

    assert socket.close_code == 4401


async def test_inactive_user_closes_with_4401_even_with_valid_token(
    ws, monkeypatch
) -> None:
    """停用账号 Token 未过期也不得放行。"""

    token = create_access_token(subject=str(uuid4()))

    async def _get_user(_session, _user_id):
        return SimpleNamespace(
            id=uuid4(), username="zhangsan", display_name="张三", is_active=False
        )

    monkeypatch.setattr(chat_ws.org_repo, "get_user", _get_user)
    socket = _Socket(token=token)

    await chat_ws.chat_socket(socket)

    assert socket.close_code == 4401


async def test_missing_chat_permission_closes_with_4403(ws, monkeypatch) -> None:
    token = create_access_token(subject=str(uuid4()))
    _authenticate_as(monkeypatch, permissions=["kb:view"])
    socket = _Socket(token=token)

    await chat_ws.chat_socket(socket)

    assert socket.close_code == 4403


# ---------- 心跳与空闲 ----------


async def test_ping_gets_pong(ws, monkeypatch) -> None:
    socket = _Socket([json.dumps({"type": "ping"})], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert socket.sent == [{"type": "pong"}]


async def test_idle_timeout_closes_with_4408(ws, monkeypatch) -> None:
    monkeypatch.setattr(chat_ws, "IDLE_TIMEOUT_SECONDS", 0.05)
    socket = _Socket(token=_authenticate_as(monkeypatch), block_forever=True)

    await chat_ws.chat_socket(socket)

    assert socket.close_code == 4408


async def test_heartbeat_sent_while_graph_is_silent(ws, monkeypatch) -> None:
    """鉴权后到首 token 可能静默几十秒，必须主动发帧，否则反代会掐线。"""

    monkeypatch.setattr(chat_ws, "HEARTBEAT_SECONDS", 0.03)

    class _Slow(_Graph):
        async def astream(self, *args, **kwargs):
            await asyncio.sleep(0.1)
            yield ("updates", {"faq_match": {"faq_hit": False}})
            yield ("updates", {"audit": {"audit_id": str(uuid4())}})

    monkeypatch.setattr(chat_ws, "qa_graph", _Slow())
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert "heartbeat" in socket.types
    _assert_protocol(socket)


async def test_ping_during_ask_gets_pong_before_done(ws, monkeypatch) -> None:
    """问答进行中也要回 pong，不能把 ping 积到答完。"""

    monkeypatch.setattr(chat_ws, "ACK_CLIENT_DURING_ASK", True)

    class _Slow(_Graph):
        async def astream(self, *args, **kwargs):
            await asyncio.sleep(0.12)
            yield ("updates", {"faq_match": {"faq_hit": False}})
            yield ("updates", {"audit": {"audit_id": str(uuid4())}})

    monkeypatch.setattr(chat_ws, "qa_graph", _Slow())
    socket = _Socket(
        [_ask(), json.dumps({"type": "ping"})],
        token=_authenticate_as(monkeypatch),
        empty_wait=0.4,
    )

    await chat_ws.chat_socket(socket)

    assert "pong" in socket.types
    assert socket.types.index("pong") < socket.types.index("done")
    _assert_protocol(socket)


# ---------- 消息校验 ----------


@pytest.mark.parametrize(
    ("raw", "hint"),
    [
        ("{不是 JSON", "合法 JSON"),
        (json.dumps(["ask"]), "必须是对象"),
        (json.dumps({"type": "whatever"}), "未知消息类型"),
    ],
)
async def test_bad_message_keeps_connection_alive(ws, monkeypatch, raw, hint) -> None:
    """坏消息只回 error，不关连接——否则一个前端 bug 会连带踢掉整条会话。"""

    socket = _Socket([raw], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert socket.types == ["error"]
    assert socket.sent[0]["code"] == "SYS_VALIDATION"
    assert hint in socket.sent[0]["message"]
    assert socket.close_code is None


async def test_empty_question_returns_error(ws, monkeypatch) -> None:
    socket = _Socket([_ask("   ")], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert socket.types == ["error"]
    assert "不能为空" in socket.sent[0]["message"]


# ---------- 转发 ----------


async def test_allowed_run_forwards_status_token_citation_done(ws, monkeypatch) -> None:
    monkeypatch.setattr(chat_ws, "qa_graph", _Graph(_allowed_run()))
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert socket.types == ["status"] * 6 + [
        "token",
        "token",
        "status",
        "citation",
        "done",
    ]
    assert [m["stage"] for m in socket.sent if m["type"] == "status"] == [
        "faq_hit",
        "rewrite",
        "retrieve",
        "rerank",
        "cutoff",
        "acl",
        "generate",
    ]
    assert "".join(m["delta"] for m in socket.sent if m["type"] == "token") == (
        "依据制度，需提交住宿发票。"
    )
    citation = next(m for m in socket.sent if m["type"] == "citation")
    assert citation["title"] == "差旅管理制度"
    assert socket.sent[-1]["type"] == "done"
    assert socket.sent[-1]["faq_hit"] is False
    assert UUID(socket.sent[-1]["audit_id"])  # 必须是可回查的有效 id
    _assert_protocol(socket)


async def test_ask_is_driven_through_the_graph(ws, monkeypatch) -> None:
    """WS 只做转发：`ask` 必须原样进图，且两种 stream_mode 都要开。"""

    graph = _Graph(_allowed_run())
    monkeypatch.setattr(chat_ws, "qa_graph", graph)
    socket = _Socket([_ask("年假几天")], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert len(graph.asks) == 1
    assert graph.asks[0]["input"]["question"] == "年假几天"
    assert set(graph.asks[0]["stream_mode"]) == {"updates", "custom"}
    assert chat_ws.DEPS_KEY in graph.asks[0]["config"]["configurable"]


async def test_denied_units_never_leak_through_the_socket(ws, monkeypatch) -> None:
    """P3：denied 只以布尔量告知；标题、正文、unit_id 一律不出现在出站消息里。"""

    denied_id = uuid4()
    graph = _Graph(
        [
            (
                "updates",
                {
                    "acl_filter": {
                        "allowed": [],
                        "denied": [
                            SimpleNamespace(
                                unit_id=denied_id,
                                title=DENIED_TITLE,
                                content=DENIED_TEXT,
                            )
                        ],
                        "acl_notice": True,
                    }
                },
            ),
            ("updates", {"respond_denied": {"answer": "抱歉，没有可访问的资料。"}}),
            ("updates", {"audit": {"audit_id": str(uuid4())}}),
        ]
    )
    monkeypatch.setattr(chat_ws, "qa_graph", graph)
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    outbound = "".join(socket.raw)
    assert DENIED_TITLE not in outbound
    assert DENIED_TEXT not in outbound
    assert str(denied_id) not in outbound
    assert [m for m in socket.sent if m["type"] == "acl_notice"] == [
        {"type": "acl_notice", "has_denied": True}
    ]
    _assert_protocol(socket)


async def test_non_streaming_exit_flushes_answer_as_one_token(ws, monkeypatch) -> None:
    """FAQ 命中等固定文案出口不流式产出，必须补一个 token，否则前端气泡是空的。"""

    graph = _Graph(
        [
            ("updates", {"faq_match": {"faq_hit": True}}),
            ("updates", {"respond_faq": {"answer": "满 1 年 5 天"}}),
            ("updates", {"audit": {"audit_id": str(uuid4())}}),
        ]
    )
    monkeypatch.setattr(chat_ws, "qa_graph", graph)
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert [m.get("delta") for m in socket.sent if m["type"] == "token"] == ["满 1 年 5 天"]
    assert socket.sent[-1]["faq_hit"] is True


async def test_streamed_answer_is_not_sent_twice(ws, monkeypatch) -> None:
    """已逐字下发过的答案不得在收尾时再补一遍。"""

    graph = _Graph(
        [
            ("custom", {"type": "token", "delta": "答"}),
            ("updates", {"generate": {"answer": "答", "citations": []}}),
            ("updates", {"audit": {"audit_id": str(uuid4())}}),
        ]
    )
    monkeypatch.setattr(chat_ws, "qa_graph", graph)
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert [m.get("delta") for m in socket.sent if m["type"] == "token"] == ["答"]


async def test_dependency_unavailable_stops_the_turn(ws, monkeypatch) -> None:
    """P13：依赖不可用时失败关闭——只有错误帧，一个字都不作答。

    真正的降级风险是「吞掉故障、拿空召回继续生成」：那样前端会看到一段像是在回答的普通
    文本，审计里也记不出异常。所以这里同时钉住三件事：出站只有 error、code 是 503 语义的
    `SYS_DEPENDENCY_UNAVAILABLE`、审计补一条 interrupted（P12）。
    """

    monkeypatch.setattr(chat_ws, "qa_graph", _MilvusDownGraph())
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    _assert_protocol(socket)
    assert socket.types == ["error"]
    assert socket.sent[0]["code"] == "SYS_DEPENDENCY_UNAVAILABLE"
    assert [row.answer_status for row in ws.rows] == ["interrupted"]


async def test_upstream_error_becomes_error_message(ws, monkeypatch) -> None:
    monkeypatch.setattr(chat_ws, "qa_graph", _FailingGraph())
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert socket.types == ["error"]
    assert socket.sent[0]["code"] == "AI_UPSTREAM_ERROR"
    assert socket.close_code is None


async def test_unexpected_error_becomes_internal_error(ws, monkeypatch) -> None:
    """一条坏问答不能把整条连接带走，也不能把堆栈漏给前端。"""

    monkeypatch.setattr(chat_ws, "qa_graph", _BoomGraph())
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert socket.types == ["error"]
    assert socket.sent[0]["code"] == "SYS_INTERNAL"
    assert "secret" not in socket.sent[0]["message"]


# ---------- 断流（P12） ----------


async def test_disconnect_mid_answer_writes_interrupted_audit(ws, monkeypatch) -> None:
    """流被中断时 audit 节点不会执行，必须补记一条 interrupted，否则 P12 有洞。"""

    monkeypatch.setattr(chat_ws, "qa_graph", _Graph(_allowed_run()))
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch), fail_on={"token"})

    await chat_ws.chat_socket(socket)

    assert len(ws.rows) == 1
    assert ws.rows[0].answer_status == "interrupted"
    assert ws.rows[0].question == "出差住宿费怎么报销"
    assert ws.rows[0].trace_id not in {"", "-"}


async def test_disconnect_after_audit_does_not_write_a_second_row(ws, monkeypatch) -> None:
    """审计节点已经写过行时不得再补一条：P12 要求每次问答恰好一条。"""

    monkeypatch.setattr(chat_ws, "qa_graph", _Graph(_allowed_run()))
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch), fail_on={"done"})

    await chat_ws.chat_socket(socket)

    assert ws.rows == []


@pytest.mark.parametrize("graph", [_FailingGraph(), _BoomGraph()])
async def test_failed_turn_still_writes_exactly_one_audit(ws, monkeypatch, graph) -> None:
    """上游报错同样跑不到 audit 节点，不补记 P12 就在失败路径上失效。"""

    monkeypatch.setattr(chat_ws, "qa_graph", graph)
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert socket.types == ["error"]
    assert [row.answer_status for row in ws.rows] == ["interrupted"]


async def test_interrupted_audit_reuses_the_same_table(ws, monkeypatch) -> None:  # noqa: ARG001
    """补记走的是与 audit 节点同一条写入口（同表、同字段口径）。"""

    monkeypatch.setattr(chat_ws, "qa_graph", _Graph(_allowed_run()))
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch), fail_on={"token"})

    await chat_ws.chat_socket(socket)

    (row,) = ws.rows
    assert type(row).__tablename__ == "qa_audit_logs"
    assert row.latency_ms >= 0


# ---------- 真实图端到端 ----------


async def test_real_graph_allowed_path_over_socket(ws, monkeypatch, env) -> None:
    """走真实图与真实节点：状态推进、逐字 token、引用卡片一条都不能少。"""

    socket = _Socket([_ask(QUESTION)], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert [m["stage"] for m in socket.sent if m["type"] == "status"] == [
        "faq_hit",
        "rewrite",
        "retrieve",
        "rerank",
        "cutoff",
        "acl",
        "generate",
    ]
    assert "".join(m["delta"] for m in socket.sent if m["type"] == "token") == (
        "依据制度，需提交住宿发票。"
    )
    assert [m["title"] for m in socket.sent if m["type"] == "citation"] == ["差旅管理制度"]
    assert socket.sent[-1]["type"] == "done"
    assert socket.sent[-1]["faq_hit"] is False
    _assert_protocol(socket)
    assert len(ws.rows) == 1, "P12：真实图一次问答一条审计"
    assert ws.rows[0].answer_status == "answered"


async def test_real_graph_denied_path_never_leaks_over_socket(ws, monkeypatch, env) -> None:
    """P3 的端到端版本：机密单元的标题与正文一个字都不能出现在出站消息里。

    只命中无权资料时 `acl_notice` 为假（节点口径：没有可读内容就是拒答，不需要再提示
    "部分被过滤"），固定拒答文案同样以 token 下发，前端只需处理一种形状。
    """

    env.mode = "denied"
    socket = _Socket([_ask(QUESTION)], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    outbound = "".join(socket.raw)
    assert DENIED_TITLE not in outbound
    assert DENIED_TEXT not in outbound
    assert [m for m in socket.sent if m["type"] == "acl_notice"] == []
    assert [m for m in socket.sent if m["type"] == "citation"] == []
    assert [m["delta"] for m in socket.sent if m["type"] == "token"] == [DENIED_ANSWER]
    assert socket.sent[-1]["type"] == "done"
    assert socket.sent[-1]["faq_hit"] is False
    _assert_protocol(socket)
    assert ws.rows[0].answer_status == "denied"


async def test_real_graph_gap_path_over_socket(ws, monkeypatch, env) -> None:
    """缺口出口同样要把固定文案送达浏览器，而不是留一个空气泡。"""

    env.mode = "gap"
    env.max_similarity = 0.11
    socket = _Socket([_ask(QUESTION)], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert [m["type"] for m in socket.sent].count("token") == 1
    assert socket.sent[-1]["type"] == "done"
    assert socket.sent[-1]["faq_hit"] is False
    assert env.gap_calls == 1


# ---------- 会话历史（tasklist 14.4） ----------


async def test_turn_persists_question_first_then_answer_with_citations(
    ws, monkeypatch
) -> None:
    """一轮问答落两条：提问先落、答案带引用后落，并把会话 id 回给前端。"""

    monkeypatch.setattr(chat_ws, "qa_graph", _Graph(_allowed_run()))
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    user_message, assistant_message = ws.messages
    assert (user_message.role, user_message.content) == ("user", "出差住宿费怎么报销")
    assert user_message.citations is None
    assert assistant_message.role == "assistant"
    assert assistant_message.content == "依据制度，需提交住宿发票。"
    assert [citation["title"] for citation in assistant_message.citations] == [
        "差旅管理制度"
    ]
    assert user_message.session_id == assistant_message.session_id == ws.owned.id
    assert socket.sent[-1]["session_id"] == str(ws.owned.id)


async def test_ask_without_session_id_creates_one_titled_by_the_question(
    ws, monkeypatch
) -> None:
    """会话由第一句话定义：没有 `session_id` 就现建一个，标题取那个问题。"""

    user_id = uuid4()
    monkeypatch.setattr(chat_ws, "qa_graph", _Graph(_allowed_run()))
    raw = json.dumps(
        {"type": "ask", "content": "出差住宿费怎么报销"}, ensure_ascii=False
    )
    socket = _Socket([raw], token=_authenticate_as(monkeypatch, user_id=user_id))

    await chat_ws.chat_socket(socket)

    (session_row,) = ws.sessions
    assert session_row.title == "出差住宿费怎么报销"
    # 归属只认登录态，客户端说什么都不作数
    assert session_row.user_id == user_id
    assert socket.sent[-1]["session_id"] == str(session_row.id)
    assert {message.session_id for message in ws.messages} == {session_row.id}


async def test_interrupted_turn_keeps_only_the_question(ws, monkeypatch) -> None:
    """中断的那一轮历史里只留问题：没有答案就不写助手消息，也不编一个。"""

    monkeypatch.setattr(chat_ws, "qa_graph", _Graph(_allowed_run()))
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch), fail_on={"token"})

    await chat_ws.chat_socket(socket)

    (user_message,) = ws.messages
    assert user_message.role == "user"
    assert [row.answer_status for row in ws.rows] == ["interrupted"]


async def test_foreign_session_is_rejected_without_running_the_graph(
    ws, monkeypatch
) -> None:
    """别人的 `session_id` 既不能用来写历史，也不能借它拿到答案。"""

    graph = _Graph(_allowed_run())
    monkeypatch.setattr(chat_ws, "qa_graph", graph)
    # 库里查不到：不存在与不属于当前用户，对外必须是同一个结果
    ws.owned = None
    socket = _Socket([_ask()], token=_authenticate_as(monkeypatch))

    await chat_ws.chat_socket(socket)

    assert socket.sent == [
        {"type": "error", "code": "AI_SESSION_NOT_FOUND", "message": "会话不存在"}
    ]
    assert graph.asks == [], "被拒的会话连图都不该跑"
    assert ws.messages == []
    assert ws.rows == []


# ---------- ASGI 层 ----------


def test_endpoint_answers_handshake_and_closes_4401_without_token(ws) -> None:
    """路由真的挂在 `/api/v1/ws/chat`，且握手后能拿到 4401（浏览器据此弹登录）。"""

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/api/v1/ws/chat") as socket:
            socket.receive_json()

    assert caught.value.code == 4401


async def test_endpoint_pong_over_asgi(ws, monkeypatch) -> None:
    client = TestClient(app)
    token = _authenticate_as(monkeypatch)

    with client.websocket_connect(f"/api/v1/ws/chat?access_token={token}") as socket:
        socket.send_json({"type": "ping"})
        assert socket.receive_json() == {"type": "pong"}
