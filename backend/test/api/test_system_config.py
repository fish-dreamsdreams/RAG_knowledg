"""模型配置接口测试（tasklist 13.4）。

需要 PostgreSQL。最要紧的一条：**任何响应里都不能出现明文 API Key**，所以每个写密钥的用例
都顺手断言明文不在响应体内。

用例会覆盖单行配置表，因此进用例先备份、出用例再恢复——演示环境里管理员可能已经配好了网关，
不能让跑一次测试把网关配置清掉。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.common.crypto import decrypt_secret
from app.common.db import SessionLocal
from app.models import ModelConfig

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "Demo@123456"
PLAINTEXT_KEY = "sk-console-abcdefghijkl"


async def login_headers(client: AsyncClient, username: str = "admin") -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
async def isolated_config():
    """备份 → 清空 → 用例 → 恢复。返回可读当前行的辅助函数。"""

    async with SessionLocal() as session:
        rows = (await session.execute(select(ModelConfig))).scalars().all()
        backup = [
            {column.name: getattr(row, column.name) for column in ModelConfig.__table__.columns}
            for row in rows
        ]
        await session.execute(delete(ModelConfig))
        await session.commit()

    async def _read() -> ModelConfig:
        async with SessionLocal() as session:
            row = (await session.execute(select(ModelConfig).limit(1))).scalar_one_or_none()
            assert row is not None
            session.expunge(row)
            return row

    yield _read

    async with SessionLocal() as session:
        await session.execute(delete(ModelConfig))
        for fields in backup:
            session.add(ModelConfig(**fields))
        await session.commit()


async def test_get_returns_defaults_without_key(
    client: AsyncClient, isolated_config
) -> None:
    """表为空时回默认值（网关字段留空，等管理员填），密钥标记为未配置。"""

    headers = await login_headers(client)
    response = await client.get("/api/v1/system/model-config", headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["base_url"] == "" and data["chat_model"] == ""
    assert data["api_key_configured"] is False
    assert data["api_key_masked"] is None
    assert data["temperature"] == 0.2
    assert data["max_tokens"] == 2048
    assert data["rerank_model"] == "BAAI/bge-reranker-v2-m3"
    assert data["faq_sim_threshold"] == 0.88
    # 0.62 是真实问答标定后的默认值（tasklist 15.2），不要改回 0.35
    assert data["gap_sim_threshold"] == 0.62
    assert data["faq_cluster_min_freq"] == 3


async def test_put_stores_ciphertext_and_returns_mask(
    client: AsyncClient, isolated_config
) -> None:
    headers = await login_headers(client)
    response = await client.put(
        "/api/v1/system/model-config",
        json={
            "base_url": "https://gateway.example.com/v1",
            "chat_model": "demo-chat",
            "temperature": 0.3,
            "api_key": PLAINTEXT_KEY,
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert PLAINTEXT_KEY not in response.text
    data = response.json()["data"]
    assert data["api_key_configured"] is True
    assert data["api_key_masked"] == "********ijkl"
    assert data["base_url"] == "https://gateway.example.com/v1"
    assert data["chat_model"] == "demo-chat"
    assert data["temperature"] == 0.3

    row = await isolated_config()
    assert row.api_key_encrypted is not None
    assert PLAINTEXT_KEY not in row.api_key_encrypted
    assert decrypt_secret(row.api_key_encrypted) == PLAINTEXT_KEY


async def test_put_only_touches_provided_fields(client: AsyncClient, isolated_config) -> None:
    """控制台分 Tab 保存：没传的字段不能被顺手清空。"""

    headers = await login_headers(client)
    await client.put(
        "/api/v1/system/model-config",
        json={"base_url": "https://gateway.example.com/v1", "chat_model": "demo-chat"},
        headers=headers,
    )
    response = await client.put(
        "/api/v1/system/model-config", json={"temperature": 0.5}, headers=headers
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["temperature"] == 0.5
    assert data["base_url"] == "https://gateway.example.com/v1"
    assert data["chat_model"] == "demo-chat"


async def test_put_with_empty_key_clears_it(client: AsyncClient, isolated_config) -> None:
    """传空串 = 清除，回落到 env 兜底。"""

    headers = await login_headers(client)
    await client.put(
        "/api/v1/system/model-config", json={"api_key": PLAINTEXT_KEY}, headers=headers
    )
    response = await client.put(
        "/api/v1/system/model-config", json={"api_key": ""}, headers=headers
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["api_key_configured"] is False
    assert data["api_key_masked"] is None

    row = await isolated_config()
    assert row.api_key_encrypted is None


async def test_put_rejects_out_of_range_threshold(client: AsyncClient, isolated_config) -> None:
    headers = await login_headers(client)
    response = await client.put(
        "/api/v1/system/model-config", json={"faq_sim_threshold": 1.5}, headers=headers
    )
    assert response.status_code == 400, response.text


async def test_undecryptable_stored_key_reports_error(
    client: AsyncClient, isolated_config
) -> None:
    """存进去的密文解不开时明确报错：假装"没配"会让问答悄悄用 env 的钥匙跑。"""

    headers = await login_headers(client)
    async with SessionLocal() as session:
        session.add(ModelConfig(api_key_encrypted="not-a-fernet-token"))
        await session.commit()

    response = await client.get("/api/v1/system/model-config", headers=headers)
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "SYS_INTERNAL"


async def test_requires_model_permission(client: AsyncClient, fake_identity) -> None:
    fake_identity(permissions=["dash:view"])

    assert (await client.get("/api/v1/system/model-config")).status_code == 403
    assert (
        await client.put("/api/v1/system/model-config", json={"chat_model": "x"})
    ).status_code == 403
