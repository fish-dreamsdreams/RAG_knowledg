"""结构化日志与 trace_id 透传。"""

import logging
import sys
from contextvars import ContextVar

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def set_trace_id(value: str) -> None:
    """把当前请求的 trace_id 写入 ContextVar，供日志与错误响应透传。"""
    _trace_id.set(value)


def get_trace_id() -> str:
    """读取当前上下文的 trace_id；未设置时为 '-'。"""
    return _trace_id.get()


class TraceIdFilter(logging.Filter):
    """把 ContextVar 里的 trace_id 写进每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """始终放行；只往 record 上挂 trace_id 字段。"""
        record.trace_id = _trace_id.get()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """配置根日志：标准输出、带 trace_id 的格式，并清掉已有 handler。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(trace_id)s] %(name)s: %(message)s")
    )
    handler.addFilter(TraceIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
