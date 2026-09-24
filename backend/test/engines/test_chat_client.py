"""Chat 客户端构造测试（tasklist 12.2）。

锁住三件事：解析顺序（数据库配置优先、env 兜底）、缺配置时的报错是否说得清、以及参数是否
真的透传到客户端。这几条都是"配错了很难查"的地方——报错说不清缺什么，就只能对着网关的
401 猜。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.common.crypto import encrypt_secret
from app.engines.chat import build_chat_model
from app.engines.chat import client as chat_client

ENV_BASE_URL = "https://env.example.com"
ENV_API_KEY = "env-key"
ENV_MODEL = "env-model"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """把 env 侧配置固定成已知值，用例再按需覆盖数据库侧。"""

    monkeypatch.setattr(chat_client.settings, "llm_base_url", ENV_BASE_URL)
    monkeypatch.setattr(chat_client.settings, "llm_api_key", ENV_API_KEY)
    monkeypatch.setattr(chat_client.settings, "llm_chat_model", ENV_MODEL)
    monkeypatch.setattr(chat_client.settings, "llm_timeout_seconds", 60)


def _model_config(**overrides):
    fields = {
        "base_url": "",
        "chat_model": "",
        "temperature": 0.2,
        "max_tokens": 512,
        # 与真实单行配置一致：没配过密钥时为空，此时回落到 env
        "api_key_encrypted": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _secret(chat) -> str:
    key = getattr(chat, "openai_api_key", None)
    return key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)


def test_falls_back_to_env_when_database_is_empty() -> None:
    """系统配置页没填（`model_configs` 里三个字段都是空串）时必须落到 env。

    全新部署就是这个状态：配置页还没人碰过，问答链不能因此完全用不了。
    """

    chat = build_chat_model(_model_config())

    assert chat.model_name == ENV_MODEL
    assert str(chat.openai_api_base) == ENV_BASE_URL
    assert _secret(chat) == ENV_API_KEY


def test_database_overrides_env() -> None:
    """数据库是运行期权威：管理员在系统配置页改完，下一次请求就该生效。"""

    chat = build_chat_model(
        _model_config(base_url="https://db.example.com", chat_model="db-model")
    )

    assert chat.model_name == "db-model"
    assert str(chat.openai_api_base) == "https://db.example.com"


def test_blank_strings_are_treated_as_unset() -> None:
    """空白串不能当成"配过了"，否则会拼出一个非法 base_url。"""

    chat = build_chat_model(_model_config(base_url="   ", chat_model="  "))

    assert chat.model_name == ENV_MODEL
    assert str(chat.openai_api_base) == ENV_BASE_URL


def test_parameters_are_passed_through() -> None:
    chat = build_chat_model(_model_config(temperature=0.7, max_tokens=99), streaming=True)

    assert chat.temperature == 0.7
    assert chat.max_tokens == 99
    assert chat.streaming is True


def test_explicit_timeout_is_passed_through(monkeypatch) -> None:
    captured: dict = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(chat_client, "ChatOpenAI", _Fake)
    chat_client.build_chat_model(_model_config(), timeout=120)

    assert captured["timeout"] == 120


def test_stored_ciphertext_overrides_env_key() -> None:
    """系统配置页保存的密钥优先于 env（tasklist 13.4）：控制台改完立刻生效。"""

    chat = build_chat_model(
        _model_config(api_key_encrypted=encrypt_secret("sk-from-console-abcdef"))
    )

    assert _secret(chat) == "sk-from-console-abcdef"


def test_undecryptable_ciphertext_fails_loudly(monkeypatch) -> None:
    """密文解不开时报错，不悄悄改用 env 的钥匙——那样管理员会以为用的是自己保存的那把。"""

    cipher = encrypt_secret("sk-from-console-abcdef")
    monkeypatch.setattr(chat_client.settings, "secret_key", "another-secret")
    monkeypatch.setattr(chat_client.settings, "config_encryption_key", "")

    with pytest.raises(RuntimeError, match="无法解密"):
        build_chat_model(_model_config(api_key_encrypted=cipher))


def test_missing_model_names_the_missing_item(monkeypatch) -> None:
    """报错要指出缺哪一项。只说"网关配置不完整"，等于让人去猜。"""

    monkeypatch.setattr(chat_client.settings, "llm_chat_model", "")

    with pytest.raises(RuntimeError, match="chat_model"):
        build_chat_model(_model_config())


def test_missing_api_key_names_the_missing_item(monkeypatch) -> None:
    monkeypatch.setattr(chat_client.settings, "llm_api_key", "")

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        build_chat_model(_model_config())


def test_missing_base_url_names_the_missing_item(monkeypatch) -> None:
    monkeypatch.setattr(chat_client.settings, "llm_base_url", "")

    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        build_chat_model(_model_config())


def test_all_missing_items_are_listed_at_once(monkeypatch) -> None:
    """一次把缺的都列出来，免得改一项跑一次。"""

    monkeypatch.setattr(chat_client.settings, "llm_base_url", "")
    monkeypatch.setattr(chat_client.settings, "llm_api_key", "")
    monkeypatch.setattr(chat_client.settings, "llm_chat_model", "")

    with pytest.raises(RuntimeError) as excinfo:
        build_chat_model(_model_config())

    message = str(excinfo.value)
    assert all(item in message for item in ("LLM_BASE_URL", "LLM_API_KEY", "chat_model"))
