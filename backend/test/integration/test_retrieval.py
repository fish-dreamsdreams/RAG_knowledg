"""三路召回与父块回溯的集成测试（tasklist 9.4）。

需要 PostgreSQL 与 Milvus 就绪。向量手工构造（one-hot dense + 单 token sparse），
不加载 BGE-M3，让相似度完全可控：命中的余弦恰好是 1.0，不命中是 0.0。
"""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID, uuid4

import numpy as np
import pytest

from app.common.config import EMBED_DIM
from app.common.db import SessionLocal
from app.engines.embed import Embedding
from app.engines.retrieve import (
    ChunkVector,
    dense_similarity,
    delete_by_unit,
    ensure_collection,
    expand_parents,
    hybrid_search,
    hyde_search,
    upsert_chunks,
)
from app.engines.retrieve import search as search_engine
from app.models.knowledge import Chunk, KnowledgeUnit

pytestmark = pytest.mark.integration

PARENT_CHARS = 3000


class Retrieval(NamedTuple):
    unit_id: UUID
    parent_a: UUID
    parent_b: UUID
    child_a1: UUID
    child_a2: UUID
    child_b1: UUID


def _vector(seed: int) -> list[float]:
    vec = [0.0] * EMBED_DIM
    vec[seed % EMBED_DIM] = 1.0
    return vec


def _embedding(seed: int) -> Embedding:
    return Embedding(
        dense=np.array([_vector(seed)], dtype=np.float32),
        sparse=[{seed % EMBED_DIM: 1.0}],
    )


@pytest.fixture
async def retrieval():
    """一个单元、两个父块、三个子块：A 系启用，B 系停用。"""

    ensure_collection()
    unit_id = uuid4()

    async with SessionLocal() as session:
        session.add(
            KnowledgeUnit(
                id=unit_id,
                title=f"召回测试 {unit_id.hex[:6]}",
                format="md",
                source_path="test/召回.md",
                source_filename="召回.md",
                file_size=16,
                status="enabled",
                parse_status="indexed",
            )
        )
        parent_a = Chunk(
            unit_id=unit_id,
            level="parent",
            ordinal=0,
            content="甲" * PARENT_CHARS,
            char_count=PARENT_CHARS,
        )
        parent_b = Chunk(
            unit_id=unit_id,
            level="parent",
            ordinal=1,
            content="乙" * PARENT_CHARS,
            char_count=PARENT_CHARS,
        )
        session.add_all([parent_a, parent_b])
        await session.flush()
        child_a1 = Chunk(
            unit_id=unit_id,
            parent_id=parent_a.id,
            level="child",
            ordinal=0,
            content="甲一",
            char_count=2,
        )
        child_a2 = Chunk(
            unit_id=unit_id,
            parent_id=parent_a.id,
            level="child",
            ordinal=1,
            content="甲二",
            char_count=2,
        )
        child_b1 = Chunk(
            unit_id=unit_id,
            parent_id=parent_b.id,
            level="child",
            ordinal=0,
            content="乙一",
            char_count=2,
        )
        session.add_all([child_a1, child_a2, child_b1])
        await session.commit()

        record = Retrieval(
            unit_id=unit_id,
            parent_a=parent_a.id,
            parent_b=parent_b.id,
            child_a1=child_a1.id,
            child_a2=child_a2.id,
            child_b1=child_b1.id,
        )

    upsert_chunks(
        [
            ChunkVector(
                str(record.child_a1),
                str(unit_id),
                str(record.parent_a),
                True,
                _vector(11),
                {11: 1.0},
            ),
            ChunkVector(
                str(record.child_a2),
                str(unit_id),
                str(record.parent_a),
                True,
                _vector(12),
                {12: 1.0},
            ),
            ChunkVector(
                str(record.child_b1),
                str(unit_id),
                str(record.parent_b),
                False,
                _vector(13),
                {13: 1.0},
            ),
        ]
    )

    yield record

    delete_by_unit(str(unit_id))
    async with SessionLocal() as session:
        unit = await session.get(KnowledgeUnit, unit_id)
        if unit is not None:
            await session.delete(unit)
            await session.commit()


async def test_disabled_child_is_not_recalled(retrieval: Retrieval) -> None:
    """两路都必须过滤 enabled=false（tasklist 9.4、P9）。"""

    query = _embedding(11)

    hybrid_ids = {item.chunk_id for item in hybrid_search(query)}
    hyde_ids = {item.chunk_id for item in hyde_search(query)}

    assert str(retrieval.child_a1) in hybrid_ids
    assert str(retrieval.child_b1) not in hybrid_ids, "hybrid 路的 dense 子请求漏了过滤"
    assert str(retrieval.child_b1) not in hyde_ids, "HyDE 路漏了过滤"


async def test_only_disabled_match_yields_nothing(retrieval: Retrieval) -> None:
    """让停用切片与查询完全相同：三路都必须视而不见，而不是照样返回满分。

    库里可能还有其它测试或演示数据，所以断言「结果不含该切片」与「相似度很低」，
    而不是断言结果集为空。
    """

    query = _embedding(13)

    assert str(retrieval.child_b1) not in {item.chunk_id for item in hybrid_search(query)}
    assert str(retrieval.child_b1) not in {item.chunk_id for item in hyde_search(query)}
    assert dense_similarity(query) < 0.5, "完全匹配的停用切片不得贡献相似度"


async def test_hyde_route_does_not_change_max_similarity(
    retrieval: Retrieval, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P8：HyDE 路即便命中满相似度，也不得抬高 `max_similarity`。"""

    def fake_encode(text: str) -> Embedding:
        # 问句命中 A1（余弦 1.0），假设答案命中 A2（余弦 1.0）
        return _embedding(11 if text == "问句" else 12)

    monkeypatch.setattr(search_engine, "encode_question", fake_encode)

    base = search_engine.recall("问句")
    with_hyde = search_engine.recall("问句", hyde="假设答案")

    assert base.max_similarity == pytest.approx(1.0)
    assert with_hyde.max_similarity == base.max_similarity, (
        "max_similarity 只能来自问句 dense 路"
    )
    # HyDE 路确实参与了融合，只是不参与相似度
    assert str(retrieval.child_a2) in {item.chunk_id for item in with_hyde.chunks}


async def test_hyde_route_can_be_skipped(retrieval: Retrieval, monkeypatch) -> None:
    """`hyde` 为空或 HYDE_ENABLED=false 时跳过第②路，不报错。"""

    monkeypatch.setattr(search_engine, "encode_question", lambda text: _embedding(11))

    without_hyde = search_engine.recall("问句")
    disabled = search_engine.recall("问句", hyde=None)

    assert disabled.chunks == without_hyde.chunks

    monkeypatch.setattr(search_engine.settings, "hyde_enabled", False)
    switched_off = search_engine.recall("问句", hyde="假设答案")
    assert switched_off.chunks == without_hyde.chunks


async def test_expand_parents_dedupes_and_truncates(retrieval: Retrieval) -> None:
    """同一父块的多个命中子块只取一次正文；总量按父块顺序截断（TECH_SPEC §8.1）。"""

    async with SessionLocal() as session:
        contexts = await expand_parents(
            session,
            [retrieval.child_a1, retrieval.child_a2, retrieval.child_b1],
        )

    assert [item.parent_id for item in contexts] == [
        retrieval.parent_a,
        retrieval.parent_b,
    ]
    assert contexts[0].child_ids == [retrieval.child_a1, retrieval.child_a2]
    assert len(contexts[0].content) == PARENT_CHARS

    async with SessionLocal() as session:
        truncated = await expand_parents(
            session, [retrieval.child_a1, retrieval.child_b1], max_chars=100
        )

    assert len(truncated) == 1, "第一个父块已吃满上限，后面的父块不该再进上下文"
    assert len(truncated[0].content) == 100


async def test_expand_parents_ignores_missing_parent(retrieval: Retrieval) -> None:
    """父块被删时跳过该段，不让上下文里出现空内容。"""

    async with SessionLocal() as session:
        parent = await session.get(Chunk, retrieval.parent_b)
        assert parent is not None
        await session.delete(parent)
        await session.commit()

    async with SessionLocal() as session:
        contexts = await expand_parents(session, [retrieval.child_b1])

    assert contexts == []
