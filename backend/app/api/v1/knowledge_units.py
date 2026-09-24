"""知识单元接口：导入、维护、切片预览与权限配置（tasklist 6.4 / 7）。

解析是异步的：导入接口只做「保存原件 → 建单元与任务行 → 投递 Celery」，
**不在请求线程里触发解析**（TECH_SPEC §8.0），前端拿 unit_id 后轮询 import-status。

数据权限：列表与详情都要过 `AclEngine`。无力读取的单元对调用方与不存在无法区分
（同样 404），不泄露存在性（P9/P19）；只有持 `kb:acl` 的管理员才看得到全量，
否则它无法给无权单元配权限。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    PageParams,
    pagination,
    require_auth_query,
    require_perm,
)
from app.common.db import get_session
from app.engines.acl import AclSubject
from app.schemas.common import page, success
from app.schemas.knowledge import AclPayload, ChunkUpdate, UnitEnabled, UnitUpdate
from app.services import knowledge as knowledge_service

router = APIRouter(prefix="/knowledge-units", tags=["knowledge"])


def _subject(current: CurrentUser) -> AclSubject:
    """`CurrentUser` → 判定主体（字段同名，直接映射）。"""

    return AclSubject(
        user_id=current.user_id,
        department_id=current.department_id,
        role_ids=current.role_ids,
    )


def _sees_all_units(current: CurrentUser) -> bool:
    """持 `kb:acl` 的人要能给任意单元配权限，列表与详情必须能看见无权单元。"""

    return "kb:acl" in current.permissions or "*" in current.permissions


@router.post("/import")
async def import_knowledge_units(
    files: list[UploadFile] = File(
        ..., description="pdf/docx/md/markdown/txt，单文件 ≤20MB，单次 ≤50 个"
    ),
    from_gap_id: UUID | None = Form(
        None, description="缺口转建时带上，导入完成后自动把缺口置为已补全"
    ),
    category: str | None = Form(
        None,
        max_length=64,
        description="可选：写入这批文档的分类（导入抽屉的分类会落到每个单元上）",
    ),
    current: CurrentUser = Depends(require_perm("kb:import")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """受理批量导入，返回单元 id 列表与 upload_batch_id。"""

    result = await knowledge_service.import_units(
        session,
        files,
        created_by=current.user_id,
        from_gap_id=from_gap_id,
        category=category,
    )
    return success(result.model_dump(mode="json"))


@router.get("/{unit_id}/import-status")
async def get_import_status(
    unit_id: UUID,
    current: CurrentUser = Depends(require_perm("kb:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """导入进度：stage / progress / error，供前端轮询展示解析中状态。"""

    status = await knowledge_service.get_import_status(session, unit_id)
    return success(status.model_dump(mode="json"))


@router.get("")
async def list_knowledge_units(
    keyword: str | None = Query(None, max_length=100, description="标题或原文件名"),
    category: str | None = Query(None, max_length=64),
    status: str | None = Query(None, pattern="^(enabled|disabled)$"),
    unit_format: str | None = Query(
        None, alias="format", pattern="^(pdf|docx|md|txt)$"
    ),
    params: PageParams = Depends(pagination),
    current: CurrentUser = Depends(require_perm("kb:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """知识单元列表：带 `parse_status` 与进度，供「解析中」Tag 展示（tasklist 7.1）。"""

    items, total = await knowledge_service.list_units(
        session,
        _subject(current),
        offset=params.offset,
        limit=params.page_size,
        keyword=keyword,
        category=category,
        status=status,
        unit_format=unit_format,
        bypass_acl=_sees_all_units(current),
    )
    return page(
        [item.model_dump(mode="json") for item in items],
        total,
        params.page,
        params.page_size,
    )


@router.get("/acl-options")
async def list_acl_options(
    keyword: str | None = Query(
        None, max_length=64, description="按姓名/账号过滤人员候选"
    ),
    current: CurrentUser = Depends(require_perm("kb:acl")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """配置四维权限时的候选项（部门/角色/人员）。

    必须声明在 `/{unit_id}` 之前：靠后的路由会把 `acl-options` 当 UUID 解析后报 422。
    """

    options = await knowledge_service.list_acl_options(session, keyword=keyword)
    return success(options.model_dump(mode="json"))


@router.get("/{unit_id}")
async def get_knowledge_unit(
    unit_id: UUID,
    current: CurrentUser = Depends(require_perm("kb:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """知识单元详情：无权读取与不存在都返回 404，不泄露存在性（P9）。"""

    detail = await knowledge_service.get_unit_detail(
        session,
        _subject(current),
        unit_id,
        bypass_acl=_sees_all_units(current),
    )
    return success(detail.model_dump(mode="json"))


@router.put("/{unit_id}")
async def update_knowledge_unit(
    unit_id: UUID,
    payload: UnitUpdate,
    current: CurrentUser = Depends(require_perm("kb:edit")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """更新标题/分类；带 `version` 乐观锁，不匹配返回 409（TECH_SPEC §5.1）。"""

    detail = await knowledge_service.update_unit(session, unit_id, payload)
    return success(detail.model_dump(mode="json"))


@router.put("/{unit_id}/enabled")
async def set_knowledge_unit_enabled(
    unit_id: UUID,
    payload: UnitEnabled,
    current: CurrentUser = Depends(require_perm("kb:edit")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """启用/停用：Milvus 检索副本与 PG 状态一并切换（tasklist 5.4）。"""

    detail = await knowledge_service.set_enabled(
        session, unit_id, enabled=payload.enabled
    )
    return success(detail.model_dump(mode="json"))


@router.delete("/{unit_id}")
async def delete_knowledge_unit(
    unit_id: UUID,
    current: CurrentUser = Depends(require_perm("kb:delete")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """删除知识单元：级联清理切片、向量、原件与图片对象（P10）。"""

    await knowledge_service.delete_unit(session, unit_id)
    return success({"unit_id": str(unit_id)})


@router.get("/{unit_id}/assets/{name}")
async def get_knowledge_unit_asset(
    unit_id: UUID,
    name: str,
    current: CurrentUser = Depends(require_auth_query),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """知识单元图片代理（tasklist 17.4）。

    桶是私有的，前端**不直连**对象存储；这里校验 ACL 后转发字节。鉴权走查询参数
    `?access_token=`（`<img>` 带不了 Authorization 头），参数名与 WebSocket 握手一致。

    不要求 `kb:view`：图片与正文同口径，只要登录态有效且单元 ACL 放行即可——否则只有
    `ai:chat` 的用户会在问答引用里看到自己有权看的图裂掉。
    """

    content, media_type = await knowledge_service.read_unit_asset(
        session, _subject(current), unit_id, name
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={
            # 显式三件套：类型由白名单决定、禁止嗅探、只允许私有缓存（TECH_SPEC §8.0）
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private",
        },
    )


@router.get("/{unit_id}/acl")
async def get_knowledge_unit_acl(
    unit_id: UUID,
    current: CurrentUser = Depends(require_perm("kb:acl")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """读取四维 ACL 与当前 `version`（配置界面的初值）。"""

    view = await knowledge_service.get_acl(session, unit_id)
    return success(view.model_dump(mode="json"))


@router.put("/{unit_id}/acl")
async def save_knowledge_unit_acl(
    unit_id: UUID,
    payload: AclPayload,
    current: CurrentUser = Depends(require_perm("kb:acl")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """保存四维 ACL；保存后主动删 `kb:acl:unit:{id}` 缓存（tasklist 7.2、P11）。"""

    view = await knowledge_service.save_acl(session, unit_id, payload)
    return success(view.model_dump(mode="json"))


@router.get("/{unit_id}/chunks")
async def list_knowledge_unit_chunks(
    unit_id: UUID,
    params: PageParams = Depends(pagination),
    current: CurrentUser = Depends(require_perm("kb:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """切片预览：只返回子块摘要，分页按文档顺序。"""

    chunks = await knowledge_service.list_chunks(
        session, unit_id, offset=params.offset, limit=params.page_size
    )
    return success(chunks.model_dump(mode="json"))


@router.get("/{unit_id}/chunks/{chunk_id}")
async def get_knowledge_unit_chunk(
    unit_id: UUID,
    chunk_id: UUID,
    current: CurrentUser = Depends(require_perm("kb:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """子块全文，供编辑框使用。"""

    detail = await knowledge_service.get_chunk(session, unit_id, chunk_id)
    return success(detail.model_dump(mode="json"))


@router.put("/{unit_id}/chunks/{chunk_id}")
async def update_knowledge_unit_chunk(
    unit_id: UUID,
    chunk_id: UUID,
    payload: ChunkUpdate,
    current: CurrentUser = Depends(require_perm("kb:edit")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """保存子块并重嵌。未调用本接口则正文与向量都不改。"""

    detail = await knowledge_service.update_chunk(session, unit_id, chunk_id, payload)
    return success(detail.model_dump(mode="json"))


@router.post("/{unit_id}/retry")
async def retry_knowledge_unit(
    unit_id: UUID,
    current: CurrentUser = Depends(require_perm("kb:edit")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """解析失败重试：重置状态并重新投递 ingest（tasklist 7.4）。"""

    result = await knowledge_service.retry_unit(session, unit_id)
    return success(result.model_dump(mode="json"))
