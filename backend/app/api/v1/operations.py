"""运营闭环接口：知识缺口、审计查询与看板（tasklist 13.1 ~ 13.3）。

三类资源放同一个模块：它们同属「运营」这一块，共用同一套时间窗口径，审计与看板还是同一个
权限（`dash:view`）。拆成三个文件会把「看板的每个数字都来自审计」这层关系藏起来。

权限沿用 PRD §4 的按钮码：缺口列表 `gap:view`、转建 `gap:convert`、审计与看板 `dash:view`。
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, PageParams, pagination, require_perm
from app.common.db import get_session
from app.schemas.common import page, success
from app.services import operations as operations_service
from app.services.operations import RANGE_7D

router = APIRouter(tags=["operations"])

_RANGE_PATTERN = "^(today|7d)$"
_STATUS_PATTERN = "^(answered|denied|gap|interrupted)$"
_GAP_STATUS_PATTERN = "^(open|converted|filled)$"


@router.get("/knowledge-gaps")
async def list_knowledge_gaps(
    status: str | None = Query(None, pattern=_GAP_STATUS_PATTERN),
    department_id: UUID | None = Query(None),
    params: PageParams = Depends(pagination),
    current: CurrentUser = Depends(require_perm("gap:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """缺口清单，按频次降序——同样补一篇文档，高频缺口先补收益最大。"""

    items, total = await operations_service.list_gaps(
        session,
        offset=params.offset,
        limit=params.page_size,
        status=status,
        department_id=department_id,
    )
    return page(
        [item.model_dump(mode="json") for item in items], total, params.page, params.page_size
    )


@router.post("/knowledge-gaps/{gap_id}/convert")
async def convert_knowledge_gap(
    gap_id: UUID,
    current: CurrentUser = Depends(require_perm("gap:convert")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """转建：标记 `converted` 并返回导入抽屉的预填信息。

    只标记不建单元：真正的知识单元由导入接口带 `from_gap_id` 创建，索引完成后回填
    `filled`（P16）。
    """

    result = await operations_service.convert_gap(session, gap_id)
    return success(result.model_dump(mode="json"))


@router.get("/audit-logs")
async def list_audit_logs(
    start: date | None = Query(None, description="起始日期（含），默认与 end 构成近 7 天"),
    end: date | None = Query(None, description="结束日期（含），默认今天"),
    user_id: UUID | None = Query(None),
    faq_hit: bool | None = Query(None),
    answer_status: str | None = Query(None, pattern=_STATUS_PATTERN),
    params: PageParams = Depends(pagination),
    current: CurrentUser = Depends(require_perm("dash:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """审计流水，时间倒序。日期按业务时区解释，避免 UTC 边界把一天的记录劈成两半。"""

    start_at, end_at = operations_service.resolve_day_range(start, end)
    items, total = await operations_service.list_audit_logs(
        session,
        offset=params.offset,
        limit=params.page_size,
        start=start_at,
        end=end_at,
        user_id=user_id,
        faq_hit=faq_hit,
        answer_status=answer_status,
    )
    return page(
        [item.model_dump(mode="json") for item in items], total, params.page, params.page_size
    )


@router.get("/dashboard/summary")
async def dashboard_summary(
    range: str = Query(RANGE_7D, pattern=_RANGE_PATTERN),
    current: CurrentUser = Depends(require_perm("dash:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """顶部指标卡：PV / UV / 知识数 / FAQ 命中率 / 覆盖率 / 时延。"""

    summary = await operations_service.dashboard_summary(session, range_key=range)
    return success(summary.model_dump(mode="json"))


@router.get("/dashboard/top-questions")
async def dashboard_top_questions(
    range: str = Query(RANGE_7D, pattern=_RANGE_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    current: CurrentUser = Depends(require_perm("dash:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    items = await operations_service.top_questions(session, range_key=range, limit=limit)
    return success([item.model_dump(mode="json") for item in items])


@router.get("/dashboard/top-knowledge")
async def dashboard_top_knowledge(
    range: str = Query(RANGE_7D, pattern=_RANGE_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    current: CurrentUser = Depends(require_perm("dash:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """热门知识榜：按被引用次数排序（审计里的 citation 反查单元标题）。"""

    items = await operations_service.top_knowledge(session, range_key=range, limit=limit)
    return success([item.model_dump(mode="json") for item in items])


@router.get("/dashboard/token-trend")
async def dashboard_token_trend(
    range: str = Query(RANGE_7D, pattern=_RANGE_PATTERN),
    current: CurrentUser = Depends(require_perm("dash:view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Token 趋势与响应时长分布：按天聚合，没有问答的日子补零。"""

    series = await operations_service.token_trend(session, range_key=range)
    return success(series.model_dump(mode="json"))
