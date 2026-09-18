"""系统配置接口：模型网关与阈值（tasklist 13.4）。只需 `sys:model`。

Embedding / Rerank 走本地权重，控制台不提供配置项（PRD §6.7）；这里能改的是 Chat 网关
（Base URL / API Key / 模型名 / 温度 / 最大 Token）与三个阈值。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_perm
from app.common.db import get_session
from app.schemas.common import success
from app.schemas.system import ModelConfigUpdate
from app.services import system as system_service

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/model-config")
async def get_model_config(
    current: CurrentUser = Depends(require_perm("sys:model")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """读配置。API Key 只回掩码与「是否已配置」，明文永不出网关（PRD §6.7）。"""

    view = await system_service.get_model_config(session)
    return success(view.model_dump(mode="json"))


@router.put("/model-config")
async def update_model_config(
    payload: ModelConfigUpdate,
    current: CurrentUser = Depends(require_perm("sys:model")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """局部更新，返回更新后的视图（仍是掩码）。"""

    view = await system_service.update_model_config(session, payload)
    return success(view.model_dump(mode="json"))
