"""Milvus collection 维护测试：写入、启停可见性（P9）与删除。

需要运行中的 Milvus（`docker compose up -d`）。用随机 unit_id，不触碰演示数据。
"""

from uuid import uuid4

import numpy as np
import pytest

# pymilvus 3.0 未在顶层导出该枚举（客户端内部从 client.types 引入）
from pymilvus.client.types import ConsistencyLevel

from app.common.config import EMBED_DIM
from app.engines.retrieve import milvus_store
from app.engines.retrieve.milvus_store import COLLECTION_NAME, ChunkVector

pytestmark = pytest.mark.integration


def unit_vector(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return (vector / np.linalg.norm(vector)).tolist()


def make_chunks(unit_id: str, count: int = 3) -> list[ChunkVector]:
    return [
        ChunkVector(
            chunk_id=str(uuid4()),
            unit_id=unit_id,
            parent_id=str(uuid4()),
            enabled=True,
            dense=unit_vector(index),
            sparse={1903 + index: 0.1 + index / 100},
        )
        for index in range(count)
    ]


def enabled_chunk_ids(client, unit_id: str, query: list[float]) -> set[str]:
    """按 `enabled == true` 检索，返回该单元的命中切片 id。"""

    hits = client.search(
        COLLECTION_NAME,
        data=[query],
        anns_field="dense",
        limit=20,
        filter=f'enabled == true and unit_id == "{unit_id}"',
        search_params={"metric_type": "COSINE", "params": {"ef": 64}},
        output_fields=["chunk_id"],
    )
    return {hit["id"] for hit in hits[0]}


def test_collection_has_expected_schema() -> None:
    milvus_store.ensure_collection()
    client = milvus_store.get_client()

    assert client.has_collection(COLLECTION_NAME)
    description = client.describe_collection(COLLECTION_NAME)
    fields = {field["name"]: field for field in description["fields"]}

    assert fields["chunk_id"]["is_primary"] is True
    assert fields["dense"]["params"]["dim"] == EMBED_DIM
    assert fields["sparse"]["type"].name == "SPARSE_FLOAT_VECTOR"
    # 停用/删除要立刻对检索可见；describe 返回的是枚举值（Strong=0）而非字符串
    assert description["consistency_level"] == ConsistencyLevel.Strong


def test_ensure_collection_is_idempotent() -> None:
    milvus_store.ensure_collection()
    milvus_store.ensure_collection()

    assert milvus_store.get_client().has_collection(COLLECTION_NAME)


def test_upsert_and_delete_by_unit_roundtrip() -> None:
    unit_id = str(uuid4())
    chunks = make_chunks(unit_id, count=3)

    try:
        assert milvus_store.upsert_chunks(chunks) == 3
        assert milvus_store.count_unit_chunks(unit_id) == 3

        # 同 id 重复写入不应产生重复实体
        milvus_store.upsert_chunks(chunks[:1])
        assert milvus_store.count_unit_chunks(unit_id) == 3

        assert milvus_store.delete_by_unit(unit_id) == 3
        assert milvus_store.count_unit_chunks(unit_id) == 0
        # 幂等：重复删除不报错
        assert milvus_store.delete_by_unit(unit_id) == 0
    finally:
        milvus_store.delete_by_unit(unit_id)


def test_upsert_chunks_accepts_empty_list() -> None:
    assert milvus_store.upsert_chunks([]) == 0


def test_upsert_rejects_wrong_dense_dimension() -> None:
    unit_id = str(uuid4())
    bad = ChunkVector(
        chunk_id=str(uuid4()),
        unit_id=unit_id,
        parent_id=str(uuid4()),
        enabled=True,
        dense=[0.1] * 768,
        sparse={},
    )

    with pytest.raises(ValueError, match="1024"):
        milvus_store.upsert_chunks([bad])


def test_disable_hides_chunks_from_enabled_search() -> None:
    """P9：停用后带 `enabled == true` 的检索不得再返回该单元切片。"""

    unit_id = str(uuid4())
    chunks = make_chunks(unit_id, count=2)
    client = milvus_store.get_client()

    try:
        milvus_store.upsert_chunks(chunks)
        assert enabled_chunk_ids(client, unit_id, chunks[0].dense) == {
            chunk.chunk_id for chunk in chunks
        }

        assert milvus_store.set_unit_enabled(unit_id, False) == 2
        assert enabled_chunk_ids(client, unit_id, chunks[0].dense) == set()

        # 切片仍在库里，只是被 enabled 过滤掉（停用不等于删除）
        assert milvus_store.count_unit_chunks(unit_id) == 2

        assert milvus_store.set_unit_enabled(unit_id, True) == 2
        assert enabled_chunk_ids(client, unit_id, chunks[0].dense) == {
            chunk.chunk_id for chunk in chunks
        }
    finally:
        milvus_store.delete_by_unit(unit_id)


def test_set_unit_enabled_on_missing_unit_returns_zero() -> None:
    assert milvus_store.set_unit_enabled(str(uuid4()), False) == 0


def test_delete_by_unit_leaves_other_units_untouched() -> None:
    keep_unit, drop_unit = str(uuid4()), str(uuid4())
    keep_chunks = make_chunks(keep_unit, count=2)

    try:
        milvus_store.upsert_chunks(keep_chunks)
        milvus_store.upsert_chunks(make_chunks(drop_unit, count=2))

        milvus_store.delete_by_unit(drop_unit)

        assert milvus_store.count_unit_chunks(drop_unit) == 0
        assert milvus_store.count_unit_chunks(keep_unit) == 2
    finally:
        milvus_store.delete_by_unit(keep_unit)
        milvus_store.delete_by_unit(drop_unit)
