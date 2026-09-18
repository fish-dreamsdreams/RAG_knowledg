"""清掉演示库里的历史遗留知识数据（测试残留单元 + 孤儿切片 + 陈旧向量）。

为什么需要这个脚本：集成测试按 design.md §2 直接连开发库建行、teardown 删行
（见 `test/api/test_faq.py`）。跑挂或中途打断就会留下半截数据——单元还在 PG、切片
`parent_id` 为空、向量还留在 Milvus。这种数据**不报错**，只会让问答静默落进缺口出口
（看起来像「知识库没这方面内容」）。真机排查过一次，所以把清场固化成脚本，别靠手删。

删除动作复用生产路径 `services.knowledge.delete_unit`：Milvus 实体 → PG 行（外键级联切片）
→ 对象存储前缀 → 本地原件。不另写一套 SQL，避免清得和生产删除不一致。

用法：

    backend> .venv\\Scripts\\python.exe scripts\\clean_legacy.py            # 只报告
    backend> .venv\\Scripts\\python.exe scripts\\clean_legacy.py --apply    # 执行删除
    backend> .venv\\Scripts\\python.exe scripts\\clean_legacy.py --apply --orphan-vectors-only

`--orphan-vectors-only` 只删「PG 已无对应单元」的 Milvus 向量，**不动 PG 单元**。默认路径把
PG 里**全部**单元当遗留（适合「库是干净的、只剐了半截数据」），而演示库里那 6 个单元是在用的
语料，拿它整体清会把语料删光——要清孤儿向量就用这个开关。

`--apply` 前会把待删内容导出到 `var/legacy-units-<时间戳>.json`，便于回溯。
Milvus 不可用时直接失败退出：向量没清掉就等于没清干净。

为什么要单独清孤儿向量（2026-09-16 实测）：检索只带 `enabled == true` 一个过滤条件，ACL 是
召回之后再过，所以孤儿向量会占满 top-k（实测集合 580 条里 570 条是孤儿，活单元的切片一条都
进不了 top-20）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

# 让 `python scripts/clean_legacy.py` 也能导入 app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.common.db import SessionLocal  # noqa: E402
from app.engines.retrieve import milvus_store  # noqa: E402
from app.models import (  # noqa: E402
    Chunk,
    IngestTask,
    KnowledgeUnit,
    KnowledgeUnitAcl,
    KnowledgeUnitAsset,
)
from app.services import knowledge  # noqa: E402

VAR_DIR = Path(__file__).resolve().parent.parent / "var"
MILVUS_SCAN_LIMIT = 10000
# 待删单元关联的行（按 unit_id 删这些表；chunks 由外键级联，一并导出只为回溯）
SCOPED_MODELS = (Chunk, KnowledgeUnitAcl, KnowledgeUnitAsset, IngestTask)


def _milvus_units() -> dict[str, int]:
    """Milvus 里每个 unit_id 的实体数。collection 不存在时返回空字典。"""

    client = milvus_store.get_client()
    with milvus_store.milvus_call("has_collection"):
        exists = client.has_collection(milvus_store.COLLECTION_NAME)
    if not exists:
        return {}
    with milvus_store.milvus_call("scan unit ids"):
        rows = client.query(
            milvus_store.COLLECTION_NAME,
            filter="chunk_id != ''",
            output_fields=["unit_id"],
            limit=MILVUS_SCAN_LIMIT,
        )
    if len(rows) >= MILVUS_SCAN_LIMIT:
        raise SystemExit(f"Milvus 实体数触到扫描上限 {MILVUS_SCAN_LIMIT}，请抬高上限后再跑")

    counts: dict[str, int] = {}
    for row in rows:
        unit_id = str(row["unit_id"])
        counts[unit_id] = counts.get(unit_id, 0) + 1
    return counts


async def _pg_inventory(session) -> dict[str, Any]:
    units = (await session.execute(select(KnowledgeUnit))).scalars().all()
    unit_ids = [unit.id for unit in units]

    async def count(model, *where) -> int:
        return (
            await session.execute(select(func.count()).select_from(model).where(*where))
        ).scalar_one()

    scoped = {
        "chunks": 0,
        "acl": 0,
        "assets": 0,
        "tasks": 0,
    }
    if unit_ids:
        scoped["chunks"] = await count(Chunk, Chunk.unit_id.in_(unit_ids))
        scoped["acl"] = await count(KnowledgeUnitAcl, KnowledgeUnitAcl.unit_id.in_(unit_ids))
        scoped["assets"] = await count(
            KnowledgeUnitAsset, KnowledgeUnitAsset.unit_id.in_(unit_ids)
        )
        scoped["tasks"] = await count(IngestTask, IngestTask.unit_id.in_(unit_ids))

    return {
        "units": [
            {
                "id": str(unit.id),
                "title": unit.title,
                "parse_status": unit.parse_status,
                "source_path": unit.source_path,
            }
            for unit in units
        ],
        "chunks_total": await count(Chunk),
        "orphan_children": await count(
            Chunk, Chunk.level == "child", Chunk.parent_id.is_(None)
        ),
        "scoped": scoped,
    }


def _row_to_dict(row) -> dict:
    """ORM 行 → 可 JSON 化的字典（UUID/时间转字符串，正文原样保留）。"""

    data: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            value = str(value)
        data[column.name] = value
    return data


async def _export(session, units: list[dict], milvus_counts: dict[str, int]) -> Path:
    """导出待删单元行与其切片/ACL/资产/任务行，返回落盘路径。"""

    unit_ids = [UUID(unit["id"]) for unit in units]
    rows: list[dict] = []
    for model in SCOPED_MODELS:
        if not unit_ids:
            break
        found = (
            await session.execute(select(model).where(model.unit_id.in_(unit_ids)))
        ).scalars().all()
        rows.extend(_row_to_dict(row) for row in found)

    VAR_DIR.mkdir(parents=True, exist_ok=True)
    path = VAR_DIR / f"legacy-units-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    path.write_text(
        json.dumps(
            {
                "exported_at": datetime.now(UTC).isoformat(),
                "milvus_entities": milvus_counts,
                "units": units,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description="清理演示库遗留知识数据")
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认只报告）")
    parser.add_argument(
        "--orphan-vectors-only",
        action="store_true",
        help="只删 PG 已无对应单元的 Milvus 向量，不动 PG 单元（演示库有在用语料时用这个）",
    )
    args = parser.parse_args()

    milvus_counts = _milvus_units()

    async with SessionLocal() as session:
        inventory = await _pg_inventory(session)
        units = inventory["units"]
        scoped = inventory["scoped"]
        pg_ids = {unit["id"] for unit in units}
        orphan_vectors = {
            unit_id: total
            for unit_id, total in milvus_counts.items()
            if unit_id not in pg_ids
        }

        print(
            f"PG 知识单元 {len(units)} 个；切片共 {inventory['chunks_total']} 行"
            f"（其中孤儿 child {inventory['orphan_children']} 行）"
        )
        print(
            f"  待删单元名下：切片 {scoped['chunks']} / ACL {scoped['acl']} / "
            f"资产 {scoped['assets']} / 导入任务 {scoped['tasks']} 行"
        )
        for unit in units:
            print(f"    - {unit['id'][:8]}  {unit['title']}  [{unit['parse_status']}]")

        print(
            f"Milvus 实体 {sum(milvus_counts.values())} 条，"
            f"覆盖 {len(milvus_counts)} 个 unit_id"
        )
        if orphan_vectors:
            print(f"  其中 {len(orphan_vectors)} 个 unit_id 已无对应 PG 单元（孤儿向量）：")
            for unit_id, total in sorted(orphan_vectors.items()):
                print(f"    - {unit_id[:8]}  {total} 条")

        if not args.apply:
            print("\n这是报告模式，未改动数据。确认无误后加 --apply 执行。")
            return 0

        if args.orphan_vectors_only:
            # 只清向量：PG 单元与它们的切片、ACL、资产一律不动，导出也不带 PG 行
            if not orphan_vectors:
                print("\n没有孤儿向量，无需处理。")
                return 0
            backup = await _export(session, [], milvus_counts)
            print(f"\n已导出待删内容：{backup.name}")
            for unit_id in orphan_vectors:
                removed = await asyncio.to_thread(milvus_store.delete_by_unit, unit_id)
                print(f"已删除孤儿向量 {unit_id[:8]} 共 {removed} 条")
            after = await _pg_inventory(session)
        else:
            backup = await _export(session, units, milvus_counts)
            print(f"\n已导出待删内容：{backup.name}")

            # 1) 有 PG 单元的：走生产删除路径（Milvus → PG 级联 → 对象存储 → 本地原件）
            for unit in units:
                await knowledge.delete_unit(session, UUID(unit["id"]))
            print(f"已删除知识单元 {len(units)} 个")

            # 2) 孤儿向量：PG 已无对应单元，只能按 unit_id 直删 Milvus 实体
            for unit_id in orphan_vectors:
                removed = await asyncio.to_thread(milvus_store.delete_by_unit, unit_id)
                print(f"已删除孤儿向量 {unit_id[:8]} 共 {removed} 条")

            after = await _pg_inventory(session)

    remaining_vectors = _milvus_units()
    # 两种模式的「干净」都是「不再有孤儿向量」：默认模式还要 PG 单元清空
    after_ids = {unit["id"] for unit in after["units"]}
    leftover_orphans = {
        unit_id for unit_id in remaining_vectors if unit_id not in after_ids
    }
    print(
        f"\n复核：PG 单元 {len(after['units'])} 个 / 切片 {after['chunks_total']} 行 / "
        f"孤儿 child {after['orphan_children']} 行；"
        f"Milvus 实体 {sum(remaining_vectors.values())} 条（孤儿 {len(leftover_orphans)} 个 unit_id）"
    )
    clean = not leftover_orphans and (args.orphan_vectors_only or not after["units"])
    print("清理完成" if clean else "仍有残留，见上面的复核数字")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
