"""依赖不可用时的失败关闭（P13 的检索层）。

设计文档 P13：依赖不可用必须返回 503，**绝不降级为无 ACL 全库检索**。这里断言的是它在
检索层的样子：故障转成 503，而不是被吞成空结果——空结果会被后面的节点当成「知识库里
没有这条」，然后照常生成答案，那才是最坏的那种降级。

不去停 Milvus 容器：把客户端指向一个没人监听的端口，拿到的就是真实的连接失败，
比 mock 更贴近「Milvus 挂了」，而且不依赖 compose，能进默认单测套件（直接 `pytest`）。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.common.config import EMBED_DIM, settings
from app.common.errors import AppError, ErrorCode
from app.engines.embed import Embedding
from app.engines.retrieve import milvus_store, search

# 127.0.0.1:1 上没有服务：连接被立刻拒绝，用例不会卡在超时上
DEAD_URI = "http://127.0.0.1:1"


@pytest.fixture
def dead_milvus(monkeypatch):
    """指向死端口并清掉进程内单例，保证用例真的重新建连（结束由 monkeypatch 还原）。"""

    monkeypatch.setattr(settings, "milvus_uri", DEAD_URI)
    monkeypatch.setattr(milvus_store, "_client", None)
    monkeypatch.setattr(milvus_store, "_collection_ready", False)
    return DEAD_URI


def _embedding() -> Embedding:
    """一条合法形状的查询向量：本用例要的是连不上，不是向量算得对不对。"""

    dense = np.zeros((1, EMBED_DIM), dtype=np.float32)
    dense[0, 0] = 1.0
    return Embedding(dense=dense, sparse=[{1: 1.0}])


def test_client_construction_failure_becomes_503(dead_milvus) -> None:
    """建连失败发生在 `get_client()` 内部，同样必须是 503（P13）。"""

    with pytest.raises(AppError) as failure:
        milvus_store.get_client()

    assert failure.value.code == ErrorCode.SYS_DEPENDENCY_UNAVAILABLE
    assert failure.value.http_status == 503


def test_hybrid_search_fails_closed(dead_milvus) -> None:
    """检索不可用时抛 503，不返回空列表：返回空列表就是降级成「没有知识」。"""

    with pytest.raises(AppError) as failure:
        search.hybrid_search(_embedding())

    assert failure.value.code == ErrorCode.SYS_DEPENDENCY_UNAVAILABLE
    assert failure.value.http_status == 503


def test_hyde_search_fails_closed(dead_milvus) -> None:
    """第二条召回路径同口径（P13 不区分哪一路先失败）。"""

    with pytest.raises(AppError) as failure:
        search.hyde_search(_embedding())

    assert failure.value.code == ErrorCode.SYS_DEPENDENCY_UNAVAILABLE
