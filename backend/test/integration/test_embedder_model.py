"""BGE-M3 真实编码测试（不变量 P7）。

需要 GPU 与本地权重（`uv pip install torch --index-url .../cu128` + 首次运行下载权重），
因此标记为 integration：`pytest -m integration`。
"""

import numpy as np
import pytest

from app.common.config import EMBED_DIM
from app.engines.embed import Embedder, get_embedder

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def embedder():
    return get_embedder()


def test_embedder_is_process_singleton() -> None:
    assert get_embedder() is get_embedder()


def test_dense_dimension_and_norm(embedder) -> None:
    """P7：dense 维度恒为 1024，L2 范数为 1。"""

    result = embedder.encode(
        ["总公司财务部的报销流程是什么？", "员工差旅费报销上限为 2000 元。", "食堂今日菜单"]
    )

    assert result.dense.shape == (3, EMBED_DIM)
    assert result.dense.dtype == np.float32
    assert np.allclose(np.linalg.norm(result.dense, axis=1), 1.0, atol=1e-6)


def test_sparse_weights_are_positive_int_keyed(embedder) -> None:
    result = embedder.encode(["员工差旅费报销上限为 2000 元。"])

    weights = result.sparse[0]
    assert weights, "稀疏权重不应为空"
    assert all(isinstance(key, int) and key > 0 for key in weights)
    assert all(value > 0 for value in weights.values())


def test_encoding_is_deterministic(embedder) -> None:
    text = "差旅费报销需要部门负责人审批。"

    first = embedder.encode([text])
    second = embedder.encode([text])

    assert np.array_equal(first.dense, second.dense)
    assert first.sparse == second.sparse


def test_similar_text_scores_higher_than_unrelated(embedder) -> None:
    """基本语义合理性：相似问句的余弦分应高于无关句。"""

    result = embedder.encode(
        ["差旅费报销上限是多少", "出差费用报销的最高额度是多少", "公司年会在几月举行"]
    )
    query, related, unrelated = result.dense

    assert float(query @ related) > float(query @ unrelated)


def test_batch_and_single_encode_agree(embedder) -> None:
    """批量与逐条编码必须一致，否则入库/查询两侧会漂移。"""

    texts = ["报销流程", "审批权限"]

    batched = embedder.encode(texts)

    for index, text in enumerate(texts):
        single = embedder.encode([text])
        assert np.allclose(batched.dense[index], single.dense[0], atol=1e-6)


def test_concurrent_first_encode_loads_weights_once(monkeypatch, tmp_path) -> None:
    """并发首次编码只能加载一份权重（否则显存翻倍）。

    这里用假模型，避免真的加载 2.2GB 权重；但保留 FlagEmbedding 导入，
    所以仍归在 integration 层（单元测试套件不应依赖 ML 依赖）。
    """

    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import FlagEmbedding

    load_count = 0
    count_lock = threading.Lock()

    class FakeModel:
        def __init__(self, *args, **kwargs) -> None:
            nonlocal load_count
            with count_lock:
                load_count += 1
            time.sleep(0.05)  # 放大竞态窗口

        def encode(self, texts, **kwargs):
            return {
                "dense_vecs": np.ones((len(texts), EMBED_DIM), dtype=np.float16),
                "lexical_weights": [{"1903": 0.1} for _ in texts],
            }

    monkeypatch.setattr(FlagEmbedding, "BGEM3FlagModel", FakeModel)
    # 传本地目录，避免 resolve_model_path 联网（假模型不需要真权重）
    embedder = Embedder(model_name=str(tmp_path), device="cpu")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: embedder.encode(["并行编码"]), range(8)))

    assert load_count == 1, f"权重被加载了 {load_count} 次"
    assert all(result.dense.shape == (1, EMBED_DIM) for result in results)
    assert all(result.sparse[0] == {1903: 0.1} for result in results)
