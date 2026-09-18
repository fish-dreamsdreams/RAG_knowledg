"""模型配置加解密测试（tasklist 13.4）。不连库、不连网。

锁住三件事：明文能原样还原、换密钥/被篡改一律报错（不返回空串）、掩码不泄漏。
"返回空串"这条尤其要盯：上层会把空串当成"没配密钥"而回落到 env，配错密钥就变成一个
查不出来的隐患。
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.common import crypto
from app.common.crypto import CryptoError, decrypt_secret, encrypt_secret, mask_secret

PLAINTEXT = "sk-live-abcdefghijkl"


@pytest.fixture(autouse=True)
def clean_key(monkeypatch):
    """每个用例从"未显式配置密钥"的状态开始（与开发默认一致）。"""

    monkeypatch.setattr(crypto.settings, "config_encryption_key", "")


def test_roundtrip_hides_plaintext() -> None:
    cipher = encrypt_secret(PLAINTEXT)

    assert PLAINTEXT not in cipher
    assert decrypt_secret(cipher) == PLAINTEXT


def test_same_plaintext_gets_different_ciphertext() -> None:
    """Fernet 每次加密都带新的 IV，密文不重复。"""

    assert encrypt_secret(PLAINTEXT) != encrypt_secret(PLAINTEXT)


def test_ciphertext_from_another_key_is_rejected(monkeypatch) -> None:
    """换过 SECRET_KEY（或显式密钥）后旧密文解不开，必须报错而不是回空串。"""

    cipher = encrypt_secret(PLAINTEXT)
    monkeypatch.setattr(crypto.settings, "secret_key", "another-secret")

    with pytest.raises(CryptoError):
        decrypt_secret(cipher)


def test_tampered_ciphertext_is_rejected() -> None:
    """Fernet 带 HMAC：改一个字符就该被认出来。"""

    cipher = encrypt_secret(PLAINTEXT)
    tampered = f"{cipher[:-6]}{'AAAAAA' if not cipher.endswith('AAAAAA') else 'BBBBBB'}"

    with pytest.raises(CryptoError):
        decrypt_secret(tampered)


def test_explicit_key_overrides_derived_one(monkeypatch) -> None:
    monkeypatch.setattr(
        crypto.settings, "config_encryption_key", Fernet.generate_key().decode()
    )

    assert decrypt_secret(encrypt_secret(PLAINTEXT)) == PLAINTEXT


def test_invalid_key_names_the_variable(monkeypatch) -> None:
    """密钥格式不对要指名道姓，否则只能对着 500 猜。"""

    monkeypatch.setattr(crypto.settings, "config_encryption_key", "not-a-fernet-key")

    with pytest.raises(CryptoError, match="CONFIG_ENCRYPTION_KEY"):
        encrypt_secret(PLAINTEXT)


def test_derived_key_is_stable_across_calls() -> None:
    """派生密钥必须稳定，否则本次进程内自己写的密文自己都解不开。"""

    assert decrypt_secret(encrypt_secret(PLAINTEXT)) == PLAINTEXT


def test_empty_plaintext_is_rejected() -> None:
    with pytest.raises(ValueError):
        encrypt_secret("")


def test_mask_keeps_only_the_tail() -> None:
    assert mask_secret(PLAINTEXT) == "********ijkl"
    assert mask_secret("") == ""
    # 短密钥整体掩掉：留尾 4 位会把大半把钥匙露出来
    assert mask_secret("short") == "*****"
