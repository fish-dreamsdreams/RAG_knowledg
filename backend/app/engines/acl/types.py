"""四维权限的取值对象与纯判定（tasklist 8.1，design.md §2.5）。

判定式：`acl_global OR department 命中 OR role 交集非空 OR user 命中`。
四维全空即**拒绝**——空 ACL 不表示"公开"（P1）。

只比 `users.department_id`：部门树不参与权限继承，给上级部门配的 ACL 不会自动下发给
子部门成员（P2）。要按层级授权就显式给每个部门各配一条。

把判定做成纯函数（`allows`）而不是塞进引擎方法，是为了让 P1/P2 与属性测试不需要
PostgreSQL：随机造 ACL 组合就能穷举验证。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import UUID

# 与 knowledge_unit_acl.principal_type 的取值一致
PRINCIPAL_DEPARTMENT = "department"
PRINCIPAL_ROLE = "role"
PRINCIPAL_USER = "user"
PRINCIPAL_TYPES = (PRINCIPAL_DEPARTMENT, PRINCIPAL_ROLE, PRINCIPAL_USER)


@dataclass(frozen=True)
class AclSubject:
    """判定所需的最小身份信息。

    `api.deps.CurrentUser` 结构兼容（同名属性），可直接传入；`role_ids` 允许传 list。
    """

    user_id: UUID
    department_id: UUID | None = None
    role_ids: Iterable[UUID] = field(default_factory=frozenset)


@dataclass(frozen=True)
class UnitAcl:
    """一个单元的权限快照：全局开关 + 三类命中项。"""

    acl_global: bool = False
    departments: frozenset[UUID] = field(default_factory=frozenset)
    roles: frozenset[UUID] = field(default_factory=frozenset)
    users: frozenset[UUID] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        """四维全空：这种配置恒为拒绝，不是"对所有人开放"。"""

        return not (self.acl_global or self.departments or self.roles or self.users)


def allows(acl: UnitAcl | None, subject: AclSubject) -> bool:
    """单单元判定。`acl` 为 None（单元不存在或查不到）时拒绝。"""

    if acl is None:
        return False
    if acl.acl_global:
        return True
    if subject.department_id is not None and subject.department_id in acl.departments:
        return True
    if acl.roles and set(subject.role_ids) & acl.roles:
        return True
    return subject.user_id in acl.users
