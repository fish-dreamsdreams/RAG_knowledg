"""模型配置里敏感字段的加解密（tasklist 13.4）。

只做两件事：明文变密文、密文还原明文。**不碰数据库、不碰 HTTP**，因此可以在
仓储、服务、引擎任一层调用。

**为什么用 Fernet**：它是「版本号 + 时间戳 + HMAC」的现成格式，密文自包含，库里只存一列
字符串（`model_configs.api_key_encrypted`）就够；自造 AES 封装还得自己定字节布局、自己做
完整性校验，错一处就是静默降级。

**密钥来源**：优先 `CONFIG_ENCRYPTION_KEY`；留空时从 `SECRET_KEY` 派生。派生只为省掉一项
开发配置——轮换 `SECRET_KEY` 会让已存密文全部解不开，生产必须显式配置。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.common.config import settings

# 派生用途串：同一个 SECRET_KEY 在不同用途下派生出不同密钥，JWT 签名与配置加密不共用一串字节
_DERIVE_INFO = "kb-model-config-v1"
# 掩码固定长度的星号 + 尾 4 位：够管理员认出配的是哪把钥匙，又不足以还原
MASK_STARS = "********"
MASK_TAIL_CHARS = 4
# 短于这个长度就整体掩掉——留尾 4 位会把大半把钥匙露出来
MASK_KEEP_TAIL_MIN_LENGTH = 12


class CryptoError(RuntimeError):
    """密钥不可用或密文解不开。

    刻意不是 `AppError`：这一层不知道调用方对外该给 500 还是运行时错误，由上层决定
    （接口转 `AppError.internal`，问答链转 `RuntimeError`）。
    """


def _fernet() -> Fernet:
    """按配置构造 Fernet：优先 CONFIG_ENCRYPTION_KEY，否则从 SECRET_KEY 派生。"""
    raw = settings.config_encryption_key.strip()
    if raw:
        key = raw.encode()
    else:
        digest = hashlib.sha256(f"{_DERIVE_INFO}:{settings.secret_key}".encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CryptoError(
            "CONFIG_ENCRYPTION_KEY 不是合法的 Fernet 密钥（应为 urlsafe base64 的 32 字节）"
        ) from exc


def encrypt_secret(plaintext: str) -> str:
    """明文 → 可入库的密文。空串由调用方处理（表示清除已存密钥），这里直接拒绝。"""

    if not plaintext:
        raise ValueError("待加密内容不能为空")
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """密文 → 明文。

    解不开一律抛 `CryptoError`，**不返回空串**：空串会被上层当成「没配密钥」而悄悄回退到
    env，配错密钥就此变成查不出来的隐患（问答用 env 的钥匙跑，控制台显示"已配置"）。
    """

    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CryptoError("模型配置的 API Key 无法解密，请重新保存密钥") from exc


def mask_secret(plaintext: str) -> str:
    """回显用的掩码。"""

    if not plaintext:
        return ""
    if len(plaintext) < MASK_KEEP_TAIL_MIN_LENGTH:
        return "*" * len(plaintext)
    return f"{MASK_STARS}{plaintext[-MASK_TAIL_CHARS:]}"
