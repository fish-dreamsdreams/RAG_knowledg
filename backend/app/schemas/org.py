"""组织架构出入参：部门、用户、角色、权限码。"""

import uuid

from pydantic import BaseModel, Field


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    parent_id: uuid.UUID | None = None
    is_active: bool = True


class DepartmentUpdate(BaseModel):
    """PATCH 语义：只处理请求里出现过的字段（`exclude_unset`）。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    parent_id: uuid.UUID | None = None
    is_active: bool | None = None


class DepartmentNode(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None = None
    is_active: bool = True
    children: list["DepartmentNode"] = []


class RoleBrief(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class RoleOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    permission_ids: list[uuid.UUID] = []


class RoleCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    permission_ids: list[uuid.UUID] = []


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    permission_ids: list[uuid.UUID] | None = None


class PermissionOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    type: str
    parent_id: uuid.UUID | None = None
    sort: int = 0


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    department_id: uuid.UUID
    role_ids: list[uuid.UUID] = []
    is_active: bool = True


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    department_id: uuid.UUID | None = None
    role_ids: list[uuid.UUID] | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    department_id: uuid.UUID
    department_name: str | None = None
    is_active: bool = True
    role_ids: list[uuid.UUID] = []
    roles: list[RoleBrief] = []
