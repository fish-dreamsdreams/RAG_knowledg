"""构造 Chat 客户端。

网关地址、模型名、密钥的解析顺序是「数据库配置优先，env 兜底」：

- `model_configs.base_url` / `chat_model` 是运行期权威，管理员应在系统配置页改。
- `api_key_encrypted` 是 Fernet 密文（tasklist 13.4 起由系统配置接口写入）：解出来用它，
  没有密文才回落 env。解不开一律报错而不是静默用 env——静默换钥匙会让管理员以为用的是
  自己在控制台保存的那把。

**模型名刻意不设默认值**：不同网关的模型名毫无共性，猜一个只会让「配错了」表现为运行时
400，而不是在配置检查时就报清楚。缺什么就说什么，比抛一句网关的原始报错有用。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.common.config import settings
from app.common.crypto import CryptoError, decrypt_secret
from app.models import ModelConfig


def _resolve_base_url(model_config: ModelConfig) -> str:
    return (model_config.base_url or "").strip() or settings.llm_base_url.strip()


def _resolve_model(model_config: ModelConfig) -> str:
    return (model_config.chat_model or "").strip() or settings.llm_chat_model.strip()


def _resolve_api_key(model_config: ModelConfig) -> str:
    """库里的密文优先，env 兜底。"""

    if model_config.api_key_encrypted:
        try:
            return decrypt_secret(model_config.api_key_encrypted)
        except CryptoError as exc:
            raise RuntimeError(str(exc)) from exc
    return settings.llm_api_key.strip()


def build_chat_model(model_config: ModelConfig, *, streaming: bool = False) -> ChatOpenAI:
    """构造 Chat 客户端。

    配置不全时抛 `RuntimeError`：这是部署错误而非用户错误，要在日志里说清缺哪一项，而不是
    等网关回一个看不懂的 401。
    """

    base_url = _resolve_base_url(model_config)
    model = _resolve_model(model_config)
    api_key = _resolve_api_key(model_config)

    missing = [
        name
        for name, value in (
            ("LLM_BASE_URL", base_url),
            ("LLM_API_KEY", api_key),
            ("chat_model / LLM_CHAT_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Chat 网关配置不完整，缺少：" + "、".join(missing))

    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=model_config.temperature,
        max_tokens=model_config.max_tokens,
        timeout=settings.llm_timeout_seconds,
        streaming=streaming,
        # 不显式打开时多数网关的流式响应不带 usage，审计里的 token 就永远是 0
        stream_usage=streaming,
    )
