"""数据模型约束与级联测试（tasklist 2.4、不变量 P10 的数据库侧）。

运行前需 `docker compose up -d postgres`：
    .venv\\Scripts\\python.exe -m pytest -m integration
"""

import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Chunk,
    Department,
    IngestTask,
    KnowledgeUnit,
    KnowledgeUnitAcl,
    User,
)
from app.services.seed import run_seed

pytestmark = pytest.mark.integration


async def _make_unit(session: AsyncSession) -> KnowledgeUnit:
    unit = KnowledgeUnit(
        title="测试知识单元",
        format="md",
        source_path="./var/uploads/test.md",
        source_filename="test.md",
    )
    session.add(unit)
    await session.flush()
    return unit


async def test_created_at_defaults_are_functions_not_frozen_literals(
    session: AsyncSession,
) -> None:
    """带默认时间的列必须是函数默认值。

    回归用例：`DEFAULT 'now()'`（字符串形式的 `server_default`）会被 PostgreSQL 当成固定
    时间输入在 DDL 时解析掉，全表时间戳冻结在建表那一刻——`chat_messages` 与
    `qa_audit_logs` 两列都踩过：会话回放顺序随机、看板趋势挤成一个点。
    """

    rows = (
        await session.execute(
            text(
                "SELECT table_name, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name IN "
                "('created_at', 'updated_at') AND column_default IS NOT NULL"
            )
        )
    ).all()

    frozen = {table: default for table, default in rows if "'" in (default or "")}
    assert frozen == {}, f"这些列的默认值被冻成了字面量：{frozen}"
    assert {default for _, default in rows} == {"now()"}


async def test_seed_is_idempotent() -> None:
    first = await run_seed()
    second = await run_seed()

    assert second["users_created"] == 0, "重复加载不应重复创建用户"
    assert second["model_config_created"] == 0, "模型配置只应有一行"
    assert second["departments"] == first["departments"]
    assert second["permissions"] == first["permissions"]


async def test_seed_contains_child_department_for_non_inheritance(
    session: AsyncSession,
) -> None:
    """P2 需要「父部门下存在子部门」的数据形态。"""

    sales = (
        await session.execute(select(Department).where(Department.name == "销售部"))
    ).scalar_one()
    east = (
        await session.execute(
            select(Department).where(Department.name == "销售部-华东大区")
        )
    ).scalar_one()
    assert east.parent_id == sales.id


async def test_seed_users_have_hashed_password(session: AsyncSession) -> None:
    user = (await session.execute(select(User).limit(1))).scalar_one()
    assert user.password_hash != "Demo@123456"
    assert user.password_hash.startswith("$2")


async def test_acl_triple_is_unique(session: AsyncSession) -> None:
    unit = await _make_unit(session)
    principal_id = uuid.uuid4()
    session.add(
        KnowledgeUnitAcl(unit_id=unit.id, principal_type="role", principal_id=principal_id)
    )
    await session.flush()
    session.add(
        KnowledgeUnitAcl(unit_id=unit.id, principal_type="role", principal_id=principal_id)
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_unit_cascades_children(session: AsyncSession) -> None:
    """物理删除知识单元：ACL、切片、导入任务必须随库级联清空（P10）。"""

    unit = await _make_unit(session)
    session.add(
        KnowledgeUnitAcl(
            unit_id=unit.id, principal_type="department", principal_id=uuid.uuid4()
        )
    )
    session.add(IngestTask(id=f"task-{uuid.uuid4()}", unit_id=unit.id, stage="queued"))
    parent = Chunk(unit_id=unit.id, level="parent", ordinal=0, content="父块", char_count=2)
    session.add(parent)
    await session.flush()
    session.add(
        Chunk(
            unit_id=unit.id,
            parent_id=parent.id,
            level="child",
            ordinal=0,
            content="子块",
            char_count=2,
        )
    )
    await session.flush()

    await session.execute(delete(KnowledgeUnit).where(KnowledgeUnit.id == unit.id))

    async def count(model, **filters) -> int:
        statement = select(func.count()).select_from(model)
        for column, value in filters.items():
            statement = statement.where(getattr(model, column) == value)
        return (await session.execute(statement)).scalar_one()

    assert await count(KnowledgeUnitAcl, unit_id=unit.id) == 0
    assert await count(Chunk, unit_id=unit.id) == 0
    assert await count(IngestTask, unit_id=unit.id) == 0


async def test_deleting_parent_chunk_cascades_children(session: AsyncSession) -> None:
    unit = await _make_unit(session)
    parent = Chunk(unit_id=unit.id, level="parent", ordinal=0, content="父块", char_count=2)
    session.add(parent)
    await session.flush()
    session.add(
        Chunk(
            unit_id=unit.id,
            parent_id=parent.id,
            level="child",
            ordinal=0,
            content="子块",
            char_count=2,
        )
    )
    await session.flush()

    await session.execute(delete(Chunk).where(Chunk.id == parent.id))

    remaining = (
        await session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.unit_id == unit.id)
        )
    ).scalar_one()
    assert remaining == 0
