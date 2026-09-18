"""知识单元接口的请求/响应模型（TECH_SPEC §4.2）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ImportedUnit(BaseModel):
    unit_id: UUID
    task_id: str
    source_filename: str


class ImportResult(BaseModel):
    """批量导入的受理结果：单元已建、任务已投，解析是异步的。"""

    upload_batch_id: UUID
    items: list[ImportedUnit]


class ImportStatus(BaseModel):
    unit_id: UUID
    parse_status: str
    stage: str
    progress: int
    error: str | None = None


class UnitListItem(BaseModel):
    """列表行：带 `parse_status` 与进度，供「解析中」Tag 展示（tasklist 7.1）。

    `acl_*` 三类名称只用于展示权限标签（PRD §6.3）；真正配置走 `AclView` 的 id。
    主体被删时名称缺失，行里仍是原 id 的短形式，不静默隐藏条目。
    """

    unit_id: UUID
    title: str
    category: str | None = None
    format: str
    status: str
    parse_status: str
    # 最近一次导入任务的 stage / progress；没有任务行为 None
    stage: str | None = None
    progress: int = 0
    parse_error: str | None = None
    file_size: int | None = None
    acl_global: bool = False
    acl_departments: list[str] = Field(default_factory=list)
    acl_roles: list[str] = Field(default_factory=list)
    acl_users: list[str] = Field(default_factory=list)
    version: int
    created_at: datetime
    updated_at: datetime


class UnitDetail(UnitListItem):
    source_filename: str
    created_by: UUID | None = None


class UnitUpdate(BaseModel):
    """更新元信息。

    `version` 必填做乐观锁（TECH_SPEC §5.1）；`title` / `category` 只有出现在请求体
    里才会改，因此传 `"category": null` 表示清空，不传表示保持原值。
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=64)
    version: int = Field(ge=1)


class AclPayload(BaseModel):
    """四维 ACL 保存体（tasklist 7.2）。三类主体为空表示该维度不授权。"""

    acl_global: bool = False
    departments: list[UUID] = Field(default_factory=list)
    roles: list[UUID] = Field(default_factory=list)
    users: list[UUID] = Field(default_factory=list)
    version: int = Field(ge=1)


class AclLabeledPrincipal(BaseModel):
    """带展示名的授权主体；主体已被删时 `name` 退化成 id 前 8 位。"""

    id: UUID
    name: str


class AclPrincipalLabels(BaseModel):
    departments: list[AclLabeledPrincipal] = Field(default_factory=list)
    roles: list[AclLabeledPrincipal] = Field(default_factory=list)
    users: list[AclLabeledPrincipal] = Field(default_factory=list)


class AclView(BaseModel):
    acl_global: bool
    departments: list[UUID]
    roles: list[UUID]
    users: list[UUID]
    version: int
    # 展示用名称：弹窗初值要显示「财务部」而不是 UUID（PRD §6.3）
    labels: AclPrincipalLabels = Field(default_factory=AclPrincipalLabels)


class ChunkItem(BaseModel):
    """切片预览行：只给序号、长度与摘要；列表只含子块。"""

    chunk_id: UUID
    level: str
    ordinal: int
    char_count: int
    excerpt: str
    parent_ordinal: int | None = None


class ChunkDetail(BaseModel):
    """子块全文（编辑框用）。"""

    chunk_id: UUID
    unit_id: UUID
    parent_id: UUID | None
    level: str
    ordinal: int
    char_count: int
    content: str
    content_hash: str | None
    excerpt: str


class ChunkUpdate(BaseModel):
    content: str


class ChunkList(BaseModel):
    unit_id: UUID
    child_total: int
    chunks: list[ChunkItem]


class RetryResult(BaseModel):
    unit_id: UUID
    task_id: str
    parse_status: str


class UnitEnabled(BaseModel):
    """启用/停用开关；`set_unit_enabled` 不动 `version`，因此不带乐观锁。"""

    enabled: bool


class AclOptionDepartment(BaseModel):
    id: UUID
    name: str
    parent_id: UUID | None = None
    is_active: bool = True


class AclOptionRole(BaseModel):
    id: UUID
    name: str
    code: str


class AclOptionUser(BaseModel):
    id: UUID
    display_name: str
    username: str
    department_name: str | None = None


class AclOptions(BaseModel):
    """四维权限弹窗的候选项（`GET /knowledge-units/acl-options`）。

    部门返回**扁平**列表带 `parent_id`：树形结构是前端展示的事，后端不替它决定层级怎么画，
    同时勾选父部门不级联子部门（P2）也由前端 `treeCheckStrictly` 保证。
    """

    departments: list[AclOptionDepartment] = Field(default_factory=list)
    roles: list[AclOptionRole] = Field(default_factory=list)
    # 人员可能很多，按 keyword 服务端过滤后截断上限
    users: list[AclOptionUser] = Field(default_factory=list)
