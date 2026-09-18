"""知识单元与切片的数据库访问（唯一 SQL 出口，层约定见 design.md §3.1）。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import (
    Chunk,
    IngestTask,
    KnowledgeUnit,
    KnowledgeUnitAcl,
    KnowledgeUnitAsset,
)


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符：用户输入的 `%` / `_` 必须当普通字符搜。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def get_unit(session: AsyncSession, unit_id: UUID) -> KnowledgeUnit | None:
    return await session.get(KnowledgeUnit, unit_id)


async def get_units_by_ids(
    session: AsyncSession, unit_ids: Sequence[UUID]
) -> list[KnowledgeUnit]:
    """按 id 批量取单元，保持调用方给定顺序。

    生成引用只需要单元标题，不能为每条 citation 单独查一次；更重要的是，调用方必须先用
    `AclEngine.filter` 筛掉无权 id，禁止把这当成绕过 ACL 的通用单元读取入口。
    """

    unique = list(dict.fromkeys(unit_ids))
    if not unique:
        return []
    rows = (
        await session.execute(select(KnowledgeUnit).where(KnowledgeUnit.id.in_(unique)))
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    return [by_id[unit_id] for unit_id in unique if unit_id in by_id]


async def set_unit_status(session: AsyncSession, unit: KnowledgeUnit, status: str) -> None:
    unit.status = status


async def get_unit_asset(
    session: AsyncSession, unit_id: UUID, rel_path: str
) -> KnowledgeUnitAsset | None:
    """按 `(unit_id, rel_path)` 取资产行；图片代理只吐**登记过**的对象（tasklist 17.4）。"""

    return (
        await session.execute(
            select(KnowledgeUnitAsset).where(
                KnowledgeUnitAsset.unit_id == unit_id,
                KnowledgeUnitAsset.rel_path == rel_path,
            )
        )
    ).scalar_one_or_none()


async def list_asset_names(session: AsyncSession, unit_id: UUID) -> set[str]:
    """该单元已登记的图片文件名，用来校验切片正文里的 `assets/{name}`。"""

    result = await session.execute(
        select(KnowledgeUnitAsset.name).where(KnowledgeUnitAsset.unit_id == unit_id)
    )
    return set(result.scalars().all())


async def delete_unit_row(session: AsyncSession, unit: KnowledgeUnit) -> None:
    """删除知识单元；切片由外键级联清除（`chunks.unit_id` ondelete=CASCADE）。"""

    await session.delete(unit)


async def count_chunks(
    session: AsyncSession, unit_id: UUID, *, level: str | None = None
) -> int:
    """该单元的切片行数（P15 的残留检查用）；`level="child"` 时只数子块。"""

    statement = select(func.count()).select_from(Chunk).where(Chunk.unit_id == unit_id)
    if level is not None:
        statement = statement.where(Chunk.level == level)
    result = await session.execute(statement)
    return int(result.scalar_one())


async def count_units(session: AsyncSession) -> int:
    """知识单元总数（看板「知识数」，tasklist 13.3）。"""

    total = await session.execute(select(func.count()).select_from(KnowledgeUnit))
    return int(total.scalar_one())


async def create_unit(session: AsyncSession, unit: KnowledgeUnit) -> KnowledgeUnit:
    session.add(unit)
    await session.flush()
    return unit


async def create_ingest_task(session: AsyncSession, task: IngestTask) -> IngestTask:
    session.add(task)
    await session.flush()
    return task


async def latest_ingest_task(session: AsyncSession, unit_id: UUID) -> IngestTask | None:
    """该单元最近一次导入任务（重试会新建任务行，取最新的）。"""

    result = await session.execute(
        select(IngestTask)
        .where(IngestTask.unit_id == unit_id)
        .order_by(IngestTask.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_ingest_progress(
    session: AsyncSession, unit_ids: Sequence[UUID]
) -> dict[UUID, tuple[str, int]]:
    """批量取每个单元最新任务的 (stage, progress)：列表页展示「解析中」Tag 用。"""

    if not unit_ids:
        return {}
    result = await session.execute(
        select(IngestTask.unit_id, IngestTask.stage, IngestTask.progress)
        .where(IngestTask.unit_id.in_(unit_ids))
        .distinct(IngestTask.unit_id)
        .order_by(IngestTask.unit_id, IngestTask.created_at.desc())
    )
    return {unit_id: (stage, progress) for unit_id, stage, progress in result}


def _filter_conditions(
    *,
    keyword: str | None,
    category: str | None,
    status: str | None,
    unit_format: str | None,
) -> list[Any]:
    """筛选条件（TECH_SPEC §4.4）；关键字同时匹配标题与原文件名。"""

    conditions: list[Any] = []
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        conditions.append(
            or_(
                KnowledgeUnit.title.ilike(pattern, escape="\\"),
                KnowledgeUnit.source_filename.ilike(pattern, escape="\\"),
            )
        )
    if category:
        conditions.append(KnowledgeUnit.category == category)
    if status:
        conditions.append(KnowledgeUnit.status == status)
    if unit_format:
        conditions.append(KnowledgeUnit.format == unit_format)
    return conditions


async def list_unit_ids(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    category: str | None = None,
    status: str | None = None,
    unit_format: str | None = None,
) -> list[UUID]:
    """按筛选条件取出全部匹配的 id。

    数据权限只能在 `AclEngine` 里判定（TECH_SPEC §6），所以列表必须先有候选集合，
    不能把 ACL 翻写进 SQL。
    """

    result = await session.execute(
        select(KnowledgeUnit.id).where(
            *_filter_conditions(
                keyword=keyword,
                category=category,
                status=status,
                unit_format=unit_format,
            )
        )
    )
    return list(result.scalars().all())


async def list_units(
    session: AsyncSession,
    *,
    allowed_ids: Sequence[UUID] | None,
    keyword: str | None = None,
    category: str | None = None,
    status: str | None = None,
    unit_format: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[KnowledgeUnit], int]:
    """分页查询知识单元，返回 `(行, 总数)`。

    `allowed_ids` 是数据权限过滤的结果：**必须**传 `AclEngine.filter` 的 allowed。
    只有需要配置权限的管理员才允许传 None（不过滤），普通列表传 None 会泄露无权单元。
    """

    conditions = _filter_conditions(
        keyword=keyword, category=category, status=status, unit_format=unit_format
    )
    if allowed_ids is not None:
        if not allowed_ids:
            return [], 0
        conditions.append(KnowledgeUnit.id.in_(allowed_ids))

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(KnowledgeUnit).where(*conditions)
            )
        ).scalar_one()
    )
    result = await session.execute(
        select(KnowledgeUnit)
        .where(*conditions)
        .order_by(KnowledgeUnit.created_at.desc(), KnowledgeUnit.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def get_acl_entries_for_units(
    session: AsyncSession, unit_ids: Sequence[UUID]
) -> dict[UUID, list[KnowledgeUnitAcl]]:
    """批量取多单元的 ACL 条目，按 unit_id 分组。

    列表页的权限标签要给每行拼部门/角色/人员名；按行查就是 N+1，一页 20 行打 60 次库。
    """

    if not unit_ids:
        return {}
    result = await session.execute(
        select(KnowledgeUnitAcl).where(KnowledgeUnitAcl.unit_id.in_(unit_ids))
    )
    grouped: dict[UUID, list[KnowledgeUnitAcl]] = {}
    for entry in result.scalars().all():
        grouped.setdefault(entry.unit_id, []).append(entry)
    return grouped


async def get_acl_entries(
    session: AsyncSession, unit_id: UUID
) -> list[KnowledgeUnitAcl]:
    result = await session.execute(
        select(KnowledgeUnitAcl)
        .where(KnowledgeUnitAcl.unit_id == unit_id)
        .order_by(KnowledgeUnitAcl.principal_type, KnowledgeUnitAcl.principal_id)
    )
    return list(result.scalars().all())


async def replace_acl_entries(
    session: AsyncSession, unit_id: UUID, entries: Sequence[tuple[str, UUID]]
) -> None:
    """整表替换一个单元的 ACL 条目：先删后插，避免算差集。

    唯一约束是 `(unit_id, principal_type, principal_id)`，调用方需自行去重。
    """

    await session.execute(
        delete(KnowledgeUnitAcl).where(KnowledgeUnitAcl.unit_id == unit_id)
    )
    session.add_all(
        KnowledgeUnitAcl(
            unit_id=unit_id, principal_type=principal_type, principal_id=principal_id
        )
        for principal_type, principal_id in entries
    )
    await session.flush()


async def list_parent_chunks(
    session: AsyncSession, unit_id: UUID, *, offset: int, limit: int
) -> list[Chunk]:
    """父块分页（导入核对等仍按父块展开）。"""

    result = await session.execute(
        select(Chunk)
        .where(Chunk.unit_id == unit_id, Chunk.level == "parent")
        .order_by(Chunk.ordinal)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_unit_child_chunks(
    session: AsyncSession, unit_id: UUID, *, offset: int, limit: int
) -> list[tuple[Chunk, int]]:
    """该单元的子块分页，按父块序号再按子块序号。每项是 (子块, 父块 ordinal)。

    不在 SQL 里对 `chunks` 自连接：同一张表 aliased 后再 `select(实体, 列)`，
    结果行容易映射空，表现为 child_total>0 但列表为空。
    """

    result = await session.execute(
        select(Chunk).where(Chunk.unit_id == unit_id, Chunk.level == "child")
    )
    children = list(result.scalars().all())
    parent_ids = [row.parent_id for row in children if row.parent_id is not None]
    parents = {
        item.id: item.ordinal for item in await get_chunks_by_ids(session, parent_ids)
    }
    children.sort(
        key=lambda row: (parents.get(row.parent_id, 0), row.ordinal, str(row.id))
    )
    return [
        (row, parents.get(row.parent_id, 0))
        for row in children[offset : offset + limit]
    ]


async def list_child_chunks(
    session: AsyncSession, parent_ids: Sequence[UUID]
) -> list[Chunk]:
    if not parent_ids:
        return []
    result = await session.execute(
        select(Chunk)
        .where(Chunk.parent_id.in_(parent_ids))
        .order_by(Chunk.parent_id, Chunk.ordinal)
    )
    return list(result.scalars().all())


async def get_chunks_by_ids(
    session: AsyncSession, chunk_ids: Sequence[UUID]
) -> list[Chunk]:
    """按 id 批量取切片，父块回溯与子块去重都用它。"""

    if not chunk_ids:
        return []
    result = await session.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
    return list(result.scalars().all())
