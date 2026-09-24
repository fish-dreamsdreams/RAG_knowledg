"""权限码并集纯函数测试（tasklist 4.5）：直接测生产代码使用的合并逻辑。"""

from itertools import combinations
from uuid import uuid4

from app.repositories.org import merge_permission_codes

EMPLOYEE, KB_ADMIN, AUDITOR, EMPTY = (uuid4() for _ in range(4))


def test_union_equals_sum_of_selected_roles() -> None:
    rows = [
        (EMPLOYEE, "chat"),
        (EMPLOYEE, "ai:chat"),
        (KB_ADMIN, "chat"),
        (KB_ADMIN, "kb:view"),
        (KB_ADMIN, "kb:import"),
        (KB_ADMIN, "faq:review"),
        (AUDITOR, "dash:view"),
        (AUDITOR, "gap:view"),
    ]
    role_ids = [EMPLOYEE, KB_ADMIN, AUDITOR, EMPTY]

    for size in range(len(role_ids) + 1):
        for selected in combinations(role_ids, size):
            actual = merge_permission_codes(list(selected), rows)
            expected = {code for role_id, code in rows if role_id in set(selected)}
            assert actual == sorted(expected)
            assert len(actual) == len(set(actual)), "并集不得含重复权限码"


def test_roles_not_selected_contribute_nothing() -> None:
    rows = [(EMPLOYEE, "chat"), (KB_ADMIN, "kb:import")]
    assert merge_permission_codes([EMPLOYEE], rows) == ["chat"]
    assert merge_permission_codes([], rows) == []


def test_role_without_permissions_stays_empty() -> None:
    rows = [(EMPLOYEE, "chat")]
    assert merge_permission_codes([EMPTY, AUDITOR], rows) == []
