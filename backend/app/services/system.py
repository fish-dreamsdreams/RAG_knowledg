"""系统配置服务：模型网关与阈值（tasklist 13.4）。

两条硬规矩：

1. **密钥只进不出**：API Key 以 Fernet 密文入库，读回来只给掩码 + 「是否已配置」，
   控制台永远拿不到明文（PRD §6.7）。
2. **改哪项写哪项**：`ModelConfigUpdate` 只覆盖显式传了的字段，未传的保持原值——
   控制台分 Tab 保存时不会把别的字段清空。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.crypto import CryptoError, decrypt_secret, encrypt_secret, mask_secret
from app.common.errors import AppError
from app.models import ModelConfig
from app.repositories import system as system_repo
from app.schemas.system import ModelConfigUpdate, ModelConfigView


def _to_view(row: ModelConfig) -> ModelConfigView:
    """密文 → 掩码。解不开就报 500：那说明加密密钥变了，硬失败比假装"没配"更有用。"""

    masked: str | None = None
    if row.api_key_encrypted:
        try:
            masked = mask_secret(decrypt_secret(row.api_key_encrypted))
        except CryptoError as exc:
            raise AppError.internal(str(exc)) from exc

    return ModelConfigView(
        base_url=row.base_url,
        chat_model=row.chat_model,
        temperature=row.temperature,
        max_tokens=row.max_tokens,
        rerank_model=row.rerank_model,
        faq_sim_threshold=row.faq_sim_threshold,
        gap_sim_threshold=row.gap_sim_threshold,
        faq_cluster_min_freq=row.faq_cluster_min_freq,
        api_key_configured=bool(row.api_key_encrypted),
        api_key_masked=masked,
    )


async def get_model_config(session: AsyncSession) -> ModelConfigView:
    return _to_view(await system_repo.load_model_config(session))


async def update_model_config(
    session: AsyncSession, payload: ModelConfigUpdate
) -> ModelConfigView:
    """局部更新。`api_key`：非空串 = 加密覆盖，空串 = 清除（回落到 env），不传/null = 不动。"""

    await system_repo.lock_model_config(session)
    row = await system_repo.load_model_config(session)

    fields = payload.model_dump(exclude_unset=True)
    api_key = fields.pop("api_key", None)
    for name, value in fields.items():
        if value is not None:
            setattr(row, name, value)

    if "api_key" in payload.model_fields_set and api_key is not None:
        stripped = api_key.strip()
        row.api_key_encrypted = encrypt_secret(stripped) if stripped else None

    await system_repo.save_model_config(session, row)
    await session.commit()
    return _to_view(row)
