"""健康检查。"""

from fastapi import APIRouter

from app.schemas.common import success

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict:
    return success({"status": "ok"})
