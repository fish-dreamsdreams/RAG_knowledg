"""编码后处理单元测试（tasklist 5.5 的 P7 部分，不需要模型与 GPU）。

后处理是「dense 范数恒为 1」「sparse 键为 int」两条约定的落地点，
把它单独抽出来测，可以让不变量 P7 在没有显卡的机器上也能回归。
"""

import numpy as np
import pytest

from app.common.config import EMBED_DIM
from app.engines.embed import Embedder, normalize_dense, to_sparse_weights


def test_normalize_dense_scales_to_unit_norm() -> None:
    vectors = np.array([[3.0, 4.0] + [0.0] * (EMBED_DIM - 2)] * 3, dtype=np.float32)

    normalized = normalize_dense(vectors)

    assert normalized.dtype == np.float32
    assert normalized.shape == (3, EMBED_DIM)
    assert np.allclose(np.linalg.norm(normalized, axis=1), 1.0, atol=1e-6)


def test_normalize_dense_fixes_float16_precision_loss() -> None:
    """M3 用 fp16 推理，原始范数约 0.9995；归一化后必须严格回到 1。"""

    raw = np.random.default_rng(0).standard_normal((4, EMBED_DIM)).astype(np.float16)
    fp32 = raw.astype(np.float32)
    before = np.linalg.norm(fp32, axis=1)
    assert not np.allclose(before, 1.0), "构造的数据本身就不是单位向量"

    after = np.linalg.norm(normalize_dense(raw), axis=1)

    assert np.allclose(after, 1.0, atol=1e-6)


def test_normalize_dense_keeps_zero_vector_finite() -> None:
    normalized = normalize_dense(np.zeros((1, EMBED_DIM), dtype=np.float32))

    assert np.all(np.isfinite(normalized))


def test_normalize_dense_rejects_wrong_dimension() -> None:
    with pytest.raises(ValueError, match="1024"):
        normalize_dense(np.zeros((2, 768), dtype=np.float32))


def test_to_sparse_weights_converts_string_keys_to_int() -> None:
    """M3 返回 {'1903': 0.11}，Milvus 稀疏向量要求 {1903: 0.11}。"""

    weights = {"1903": 0.11, "2649": 0.15, "29906": 0.2098}

    converted = to_sparse_weights(weights)

    assert converted == {1903: 0.11, 2649: 0.15, 29906: 0.2098}
    assert all(isinstance(key, int) for key in converted)
    assert all(isinstance(value, float) for value in converted.values())


def test_to_sparse_weights_drops_non_positive_weights() -> None:
    """IP 度量下负权重会污染排序，直接丢弃。"""

    converted = to_sparse_weights({"1": 0.5, "2": 0.0, "3": -0.2})

    assert converted == {1: 0.5}


def test_encode_empty_input_does_not_load_model() -> None:
    """空输入必须短路返回：不能让单元测试触发模型加载（会拉 torch 与权重）。"""

    embedder = Embedder()

    result = embedder.encode([])

    assert result.dense.shape == (0, EMBED_DIM)
    assert result.sparse == []
    assert embedder.model_path is None, "空输入不应加载模型"


# --- 显存不足与预热（不需要模型与 GPU，用替身模型驱动） --------------------------


class _FakeModel:
    """替身模型：只关心 batch 序列与前几次该抛什么。"""

    def __init__(self, *, failures: int = 0, message: str = "CUDA out of memory") -> None:
        self.failures = failures
        self.message = message
        self.batch_sizes: list[int] = []

    def encode(self, texts, **kwargs):
        self.batch_sizes.append(kwargs["batch_size"])
        if len(self.batch_sizes) <= self.failures:
            raise RuntimeError(self.message)
        return {
            "dense_vecs": np.ones((len(texts), EMBED_DIM), dtype=np.float16),
            "lexical_weights": [{"1": 0.5} for _ in texts],
        }


def _embedder_with(model) -> Embedder:
    """注入替身模型，绕过 `_load()` 的真实加载。"""

    embedder = Embedder()
    embedder._model = model  # noqa: SLF001 - 单测注入替身
    return embedder


def test_encode_halves_batch_and_retries_on_oom() -> None:
    """显存不足时减半 batch 重试，不放弃也不偷换设备。"""

    model = _FakeModel(failures=1)
    embedder = _embedder_with(model)

    result = embedder.encode(["甲", "乙"], batch_size=16)

    assert model.batch_sizes == [16, 8]
    assert result.dense.shape == (2, EMBED_DIM)
    assert np.allclose(np.linalg.norm(result.dense, axis=1), 1.0, atol=1e-6)


def test_encode_gives_up_after_oom_retry_limit() -> None:
    """重试仍不足就抛出，由流水线把单元置为 failed（P15），不静默降级。"""

    model = _FakeModel(failures=99)
    embedder = _embedder_with(model)

    with pytest.raises(RuntimeError, match="out of memory"):
        embedder.encode(["甲"], batch_size=16)

    assert model.batch_sizes == [16, 8, 4]


def test_encode_does_not_retry_when_batch_is_already_one() -> None:
    model = _FakeModel(failures=99)
    embedder = _embedder_with(model)

    with pytest.raises(RuntimeError):
        embedder.encode(["甲"], batch_size=1)

    assert model.batch_sizes == [1]


def test_encode_does_not_retry_other_runtime_errors() -> None:
    """只有 OOM 才值得减 batch，其他 RuntimeError 重试等于掩盖真问题。"""

    model = _FakeModel(failures=1, message="mat1 and mat2 shapes cannot be multiplied")
    embedder = _embedder_with(model)

    with pytest.raises(RuntimeError, match="mat1"):
        embedder.encode(["甲"], batch_size=16)

    assert model.batch_sizes == [16]


def test_encode_retries_cublas_style_oom_message() -> None:
    """部分路径只给带关键词的 RuntimeError（如 cublas），同样要认。"""

    model = _FakeModel(failures=1, message="CUDA error: out of memory")
    embedder = _embedder_with(model)

    embedder.encode(["甲"], batch_size=16)

    assert model.batch_sizes == [16, 8]


def test_warmup_returns_false_and_swallows_failure() -> None:
    """预热失败只该退回「首次使用时加载」，不该把启动打断。"""

    embedder = _embedder_with(_FakeModel(failures=99))

    assert embedder.warmup() is False


def test_warmup_returns_true_and_runs_minimal_encode() -> None:
    model = _FakeModel()
    embedder = _embedder_with(model)

    assert embedder.warmup() is True
    assert model.batch_sizes == [1]
