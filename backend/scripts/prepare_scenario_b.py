"""场景 B 数据准备（tasklist 15.2）：客服退款文档 → FAQ 挖掘 → 审核发布（缓存同步开启）。

演示链路（PRD §10 场景 B）：

1. 客服退款文档进库（全局可见）；
2. 客服团队的高频退款问法（`data/seeds/qa_demo_logs.json` 展开的 67 条审计行）被 FAQ 挖掘归并成候选；
3. 运营审核后发布，缓存同步开启 → 之后同类问法直接命中 FAQ，**不调检索、不调生成**（P4）；
4. 清关问法保持在没有文档的状态，问答落成 `knowledge_gaps`，供看板「转建导入」演示。

第 4 步是刻意不导入清关文档的：那份文档是"转建导入"环节的素材，先导进来缺口演示就没了。
需要补齐时用 `--import-customs`（即演示里"缺口 → 转建导入"之后的那一步）。

用法（后端与 worker 都要在跑）：

    cd backend
    .venv\\Scripts\\python.exe scripts\\prepare_scenario_b.py                    # 导入 + 挖掘 + 发布
    .venv\\Scripts\\python.exe scripts\\prepare_scenario_b.py --import-customs   # 再补清关文档
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from demo_http import (
    BASE_URL,
    DOC_DIR_B,
    KB_ADMIN,
    SYS_ADMIN,
    Plan,
    import_plan,
    login,
)

REFUND_PLAN: tuple[Plan, ...] = (
    Plan("客服退款流程.md", (), True, "场景 B：客服高频退款问法要能被 FAQ 沉淀后直接答"),
)
CUSTOMS_PLAN: tuple[Plan, ...] = (
    Plan("清关问题说明.txt", (), True, "场景 B 转建导入：缺口补齐后清关问法要能答"),
)

# 发布出去的答案。取自《客服退款流程》第二章、第三章，审核动作就是"照文档写一份标准答案"：
# FAQ 命中的是**固定答案**，不经过模型，所以这段文字必须自己站得住。
REFUND_ANSWER = (
    "退款时效按商品类型分档：生鲜、冷藏冷冻类须签收后 24 小时内申请（需实物照片或开箱视频）；"
    "普通商品签收后 7 天内可无理由退款（商品需完好、不影响二次销售）；定制商品、贴身用品与已激活的"
    "数码产品不支持无理由退款，质量问题另走检测流程。\n"
    "受理与到账：客服接到请求后 2 小时内建工单，审核时限 48 小时；通过后款项 3 至 7 个工作日退回原支付"
    "渠道，超时未到账可代客查询支付通道流水。退货须在审核通过后 7 天内寄回，逾期工单自动关闭；退回"
    "运费质量问题由商家承担、无理由退货由客户承担。\n"
    "不予退款情形：超出时限、商品人为损坏、缺少必要凭证、定制商品已完成生产。金额超过 3000 元或客户"
    "已投诉的工单必须升级处理。"
)

CANDIDATE_POLL_INTERVAL_SECONDS = 5
CANDIDATE_POLL_TIMEOUT_SECONDS = 600


def mine_candidates(client: httpx.Client, headers: dict[str, str]) -> None:
    response = client.post("/faq/mine", headers=headers)
    response.raise_for_status()
    print(f"挖掘任务已投递：task_id={response.json()['data']['task_id']}")


def pending_candidates(client: httpx.Client, headers: dict[str, str]) -> list[dict]:
    response = client.get(
        "/faq/candidates",
        headers=headers,
        params={"status": "pending", "page": 1, "page_size": 50},
    )
    response.raise_for_status()
    return response.json()["data"]["items"]


def wait_candidates(client: httpx.Client, headers: dict[str, str]) -> list[dict]:
    """等挖掘产出候选。挖掘要编码上百条问句，跑几分钟是正常的。"""

    deadline = time.monotonic() + CANDIDATE_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        items = pending_candidates(client, headers)
        if items:
            return items
        print("    候选还没出来，等 5s…")
        time.sleep(CANDIDATE_POLL_INTERVAL_SECONDS)
    raise SystemExit("挖掘超过 10 分钟仍无候选，先看 worker 日志 backend\\var\\celery.err.log")


def main() -> int:
    parser = argparse.ArgumentParser(description="准备场景 B 数据：客服退款文档 + FAQ 发布 + 知识缺口")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--import-customs", action="store_true", help="补导清关文档（转建导入那一步）")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        kb_headers = login(client, *KB_ADMIN)
        admin_headers = login(client, *SYS_ADMIN)

        print("== 1. 导入客服退款文档 ==")
        import_plan(client, kb_headers, admin_headers, REFUND_PLAN, DOC_DIR_B)

        print("\n== 2. 触发 FAQ 挖掘 ==")
        mine_candidates(client, kb_headers)
        items = wait_candidates(client, kb_headers)
        print(f"候选 {len(items)} 条：")
        for item in sorted(items, key=lambda row: row["freq"], reverse=True):
            print(
                f"    freq={item['freq']:>2} {item['question'][:38]!r} "
                f"同义 {len(item['similar_questions'])} 条 建议答案={'有' if item['suggested_answer'] else '无'}"
            )

        print("\n== 3. 审核发布退款类候选（发布即同步缓存）==")
        refund_items = [
            item
            for item in items
            if any(word in item["question"] for word in ("退款", "退货", "无理由", "退换"))
        ]
        if not refund_items:
            print("    没有识别到退款类候选，人工看一遍候选列表再决定是否发布")
        for item in sorted(refund_items, key=lambda row: row["freq"], reverse=True):
            response = client.post(
                f"/faq/candidates/{item['candidate_id']}/publish",
                headers=kb_headers,
                json={"answer": REFUND_ANSWER},
            )
            response.raise_for_status()
            faq = response.json()["data"]
            print(
                f"    已发布 {item['question'][:32]!r} → faq_id={faq['faq_id']} "
                f"cache_enabled={faq['cache_enabled']} status={faq['status']}"
            )

        left = [
            item for item in items if item["candidate_id"] not in {row["candidate_id"] for row in refund_items}
        ]
        if left:
            print(f"    留待人工审核的非退款候选：{len(left)} 条")

        if args.import_customs:
            print("\n== 4. 补导清关文档（转建导入那一步）==")
            import_plan(client, kb_headers, admin_headers, CUSTOMS_PLAN, DOC_DIR_B)

    print("\n准备完成。问答侧验收：")
    print("  · 同类退款问法 → 审计 faq_hit=true、prompt/completion tokens 为 0（不检索、不生成）")
    print("  · 清关问法     → 审计 answer_status=gap，knowledge_gaps 的 freq 递增")
    return 0


if __name__ == "__main__":
    sys.exit(main())
