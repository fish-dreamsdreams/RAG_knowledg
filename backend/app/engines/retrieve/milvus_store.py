"""`kb_child_chunks` collection：初始化、写入、启停与删除（tasklist 5.3 / 5.4）。

设计要点（均在本机 v2.4.4 服务端实测确认）：

- **稠密 + 稀疏同一 collection**：dense FLOAT_VECTOR(1024, COSINE, HNSW)，
  sparse SPARSE_FLOAT_VECTOR(IP)，与子块同批次写入（TECH_SPEC §1）。
- **一致性用 Strong**：默认 Bounded 下，改完 `enabled` 后检索仍可能返回已停用切片
  （实测命中）。停用必须立刻生效（不变量 P9），演示规模下 Strong 的代价可以接受。
- **不支持部分更新**：只传 `chunk_id` + `enabled` 会被服务端拒绝（缺非空字段），
  因此翻转 `enabled` 需要把整行读回来（含向量）再回写。向量可回读，无需重新编码。
- **依赖不可用一律 503**：按 TECH_SPEC §6，Milvus 故障不得降级为「不过滤」，直接抛
  `SYS_DEPENDENCY_UNAVAILABLE`。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import NamedTuple

from pymilvus import DataType, MilvusClient
from pymilvus.exceptions import MilvusException

from app.common.config import EMBED_DIM, settings
from app.common.errors import AppError

logger = logging.getLogger(__name__)

COLLECTION_NAME = "kb_child_chunks"
# 单批写入行数：太大容易触发 gRPC 消息上限
UPSERT_BATCH_SIZE = 200
# 翻转 enabled 时回读的字段（稀疏/稠密向量必须一起回读，否则回写会丢向量）
READ_BACK_FIELDS = ["chunk_id", "parent_id", "enabled", "dense", "sparse"]

_client: MilvusClient | None = None
# 两个锁职责不同且**不可重入**：建客户端 与 建 collection 各用一把。
# 合并成一把会自死锁（ensure_collection 持锁时又调 get_client）。
_client_lock = threading.Lock()
_collection_lock = threading.Lock()
_collection_ready = False
# 客户端级调用超时：Milvus 卡死时必须抛错转 503，而不是把请求线程挂死
MILVUS_TIMEOUT_SECONDS = 10


class ChunkVector(NamedTuple):
    """一条子块实体：PG `chunks` 行 + 其 dense/sparse 向量。"""

    chunk_id: str
    unit_id: str
    parent_id: str
    enabled: bool
    dense: list[float]
    sparse: dict[int, float]


@contextmanager
def milvus_call(action: str) -> Iterator[None]:
    """把 Milvus 故障统一转成 503，并记 exception 级日志（TECH_SPEC §6）。"""

    try:
        yield
    except (MilvusException, OSError) as exc:
        logger.exception("Milvus 调用失败：%s", action)
        raise AppError.dependency_unavailable() from exc


def _literal(value: str) -> str:
    """过滤表达式里的字符串字面量，拒绝引号/反斜杠以免拼接出意外条件。"""

    if '"' in value or "\\" in value:
        raise AppError.validation("资源标识不合法")
    return f'"{value}"'


def _batched(rows: Sequence[dict], size: int) -> Iterator[Sequence[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def get_client() -> MilvusClient:
    """进程内 MilvusClient 单例。

    建连也要走 `milvus_call`：Milvus 挂掉时失败就发生在构造这一步（`Fail connecting to
    server`），漏在外面就会以原始异常冲到 WS 层被兜成 500，而 P13 要求依赖不可用必须是 503。
    失败时单例保持为 None，下一次调用会重新尝试建连。
    """

    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                with milvus_call("connect"):
                    _client = MilvusClient(
                        uri=settings.milvus_uri, timeout=MILVUS_TIMEOUT_SECONDS
                    )
    return _client


def ensure_collection() -> None:
    """建表 + 建索引（幂等）。dense 与 sparse 两个向量字段同一批次写入。"""

    global _collection_ready
    if _collection_ready:
        return

    with _collection_lock:
        if _collection_ready:
            return
        client = get_client()
        with milvus_call("describe_collection"):
            exists = client.has_collection(COLLECTION_NAME)
        if not exists:
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("unit_id", DataType.VARCHAR, max_length=64)
            schema.add_field("parent_id", DataType.VARCHAR, max_length=64)
            schema.add_field("enabled", DataType.BOOL)
            schema.add_field("dense", DataType.FLOAT_VECTOR, dim=EMBED_DIM)
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)

            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="dense",
                index_type="HNSW",
                metric_type="COSINE",
                params={"M": 16, "efConstruction": 200},
            )
            index_params.add_index(
                field_name="sparse",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
            )
            with milvus_call("create_collection"):
                client.create_collection(
                    COLLECTION_NAME,
                    schema=schema,
                    index_params=index_params,
                    # 停用/删除必须立刻对检索可见（P9）
                    consistency_level="Strong",
                )
            logger.info("已创建 Milvus collection：%s", COLLECTION_NAME)
        _collection_ready = True


def _to_payload(row: ChunkVector, *, enabled: bool | None = None) -> dict:
    if len(row.dense) != EMBED_DIM:
        raise ValueError(f"dense 维度必须为 {EMBED_DIM}，实际 {len(row.dense)}")
    return {
        "chunk_id": row.chunk_id,
        "unit_id": row.unit_id,
        "parent_id": row.parent_id,
        "enabled": row.enabled if enabled is None else enabled,
        "dense": list(row.dense),
        "sparse": dict(row.sparse),
    }


def upsert_chunks(rows: Sequence[ChunkVector]) -> int:
    """写入（或覆盖）子块实体，dense 与 sparse 同一批次。返回写入行数。"""

    if not rows:
        return 0

    ensure_collection()
    client = get_client()
    payload = [_to_payload(row) for row in rows]
    for batch in _batched(payload, UPSERT_BATCH_SIZE):
        with milvus_call("upsert"):
            client.upsert(collection_name=COLLECTION_NAME, data=list(batch))
    return len(payload)


def delete_by_unit(unit_id: str) -> int:
    """按 unit_id 删除该单元的全部切片实体（幂等）。返回删除前的行数。"""

    ensure_collection()
    client = get_client()
    with milvus_call("query unit rows"):
        rows = client.query(
            COLLECTION_NAME,
            filter=f"unit_id == {_literal(unit_id)}",
            output_fields=["chunk_id"],
        )
    if not rows:
        return 0
    with milvus_call("delete by unit"):
        client.delete(collection_name=COLLECTION_NAME, filter=f"unit_id == {_literal(unit_id)}")
    return len(rows)


def set_unit_enabled(unit_id: str, enabled: bool) -> int:
    """把该单元的全部切片 `enabled` 置为指定值（Milvus 无部分更新，需整行回写）。

    向量从 Milvus 回读后原样写回，因此不需要重新编码。
    """

    ensure_collection()
    client = get_client()
    with milvus_call("query unit rows"):
        rows = client.query(
            COLLECTION_NAME,
            filter=f"unit_id == {_literal(unit_id)}",
            output_fields=READ_BACK_FIELDS,
        )
    if not rows:
        return 0

    payload = [
        {
            "chunk_id": row["chunk_id"],
            "unit_id": unit_id,
            "parent_id": row["parent_id"],
            "enabled": enabled,
            "dense": list(row["dense"]),
            "sparse": dict(row["sparse"]),
        }
        for row in rows
    ]
    for batch in _batched(payload, UPSERT_BATCH_SIZE):
        with milvus_call("upsert enabled flag"):
            client.upsert(collection_name=COLLECTION_NAME, data=list(batch))
    return len(payload)


def count_unit_chunks(unit_id: str) -> int:
    """该单元在 Milvus 中的切片数（用于导入校验与演示核对）。"""

    ensure_collection()
    client = get_client()
    with milvus_call("count unit rows"):
        rows = client.query(
            COLLECTION_NAME,
            filter=f"unit_id == {_literal(unit_id)}",
            output_fields=["count(*)"],
        )
    return int(rows[0]["count(*)"]) if rows else 0
