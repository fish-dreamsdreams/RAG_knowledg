"""业务错误码与统一异常处理（TECH_SPEC §4.2、§4.3）。"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.common.logging import get_trace_id

logger = logging.getLogger(__name__)


class ErrorCode:
    """稳定业务错误码；响应 JSON 的 code 字段只允许使用这里的常量。"""

    # 通用
    OK = "OK"
    SYS_VALIDATION = "SYS_VALIDATION"
    SYS_DEPENDENCY_UNAVAILABLE = "SYS_DEPENDENCY_UNAVAILABLE"
    SYS_INTERNAL = "SYS_INTERNAL"
    # 认证与权限
    AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
    AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
    # 组织
    ORG_DEPT_NOT_EMPTY = "ORG_DEPT_NOT_EMPTY"
    ORG_ROLE_IN_USE = "ORG_ROLE_IN_USE"
    ORG_ROLE_CODE_TAKEN = "ORG_ROLE_CODE_TAKEN"
    ORG_USERNAME_TAKEN = "ORG_USERNAME_TAKEN"
    # 知识
    KB_NOT_FOUND = "KB_NOT_FOUND"
    KB_UNSUPPORTED_FORMAT = "KB_UNSUPPORTED_FORMAT"
    KB_VERSION_CONFLICT = "KB_VERSION_CONFLICT"
    # FAQ
    FAQ_CANDIDATE_CLOSED = "FAQ_CANDIDATE_CLOSED"
    # 知识缺口
    GAP_NOT_CONVERTIBLE = "GAP_NOT_CONVERTIBLE"
    # 问答
    AI_UPSTREAM_ERROR = "AI_UPSTREAM_ERROR"
    AI_SESSION_NOT_FOUND = "AI_SESSION_NOT_FOUND"


class AppError(Exception):
    """业务异常：message 面向用户，禁止携带堆栈、SQL、密钥。"""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        """构造业务异常：code 给前端分支，message 给用户，http_status 映射 HTTP。"""
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    @classmethod
    def validation(cls, message: str = "请求参数不合法") -> "AppError":
        """请求体或查询参数不合法（400）。"""
        return cls(ErrorCode.SYS_VALIDATION, message, 400)

    @classmethod
    def unauthorized(cls, message: str = "登录已过期，请重新登录") -> "AppError":
        """令牌无效或过期（401）。"""
        return cls(ErrorCode.AUTH_INVALID_TOKEN, message, 401)

    @classmethod
    def forbidden(cls, message: str = "无该功能权限") -> "AppError":
        """已登录但缺少功能权限（403）。"""
        return cls(ErrorCode.AUTH_FORBIDDEN, message, 403)

    @classmethod
    def not_found(cls, message: str = "资源不存在") -> "AppError":
        """知识或其他资源不存在（404）。"""
        return cls(ErrorCode.KB_NOT_FOUND, message, 404)

    @classmethod
    def dept_not_empty(cls, message: str = "该部门下存在子部门或用户，无法删除") -> "AppError":
        """部门仍有子部门或用户，禁止删除（409）。"""
        return cls(ErrorCode.ORG_DEPT_NOT_EMPTY, message, 409)

    @classmethod
    def role_in_use(cls, message: str = "该角色仍被用户引用，无法删除") -> "AppError":
        """角色仍被用户引用，禁止删除（409）。"""
        return cls(ErrorCode.ORG_ROLE_IN_USE, message, 409)

    @classmethod
    def role_code_taken(cls, message: str = "角色编码已存在") -> "AppError":
        """角色编码冲突（409）。"""
        return cls(ErrorCode.ORG_ROLE_CODE_TAKEN, message, 409)

    @classmethod
    def username_taken(cls, message: str = "用户名已存在") -> "AppError":
        """用户名已被占用（409）。"""
        return cls(ErrorCode.ORG_USERNAME_TAKEN, message, 409)

    @classmethod
    def unsupported_format(cls, message: str = "不支持的文件类型") -> "AppError":
        """导入文件类型不在允许列表（422）。"""
        return cls(ErrorCode.KB_UNSUPPORTED_FORMAT, message, 422)

    @classmethod
    def candidate_closed(
        cls, message: str = "该候选已被处理，请刷新后重试"
    ) -> "AppError":
        """FAQ 候选已关闭或已处理，禁止再操作（409）。"""
        return cls(ErrorCode.FAQ_CANDIDATE_CLOSED, message, 409)

    @classmethod
    def version_conflict(
        cls, message: str = "该知识单元已被他人修改，请刷新后重试"
    ) -> "AppError":
        """知识单元版本冲突，需刷新后重试（409）。"""
        return cls(ErrorCode.KB_VERSION_CONFLICT, message, 409)

    @classmethod
    def gap_not_convertible(cls, message: str = "该缺口已补全，无需再次转建") -> "AppError":
        """知识缺口已补全，不能再次转建（409）。"""
        return cls(ErrorCode.GAP_NOT_CONVERTIBLE, message, 409)

    @classmethod
    def dependency_unavailable(cls, message: str = "依赖服务暂不可用，请稍后重试") -> "AppError":
        """PostgreSQL / Redis / Milvus 等依赖暂不可用（503）。"""
        return cls(ErrorCode.SYS_DEPENDENCY_UNAVAILABLE, message, 503)

    @classmethod
    def ai_upstream(cls, message: str = "模型服务暂不可用，请稍后重试") -> "AppError":
        """上游 Chat / 视觉网关失败（502）。"""
        return cls(ErrorCode.AI_UPSTREAM_ERROR, message, 502)

    @classmethod
    def session_not_found(cls, message: str = "会话不存在") -> "AppError":
        """会话不存在，或不属于当前用户——两者必须同一句话、同一个码（P4 同理）。

        区分开就等于给出了一个探测「这个会话 id 存不存在、是谁的」的接口。
        """

        return cls(ErrorCode.AI_SESSION_NOT_FOUND, message, 404)

    @classmethod
    def internal(cls, message: str = "服务器内部错误") -> "AppError":
        """未归类的内部错误（500）；message 仍不得带堆栈或密钥。"""
        return cls(ErrorCode.SYS_INTERNAL, message, 500)


def _error_response(code: str, message: str, http_status: int) -> JSONResponse:
    """组装统一错误体 `{code, message, data: null}`，并带上 X-Trace-Id。"""
    return JSONResponse(
        status_code=http_status,
        content={"code": code, "message": message, "data": None},
        headers={"X-Trace-Id": get_trace_id()},
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """把 AppError 转成统一 JSON；5xx 额外打错误日志。"""
    assert isinstance(exc, AppError)
    if exc.http_status >= 500:
        logger.error("业务异常 %s %s: %s", request.method, request.url.path, exc.code)
    return _error_response(exc.code, exc.message, exc.http_status)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """请求校验失败：细节只进日志，对外固定 SYS_VALIDATION。"""
    assert isinstance(exc, RequestValidationError)
    # 细节只进日志，响应只给业务码（TECH_SPEC §4.2）
    logger.info("参数校验失败 %s %s: %s", request.method, request.url.path, exc.errors())
    return _error_response(ErrorCode.SYS_VALIDATION, "请求参数不合法", 400)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底未捕获异常，对外只返回 SYS_INTERNAL，不泄露堆栈。"""
    logger.exception("未预期异常 %s %s", request.method, request.url.path)
    return _error_response(ErrorCode.SYS_INTERNAL, "服务器内部错误", 500)


def register_exception_handlers(app: FastAPI) -> None:
    """注册业务异常、校验失败与未捕获异常三类处理器。"""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
