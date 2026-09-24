"""四维数据权限引擎（tasklist 8，design.md §2.5）。

对外只需两个入口：`AclEngine.filter` 做批量判定，`AclEngine.invalidate` 在权限保存后
主动失效快照。
"""

from app.engines.acl.engine import AclEngine
from app.engines.acl.types import AclSubject, UnitAcl, allows

__all__ = ["AclEngine", "AclSubject", "UnitAcl", "allows"]
