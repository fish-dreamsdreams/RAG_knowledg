"""知识单元用例：导入受理、启用/停用与删除的检索副本同步（tasklist 5.4 / 6.4）。

**写入顺序是有意的**：先改检索副本（Milvus），再改权威源（PostgreSQL）并提交。

- 停用：Milvus 先置 `enabled=false`。若此时 PG 提交失败，结果是「Milvus 已停用、PG 仍启用」，
  用户看不到这份知识 —— 失败方向是**收敛**的（fail-closed），不会泄露。
- 删除：Milvus 先删实体，再删 PG 行。反过来会留下「有向量、无正文」的孤儿切片：
  检索仍能召回它，但回溯父块与溯源都拿不到内容，属于更坏的状态。
- 两个动作都幂等：Milvus 侧无对应实体时返回 0，不报错。

删除时级联清 PG 切片、Milvus 实体与原件/解析产物/图片对象（P10）。对象 key 一律由
`engines/storage` 按 `kb/units/{unit_id}/` 前缀拼接，不会越界删除其他单元的数据；
历史遗留的本地原件仍按旧路径清理（只允许删 `UPLOAD_DIR` 内的文件）。

导入受理（6.4）只做「落盘 + 建行 + 投队列」，**不在请求线程里触发解析**（TECH_SPEC §8.0）；
真正的解析/切块/编码由 Celery 跑（`services/ingest.py`）。单元建为 `disabled`（未授权），
导入完成后仍需管理员显式配 ACL 并启用才会进检索。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.config import settings
from app.common.db import is_transient_conflict, run_with_conflict_retry
from app.common.errors import AppError
from app.engines.acl import AclEngine, AclSubject
from app.engines.acl.types import (
    PRINCIPAL_DEPARTMENT,
    PRINCIPAL_ROLE,
    PRINCIPAL_TYPES,
    PRINCIPAL_USER,
)
from app.engines.chunking import asset_names, content_hash
from app.engines.mineru import SUPPORTED_EXTENSIONS
from app.engines.retrieve import milvus_store
from app.engines.retrieve.milvus_store import ChunkVector
from app.engines.storage import object_store
from app.models.knowledge import Chunk, IngestTask, KnowledgeUnit, KnowledgeUnitAcl
from app.models.faq import KnowledgeGap
from app.repositories import faq as faq_repo
from app.repositories import knowledge as knowledge_repo
from app.repositories import org as org_repo
from app.schemas.knowledge import (
    AclLabeledPrincipal,
    AclOptionDepartment,
    AclOptionRole,
    AclOptionUser,
    AclOptions,
    AclPayload,
    AclPrincipalLabels,
    AclView,
    ChunkDetail,
    ChunkItem,
    ChunkList,
    ChunkUpdate,
    ImportedUnit,
    ImportResult,
    ImportStatus,
    RetryResult,
    UnitDetail,
    UnitListItem,
    UnitUpdate,
)
from app.services.ingest import (
    ERROR_MESSAGE_LIMIT,
    STAGE_CHUNKING,
    STAGE_EMBEDDING,
    STAGE_INDEXED,
    STAGE_PARSING,
    STAGE_QUEUED,
    safe_error,
)

# 上传约束（TECH_SPEC §7）
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BATCH_FILES = 50
# 扩展名 → knowledge_units.format（.markdown 归一到 md）
_FORMAT_BY_SUFFIX = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
}
# 原件写入对象存储时的 Content-Type（回吐时只做下载，不参与渲染）
_UPLOAD_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
# 与 `engines/celery/ingest.py` 的任务名一致；用名字投递，API 进程无需导入任务模块
INGEST_TASK_NAME = "ingest.ingest_unit"
# ACL 弹窗的人员候选上限：再多也不该靠下拉翻，前端按关键词搜
ACL_USER_LIMIT = 200

logger = logging.getLogger(__name__)

acl_engine = AclEngine()
# 切片预览只给摘要，不吐全文：预览是为了确认切块质量，不是为了替代原文
_CHUNK_EXCERPT_CHARS = 200
# 解析未完成时不允许改切片，否则会被整篇重切覆盖或写到半成品上
_BUSY_PARSE_STATUSES = frozenset(
    {STAGE_QUEUED, STAGE_PARSING, STAGE_CHUNKING, STAGE_EMBEDDING}
)
_MAX_CHUNK_EDIT_CHARS = 4000
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_ASSET_REF_RE = re.compile(r"^(?:\./)?assets/[^)\s/]+$")

# ---------- 知识维护（tasklist 7.1 ~ 7.4） ----------


def _to_list_item(
    unit: KnowledgeUnit, stage: str | None, progress: int, labels: _AclLabels
) -> UnitListItem:
    """单元行 → 列表行；带最新导入任务的 stage/progress（7.1）与权限标签（PRD §6.3）。"""

    return UnitListItem(
        unit_id=unit.id,
        title=unit.title,
        category=unit.category,
        format=unit.format,
        status=unit.status,
        parse_status=unit.parse_status,
        stage=stage,
        progress=progress,
        parse_error=unit.parse_error,
        file_size=unit.file_size,
        acl_global=unit.acl_global,
        acl_departments=labels.departments,
        acl_roles=labels.roles,
        acl_users=labels.users,
        version=unit.version,
        created_at=unit.created_at,
        updated_at=unit.updated_at,
    )


@dataclass(frozen=True)
class _AclLabels:
    """一个单元的权限标签：三类主体的展示名。"""

    departments: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    users: list[str] = field(default_factory=list)


async def _principal_names(
    session: AsyncSession, rows: Sequence[KnowledgeUnitAcl]
) -> dict[str, dict[UUID, str]]:
    """三类主体 id → 名称（三次查询，不按行查）。"""

    def _ids(principal_type: str) -> set[UUID]:
        return {row.principal_id for row in rows if row.principal_type == principal_type}

    return {
        PRINCIPAL_DEPARTMENT: await org_repo.department_names(
            session, _ids(PRINCIPAL_DEPARTMENT)
        ),
        PRINCIPAL_ROLE: await org_repo.role_names(session, _ids(PRINCIPAL_ROLE)),
        PRINCIPAL_USER: await org_repo.user_display_names(session, _ids(PRINCIPAL_USER)),
    }


async def _acl_labels(
    session: AsyncSession, unit_ids: Sequence[UUID]
) -> dict[UUID, _AclLabels]:
    """批量拼权限标签（列表行）；主体被删时退化成 id 前 8 位，不静默丢掉这一条。"""

    entries = await knowledge_repo.get_acl_entries_for_units(session, list(unit_ids))
    if not entries:
        return {}

    flat = [entry for rows in entries.values() for entry in rows]
    names_by_type = await _principal_names(session, flat)

    labels: dict[UUID, _AclLabels] = {}
    for unit_id, rows in entries.items():
        grouped: dict[str, list[str]] = {item: [] for item in PRINCIPAL_TYPES}
        for entry in rows:
            bucket = grouped.get(entry.principal_type)
            if bucket is None:
                continue
            names = names_by_type[entry.principal_type]
            bucket.append(names.get(entry.principal_id) or str(entry.principal_id)[:8])
        labels[unit_id] = _AclLabels(
            departments=sorted(grouped[PRINCIPAL_DEPARTMENT]),
            roles=sorted(grouped[PRINCIPAL_ROLE]),
            users=sorted(grouped[PRINCIPAL_USER]),
        )
    return labels


async def _to_detail(session: AsyncSession, unit: KnowledgeUnit) -> UnitDetail:
    task = await knowledge_repo.latest_ingest_task(session, unit.id)
    labels = (await _acl_labels(session, [unit.id])).get(unit.id, _AclLabels())
    item = _to_list_item(unit, task.stage if task else None, task.progress if task else 0, labels)
    return UnitDetail(
        **item.model_dump(),
        source_filename=unit.source_filename,
        created_by=unit.created_by,
    )


def _excerpt(text: str) -> str:
    return text if len(text) <= _CHUNK_EXCERPT_CHARS else f"{text[:_CHUNK_EXCERPT_CHARS]}…"


def _chunk_item(chunk: Chunk, *, parent_ordinal: int | None = None) -> ChunkItem:
    return ChunkItem(
        chunk_id=chunk.id,
        level=chunk.level,
        ordinal=chunk.ordinal,
        char_count=chunk.char_count,
        excerpt=_excerpt(chunk.content or ""),
        parent_ordinal=parent_ordinal,
    )


def _chunk_detail(chunk: Chunk) -> ChunkDetail:
    text = chunk.content or ""
    return ChunkDetail(
        chunk_id=chunk.id,
        unit_id=chunk.unit_id,
        parent_id=chunk.parent_id,
        level=chunk.level,
        ordinal=chunk.ordinal,
        char_count=chunk.char_count,
        content=text,
        content_hash=chunk.content_hash,
        excerpt=_excerpt(text),
    )


def _validate_chunk_images(content: str, known_names: set[str]) -> None:
    for raw in _MD_IMAGE_RE.findall(content):
        target = raw.strip().split()[0]
        if not _ASSET_REF_RE.fullmatch(target):
            raise AppError.validation("图片引用只允许 assets/{文件名}")
    missing = [name for name in asset_names(content) if name not in known_names]
    if missing:
        raise AppError.validation(f"图片不存在：{missing[0]}")


async def list_units(
    session: AsyncSession,
    subject: AclSubject,
    *,
    offset: int,
    limit: int,
    keyword: str | None = None,
    category: str | None = None,
    status: str | None = None,
    unit_format: str | None = None,
    bypass_acl: bool = False,
) -> tuple[list[UnitListItem], int]:
    """知识单元列表（7.1）。

    数据权限先于分页生效：先取全部候选 id 交 `AclEngine` 判定，再用判定结果做分页
    查询。否则先把无权单元算进 `total`、再在内存里剔除，分页会缺页且会泄露数量。
    代价是每次列表要捞出全部候选 id——演示规模可接受；量级变大时再改成游标扫描。
    """

    allowed_ids: list[UUID] | None = None
    if not bypass_acl:
        candidates = await knowledge_repo.list_unit_ids(
            session,
            keyword=keyword,
            category=category,
            status=status,
            unit_format=unit_format,
        )
        allowed_ids, _ = await acl_engine.filter(subject, candidates, session=session)

    rows, total = await knowledge_repo.list_units(
        session,
        allowed_ids=allowed_ids,
        keyword=keyword,
        category=category,
        status=status,
        unit_format=unit_format,
        offset=offset,
        limit=limit,
    )
    progress = await knowledge_repo.latest_ingest_progress(
        session, [row.id for row in rows]
    )
    labels = await _acl_labels(session, [row.id for row in rows])
    items: list[UnitListItem] = []
    for row in rows:
        stage, value = progress.get(row.id, (None, 0))
        items.append(_to_list_item(row, stage, value, labels.get(row.id, _AclLabels())))
    return items, total


async def get_unit_detail(
    session: AsyncSession,
    subject: AclSubject,
    unit_id: UUID,
    *,
    bypass_acl: bool = False,
) -> UnitDetail:
    """单元详情；无权读取与不存在返回同样的 404，不泄露单元是否存在（P9）。"""

    unit = await _get_unit_or_404(session, unit_id)
    if not bypass_acl:
        allowed, _ = await acl_engine.filter(subject, [unit_id], session=session)
        if not allowed:
            raise AppError.not_found("知识单元不存在")
    return await _to_detail(session, unit)


async def update_unit(
    session: AsyncSession, unit_id: UUID, payload: UnitUpdate
) -> UnitDetail:
    """更新元信息（7.1），带 `version` 乐观锁（TECH_SPEC §5.1）。

    锁定靠 `UPDATE ... WHERE version = :expected` 的条件写，不靠 Python 里比一下
    再自增：后者在 READ COMMITTED 下两个并发请求都会成功，属于丢失更新。
    """

    unit = await _get_unit_or_404(session, unit_id)

    values: dict[str, object] = {"version": KnowledgeUnit.version + 1}
    fields = payload.model_fields_set
    if "title" in fields and payload.title is not None:
        values["title"] = payload.title
    if "category" in fields:
        values["category"] = payload.category

    result = await session.execute(
        update(KnowledgeUnit)
        .where(KnowledgeUnit.id == unit_id, KnowledgeUnit.version == payload.version)
        .values(**values)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise AppError.version_conflict()

    await session.commit()
    await session.refresh(unit)
    logger.info("知识单元 %s 元信息已更新至 version=%s", unit_id, unit.version)
    return await _to_detail(session, unit)


async def set_enabled(
    session: AsyncSession, unit_id: UUID, *, enabled: bool
) -> UnitDetail:
    """启用/停用（7.1）；检索副本与权威源的先后顺序见模块 docstring。"""

    unit = await set_unit_enabled(session, unit_id, enabled=enabled)
    return await _to_detail(session, unit)


def _acl_entries(payload: AclPayload) -> list[tuple[str, UUID]]:
    """三类主体各自去重后展开成 `(principal_type, principal_id)`。"""

    entries: list[tuple[str, UUID]] = []
    for principal_type, values in (
        (PRINCIPAL_DEPARTMENT, payload.departments),
        (PRINCIPAL_ROLE, payload.roles),
        (PRINCIPAL_USER, payload.users),
    ):
        for principal_id in dict.fromkeys(values):
            entries.append((principal_type, principal_id))
    return entries


def _acl_view(unit: KnowledgeUnit, rows: list[KnowledgeUnitAcl]) -> AclView:
    grouped: dict[str, list[UUID]] = {item: [] for item in PRINCIPAL_TYPES}
    for row in rows:
        if row.principal_type in grouped:
            grouped[row.principal_type].append(row.principal_id)
    return AclView(
        acl_global=unit.acl_global,
        departments=grouped[PRINCIPAL_DEPARTMENT],
        roles=grouped[PRINCIPAL_ROLE],
        users=grouped[PRINCIPAL_USER],
        version=unit.version,
    )


async def _acl_labels_by_id(
    session: AsyncSession, rows: list[KnowledgeUnitAcl]
) -> AclPrincipalLabels:
    """ACL 条目的展示名，按主体 id 逐个配上（不靠位置对齐）。

    与列表标签同口径：主体被删时退化成 id 前 8 位，而不是悄悄不显示。
    """

    names_by_type = await _principal_names(session, rows)

    def _items(principal_type: str) -> list[AclLabeledPrincipal]:
        names = names_by_type[principal_type]
        return [
            AclLabeledPrincipal(
                id=row.principal_id,
                name=names.get(row.principal_id) or str(row.principal_id)[:8],
            )
            for row in rows
            if row.principal_type == principal_type
        ]

    return AclPrincipalLabels(
        departments=_items(PRINCIPAL_DEPARTMENT),
        roles=_items(PRINCIPAL_ROLE),
        users=_items(PRINCIPAL_USER),
    )


async def get_acl(session: AsyncSession, unit_id: UUID) -> AclView:
    unit = await _get_unit_or_404(session, unit_id)
    rows = await knowledge_repo.get_acl_entries(session, unit_id)
    view = _acl_view(unit, rows)
    view.labels = await _acl_labels_by_id(session, rows)
    return view


async def save_acl(
    session: AsyncSession, unit_id: UUID, payload: AclPayload
) -> AclView:
    """保存四维 ACL（7.2）：整表替换 + `version` 乐观锁 + 主动删缓存（P11）。"""

    unit = await _get_unit_or_404(session, unit_id)

    result = await session.execute(
        update(KnowledgeUnit)
        .where(KnowledgeUnit.id == unit_id, KnowledgeUnit.version == payload.version)
        .values(acl_global=payload.acl_global, version=KnowledgeUnit.version + 1)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise AppError.version_conflict()

    await knowledge_repo.replace_acl_entries(session, unit_id, _acl_entries(payload))
    await session.commit()
    await session.refresh(unit)

    # 主动失效：等 TTL 过期等于刚收回的权限在 10 分钟内仍然放行（P11）
    await acl_engine.invalidate(unit_id)

    rows = await knowledge_repo.get_acl_entries(session, unit_id)
    view = _acl_view(unit, rows)
    view.labels = await _acl_labels_by_id(session, rows)
    logger.info(
        "知识单元 %s ACL 已保存：global=%s dept=%s role=%s user=%s",
        unit_id,
        view.acl_global,
        len(view.departments),
        len(view.roles),
        len(view.users),
    )
    return view


async def list_acl_options(
    session: AsyncSession, *, keyword: str | None = None
) -> AclOptions:
    """四维权限弹窗的候选项（部门/角色/人员）。

    不复用 `/org/*`：那三个接口要 `org:dept|org:role|org:user`，而配知识权限只需要 `kb:acl`。
    为了选个部门就把知识管理员提成组织管理员，是把权限放大的错误做法。

    人员量大，所以只返回前 `ACL_USER_LIMIT` 个并按 `keyword` 服务端过滤；前端用远程搜索。
    停用的部门仍然列出（带 `is_active=false`）：它上面可能挂着历史 ACL，选不到就没法核对。
    """

    departments = await org_repo.list_departments(session)
    roles = await org_repo.list_roles(session)
    users, _ = await org_repo.list_users(
        session, offset=0, limit=ACL_USER_LIMIT, keyword=keyword
    )
    department_names = await org_repo.department_names(
        session, {user.department_id for user in users}
    )
    return AclOptions(
        departments=[
            AclOptionDepartment(
                id=department.id,
                name=department.name,
                parent_id=department.parent_id,
                is_active=department.is_active,
            )
            for department in departments
        ],
        roles=[AclOptionRole(id=role.id, name=role.name, code=role.code) for role in roles],
        users=[
            AclOptionUser(
                id=user.id,
                display_name=user.display_name,
                username=user.username,
                department_name=department_names.get(user.department_id),
            )
            for user in users
        ],
    )


async def list_chunks(
    session: AsyncSession, unit_id: UUID, *, offset: int, limit: int
) -> ChunkList:
    """切片预览：只列子块（检索单位），分页按文档顺序。"""

    await _get_unit_or_404(session, unit_id)
    rows = await knowledge_repo.list_unit_child_chunks(
        session, unit_id, offset=offset, limit=limit
    )
    return ChunkList(
        unit_id=unit_id,
        child_total=await knowledge_repo.count_chunks(session, unit_id, level="child"),
        chunks=[
            _chunk_item(chunk, parent_ordinal=parent_ordinal)
            for chunk, parent_ordinal in rows
        ],
    )


async def get_chunk(
    session: AsyncSession, unit_id: UUID, chunk_id: UUID
) -> ChunkDetail:
    """取子块全文，供编辑框使用。"""

    chunk = await _get_editable_child(session, unit_id, chunk_id, require_idle=False)
    return _chunk_detail(chunk)


async def update_chunk(
    session: AsyncSession,
    unit_id: UUID,
    chunk_id: UUID,
    payload: ChunkUpdate,
    *,
    encoder: Callable[[list[str]], object] | None = None,
    indexer: Callable[[list[ChunkVector]], None] | None = None,
) -> ChunkDetail:
    """保存子块正文并重嵌。编码失败不落库；取消保存则不会走到这里。"""

    chunk = await _get_editable_child(session, unit_id, chunk_id, require_idle=True)
    unit = await _get_unit_or_404(session, unit_id)
    content = (payload.content or "").strip()
    if not content:
        raise AppError.validation("切片正文不能为空")
    if len(content) > _MAX_CHUNK_EDIT_CHARS:
        raise AppError.validation(f"切片正文不能超过 {_MAX_CHUNK_EDIT_CHARS} 字")
    known = await knowledge_repo.list_asset_names(session, unit_id)
    _validate_chunk_images(content, known)

    if content == (chunk.content or ""):
        return _chunk_detail(chunk)

    try:
        encode = encoder or _encode_child_texts
        embedding = await asyncio.to_thread(encode, [content])
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - 编码失败对用户只说依赖不可用
        raise AppError.dependency_unavailable("向量编码失败，请稍后重试") from exc

    dense = embedding.dense[0]
    sparse = embedding.sparse[0]
    chunk.content = content
    chunk.char_count = len(content)
    chunk.content_hash = content_hash(content)
    if chunk.parent_id is not None:
        await _rebuild_parent_from_children(session, chunk.parent_id)

    vectors = [
        ChunkVector(
            chunk_id=str(chunk.id),
            unit_id=str(unit.id),
            parent_id=str(chunk.parent_id) if chunk.parent_id else str(chunk.id),
            enabled=unit.status == "enabled",
            dense=[float(value) for value in dense],
            sparse=dict(sparse),
        )
    ]
    try:
        write = indexer or milvus_store.upsert_chunks
        await asyncio.to_thread(write, vectors)
    except AppError:
        await session.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise AppError.dependency_unavailable("向量写入失败，请稍后重试") from exc

    await session.commit()
    await session.refresh(chunk)
    logger.info("知识单元 %s 子块 %s 已更新并重嵌", unit_id, chunk_id)
    return _chunk_detail(chunk)


def _encode_child_texts(texts: list[str]):
    from app.engines.embed import get_embedder

    return get_embedder().encode(texts)


async def _rebuild_parent_from_children(session: AsyncSession, parent_id: UUID) -> None:
    parent = await session.get(Chunk, parent_id)
    if parent is None or parent.level != "parent":
        return
    siblings = await knowledge_repo.list_child_chunks(session, [parent_id])
    parent.content = "\n\n".join(row.content or "" for row in siblings)
    parent.char_count = len(parent.content)
    parent.content_hash = content_hash(parent.content)


async def _get_editable_child(
    session: AsyncSession,
    unit_id: UUID,
    chunk_id: UUID,
    *,
    require_idle: bool,
) -> Chunk:
    await _get_unit_or_404(session, unit_id)
    chunk = await session.get(Chunk, chunk_id)
    if chunk is None or chunk.unit_id != unit_id:
        raise AppError.not_found("切片不存在")
    if chunk.level != "child":
        raise AppError.validation("只允许编辑子块")
    if require_idle:
        unit = await _get_unit_or_404(session, unit_id)
        if unit.parse_status in _BUSY_PARSE_STATUSES:
            raise AppError.validation("该知识单元正在解析，请稍后再改切片")
        if unit.parse_status != STAGE_INDEXED:
            raise AppError.validation("解析完成后才能编辑切片")
    return chunk


async def retry_unit(
    session: AsyncSession,
    unit_id: UUID,
    *,
    dispatcher: Callable[[list[tuple[UUID, str]]], None] | None = None,
) -> RetryResult:
    """解析失败重试（7.4）：重置状态并重新投递 ingest。

    旧切片与向量**不在这里清**：`services/ingest.py` 一开头就整体清理并重建，交给
    同一条路径处理，避免“清了但投递失败”留下一个空单元。
    """

    unit = await _get_unit_or_404(session, unit_id)
    if unit.parse_status == STAGE_PARSING:
        raise AppError.validation("该知识单元正在解析中，请等本次解析结束后再重试")
    if unit.parse_status == STAGE_QUEUED:
        raise AppError.validation("该知识单元已有解析任务在排队")

    task_id = uuid4().hex
    await knowledge_repo.create_ingest_task(
        session, IngestTask(id=task_id, unit_id=unit_id, stage=STAGE_QUEUED, progress=0)
    )
    unit.parse_status = STAGE_QUEUED
    unit.parse_error = None
    await session.commit()

    (dispatcher or dispatch_ingest)([(unit_id, task_id)])
    logger.info("知识单元 %s 已重投解析任务 %s", unit_id, task_id)
    return RetryResult(unit_id=unit_id, task_id=task_id, parse_status=unit.parse_status)


async def _get_unit_or_404(session: AsyncSession, unit_id: UUID) -> KnowledgeUnit:
    unit = await knowledge_repo.get_unit(session, unit_id)
    if unit is None:
        raise AppError.not_found("知识单元不存在")
    return unit


async def set_unit_enabled(
    session: AsyncSession, unit_id: UUID, *, enabled: bool
) -> KnowledgeUnit:
    """启用/停用知识单元：Milvus `enabled` 与 PG `status` 一并切换。"""

    unit = await _get_unit_or_404(session, unit_id)

    # 1) 检索副本先落地，停用必须立刻对新问答生效（P9）
    await asyncio.to_thread(milvus_store.set_unit_enabled, str(unit_id), enabled)

    # 2) 权威源
    await knowledge_repo.set_unit_status(session, unit, "enabled" if enabled else "disabled")
    await session.commit()
    await session.refresh(unit)
    logger.info("知识单元 %s 已%s", unit_id, "启用" if enabled else "停用")
    return unit


def _delete_source_file(source_path: str) -> bool:
    """删除历史遗留的本地原件；只允许删上传目录内的文件，越界一律跳过。"""

    upload_root = Path(settings.upload_dir).resolve()
    target = Path(source_path)
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    else:
        target = target.resolve()

    if not target.is_relative_to(upload_root):
        logger.warning("跳过删除原件：%s 不在上传目录 %s 内", target, upload_root)
        return False
    if not target.exists():
        return False
    target.unlink()
    return True


async def _discard_object(key: str) -> None:
    """best-effort 清理已写入的对象（受理失败时回滚用）。"""

    try:
        await object_store.delete(key)
    except object_store.ObjectStoreError:
        logger.warning("回滚对象失败：%s", key, exc_info=True)


async def delete_unit(session: AsyncSession, unit_id: UUID) -> None:
    """删除知识单元：Milvus 实体 → PG 行（级联切片）→ 对象存储与本地原件。

    PG 那一段裹了**并发冲突重试**：解析任务正在往这个单元写切片时，双方的锁会互相等待，
    Postgres 挑一方回滚（真机现象：`DELETE FROM knowledge_units` 报 40P01 死锁）。删除被
    回滚属于「换个时机就能成」，而重试是在干净事务里重放整段——所以每轮都重新取一次行，
    别捧着上一个事务里那个已被 `rollback()` expire 掉的 ORM 实例。

    重试用尽仍冲突就转 503：解析仍在写，删除此刻做不到，但过一会儿再做就行。
    """

    unit = await _get_unit_or_404(session, unit_id)
    source_path = unit.source_path

    async def discard_vectors_and_rows() -> None:
        current = await _get_unit_or_404(session, unit_id)

        # 1) 先清检索副本，避免留下可被召回、却无正文可溯源的孤儿向量
        await asyncio.to_thread(milvus_store.delete_by_unit, str(unit_id))

        # 2) PG：chunks 由外键级联删除；缺口必须先摘掉对该单元的外键，否则单元根本删不掉
        reopened = await faq_repo.reopen_gaps_of_unit(session, unit_id)
        if reopened:
            logger.info("单元 %s 被删除，%s 个知识缺口退回待处理", unit_id, reopened)
        await knowledge_repo.delete_unit_row(session, current)
        await session.commit()

    try:
        await run_with_conflict_retry(session, discard_vectors_and_rows)
    except Exception as exc:  # noqa: BLE001 - 只重新包装并发冲突，其余原样抛出
        if not is_transient_conflict(exc):
            raise
        logger.error("删除单元 %s 反复撞上并发冲突：%s", unit_id, safe_error(exc))
        raise AppError.dependency_unavailable(
            "该文档正在被解析任务写入，删除暂时冲突，请稍后重试"
        ) from exc

    # 3) 对象存储：原件 / 解析产物 / 图片（按单元前缀，失败不影响删除结果）
    try:
        removed = await object_store.delete_unit_prefix(unit_id)
        if removed:
            logger.info("已删除单元 %s 的对象 %s 个", unit_id, removed)
    except object_store.ObjectStoreError:
        logger.warning("清理单元对象失败：%s", unit_id, exc_info=True)

    # 4) 历史遗留的本地原件
    try:
        if await asyncio.to_thread(_delete_source_file, source_path):
            logger.info("已删除本地原件：%s", source_path)
    except OSError:
        logger.warning("删除本地原件失败：%s", source_path, exc_info=True)

    logger.info("知识单元 %s 已删除", unit_id)


# --- 图片资产（tasklist 17.4） ---------------------------------------------


async def read_unit_asset(
    session: AsyncSession, subject: AclSubject, unit_id: UUID, name: str
) -> tuple[bytes, str]:
    """读单元图片字节，返回 `(内容, media_type)`（tasklist 17.4）。

    三道关任一不过都是 404：

    1. 单元存在，且**当前主体**的 ACL 放行——与正文同口径（fail-closed）。不给 `kb:acl`
       管理员开后门：能配权限不等于能看内容，否则越权读一张图只需要一个管理员账号。
    2. `name` 能拼出落在本单元 `assets/` 前缀内的 key（`asset_key` 内部 basename 化并拦截
       `../`、绝对路径与 Windows 保留名），且 `knowledge_unit_assets` 里有登记行；对象存在
       但没登记过的 key 一律不吐字节。
    3. 对象存储读得到——单元被删或对象被清走时是 404，而不是 500。

    用 404 而不是 403 是刻意的：与单元详情同口径，不泄露"这份资料存在但你没权限"（P9/P19）。
    """

    await _get_unit_or_404(session, unit_id)
    allowed, _ = await acl_engine.filter(subject, [unit_id], session=session)
    if not allowed:
        raise AppError.not_found("知识单元不存在")

    key = object_store.asset_key(unit_id, name)
    asset = await knowledge_repo.get_unit_asset(
        session, unit_id, f"{object_store.ASSETS_DIR}/{name}"
    )
    if key is None or asset is None:
        raise AppError.not_found("图片不存在")
    # 只认白名单内的 media_type：不回吐 assets 表里的任意字符串，避免 Content-Type 混淆
    if asset.media_type not in set(object_store.MEDIA_TYPES.values()):
        raise AppError.not_found("图片不存在")

    try:
        content = await object_store.get_bytes(key)
    except object_store.ObjectStoreError as exc:
        logger.info("图片对象读取失败：unit=%s", unit_id)
        raise AppError.not_found("图片不存在") from exc
    return content, asset.media_type


# --- 批量导入受理（tasklist 6.4） ------------------------------------------


def dispatch_ingest(pairs: list[tuple[UUID, str]]) -> None:
    """把导入任务投给 Celery：`group` + 预置 task_id（与 `ingest_tasks.id` 对齐）。

    按**任务名**投递，API 进程因而不需要导入 `engines/celery` 的任务模块，
    也就不会在接口进程里带出 MinerU/编码器依赖（TECH_SPEC §8.0）。
    """

    from celery import group

    from app.engines.celery.celery_app import celery_app

    signatures = [
        celery_app.signature(
            INGEST_TASK_NAME,
            args=[str(unit_id), task_id],
            options={"task_id": task_id},
        )
        for unit_id, task_id in pairs
    ]
    group(signatures).apply_async()


def _validate_uploads(files: list[UploadFile]) -> list[tuple[str, str, str]]:
    """先整体校验扩展名，返回 (原始文件名, format, 后缀)。

    先校验再落盘：一半合法一半不合法时不应该在磁盘上留下半成品。
    """

    if not files:
        raise AppError.validation("请至少选择一个文件")
    if len(files) > MAX_BATCH_FILES:
        raise AppError.validation(f"单次最多导入 {MAX_BATCH_FILES} 个文件")

    validated: list[tuple[str, str, str]] = []
    for upload in files:
        # 只取文件名部分：客户端可能传 ..\\..\\evil.pdf
        original = Path(upload.filename or "").name
        suffix = Path(original).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS or suffix not in _FORMAT_BY_SUFFIX:
            raise AppError.unsupported_format(f"不支持的文件类型：{original or '未知文件'}")
        validated.append((original, _FORMAT_BY_SUFFIX[suffix], suffix))
    return validated


async def _resolve_gap(
    session: AsyncSession, from_gap_id: UUID | None, file_count: int
) -> KnowledgeGap | None:
    """校验转建来源缺口。没有 `from_gap_id` 时返回 `None`（普通导入）。"""

    if from_gap_id is None:
        return None
    # 多文件时「这个缺口由哪个单元补上」没有确定答案，与其任选一个，不如让调用方拆开导
    if file_count != 1:
        raise AppError.validation("缺口转建一次只能导入一个文件")
    gap = await faq_repo.get_gap(session, from_gap_id)
    if gap is None:
        raise AppError.not_found("知识缺口不存在")
    if gap.status == faq_repo.GAP_FILLED:
        raise AppError.gap_not_convertible("该缺口已补全，无需再次转建")
    return gap


async def import_units(
    session: AsyncSession,
    files: list[UploadFile],
    *,
    created_by: UUID | None = None,
    from_gap_id: UUID | None = None,
    category: str | None = None,
    dispatcher: Callable[[list[tuple[UUID, str]]], None] | None = None,
) -> ImportResult:
    """受理批量导入：校验 → 落盘 → 建单元与任务行 → 投递 Celery。

    `from_gap_id` 是缺口转建的联动入口（tasklist 13.1）：缺口钉在转建出来的单元上，
    等该单元 `indexed` 后由导入链置为 `filled`（P16）。

    `category` 是导入抽屉里选的分类：它只作为**这批单元的初始分类**写进行里，之后仍然
    可以在编辑抽屉里逐个改。空白值当作未填（`None`），别存一个空字符串进列表筛选。

    `dispatcher` 只在测试里传入（避免真连 broker）。
    """

    parsed = _validate_uploads(files)
    gap = await _resolve_gap(session, from_gap_id, len(files))
    normalized_category = (category or "").strip() or None

    batch_id = uuid4()
    written: list[str] = []
    units: list[KnowledgeUnit] = []
    try:
        for upload, (original, file_format, suffix) in zip(files, parsed, strict=True):
            content = await upload.read()
            if not content:
                raise AppError.validation(f"文件内容为空：{original}")
            limit_mb = MAX_FILE_BYTES // (1024 * 1024)
            if len(content) > MAX_FILE_BYTES:
                raise AppError.validation(f"单个文件不得超过 {limit_mb}MB：{original}")

            # 先建行对象拿到 unit_id：对象 key 必须落在 `kb/units/{unit_id}/` 前缀内，
            # 删除时只按前缀清理，不可能碰到其他单元的数据（P10）
            # id 必须显式生成：列默认值要等 INSERT 才生效，而 key 拼装就在这之前
            unit = KnowledgeUnit(
                id=uuid4(),
                title=Path(original).stem or original,
                format=file_format,
                category=normalized_category,
                # source_path 存对象 key（原件不再落本地磁盘）
                source_path="",
                source_filename=original,
                file_size=len(content),
                # 导入即未授权：管理员配 ACL 并启用后才进检索（PRD 知识维护）
                status="disabled",
                parse_status="queued",
                upload_batch_id=batch_id,
                created_by=created_by,
            )
            key = object_store.source_key(unit.id, original)
            if key is None:
                raise AppError.validation(f"文件名不可用：{original}")
            await object_store.put_bytes(
                key, content, content_type=_UPLOAD_MEDIA_TYPES.get(suffix)
            )
            written.append(key)
            unit.source_path = key
            units.append(unit)
    except Exception:
        for key in written:
            await _discard_object(key)
        raise

    for unit in units:
        await knowledge_repo.create_unit(session, unit)

    tasks: list[IngestTask] = []
    items: list[ImportedUnit] = []
    for unit in units:
        task_id = uuid4().hex
        task = await knowledge_repo.create_ingest_task(
            session, IngestTask(id=task_id, unit_id=unit.id, stage="queued", progress=0)
        )
        tasks.append(task)
        items.append(
            ImportedUnit(
                unit_id=unit.id, task_id=task_id, source_filename=unit.source_filename
            )
        )
    # 缺口与单元在同一事务里落库：单元存在而缺口没关联上，就再也找不到这条转建记录
    if gap is not None:
        await faq_repo.link_gap_to_unit(session, gap, units[0].id)

    # 先提交再投递：worker 可能立刻就取任务，行必须已经可见
    await session.commit()

    dispatch = dispatcher if dispatcher is not None else dispatch_ingest
    pairs = [(unit.id, item.task_id) for unit, item in zip(units, items, strict=True)]
    try:
        await asyncio.to_thread(dispatch, pairs)
    except Exception as exc:  # noqa: BLE001 - 投递失败必须收敛，不能让单元卡在 queued
        logger.exception("导入任务投递失败：batch=%s", batch_id)
        message = f"任务投递失败：{safe_error(exc)}"[:ERROR_MESSAGE_LIMIT]
        for unit in units:
            unit.parse_status = "failed"
            unit.parse_error = message
        for task in tasks:
            task.stage = "failed"
            task.error = message
        await session.commit()
        raise AppError.dependency_unavailable("任务队列不可用，请稍后重试") from exc

    logger.info("已受理导入：batch=%s 单元数=%s", batch_id, len(units))
    return ImportResult(upload_batch_id=batch_id, items=items)


async def get_import_status(session: AsyncSession, unit_id: UUID) -> ImportStatus:
    """导入进度（tasklist 6.4）：取最近一次任务的 stage/progress 与错误。"""

    unit = await _get_unit_or_404(session, unit_id)
    task = await knowledge_repo.latest_ingest_task(session, unit_id)

    if task is not None:
        stage, progress = task.stage, task.progress
    else:
        stage = unit.parse_status
        progress = 100 if unit.parse_status in {"indexed", "failed"} else 0

    return ImportStatus(
        unit_id=unit.id,
        parse_status=unit.parse_status,
        stage=stage,
        progress=progress,
        error=unit.parse_error or (task.error if task else None),
    )
