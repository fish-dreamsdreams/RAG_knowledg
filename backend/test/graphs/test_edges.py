"""条件边测试（tasklist 12.1）。

纯函数，直接构造 State 断言分支去向。这里锁住的是「哪条路绝不走」——比"走了哪条路"更
值得测，因为走错分支的后果（FAQ 命中还去检索、无权也调生成）都是不可接受的。
"""

from __future__ import annotations

from app.engines.retrieve import RecalledChunk
from app.graphs.edges import (
    EXPAND_PARENT,
    RESPOND_DENIED,
    RESPOND_FAQ,
    RESPOND_GAP,
    REWRITE,
    after_acl,
    after_faq,
)

GAP_THRESHOLD = 0.62


def _chunk(chunk_id: str, unit_id: str) -> RecalledChunk:
    return RecalledChunk(
        chunk_id=chunk_id, unit_id=unit_id, parent_id=f"p-{chunk_id}", score=0.9
    )


# ---------- after_faq ----------


def test_faq_hit_short_circuits_to_respond_faq() -> None:
    """命中 FAQ 必须直接出口，绝不能落进检索链（P4）。"""

    assert after_faq({"faq_hit": True, "faq_answer": "满 1 年 5 天"}) == RESPOND_FAQ


def test_faq_miss_goes_to_rewrite() -> None:
    assert after_faq({"faq_hit": False}) == REWRITE


def test_missing_faq_flag_is_treated_as_miss() -> None:
    """State 缺字段时按未命中处理：宁可多跑一次检索，也不能误走 FAQ 出口。"""

    assert after_faq({}) == REWRITE


# ---------- after_acl ----------


def test_allowed_goes_to_expand_parent() -> None:
    state = {
        "allowed": [_chunk("c1", "u1")],
        "denied": [],
        "max_similarity": 0.9,
        "gap_threshold": GAP_THRESHOLD,
    }

    assert after_acl(state) == EXPAND_PARENT


def test_allowed_with_denied_and_relevant_similarity_still_expands() -> None:
    """有权内容的相关度达标就照常作答（部分资料无权另行提示，不改出口）。"""

    state = {
        "allowed": [_chunk("c1", "u1")],
        "denied": ["u9"],
        "max_similarity": 0.63,
        "gap_threshold": GAP_THRESHOLD,
    }

    assert after_acl(state) == EXPAND_PARENT


def test_denied_only_goes_to_respond_denied() -> None:
    state = {
        "allowed": [],
        "denied": ["u9"],
        "max_similarity": 0.9,
        "gap_threshold": GAP_THRESHOLD,
    }

    assert after_acl(state) == RESPOND_DENIED


def test_nothing_recalled_goes_to_respond_gap() -> None:
    state = {
        "allowed": [],
        "denied": [],
        "max_similarity": 0.0,
        "gap_threshold": GAP_THRESHOLD,
    }

    assert after_acl(state) == RESPOND_GAP


def test_low_similarity_goes_to_respond_gap() -> None:
    """有召回但最高相似度低于缺口阈值，说明命中的都是无关内容。"""

    state = {
        "allowed": [],
        "denied": [],
        "max_similarity": 0.2,
        "gap_threshold": GAP_THRESHOLD,
    }

    assert after_acl(state) == RESPOND_GAP


def test_denied_wins_over_gap() -> None:
    """「检索到了但无权」与「没有这方面知识」是两件事，不能混为一谈。

    相似度低也不能把权限问题说成知识缺口——那会把有文档却无权的内容谎称为不存在。
    """

    state = {
        "allowed": [],
        "denied": ["u9"],
        "max_similarity": 0.1,
        "gap_threshold": GAP_THRESHOLD,
    }

    assert after_acl(state) == RESPOND_DENIED


def test_low_similarity_goes_to_gap_even_with_allowed_hits() -> None:
    """向量召回永远返回 top-k，只有全局可读文档时 `allowed` 恒非空。

    若因此就直接作答，缺口出口等于不可达（`knowledge_gaps` 在生产里不会增长）。
    命中的都是无关内容时，宁可说「没有这方面的知识」，也不拿它编答案。
    """

    state = {
        "allowed": [_chunk("c1", "u1")],
        "denied": [],
        "max_similarity": 0.42,
        "gap_threshold": GAP_THRESHOLD,
    }

    assert after_acl(state) == RESPOND_GAP


def test_low_similarity_with_mixed_hits_goes_to_gap() -> None:
    """允许与拒绝同时在、但都不相关时仍是缺口：这一轮并不存在「相关但无权」的内容。"""

    state = {
        "allowed": [_chunk("c1", "u1")],
        "denied": ["u9"],
        "max_similarity": 0.51,
        "gap_threshold": GAP_THRESHOLD,
    }

    assert after_acl(state) == RESPOND_GAP


def test_missing_threshold_falls_back_to_zero() -> None:
    """阈值缺失时不应该把一切判成缺口，只按"有没有内容"分流。"""

    state = {"allowed": [], "denied": [], "max_similarity": 0.5}

    assert after_acl(state) == RESPOND_GAP
