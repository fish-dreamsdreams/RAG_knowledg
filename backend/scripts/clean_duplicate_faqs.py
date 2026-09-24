"""清掉演示库里问句重复的 FAQ 行（同一问句多行，多为重复跑「挖掘 → 发布」留下的）。

为什么需要这个脚本：挖掘的判重原先只比对**待审候选**，已发布的问句会被日志重新推成候选，
审核者再发布一次就得到两条同问句的 FAQ——演示库真长出了 5 行 / 4 个问句。挖掘侧已补上
「已定论跳过」（`services/faq_mining.py`，tasklist 11.4），本脚本负责清理已经长出来的重复行。

分组与删除都复用 `services.faq.find_duplicate_faqs` / `delete_faqs`，与演示库清场工具
（`tool/clean_demo_state.py`）共用一处实现——两份分组逻辑迟早会在「删哪条」上悄悄分叉。
删除本身走 `services.faq.delete_faq`：删行 + 清缓存，库与缓存不会各说各话。

用法：

    backend> .venv\\Scripts\\python.exe scripts\\clean_duplicate_faqs.py            # 只报告
    backend> .venv\\Scripts\\python.exe scripts\\clean_duplicate_faqs.py --apply    # 执行删除

保留口径：**每条问句留 `created_at` 最早的那行**（原始那条），其余删除；判重按 `faq_cache.normalize`
的归一化文本，与 FAQ 命中判定同一套口径（「年假几天？」与「年假 几天」算同一条）。
`--apply` 前会把待删行导出到 `var/duplicate-faqs-<时间戳>.json`，便于回溯。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# 让 `python scripts/clean_duplicate_faqs.py` 也能导入 app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.common.db import SessionLocal  # noqa: E402
from app.common.redis import get_redis  # noqa: E402
from app.engines import faq_cache  # noqa: E402
from app.repositories import faq as faq_repo  # noqa: E402
from app.services import faq as faq_service  # noqa: E402

VAR_DIR = Path(__file__).resolve().parent.parent / "var"


async def _cache_state(deleted_ids: list[str], published_total: int) -> bool:
    """复核缓存：条目数对得上已发布行数，且被删的 id 在缓存里连影子都不剩。"""

    redis = get_redis()
    entries = int(await redis.hlen(faq_cache.PUBLISHED_KEY))
    leftover = [
        faq_id
        for faq_id in deleted_ids
        if await redis.hexists(faq_cache.PUBLISHED_KEY, faq_id)
        or await redis.exists(f"kb:faq:emb:{faq_id}")
    ]
    print(
        f"缓存复核：Hash 条目 {entries} 条（已发布行 {published_total} 条）；"
        f"被删 id 残留 {len(leftover)} 个"
    )
    for faq_id in leftover:
        print(f"  残留：{faq_id}")
    return entries == published_total and not leftover


async def main() -> int:
    parser = argparse.ArgumentParser(description="清理问句重复的 FAQ 行")
    parser.add_argument("--apply", action="store_true", help="真的删除（默认只报告）")
    args = parser.parse_args()

    async with SessionLocal() as session:
        total_before = len(await faq_repo.all_faqs(session))
        groups = await faq_service.find_duplicate_faqs(session)

    print(f"FAQ 共 {total_before} 行；问句重复的分组 {len(groups)} 个")

    doomed = []
    for group in groups:
        print(f"\n  「{group.keep.question}」（归一化后：{group.key}）")
        for index, item in enumerate([group.keep, *group.drop]):
            preview = item.answer.replace("\n", " ")[:40]
            print(
                f"    {'保留' if index == 0 else '待删'}  {item.faq_id}  {item.status:<9} "
                f"cache_enabled={item.cache_enabled}  "
                f"{item.created_at:%Y-%m-%d %H:%M}  答案：{preview}"
            )
        if group.answers_differ:
            print(
                "    注意：组内答案不一致，上面标「保留」的那行是保留口径的默认选择"
                "——动手前先确认它是对的那条答复。"
            )
        doomed.extend(group.drop)

    if not doomed:
        print("\n没有重复问句，无需处理。")
        return 0

    if not args.apply:
        print(f"\n这是只读报告：{len(doomed)} 行待删。加 --apply 执行（每条问句保留最早的一行）。")
        return 0

    VAR_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    export = VAR_DIR / f"duplicate-faqs-{stamp}.json"
    export.write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in doomed],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n待删 {len(doomed)} 行已导出到 var\\{export.name}")

    async with SessionLocal() as session:
        deleted = await faq_service.delete_faqs(session, [item.faq_id for item in doomed])
        rows_after = await faq_repo.all_faqs(session)
        remaining = await faq_service.find_duplicate_faqs(session)
    for item in deleted:
        print(f"  已删除 {item.faq_id}  「{item.question}」")

    published_after = sum(1 for row in rows_after if row.status == faq_repo.FAQ_PUBLISHED)
    cache_ok = await _cache_state([str(item.faq_id) for item in deleted], published_after)

    print(
        f"\n复核：FAQ {len(rows_after)} 行（重复分组 {len(remaining)} 个）；"
        f"已发布 {published_after} 行"
    )
    clean = not remaining and cache_ok
    print("清理完成" if clean else "仍有残留，见上面的复核数字")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
