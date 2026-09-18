"""认证与组织的出入参。"""

import uuid

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class IdentityOut(BaseModel):
    """登录态身份：前端菜单与按钮守卫直接读 permissions。"""

    user_id: uuid.UUID
    username: str
    display_name: str
    department_id: uuid.UUID
    department_name: str | None = None
    role_ids: list[uuid.UUID] = []
    # 角色码供前端决定登录后默认落点（PRD §4）；权限判定仍只看 permissions
    role_codes: list[str] = []
    permissions: list[str] = []
    is_active: bool = True


class LoginData(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: IdentityOut
