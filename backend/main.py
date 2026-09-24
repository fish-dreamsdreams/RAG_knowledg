"""后端服务启动入口：`python main.py`。

等价于 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`——`scripts/dev-up.ps1`
起后端用的就是这条命令，本文件的默认值跟它对齐：同一个地址，同样不开热重载（前端 Vite 代理
指向 8000，开热重载会在改后端代码时断一次 WebSocket）。

它存在的理由只有一个：手工起服务、换端口、或开 `--reload` 调试时，不必先想起 uvicorn 的参数
与工作目录。整个开发栈（uvicorn + vite + celery 一起、隐藏窗口、日志与 PID 落在 `var/`）仍然
走 `scripts/dev-up.ps1`，两条路互不干扰。

用法：

    backend> .venv\\Scripts\\python.exe main.py                  # 127.0.0.1:8000
    backend> .venv\\Scripts\\python.exe main.py --reload          # 改后端代码自动重启
    backend> .venv\\Scripts\\python.exe main.py --port 8010
    backend> .venv\\Scripts\\python.exe main.py --host 0.0.0.0    # 局域网可访问

**启动前不探依赖**（PostgreSQL / Redis / Milvus）：连不上时应用自己的启动钩子与健康检查会报
出来，在这里再加一层只会多一处需要同步的判据。

**别在这台机器上开 `--workers > 1`**：每个 worker 各自加载一遍 BGE-M3 与重排模型，显存是按
一份算的（RTX 5070 Laptop 8G）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

BACKEND_DIR = Path(__file__).resolve().parent
# 用 import 字符串而不是 app 对象：`--reload` 与 `--workers` 都要求 uvicorn 能自己重新导入它
ASGI_APP = "app.main:app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动知识库管理平台后端（uvicorn）")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址，默认 {DEFAULT_HOST}")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认 {DEFAULT_PORT}"
    )
    parser.add_argument(
        "--reload", action="store_true", help="改后端代码自动重启（会断一次 WebSocket）"
    )
    parser.add_argument("--workers", type=int, default=1, help="进程数，默认 1")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn 日志级别，默认 info",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.reload and args.workers > 1:
        # uvicorn 自己也会拒绝这个组合，但报错发生在参数校验的深处，这里直说更省事
        print("--reload and --workers>1 are mutually exclusive (hot reload runs one process)")
        return 2

    # 打印内容一律 ASCII：Windows PowerShell 5.1 按系统 ANSI 码页读管道里的字节，中文会变乱码
    # （`scripts/dev-up.ps1` 开头记过同一件事，那里的 Write-Host 同样是纯 ASCII）。
    print(f"starting backend  app={ASGI_APP}  http://{args.host}:{args.port}")
    print(f"  health : http://{args.host}:{args.port}/api/v1/health")
    print(f"  docs   : http://{args.host}:{args.port}/docs")
    if args.host == "0.0.0.0":
        print("  note   : bound to 0.0.0.0 - from this machine use 127.0.0.1 in URLs")
    if args.workers > 1:
        print(f"  note   : {args.workers} workers each load their own copy of the local models")

    # reload_dirs 只在热重载时给：脚本支持从任意目录启动，不给的话 uvicorn 会盯着当前目录
    extra = {"reload_dirs": [str(BACKEND_DIR)]} if args.reload else {}
    uvicorn.run(
        ASGI_APP,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        log_level=args.log_level,
        **extra,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
