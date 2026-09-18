"""演示库清场小工具：把验收与手工演示留下的痕迹清回「导入脚本刚跑完」的样子。

用法（在 backend 目录下）：

    .venv\\Scripts\\python.exe tool\\clean_demo_state.py                    # 只读报告
    .venv\\Scripts\\python.exe tool\\clean_demo_state.py --apply            # 执行（默认四块）
    .venv\\Scripts\\python.exe tool\\clean_demo_state.py --apply --only audit,gaps
    .venv\\Scripts\\python.exe tool\\clean_demo_state.py --apply --force    # 非开发密钥时也要删

四块与口径写在 `app.services.demo_state`（按来源认，不按时间猜）。默认只读；`--apply` 会先把
待删内容导出到 `backend/var/demo-reset-<时间戳>.json`，再逐块删除，最后复核并给退出码
（`0` 干净 / `1` 有残留）。

**边界（重要）**：这是维护工具，不进 API、不进控制台。审计行「不清理历史」是产品层的不变量
（`QaAuditLog`，P12 的凭据），这里开删除口子只为演示库复位；要把它做成产品能力，先推翻那条决定。

`--force`：`SECRET_KEY` 不是开发默认值时（说明连的是别人部署的环境），`--apply` 默认拒绝执行，
必须显式加 `--force`。项目没有 `APP_ENV`，用密钥是否是默认值当「这是不是开发环境」的判据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# 让 `python tool/clean_demo_state.py` 也能导入 app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.common.config import get_settings  # noqa: E402
from app.common.db import SessionLocal  # noqa: E402
from app.common.redis import get_redis  # noqa: E402
from app.engines import faq_cache  # noqa: E402
from app.repositories import faq as faq_repo  # noqa: E402
from app.services import demo_state  # noqa: E402

VAR_DIR = Path(__file__).resolve().parent.parent / "var"
DEV_SECRET = "dev-only-change-me"
# 报告里每块最多打印多少行明细，完整内容看导出文件
PREVIEW_ROWS = 10


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="清掉验收与手工演示在库里留下的痕迹（默认只报告）",
    )
    parser.add_argument("--apply", action="store_true", help="真的删除（默认只读）")
    parser.add_argument(
        "--only",
        default=",".join(demo_state.BLOCKS),
        help=f"只处理指定块，逗号分隔，可选：{','.join(demo_state.BLOCKS)}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="SECRET_KEY 不是开发默认值时也允许 --apply",
    )
    return parser.parse_args(argv)


def _blocks(raw: str) -> tuple[str, ...]:
    blocks = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = [block for block in blocks if block not in demo_state.BLOCKS]
    if unknown or not blocks:
        raise SystemExit(
            f"--only 取值不合法：{raw!r}；可选 {','.join(demo_state.BLOCKS)}"
        )
    return blocks


def _print_report(reports: list[demo_state.BlockReport]) -> None:
    for report in reports:
        print(f"\n【{report.label}】待删 {report.count} 行，保留 {report.kept} 行")
        for note in report.notes:
            print(f"  注意：{note}")
        for row in report.doomed[:PREVIEW_ROWS]:
            detail = "  ".join(
                f"{key}={value}"
                for key, value in row.items()
                if key != "id" and value not in (None, "")
            )
            print(f"    - {row['id']}  {detail}")
        if report.count > PREVIEW_ROWS:
            print(f"    …… 其余 {report.count - PREVIEW_ROWS} 行见导出文件")


async def _cache_entries() -> int:
    return int(await get_redis().hlen(faq_cache.PUBLISHED_KEY))


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    blocks = _blocks(args.only)

    if args.apply and get_settings().secret_key != DEV_SECRET and not args.force:
        print(
            "拒绝执行：SECRET_KEY 不是开发默认值，看起来不是本地开发库。"
            "确认要清就加 --force。"
        )
        return 1

    async with SessionLocal() as session:
        reports = await demo_state.build_report(session, blocks=blocks)
        total = sum(report.count for report in reports)
        print(f"演示库清场（{'执行' if args.apply else '只读报告'}）")
        print(f"处理块：{'、'.join(blocks)}；合计待删 {total} 行")
        _print_report(reports)

        if not args.apply:
            print("\n这是只读报告，没有改动任何数据。加 --apply 执行。")
            return 0
        if total == 0:
            print("\n没有要清的行，退出码 0。")
            return 0

        VAR_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        export = VAR_DIR / f"demo-reset-{stamp}.json"
        export.write_text(
            json.dumps(
                {report.block: report.doomed for report in reports},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n待删内容已导出到 var\\{export.name}")

        # 缓存条目数删前记录：复用 delete_faqs 会同步清缓存，删完应当只剩已发布行数
        before_entries = await _cache_entries()
        deleted = await demo_state.apply_report(session, reports)

        after = await demo_state.build_report(session)
        faq_rows = await faq_repo.all_faqs(session)
        published = sum(1 for row in faq_rows if row.status == faq_repo.FAQ_PUBLISHED)
        after_entries = await _cache_entries()

    print(f"\n删除：{deleted}")
    print(
        f"复核：缓存条目 {before_entries} → {after_entries} 条（已发布 FAQ {published} 行 / "
        f"全表 {len(faq_rows)} 行）；残留待删 {sum(report.count for report in after)} 行"
    )
    clean = not any(report.count for report in after) and after_entries == published
    print("清理完成" if clean else "仍有残留，见上面的复核数字")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
