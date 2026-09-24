"""场景 A 数据导入（tasklist 15.1）：把 `data/knowledge/scenario-a/` 的制度文档导进来并配好 ACL。

刻意走 HTTP 而不是直接调 service：这就是知识管理员在控制台点的那条路——上传 → Celery 解析
（`queued→parsing→chunking→embedding→indexed`）→ 轮询进度 → 配四维权限 → 启用检索。脚本跑通即
演示能跑通；直接写库只能证明数据库能写。共用客户端在 `demo_http.py`。

用法（后端与 worker 都要在跑，见 README）：

    cd backend
    .venv\\Scripts\\python.exe scripts\\import_scenario_a.py            # 导入 + 配 ACL + 启用
    .venv\\Scripts\\python.exe scripts\\import_scenario_a.py --verify   # 只核对现状

ACL 口径（PRD §10 场景 A + tasklist 15.1）：

| 文档 | 权限 | 为什么要这样 |
|------|------|--------------|
| 差旅费用标准.md | 全局 | 场景 A 第 4 步：销售部问差旅要能拿到答案与溯源 |
| 薪酬管理制度.md | 人力资源部 | 第 5 步：销售部问薪酬必须回权限缺失，且响应与审计里不得出现细则标题 |
| 财务报销.pdf | 财务部 | 部门内部单据规范（含票据图），不对外 |

PRD 写的是「薪酬 = 人力资源部 + 管理层」，但种子角色里没有「管理层」（只有 普通员工/知识管理员/
系统管理员），所以这里只配部门，不硬造角色——补角色是组织管理的事，不在导入脚本里替它决定。
"""

from __future__ import annotations

import argparse
import sys

import httpx

from demo_http import (
    BASE_URL,
    DOC_DIR_A,
    KB_ADMIN,
    SYS_ADMIN,
    Plan,
    import_plan,
    login,
)

PLAN: tuple[Plan, ...] = (
    Plan("差旅费用标准.md", (), True, "场景 A：销售部问差旅要拿得到答案"),
    Plan("薪酬管理制度.md", ("人力资源部",), False, "场景 A：销售部问薪酬必须被拦"),
    Plan("财务报销.pdf", ("财务部",), False, "部门内部单据规范（含票据图）"),
)

# 目录里存在但不纳入导入：5.5KB 的占位件，正片是 财务报销.pdf
SKIP = {"财务报销制度.pdf": "占位件，正片见 财务报销.pdf"}


def main() -> int:
    parser = argparse.ArgumentParser(description="导入场景 A 制度文档并按场景口径配置 ACL")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--verify", action="store_true", help="只核对现状，不导入、不改权限")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        kb_headers = login(client, *KB_ADMIN)
        admin_headers = login(client, *SYS_ADMIN)
        import_plan(
            client,
            kb_headers,
            admin_headers,
            PLAN,
            DOC_DIR_A,
            configure=not args.verify,
        )

        planned = {plan.filename for plan in PLAN}
        extra = sorted(
            (path.name, SKIP.get(path.name, "未纳入计划")) for path in DOC_DIR_A.iterdir()
        )
        for name, why in extra:
            if name not in planned:
                print(f"\n目录里还有没动的文件：{name}（{why}）")

    print("\n导入完成。问答侧验收（销售问差旅能答、销售问薪酬被拦）见 tasklist 15.1。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
