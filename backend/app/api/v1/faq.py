"""FAQ 审核、发布与维护接口（tasklist 11.2 / 11.3，TECH_SPEC §4.5）。

路由不加统一前缀：TECH_SPEC 把审核类接口登记在 `/faq/...` 下，而 FAQ 列表与维护是
`/faqs`（复数、无子路径）。用两个前缀或硬拼 `/faq/faqs` 都会让 URL 变得难以解释。

权限分工：审核动作（看候选、驳回）用 `faq:review`，发布态的管理（发布、改答案、切缓存、
下线）用 `faq:publish`。规范只登记了 publish 一处，其余按同一分工延伸。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, PageParams, pagination, require_perm
from app.common.db import get_session
from app.schemas.common import page, success
from app.schemas.faq import FaqUpdate, PublishPayload, RejectPayload
from app.services import faq as faq_service

router = APIRouter(tags=["faq"])


@router.get("/faq/candidates")
async def list_faq_candidates(
    status: str | None = Query(None, pattern="^(pending|published|rejected)$"),
    params: PageParams = Depends(pagination),
    current: CurrentUser = Depends(require_perm("faq:review")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """候选列表，默认按频次降序——高频问题先审收益最大。"""

    items, total = await faq_service.list_candidates(
        session, offset=params.offset, limit=params.page_size, status=status
    )
    return page(
        [item.model_dump(mode="json") for item in items],
        total,
        params.page,
        params.page_size,
    )


@router.post("/faq/candidates/{candidate_id}/publish")
async def publish_faq_candidate(
    candidate_id: UUID,
    payload: PublishPayload,
    current: CurrentUser = Depends(require_perm("faq:publish")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """发布候选为 FAQ，并同步缓存（TECH_SPEC §5.5）。"""

    result = await faq_service.publish_candidate(
        session, candidate_id, answer=payload.answer, reviewer_id=current.user_id
    )
    return success(result.model_dump(mode="json"))


@router.post("/faq/candidates/{candidate_id}/reject")
async def reject_faq_candidate(
    candidate_id: UUID,
    payload: RejectPayload,
    current: CurrentUser = Depends(require_perm("faq:review")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """驳回候选，原因必填。"""

    result = await faq_service.reject_candidate(
        session, candidate_id, reason=payload.reason, reviewer_id=current.user_id
    )
    return success(result.model_dump(mode="json"))


@router.get("/faqs")
async def list_faqs(
    keyword: str | None = Query(None, max_length=128),
    status: str | None = Query(None, pattern="^(published|offline)$"),
    params: PageParams = Depends(pagination),
    current: CurrentUser = Depends(require_perm("faq:review")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    items, total = await faq_service.list_faqs(
        session,
        offset=params.offset,
        limit=params.page_size,
        keyword=keyword,
        status=status,
    )
    return page(
        [item.model_dump(mode="json") for item in items],
        total,
        params.page,
        params.page_size,
    )


@router.put("/faqs/{faq_id}")
async def update_faq(
    faq_id: UUID,
    payload: FaqUpdate,
    current: CurrentUser = Depends(require_perm("faq:publish")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """改答案、切 `cache_enabled` 或下线；三种改动都会同步到缓存。"""

    result = await faq_service.update_faq(session, faq_id, payload)
    return success(result.model_dump(mode="json"))


@router.post("/faq/mine")
async def trigger_faq_mining(
    current: CurrentUser = Depends(require_perm("faq:review")),
) -> dict:
    """手动触发 FAQ 挖掘（TECH_SPEC §4.5）。

    投递到队列即返回：一次挖掘要编码上百条问句，放在请求线程里会拖住连接。结果体现在候选
    列表里，前端轮询候选项即可。

    导入放在函数内：`engines.celery.mine` 会带出 Celery 依赖，而 API 进程没必要在启动时
    就加载它（TECH_SPEC §8.0 的同一考虑）。
    """

    from app.engines.celery.mine import mine_candidates

    task = mine_candidates.delay()
    return success({"task_id": task.id})
