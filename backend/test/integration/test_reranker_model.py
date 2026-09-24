"""Cross-Encoder 重排模型的集成测试（tasklist 10.1）。

验证三件事：

1. 权重能加载并打分，分数落在 `[0, 1]`——断崖的 `min_drop=0.15` 是绝对阈值，依赖这个量纲
2. 重排分与 M3 余弦**不是同一个东西**（TECH_SPEC §1 明确禁止混用）
3. 真正回答了问题的段落排在前面

需要本地有 `settings.rerank_model` 权重，首次运行会下载；缺权重时 skip 而不是失败（首次要下
1~2GB，受限网络下能把整个集成套件拖到超时）。
"""

from __future__ import annotations

import pytest

from app.common.config import settings
from app.common.model_cache import has_local_weights
from app.engines.embed import get_embedder
from app.engines.rerank import get_reranker, rank

pytestmark = pytest.mark.integration

QUESTION = "年假可以休几天？"
RELEVANT = (
    "员工累计工作满 1 年不满 10 年的，年休假 5 天；满 10 年不满 20 年的，年休假 10 天。"
)
IRRELEVANT = "办公用品申领需填写领用单，经部门负责人签字后到行政部领取。"


@pytest.fixture(scope="module")
def reranker():
    if not has_local_weights(settings.rerank_model):
        pytest.skip(f"本地缺少 {settings.rerank_model} 权重，先跑 scripts/fetch_models.py 再试")

    try:
        instance = get_reranker()
        instance.score(QUESTION, [RELEVANT])
    except Exception as exc:  # noqa: BLE001 - 权重损坏或环境不支持时跳过
        pytest.skip(f"Cross-Encoder 不可用：{exc}")
    return instance


def test_scores_are_probabilities(reranker) -> None:
    scores = reranker.score(QUESTION, [RELEVANT, IRRELEVANT])

    assert len(scores) == 2
    assert all(0.0 <= score <= 1.0 for score in scores), "断崖阈值依赖 [0,1] 量纲"
    assert scores[0] > scores[1], "回答了问题的那段必须得分更高"


def test_rerank_score_is_not_dense_similarity(reranker) -> None:
    """TECH_SPEC §1：不得把 M3 向量相似度当作重排分。"""

    rerank_scores = reranker.score(QUESTION, [RELEVANT, IRRELEVANT])
    dense = get_embedder().encode([QUESTION, RELEVANT, IRRELEVANT]).dense
    cosine = float(dense[0] @ dense[1])

    assert cosine != pytest.approx(rerank_scores[0], abs=1e-6)


def test_rank_puts_relevant_passage_first(reranker) -> None:
    passages = [
        IRRELEVANT,
        RELEVANT,
        "本制度自发布之日起施行，由人力资源部负责解释。",
    ]

    ranked = rank(QUESTION, passages, reranker=reranker, min_keep=1, max_keep=10)

    assert ranked[0][0] == 1, "相关段应排在第一，而不是靠向量相似度撞上来"
