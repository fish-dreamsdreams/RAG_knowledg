"""FAQ 挖掘的聚类与归并逻辑测试（tasklist 11.4）。

纯单元：聚类是纯函数；`run_mining` 的查库与写库被替换掉，只验证归并、频次门槛与幂等。
数据库侧的真实行为（过滤 `faq_hit`、`answer_status`）在集成测试里覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.engines import faq_cache
from app.repositories import faq as faq_repo
from app.services import faq_mining
from app.services.faq_mining import MiningResult, cluster_questions, run_mining

MIN_FREQ = 3


class _Session:
    """`run_mining` 只用到 commit。"""

    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def patches(monkeypatch):
    """把编码、系统配置与仓储层替换成内存实现，并记录建出的候选。"""

    created: list[dict] = []
    pending: list = []
    questions: list[SimpleNamespace] = []
    settled: list = []

    def _fake_encode(items):
        """按问句主题给确定性向量：年假类彼此相近，其它主题与它正交。

        用常量向量会让所有问题都合并成一簇，聚类断言就失去意义。
        """

        return [
            [1.0, 0.01, 0.0, 0.0] if text.startswith("年假") else [0.0, 0.0, 1.0, 0.0]
            for text in items
        ]

    monkeypatch.setattr(faq_mining, "_encode_many", _fake_encode)

    async def _min_freq(_session) -> int:
        return MIN_FREQ

    monkeypatch.setattr(faq_mining, "_min_freq", _min_freq)

    async def _recent(_session, *, limit: int):
        return questions[:limit]

    async def _pending(_session):
        return pending

    async def _settled(_session):
        # 真函数返回的是**归一化后**的问句（与命中判定同一口径），替身也照这个契约来
        return {faq_cache.normalize(text) for text in settled}

    async def _create(_session, **fields):
        row = SimpleNamespace(id=uuid4(), **fields)
        created.append(fields)
        pending.append(row)
        return row

    monkeypatch.setattr(faq_repo, "recent_unanswered_questions", _recent)
    monkeypatch.setattr(faq_repo, "pending_candidates", _pending)
    monkeypatch.setattr(faq_repo, "create_candidate", _create)
    monkeypatch.setattr(faq_mining, "_settled_questions", _settled)

    return SimpleNamespace(created=created, pending=pending, questions=questions, settled=settled)


# ---------- 聚类 ----------


def test_cluster_merges_similar_questions() -> None:
    questions = ["年假几天", "年假可以休几天", "年假能休多少天"]
    vectors = [[1.0, 0.0], [0.99, 0.05], [0.98, 0.1]]

    clusters = cluster_questions(questions, vectors, threshold=0.8)

    assert len(clusters) == 1
    assert sorted(clusters[0].member_indices) == [0, 1, 2]


def test_cluster_splits_dissimilar_questions() -> None:
    questions = ["年假几天", "报销要什么材料"]
    vectors = [[1.0, 0.0], [0.0, 1.0]]

    clusters = cluster_questions(questions, vectors, threshold=0.8)

    assert len(clusters) == 2
    assert {tuple(cluster.member_indices) for cluster in clusters} == {(0,), (1,)}


def test_cluster_representative_is_closest_to_centroid() -> None:
    """代表问句要最像簇心，而不是恰好第一条——输入顺序不该决定审核者看到什么。"""

    questions = ["年假可以休几天", "年假几天"]
    vectors = [[1.0, 0.0], [0.999, 0.001]]

    clusters = cluster_questions(questions, vectors, threshold=0.8)

    assert len(clusters) == 1
    assert clusters[0].representative == "年假可以休几天"


def test_cluster_handles_single_question() -> None:
    clusters = cluster_questions(["只有一条"], [[1.0, 0.0]], threshold=0.8)

    assert len(clusters) == 1
    assert clusters[0].member_indices == [0]
    assert clusters[0].representative == "只有一条"


def test_cluster_threshold_one_keeps_all_separate() -> None:
    """阈值 1.0 时只有完全相同的问题才合并，近似问法各自成簇。"""

    questions = ["甲", "乙"]
    vectors = [[1.0, 0.0], [0.99, 0.01]]

    assert len(cluster_questions(questions, vectors, threshold=1.0)) == 2


# ---------- 归并写候选 ----------


async def test_below_min_freq_writes_nothing(patches) -> None:
    patches.questions.extend(
        SimpleNamespace(question=text) for text in ["年假几天", "年假可以休几天"]
    )
    session = _Session()

    result = await run_mining(session)

    assert result == MiningResult(
        scanned=2, clusters=1, created=0, updated=0, skipped_settled=0, below_min_freq=1
    )
    assert patches.created == []
    assert session.committed is True


async def test_reaching_min_freq_creates_candidate(patches) -> None:
    patches.questions.extend(
        SimpleNamespace(question=text)
        for text in ["年假几天", "年假可以休几天", "年假能休多少天", "报销要什么材料"]
    )

    result = await run_mining(_Session())

    assert result.created == 1
    assert result.updated == 0
    assert result.clusters == 2
    assert result.below_min_freq == 1

    fields = patches.created[0]
    assert fields["freq"] == 3
    assert len(fields["similar_questions"]) == 2
    assert fields["representative_question"].startswith("年假")


async def test_second_run_updates_instead_of_duplicating(patches) -> None:
    """幂等：Beat 每天跑一次，同一簇必须更新已有候选而不是再建一条。"""

    patches.questions.extend(
        SimpleNamespace(question=text) for text in ["年假几天", "年假可以休几天", "年假能休多少天"]
    )

    await run_mining(_Session())
    assert len(patches.created) == 1

    result = await run_mining(_Session())

    assert result.created == 0
    assert result.updated == 1
    assert len(patches.created) == 1
    assert patches.pending[0].freq == 3


async def test_no_history_is_a_noop(patches) -> None:
    result = await run_mining(_Session())

    assert result == MiningResult(
        scanned=0, clusters=0, created=0, updated=0, skipped_settled=0, below_min_freq=0
    )


async def test_settled_questions_merges_faqs_and_closed_candidates(monkeypatch) -> None:
    """已定论集合 = FAQ 全体（含已下线）+ 已发布/已驳回候选，且按命中口径归一化。"""

    async def _all_faqs(_session):
        return [
            SimpleNamespace(question="年假可以休几天？"),
            SimpleNamespace(question="报销要什么材料"),
        ]

    async def _by_status(_session, statuses):
        assert set(statuses) == {
            faq_repo.CANDIDATE_PUBLISHED,
            faq_repo.CANDIDATE_REJECTED,
        }
        return [SimpleNamespace(representative_question="年假 可以休几天")]

    monkeypatch.setattr(faq_repo, "all_faqs", _all_faqs)
    monkeypatch.setattr(faq_repo, "candidates_by_status", _by_status)

    settled = await faq_mining._settled_questions(_Session())

    # 「年假可以休几天？」与「年假 可以休几天」归一化后是同一条，集合只该有 2 个元素
    assert len(settled) == 2
    assert faq_cache.normalize("年假可以休几天") in settled
    assert faq_cache.normalize("报销要什么材料") in settled


async def test_settled_question_is_not_reproposed(patches) -> None:
    """已发布 FAQ 的问句又被问到：不建候选（否则再发布一次就多一条同问句 FAQ）。"""

    patches.questions.extend(
        SimpleNamespace(question=text) for text in ["年假几天", "年假可以休几天", "年假能休多少天"]
    )
    # 代表问句是「离簇心最近」的那条，由假向量决定；三条都标成已定论，无论选哪条都得跳过
    patches.settled.extend(["年假几天", "年假可以休几天", "年假能休多少天"])

    result = await run_mining(_Session())

    assert result.created == 0
    assert result.updated == 0
    assert result.skipped_settled == 1
    assert patches.created == []


async def test_settled_dedup_uses_the_same_normalization_as_matching(patches) -> None:
    """判重用命中判定那套归一化：标点与空格不同也算同一条。"""

    patches.questions.extend(
        SimpleNamespace(question=text) for text in ["年假几天？", "年假几天", "年假几天！"]
    )
    patches.settled.append("年假 几天")

    result = await run_mining(_Session())

    assert result.skipped_settled == 1
    assert patches.created == []
