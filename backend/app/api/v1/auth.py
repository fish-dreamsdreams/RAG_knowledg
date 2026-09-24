"""认证路由：登录与当前用户（TECH_SPEC §4.5）。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_auth
from app.common.config import settings
from app.common.db import get_session
from app.common.errors import AppError
from app.common.security import create_access_token, verify_password
from app.repositories import org as org_repo
from app.schemas.auth import IdentityOut, LoginData, LoginRequest
from app.schemas.common import success
from app.services import permission as permission_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """用户名口令登录。

    用户不存在、口令错误、账号停用一律返回同一句提示，避免枚举账号。
    """

    user = await org_repo.get_user_by_username(session, payload.username)
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise AppError.unauthorized("用户名或密码错误")

    context = await permission_service.get_context(session, user)
    names = await org_repo.department_names(session, {context.department_id})
    identity = IdentityOut(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        department_id=context.department_id,
        department_name=names.get(context.department_id),
        role_ids=context.role_ids,
        role_codes=context.role_codes,
        permissions=context.permissions,
        is_active=user.is_active,
    )
    return success(
        LoginData(
            access_token=create_access_token(subject=str(user.id)),
            expires_in=settings.access_token_expire_minutes * 60,
            user=identity,
        ).model_dump()
    )


@router.get("/me")
async def me(current: CurrentUser = Depends(require_auth)) -> dict:
    """当前登录态身份：刷新页面后前端据此恢复菜单与按钮权限。"""

    return success(
        IdentityOut(
            user_id=current.user_id,
            username=current.username,
            display_name=current.display_name,
            department_id=current.department_id,
            department_name=current.department_name,
            role_ids=current.role_ids,
            role_codes=current.role_codes,
            permissions=current.permissions,
            is_active=current.is_active,
        ).model_dump()
    )
