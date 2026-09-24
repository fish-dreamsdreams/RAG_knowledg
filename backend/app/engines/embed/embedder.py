"""BGE-M3 单例编码器（tasklist 5.2，TECH_SPEC §1 Embedding 一致性约束）。

约束与落地方式：

- **进程内单例**：模型权重只加载一次；多线程并发调用**用 `threading.Lock` 串行化**
  整段模型调用（导入侧与问答侧共用同一实例，保证 dense/sparse 同源）。
- **dense 必须 L2 归一化且 dim=1024**：M3 输出经 fp16 计算后范数为 0.9995 左右，
  这里显式转 float32 重新归一化，让「范数=1」成为确定性事实而非精度近似（不变量 P7）。
- **sparse 键必须是 int**：M3 返回的 `lexical_weights` 键是字符串，Milvus 稀疏向量
  要求 `dict[int, float]`，不转换会写不进去。
- **两侧同一 max_length / 同一 pooling**：由本单例统一传参，业务侧无法各传各的。
- **显存不足只降 batch，不换设备**：OOM 时 batch 减半重试，仍失败就抛出。
  静默把本次编码放到 CPU 会写出精度不同的向量，让新旧向量不可比，比直接失败更坏。
- **启动预热**：worker/API 起来就跑一次最小编码，把权重加载与 CUDA context
  创建的代价挑到启动期（首次初始化是全局串行的，否则会拖住同进程其他任务）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, NamedTuple

import numpy as np

from app.common.config import EMBED_DIM, settings

# 导入顺序有要求：必须在 FlagEmbedding/huggingface_hub 之前
from app.common.model_cache import resolve_model_path

logger = logging.getLogger(__name__)

# 子块 ≤500 字、问句更短；1024 足够覆盖且显著快于 M3 默认的 8192。
# 入库与问句共用此值（TECH_SPEC 要求两侧同一 max_length）。
MAX_LENGTH = 1024
DEFAULT_BATCH_SIZE = 16

# 显存不足时的重试次数（每次 batch 减半，退到 1 还不行就抛）
_OOM_RETRY_LIMIT = 2


def is_oom_error(exc: BaseException) -> bool:
    """判断是否为显存不足。

    同一次 OOM 在不同代码路径上分别表现为 `torch.cuda.OutOfMemoryError`
    （它是 RuntimeError 子类）和仅带关键词的 RuntimeError（如 cublas 初始化失败），
    两种都要认，否则会漏掉一半场景。
    """

    import torch

    return isinstance(exc, torch.cuda.OutOfMemoryError) or "out of memory" in str(exc).lower()


class Embedding(NamedTuple):
    """一次编码的结果：dense (n, 1024) float32 已归一化 + sparse token→权重。"""

    dense: np.ndarray
    sparse: list[dict[int, float]]


def normalize_dense(dense: np.ndarray) -> np.ndarray:
    """转 float32 并做 L2 归一化（P7：维度 1024、范数为 1）。"""

    vectors = np.asarray(dense, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[1] != EMBED_DIM:
        raise ValueError(f"dense 形状必须为 (n, {EMBED_DIM})，实际 {vectors.shape}")

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    # 零向量无法归一化，保留原值以免除零（正常输入不会出现）
    np.divide(vectors, norms, out=vectors, where=norms > 0)
    return vectors


def to_sparse_weights(weights: Any) -> dict[int, float]:
    """M3 的 `lexical_weights`（字符串键）转 Milvus 需要的 `dict[int, float]`。"""

    result: dict[int, float] = {}
    for key, value in dict(weights).items():
        weight = float(value)
        # 稀疏向量按 IP 度量，负权重会破坏排序；M3 正常输出为正
        if weight > 0:
            result[int(key)] = weight
    return result


class Embedder:
    """BGE-M3 封装。请通过 `get_embedder()` 获取进程内单例。"""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        device: str | None = None,
        use_sparse: bool | None = None,
    ) -> None:
        self.model_name = model_name or settings.embed_model
        self.device = device or settings.embed_device
        self.use_sparse = settings.embed_sparse_enabled if use_sparse is None else use_sparse
        # 串行化整段模型调用：M3 不是线程安全的，且并发会争抢显存
        self._lock = threading.Lock()
        # 只保护首次加载，与 `_lock` 分离，避免编码期间阻塞加载
        self._load_lock = threading.Lock()
        self._model: Any = None
        self._resolved_path: str | None = None

    # --- 模型加载 ---------------------------------------------------------

    def _load(self) -> Any:
        if self._model is not None:
            return self._model

        # 双重检查：encode 在取编码锁之前调 _load，并发首调可能同时进来，
        # 若不加锁会各自加载一份权重（显存翻倍）。
        with self._load_lock:
            if self._model is not None:
                return self._model

            import torch
            from FlagEmbedding import BGEM3FlagModel

            device = self.device
            if device.startswith("cuda") and not torch.cuda.is_available():
                logger.warning("EMBED_DEVICE=%s 但 CUDA 不可用，编码回落到 cpu", device)
                device = "cpu"
            self.device = device

            self._resolved_path = resolve_model_path(self.model_name)
            logger.info("加载 BGE-M3: %s (device=%s)", self._resolved_path, device)

            use_fp16 = device.startswith("cuda")
            for attempt in range(_OOM_RETRY_LIMIT + 1):
                try:
                    self._model = BGEM3FlagModel(
                        self._resolved_path,
                        use_fp16=use_fp16,
                        device=device,
                    )
                    break
                except RuntimeError as exc:
                    # 加载权重是最容易撞上 OOM 的一步：清一次缓存再试，仍失败就抛
                    if attempt >= _OOM_RETRY_LIMIT or not is_oom_error(exc):
                        raise
                    logger.warning("加载 BGE-M3 显存不足：清理显存缓存后重试", exc_info=True)
                    torch.cuda.empty_cache()
        return self._model

    @property
    def model_path(self) -> str | None:
        """本地权重目录（加载后可见），便于排查与日志。"""

        return self._resolved_path

    # --- 编码 -------------------------------------------------------------

    def encode(
        self,
        texts: list[str],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        return_dense: bool = True,
        return_sparse: bool = True,
    ) -> Embedding:
        """编码文本。索引侧（子块）与查询侧（问句）必须都走这里。"""

        if not texts:
            return Embedding(
                dense=np.zeros((0, EMBED_DIM), dtype=np.float32),
                sparse=[],
            )

        model = self._load()
        want_sparse = return_sparse and self.use_sparse

        with self._lock:
            output = self._encode_with_oom_retry(
                model,
                texts,
                batch_size=batch_size,
                return_dense=return_dense,
                want_sparse=want_sparse,
            )

        dense = (
            normalize_dense(output["dense_vecs"])
            if return_dense
            else np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
        )
        sparse = (
            [to_sparse_weights(weights) for weights in output["lexical_weights"]]
            if want_sparse
            else [{} for _ in texts]
        )
        return Embedding(dense=dense, sparse=sparse)

    # --- 预热 ---------------------------------------------------------------

    def warmup(self) -> bool:
        """预热：加载权重并跑一次最小编码，把加载与 CUDA context 创建挑到启动期。

        worker/API 启动时调用。返回是否成功，**不抛异常**：预热失败只应该让服务
        退回「首次使用时加载」，不该让进程起不来。
        """

        try:
            self.encode([""], batch_size=1, return_dense=True, return_sparse=False)
        except Exception:  # noqa: BLE001 - 预热失败不是致命错误
            logger.warning("BGE-M3 预热失败，退回首次使用时加载", exc_info=True)
            return False
        logger.info("BGE-M3 预热完成：device=%s", self.device)
        return True

    def _encode_with_oom_retry(
        self,
        model: Any,
        texts: list[str],
        *,
        batch_size: int,
        return_dense: bool,
        want_sparse: bool,
    ) -> Any:
        """调模型编码；显存不足就减半 batch 重试。

        只降 batch、**不回落 cpu**：索引侧与查询侧必须同设备同精度（design §1），
        静默换设备会写出与存量向量不可比的新向量。
        """

        import torch

        current = batch_size
        for attempt in range(_OOM_RETRY_LIMIT + 1):
            try:
                return model.encode(
                    list(texts),
                    batch_size=current,
                    max_length=MAX_LENGTH,
                    return_dense=return_dense,
                    return_sparse=want_sparse,
                    return_colbert_vecs=False,
                )
            except RuntimeError as exc:
                if attempt >= _OOM_RETRY_LIMIT or current <= 1 or not is_oom_error(exc):
                    raise
                previous, current = current, max(1, current // 2)
                logger.warning(
                    "编码显存不足：batch_size %s → %s 后重试（第 %s 次）",
                    previous,
                    current,
                    attempt + 1,
                )
                torch.cuda.empty_cache()


_embedder: Embedder | None = None
_singleton_lock = threading.Lock()


def get_embedder() -> Embedder:
    """进程内单例（双重检查加锁）。"""

    global _embedder
    if _embedder is None:
        with _singleton_lock:
            if _embedder is None:
                _embedder = Embedder()
    return _embedder
