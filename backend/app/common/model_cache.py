"""本地模型权重缓存的统一入口（BGE-M3 / Reranker 共用）。

本机实测发现两个镜像相关的约束，集中在这里处理：

1. **垃圾文件 403**：`HF_ENDPOINT` 指向镜像时，模型仓库里的 `imgs/.DS_Store`
   等文件会返回 403，导致整个 `snapshot_download` 失败 → 下载时跳过它们。
2. **Xet 传输 401**：镜像不支持 Xet（`cas-server.xethub.hf.co` 返回 401）→ 必须先禁用。
   该开关在 `import huggingface_hub` 时被读入模块常量，所以必须在**导入之前**设置，
   因此本模块只能在任何 huggingface_hub / transformers / FlagEmbedding 导入之前被导入。
"""

from __future__ import annotations

import os

from app.common.config import settings

# 必须在 huggingface_hub 导入前生效：镜像不支持 Xet 传输
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# Windows 上缓存默认走符号链接，未开开发者模式会刷一屏警告
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
if settings.hf_home:
    os.environ.setdefault("HF_HOME", settings.hf_home)

# 权重不需要的文件：图片、macOS 垃圾文件、非 PyTorch 权重
IGNORED_PATTERNS = ["imgs/*", "*.DS_Store", "onnx/*", "*.h5", "*.msgpack", "*.ot"]


def has_local_weights(model: str) -> bool:
    """本地缓存里是否已有**权重文件**，而不只是 config.json。

    `hf_hub_download` 拉一个 config.json 就会建出 `models--…` 目录，所以只查目录会误判；
    权重落在 `snapshots/<rev>/` 下，认 `*.safetensors` 或 `*.bin`。
    """

    root = model if os.path.isdir(model) else None
    if root is None:
        from huggingface_hub import constants

        root = os.path.join(
            constants.HF_HUB_CACHE, "models--" + model.replace("/", "--")
        )

    for _, _, files in os.walk(root):
        if any(name.endswith((".safetensors", ".bin")) for name in files):
            return True
    return False


def resolve_model_path(model: str) -> str:
    """返回可加载的本地模型目录。

    `model` 本身是本地目录时直接使用；否则从 Hub 下载（跳过垃圾文件）后返回缓存快照目录。
    返回本地路径而不是仓库名，可让上层（FlagEmbedding / sentence-transformers）
    跳过它们自带的下载逻辑，避开上面的 403。
    """

    if os.path.isdir(model):
        return model

    # 延迟导入：本模块要在 huggingface_hub 之前被导入，但不代表要立刻加载它
    # 已经是本地目录就原样返回：权重可以放在仓库内（如 `backend/hub/…`），
    # 不必进 Hugging Face 缓存，`snapshot_download` 也就完全不联网
    if os.path.isdir(model):
        return model

    from huggingface_hub import snapshot_download

    return snapshot_download(repo_id=model, ignore_patterns=IGNORED_PATTERNS, max_workers=8)
