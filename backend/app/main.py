"""FastAPI 入口：中间件、异常处理、路由挂载。"""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import api_router
from app.common.config import settings
from app.common.errors import register_exception_handlers
from app.common.logging import configure_logging, set_trace_id
from app.schemas.common import success


class Utf8JSONResponse(JSONResponse):
    """显式声明 charset。

    Starlette 默认只发 `application/json`，而部分客户端（如 Windows PowerShell 5.1
    的 Invoke-WebRequest）在缺 charset 时按 Latin-1 解码，中文会变乱码。
    """

    media_type = "application/json; charset=utf-8"


async def _warmup_embedder() -> None:
    """启动时预热本地模型；失败只告警，不影响服务可用。

    放线程里执行：加载权重是阻塞调用（秒级），不能卡住事件循环。
    import 也放在函数内，`EMBED_WARMUP_ENABLED=false` 时进程完全不碰 torch。
    """

    if not settings.embed_warmup_enabled:
        return

    from app.engines.embed import get_embedder

    await asyncio.to_thread(get_embedder().warmup)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await _warmup_embedder()
    yield


app = FastAPI(
    title="知识库管理平台 API",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=Utf8JSONResponse,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id"],
)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """每个请求一个 trace_id，回写响应头（不变量 P14）。"""

    trace_id = request.headers.get("X-Trace-Id") or str(uuid4())
    set_trace_id(trace_id)
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    return response


register_exception_handlers(app)
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["system"])
async def root_health() -> dict:
    return success({"status": "ok"})
