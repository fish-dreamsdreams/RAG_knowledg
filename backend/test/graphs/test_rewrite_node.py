"""rewrite 节点测试（tasklist 12.2）。

重点在两件事：模型输出不规范时能否退到可用值，以及网关调用失败时是否**不**静默降级。
后者是个刻意的取舍——检索链只依赖本地模型，网关挂了确实仍能召回，但生成必然失败；
在改写处就报错，比让用户拿到一个质量莫名变差的答案要好查得多。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.common.errors import AppError, ErrorCode
from app.graphs.context import GraphDeps
from app.graphs.nodes import rewrite as rewrite_node
from app.graphs.nodes.rewrite import rewrite

QUESTION = "年假几天"


@pytest.fixture
def config():
    def _make() -> dict:
        deps = GraphDeps(
            session=object(),
            redis=object(),
            model_config=SimpleNamespace(temperature=0.2, max_tokens=512),
            subject=object(),
        )
        return {"configurable": {"deps": deps}}

    return _make


class _Chat:
    def __init__(
        self,
        content: object = None,
        error: Exception | None = None,
        usage: dict | None = None,
    ) -> None:
        self._content = content
        self._error = error
        self._usage = usage

    async def ainvoke(self, _messages):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._content, usage_metadata=self._usage)


@pytest.fixture
def chat(monkeypatch):
    """把网关调用换成可控的假客户端。"""

    def _install(
        content: object = None, error: Exception | None = None, usage: dict | None = None
    ) -> None:
        fake = _Chat(content=content, error=error, usage=usage)
        monkeypatch.setattr(rewrite_node, "build_chat_model", lambda *_a, **_k: fake)

    return _install


# ---------- 正常解析 ----------


async def test_valid_json_fills_all_three_fields(chat, config) -> None:
    chat(
        '{"rewritten": "年假可以休几天", "keywords": ["年假", "休假"], '
        '"hyde": "员工年假天数按工龄确定。"}'
    )

    result = await rewrite({"question": QUESTION}, config())

    assert result == {
        "rewritten": "年假可以休几天",
        "keywords": ["年假", "休假"],
        "hyde": "员工年假天数按工龄确定。",
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


async def test_usage_is_collected_for_audit(chat, config) -> None:
    """token 用量必须真的从 usage_metadata 取出：审计与看板的 token 趋势都靠它。"""

    chat(
        '{"rewritten": "年假几天", "keywords": ["年假"], "hyde": ""}',
        usage={"input_tokens": 156, "output_tokens": 322},
    )

    result = await rewrite({"question": QUESTION}, config())

    assert result["prompt_tokens"] == 156
    assert result["completion_tokens"] == 322


async def test_usage_falls_back_to_prompt_completion_naming(chat, config) -> None:
    """部分网关沿用 prompt/completion 命名，不能因此把用量记成 0。"""

    chat(
        '{"rewritten": "年假几天", "keywords": [], "hyde": ""}',
        usage={"prompt_tokens": 11, "completion_tokens": 22},
    )

    result = await rewrite({"question": QUESTION}, config())

    assert result["prompt_tokens"] == 11
    assert result["completion_tokens"] == 22


async def test_json_in_code_fence_is_parsed(chat, config) -> None:
    """模型把 JSON 包进代码块是常态，不该因此丢掉整次改写。"""

    chat('```json\n{"rewritten": "年假几天", "keywords": ["年假"], "hyde": "假设文档"}\n```')

    result = await rewrite({"question": QUESTION}, config())

    assert result["keywords"] == ["年假"]
    assert result["hyde"] == "假设文档"


async def test_json_with_surrounding_prose_is_parsed(chat, config) -> None:
    chat('好的，结果如下：\n{"rewritten": "年假能休几天", "keywords": [], "hyde": ""}\n以上。')

    result = await rewrite({"question": QUESTION}, config())

    assert result["rewritten"] == "年假能休几天"


async def test_empty_hyde_becomes_none(chat, config) -> None:
    """空串与 None 在下游是同一件事，统一成 None，免得检索节点多一种判断。"""

    chat('{"rewritten": "年假几天", "keywords": ["年假"], "hyde": "   "}')

    result = await rewrite({"question": QUESTION}, config())

    assert result["hyde"] is None


async def test_keywords_strips_blanks_and_non_strings(chat, config) -> None:
    chat('{"rewritten": "年假几天", "keywords": [" 年假 ", "", 42, null], "hyde": ""}')

    result = await rewrite({"question": QUESTION}, config())

    assert result["keywords"] == ["年假"]


# ---------- 回退 ----------


async def test_unparsable_output_falls_back_to_original(chat, config) -> None:
    chat("抱歉，我无法理解这个问题。")

    result = await rewrite({"question": QUESTION}, config())

    assert result == {
        "rewritten": QUESTION,
        "keywords": [],
        "hyde": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


async def test_missing_rewritten_falls_back_to_original(chat, config) -> None:
    """`rewritten` 缺失时退回原问句；退回空串会让后续检索去查一个空向量。"""

    chat('{"keywords": ["年假"], "hyde": "假设文档"}')

    result = await rewrite({"question": QUESTION}, config())

    assert result["rewritten"] == QUESTION
    assert result["keywords"] == ["年假"]


async def test_non_string_rewritten_falls_back(chat, config) -> None:
    chat('{"rewritten": 123, "keywords": [], "hyde": ""}')

    result = await rewrite({"question": QUESTION}, config())

    assert result["rewritten"] == QUESTION


async def test_keywords_not_a_list_is_dropped(chat, config) -> None:
    chat('{"rewritten": "年假几天", "keywords": "年假", "hyde": ""}')

    result = await rewrite({"question": QUESTION}, config())

    assert result["keywords"] == []


async def test_non_string_content_is_tolerated(chat, config) -> None:
    """部分网关会把 content 回成结构化对象，此时按不可用处理而不是崩掉。"""

    chat([{"type": "text", "text": '{"rewritten": "x", "keywords": [], "hyde": ""}'}])

    result = await rewrite({"question": QUESTION}, config())

    assert result["rewritten"] == QUESTION


# ---------- HyDE 开关 ----------


async def test_hyde_dropped_when_disabled(chat, config, monkeypatch) -> None:
    """关掉 HyDE 后 State 里就不该有 hyde，下游不必各自记得判断开关。"""

    monkeypatch.setattr(rewrite_node.settings, "hyde_enabled", False)
    chat('{"rewritten": "年假几天", "keywords": ["年假"], "hyde": "假设文档"}')

    result = await rewrite({"question": QUESTION}, config())

    assert result["hyde"] is None
    assert result["keywords"] == ["年假"]


# ---------- 上游失败 ----------


async def test_gateway_failure_raises_upstream_error(chat, config) -> None:
    """网络/鉴权/超时不能回退成原问句：那是把"网关挂了"伪装成"答案质量变差"。"""

    chat(error=RuntimeError("connection reset"))

    with pytest.raises(AppError) as excinfo:
        await rewrite({"question": QUESTION}, config())

    assert excinfo.value.code == ErrorCode.AI_UPSTREAM_ERROR
