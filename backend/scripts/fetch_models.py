"""预取本地模型权重（TECH_SPEC §1：推理不联网，权重走本地缓存）。

首次部署、换了模型名或清了缓存之后跑一次：

    backend> .venv\\Scripts\\python.exe scripts\\fetch_models.py
    backend> .venv\\Scripts\\python.exe scripts\\fetch_models.py BAAI/bge-reranker-v2-m3

不带参数时拉取配置里的 embed 与 rerank 两个模型。权重都不小（M3 约 2GB、reranker 约
1GB），网络受限时会很慢——所以集成测试只在权重已就绪时才跑，缺权重直接 skip。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让 `python scripts/fetch_models.py` 也能导入 app：脚本目录本身不在包内
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.common.config import settings  # noqa: E402
from app.common.model_cache import resolve_model_path  # noqa: E402


def main(argv: list[str]) -> int:
    models = argv[1:] or [settings.embed_model, settings.rerank_model]
    for model in models:
        print(f"拉取 {model} …", flush=True)
        try:
            print(f"  -> {resolve_model_path(model)}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 预取失败要给出清晰退出码
            print(f"失败 {model}：{exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
