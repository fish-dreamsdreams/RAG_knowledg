"""统一响应包装与分页结构（TECH_SPEC §4.2、§4.4）。"""

from typing import Any

from pydantic import BaseModel

from app.common.errors import ErrorCode


class Envelope(BaseModel):
    code: str = ErrorCode.OK
    message: str = "success"
    data: Any = None


class PageData(BaseModel):
    items: list[Any] = []
    total: int = 0
    page: int = 1
    page_size: int = 20


def success(data: Any = None, message: str = "success") -> dict[str, Any]:
    return {"code": ErrorCode.OK, "message": message, "data": data}


def page(items: list[Any], total: int, page_number: int, page_size: int) -> dict[str, Any]:
    return success(
        PageData(
            items=items, total=total, page=page_number, page_size=page_size
        ).model_dump()
    )
