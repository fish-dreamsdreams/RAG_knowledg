"""路由依赖：鉴权、功能权限、分页。

权限判定不信任 Token 里的任何权限声明：JWT 只承载 `sub`，权限码每次按角色从
缓存/数据库解析，否则角色变更要等 Token 过期才生效（P11）。
"""

import uuid
from dataclasses import dataclass, field

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.db import get_session
from app.common.errors import AppError
from app.common.security import decode_token
from app.repositories import org as org_repo
from app.services import permission as permission_service

_bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    user_id: uuid.UUID
    username: str
    display_name: str
    department_id: uuid.UUID
    role_ids: list[uuid.UUID] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    department_name: str | None = None
    is_active: bool = True
    # 角色码：只给前端做默认落点与身份展示，鉴权一律看 permissions
    role_codes: list[str] = field(default_factory=list)


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> CurrentUser:
    """校验 Bearer Token、用户存在性与停用状态（tasklist 3.3）。

    停用（`is_active=false`）或已删除的用户一律 401，Token 未过期也不放行。
    """

    if credentials is None or not credentials.credentials:
        raise AppError.unauthorized()
    return await _load_current_user(session, credentials.credentials)


async def require_auth_query(
    access_token: str | None = Query(
        None, description="JWT；`<img>` 无法自定义请求头，媒体资源用查询参数传"
    ),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> CurrentUser:
    """媒体资源接口的鉴权（tasklist 17.4）：查询参数优先，其次 Authorization 头。

    与 WebSocket 握手同一套做法（TECH_SPEC §4.1）：浏览器的 `<img src>` 不能带 Bearer 头，
    而把预签名 URL 交给前端等于在有效期内绕过 ACL 校验点——所以图片统一走后端转发，
    凭据只能走查询参数。两种传法都支持，方便脚本与前端各自取用。
    """

    token = access_token or (credentials.credentials if credentials else None)
    if not token:
        raise AppError.unauthorized()
    return await _load_current_user(session, token)


async def _load_current_user(session: AsyncSession, token: str) -> CurrentUser:
    """Token → 当前用户：JWT 只给 `sub`，权限每次按角色重新解析（P11）。"""

    payload = decode_token(token)
    subject = payload.get("sub")
    if not subject:
        raise AppError.unauthorized()
    try:
        user_id = uuid.UUID(str(subject))
    except ValueError as exc:
        raise AppError.unauthorized() from exc

    user = await org_repo.get_user(session, user_id)
    if user is None or not user.is_active:
        raise AppError.unauthorized()

    context = await permission_service.get_context(session, user)
    names = await org_repo.department_names(session, {context.department_id})
    return CurrentUser(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        department_id=context.department_id,
        role_ids=context.role_ids,
        permissions=context.permissions,
        department_name=names.get(context.department_id),
        is_active=user.is_active,
        role_codes=context.role_codes,
    )


def require_perm(code: str):
    """功能权限码依赖（TECH_SPEC §4.5）：缺码 403。"""

    async def _dependency(current: CurrentUser = Depends(require_auth)) -> CurrentUser:
        if code not in current.permissions and "*" not in current.permissions:
            raise AppError.forbidden()
        return current

    return _dependency


@dataclass
class PageParams:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def pagination(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PageParams:
    return PageParams(page=page, page_size=page_size)
