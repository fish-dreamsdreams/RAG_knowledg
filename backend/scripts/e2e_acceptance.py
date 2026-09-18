"""端到端验收（tasklist 15.3）：一条命令跑完「登录 → 导入现状 → 问答 → 审计 → 看板 → 图片权限」。

用法（compose + 后端 + worker 都在跑，前置见 README §演示数据）：

    cd backend
    .venv\\Scripts\\python.exe scripts\\e2e_acceptance.py

退出码：`0` 全通过；`1` 有断言失败；`2` 前置没满足（stack 没起、演示数据没导）。把「没跑」和
「跑挂了」分开，人和 CI 都能一眼看懂。

**它覆盖哪些不变量**：本脚本打的是真实 HTTP/WS 链路，覆盖 P3、P4、P12、P14、P19 的端到端形态
（含场景 A 的越权问法与附图代理权限）。P1/P2/P5–P11/P13/P15–P18 的逐条断言在 `backend/test/`
里（`pytest` + `pytest -m integration`），那里的断言比脚本更细——比如 P17 要读库比对正文，
而切片接口按设计只吐摘要。所以完整口径是「本脚本 + 两套 pytest」，不是只有本脚本。

**为什么不在这里导入**：导入要跑 MinerU 解析与嵌入，几分钟起步，且两个导入脚本已经幂等
（`import_scenario_a.py` / `prepare_scenario_b.py`）。这里只**核对导入现状**（单元、ACL、
启用、切片数），把一条命令的耗时压到秒级——要重导就跑那两个脚本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
import websockets

from demo_http import BASE_URL, login

WS_PATH = "/ws/chat"
PASSWORD = "Demo@123456"
FRESH_WINDOW = timedelta(minutes=30)

# 一份文档的验收口径：ACL 期望（`None` = 全局）+ 为什么（与 README §演示数据 同一张表）
@dataclass(frozen=True)
class Expectation:
    filename: str
    departments: tuple[str, ...]
    acl_global: bool
    why: str


EXPECTED_UNITS: tuple[Expectation, ...] = (
    Expectation("差旅费用标准.md", (), True, "场景 A 正向：全局，销售部要答得出"),
    Expectation("薪酬管理制度.md", ("人力资源部",), False, "场景 A 反向：销售部问它必须被拦"),
    Expectation("财务报销.pdf", ("财务部",), False, "部门内部规范，含票据图（P19 素材）"),
    Expectation("客服退款流程.md", (), True, "场景 B：FAQ 沉淀的语料"),
)

# 刻意留到「缺口 → 转建导入」那一步才导的文档：只报告状态，不参与断言
OPTIONAL_UNITS = {"清关问题说明.txt": "刻意留到缺口演示那一步，导没导都算通过"}

TITLE_OF_DENIED = "薪酬管理制度"
DENIED_UNIT_FILE = "薪酬管理制度.md"


@dataclass
class Report:
    """逐条断言的结果，最后一起汇总（`FAIL` 只打印，不影响后续检查继续跑）。"""

    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        self.results.append((label, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" —— {detail}" if detail else ""))
        return ok

    def section(self, title: str) -> None:
        print(f"\n== {title} ==")

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [item for item in self.results if not item[1]]


@dataclass
class Turn:
    """一轮问答的观测结果（只收协议里定义过的帧）。"""

    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    acl_notice: bool = False
    faq_hit: bool = False
    audit_id: str | None = None
    error: str | None = None

    def wire_text(self) -> str:
        """所有出站帧的文本拼一起：P3 要查的是「任何出站消息」，不是只看最终答案。"""

        return json.dumps(self.frames, ensure_ascii=False)


async def ask(ws_url: str, token: str, question: str, *, timeout: float = 180.0) -> Turn:
    """走真实 WebSocket 问一轮，收到 `done` 或 `error` 为止。"""

    turn = Turn()
    tokens: list[str] = []

    async with websockets.connect(f"{ws_url}?access_token={token}", max_size=None) as socket:
        await socket.send(json.dumps({"type": "ask", "content": question}))
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
            message = json.loads(raw)
            turn.frames.append(message)
            kind = message.get("type")
            if kind == "token":
                tokens.append(message.get("delta") or "")
            elif kind == "citation":
                turn.citations.append(message)
            elif kind == "acl_notice":
                turn.acl_notice = bool(message.get("has_denied"))
            elif kind == "done":
                turn.faq_hit = bool(message.get("faq_hit"))
                turn.audit_id = message.get("audit_id")
                break
            elif kind == "error":
                turn.error = f"{message.get('code')}: {message.get('message')}"
                break

    turn.answer = "".join(tokens)
    return turn


def audit_row(
    client: httpx.Client, headers: dict[str, str], question: str
) -> dict | None:
    """该问题最近一条审计（接口按时间倒序，取首个）；只认本次运行写下的行。"""

    response = client.get(
        "/audit-logs",
        headers=headers,
        params={"page": 1, "page_size": 50},
    )
    response.raise_for_status()
    cutoff = datetime.now(timezone.utc) - FRESH_WINDOW
    for item in response.json()["data"]["items"]:
        if item["question"] != question:
            continue
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            return item
    return None


@dataclass
class Session:
    """一个演示账号的两种形态：HTTP 用的请求头、WS 与图片代理用的裸 token。"""

    headers: dict[str, str]
    token: str


def sign_in(client: httpx.Client, username: str) -> Session:
    """登录一次，同时拿到请求头与裸 token（图片代理只能走查询参数，见 object_store 注释）。"""

    headers = login(client, username, PASSWORD)
    return Session(headers=headers, token=headers["Authorization"].split(" ", 1)[1])


def unit_id_of(units: dict[str, dict], filename: str) -> str | None:
    """单元 id（没导入就是 `None`，让断言带一句人话而不是 KeyError）。"""

    unit = units.get(filename)
    return str(unit["unit_id"]) if unit else None


def department_ids(client: httpx.Client, headers: dict[str, str], names: set[str]) -> dict[str, str]:
    """按名称在部门树里找 id（`GET /departments` 返回树）；复用导入脚本的口径。"""

    response = client.get("/departments", headers=headers)
    response.raise_for_status()
    found: dict[str, str] = {}

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            if node.get("name") in names:
                found[node["name"]] = node["id"]
            walk(node.get("children") or [])

    walk(response.json()["data"])
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端验收：登录 → 导入现状 → 问答 → 审计 → 看板")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    ws_url = args.base_url.replace("http://", "ws://").replace("https://", "wss://") + WS_PATH

    report = Report()

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        report.section("0. 前置：依赖与会话")

        try:
            health = client.get("/health")
        except httpx.HTTPError as exc:
            print(f"后端不可达（{exc}）。先跑 scripts\\dev-up.ps1，再跑本脚本。")
            return 2

        trace_id = health.headers.get("X-Trace-Id", "")
        report.check("P14 GET /health 带 X-Trace-Id", health.status_code == 200 and bool(trace_id), trace_id)

        try:
            who = {
                name: sign_in(client, name)
                for name in ("admin", "kbadm", "sales01", "finance01", "cs01")
            }
        except httpx.HTTPStatusError as exc:
            print(f"登录失败：{exc.response.status_code}。演示数据还没导？见 README §演示数据。")
            return 2

        admin = who["admin"]
        kb = who["kbadm"]
        sales = who["sales01"]
        finance = who["finance01"]
        cs = who["cs01"]
        report.check("五个演示账号都能登录", len(who) == 5)

        # 未知资源也要带 trace（错误响应同样要能追）；认证后拿到的是真的 404
        missing = client.get(
            "/knowledge-units/00000000-0000-0000-0000-000000000000", headers=admin.headers
        )
        report.check(
            "P14 错误响应也带 X-Trace-Id",
            missing.status_code == 404 and bool(missing.headers.get("X-Trace-Id")),
            f"{missing.status_code}",
        )

        report.section("1. 导入现状（单元 / ACL / 启用 / 切片）")

        units: dict[str, dict] = {}
        listing = client.get(
            "/knowledge-units", headers=kb.headers, params={"page": 1, "page_size": 100}
        )
        listing.raise_for_status()
        for item in listing.json()["data"]["items"]:
            detail = client.get(f"/knowledge-units/{item['unit_id']}", headers=kb.headers).json()["data"]
            units[detail["source_filename"]] = detail

        needed_depts = {
            name for item in EXPECTED_UNITS for name in item.departments
        }
        dept_ids = department_ids(client, admin.headers, needed_depts)
        id_to_name = {value: key for key, value in dept_ids.items()}

        for item in EXPECTED_UNITS:
            unit = units.get(item.filename)
            if unit is None:
                report.check(f"1 {item.filename} 已导入", False, "单元不存在，先跑导入脚本")
                continue

            unit_id = unit["unit_id"]
            acl = client.get(f"/knowledge-units/{unit_id}/acl", headers=kb.headers).json()["data"]
            chunks = client.get(f"/knowledge-units/{unit_id}/chunks", headers=kb.headers).json()["data"]

            actual_depts = {id_to_name.get(value, value) for value in acl["departments"]}
            acl_ok = acl["acl_global"] == item.acl_global and actual_depts == set(item.departments)
            report.check(
                f"1 {item.filename}：解析/启用/切片/ACL",
                unit["parse_status"] == "indexed"
                and unit["status"] == "enabled"
                and acl_ok
                and chunks["child_total"] > 0,
                f"{unit['parse_status']} | {unit['status']} | 子块 {chunks['child_total']} | "
                f"{'全局' if acl['acl_global'] else '、'.join(sorted(actual_depts)) or '未授权'}（{item.why}）",
            )

        for filename, why in OPTIONAL_UNITS.items():
            state = "已导入" if filename in units else "未导入"
            print(f"  INFO  {filename}：{state}（{why}）")

        report.section("2. 场景 A：销售部的能与不能（P3）")

        travel = asyncio.run(ask(ws_url, sales.token, "出差住宿费标准是多少？"))
        travel_audit = audit_row(client, admin.headers, "出差住宿费标准是多少？")
        travel_unit = unit_id_of(units, "差旅费用标准.md")

        report.check(
            "2 销售问差旅：有答案、有溯源",
            travel.error is None
            and bool(travel.answer.strip())
            and len(travel.citations) >= 1
            and travel_audit is not None
            and travel_audit["answer_status"] == "answered",
            f"{len(travel.citations)} 条引用 | 审计 {travel_audit['answer_status'] if travel_audit else '缺行'}",
        )
        report.check(
            "2 差旅单元在放行集合里（P1 正向）",
            travel_unit is not None
            and bool(travel_audit)
            and travel_unit in travel_audit.get("allowed_unit_ids", []),
            "" if travel_unit else "差旅单元未导入",
        )
        # 不做断言：同批召回里有没有无权单元取决于当时召回了什么，「部分资料无权查阅」提示
        # 本来就该按实际情况出现或消失，钉死它反而会把正确行为判成失败。
        print(f"  INFO  差旅这一轮 acl_notice={travel.acl_notice}（同批里有无权资料时才提示）")

        salary_question = "薪酬标准和报销补贴有哪些？"
        salary = asyncio.run(ask(ws_url, sales.token, salary_question))
        salary_audit = audit_row(client, admin.headers, salary_question)
        denied_unit = unit_id_of(units, DENIED_UNIT_FILE)

        report.check(
            "2 P3 销售问薪酬：拒答且零引用",
            salary.error is None
            and salary_audit is not None
            and salary_audit["answer_status"] == "denied"
            and not salary.citations
            and not salary_audit["citation_ids"],
            f"审计 {salary_audit['answer_status'] if salary_audit else '缺行'}",
        )
        report.check(
            "2 P3 标题不出现在任何出站消息",
            TITLE_OF_DENIED not in salary.wire_text() and TITLE_OF_DENIED not in salary.answer,
            f"检索整轮 {len(salary.frames)} 帧",
        )
        report.check(
            "2 P3 无权单元不在放行集合、且计入 denied",
            denied_unit is not None
            and bool(salary_audit)
            and denied_unit not in salary_audit.get("allowed_unit_ids", [])
            and salary_audit["denied_count"] >= 1,
            "" if denied_unit else "薪酬单元未导入",
        )

        report.section("3. 场景 B：FAQ 命中与知识缺口（P4）")

        # 用**已发布 FAQ 的问句原样**问：P4 要证的是「命中即不检索不生成」，若拿一个
        # 近似问法去问，考的就变成阈值标定（近似问法落到检索链是设计内的选择，不是缺陷）。
        faqs = client.get(
            "/faqs", headers=admin.headers, params={"page": 1, "page_size": 50}
        )
        faqs.raise_for_status()
        published = [
            item
            for item in faqs.json()["data"]["items"]
            if item["status"] == "published" and item["cache_enabled"]
        ]
        repeated = {item["question"] for item in published}
        if len(repeated) != len(published):
            print(f"  INFO  已发布 FAQ 里有同问句多行（{len(published)} 行 / {len(repeated)} 个问句），可在控制台合并")

        if not published:
            report.check("3 有已发布的 FAQ 可问", False, "先把 FAQ 候选发布（沉淀运营）")
            faq_question = ""
        else:
            faq_question = published[0]["question"]

        faq = asyncio.run(ask(ws_url, cs.token, faq_question)) if faq_question else Turn()
        faq_audit = audit_row(client, admin.headers, faq_question) if faq_question else None
        report.check(
            "3 P4 FAQ 命中：不检索、不生成",
            faq_question != ""
            and faq.error is None
            and faq.faq_hit
            and not faq.citations
            and faq_audit is not None
            and faq_audit["faq_hit"]
            and faq_audit["prompt_tokens"] == 0
            and faq_audit["completion_tokens"] == 0,
            f"问「{faq_question}」token="
            f"{faq_audit['prompt_tokens'] + faq_audit['completion_tokens'] if faq_audit else '缺行'}",
        )

        gap_question = "公司年会在哪里举办"
        gap = asyncio.run(ask(ws_url, cs.token, gap_question))
        gap_audit = audit_row(client, admin.headers, gap_question)
        gaps = client.get("/knowledge-gaps", headers=admin.headers, params={"page": 1, "page_size": 50})
        gaps.raise_for_status()
        gap_hit = any(
            item["question_text"] == gap_question for item in gaps.json()["data"]["items"]
        )
        report.check(
            "3 无知识时按缺口处理并登记",
            gap.error is None
            and gap_audit is not None
            and gap_audit["answer_status"] == "gap"
            and gap_hit,
            f"审计 {gap_audit['answer_status'] if gap_audit else '缺行'} | 缺口列表{'有' if gap_hit else '无'}",
        )

        report.section("4. 附图代理的权限边界（P19）")

        finance_question = "财务报销需要哪些票据和审批材料？"
        finance_turn = asyncio.run(ask(ws_url, finance.token, finance_question))
        assets = [asset for citation in finance_turn.citations for asset in citation.get("assets", [])]

        if not assets:
            report.check("4 财务报销答案带票据附图", False, "没有附图，检查 assets 链路")
        else:
            # 引用里给浏览器的地址是带 /api/v1 的绝对路径，而客户端的 base_url 已经带了那段：
            # 直接拼会变成 /api/v1/api/v1/…，服务端只能回一个路由级 404（比权限 404 更早，
            # 两种 404 长得一样，别把脚本的 bug 读成产品缺陷）。
            url = assets[0]["url"].removeprefix("/api/v1")
            allowed = client.get(f"{url}?access_token={finance.token}")
            denied = client.get(f"{url}?access_token={sales.token}")
            report.check(
                "4 P19 有权限部门能取到图",
                allowed.status_code == 200
                and allowed.headers.get("content-type", "").startswith("image/")
                and len(allowed.content) > 0,
                f"{allowed.status_code} {allowed.headers.get('content-type', '')} "
                f"{len(allowed.content)}B {allowed.text[:80] if allowed.status_code != 200 else ''}",
            )
            report.check(
                "4 P19 无权部门取不到图",
                denied.status_code in (403, 404)
                and not denied.headers.get("content-type", "").startswith("image/")
                # 不看长度（错误响应本身也有体），要证的是「拿到的不是那张图」
                and denied.content != allowed.content,
                f"{denied.status_code} {denied.text[:80]}",
            )

        report.section("5. 审计与看板（P12）")

        asked = {
            "出差住宿费标准是多少？": "answered",
            salary_question: "denied",
            faq_question: "answered",
            gap_question: "gap",
        }
        asked.pop("", None)  # FAQ 没发布时不留一个空问句的假失败
        for question, expected in asked.items():
            row = audit_row(client, admin.headers, question)
            report.check(
                f"5 P12 「{question[:12]}…」恰好一条新鲜审计",
                row is not None and row["answer_status"] == expected and bool(row["trace_id"]),
                f"{row['answer_status'] if row else '缺行'}",
            )

        rows = client.get(
            "/audit-logs", headers=admin.headers, params={"page": 1, "page_size": 100}
        ).json()["data"]["items"]
        report.check(
            "5 P12 本轮没有 interrupted 残留",
            not any(item["answer_status"] == "interrupted" for item in rows),
            "",
        )

        summary = client.get("/dashboard/summary", headers=admin.headers, params={"range": "today"})
        top_questions = client.get("/dashboard/top-questions", headers=admin.headers)
        top_knowledge = client.get("/dashboard/top-knowledge", headers=admin.headers)
        token_trend = client.get("/dashboard/token-trend", headers=admin.headers)

        summary_data = summary.json()["data"] if summary.status_code == 200 else {}
        report.check(
            "5 看板四个接口都能出数",
            summary.status_code == 200
            and top_questions.status_code == 200
            and top_knowledge.status_code == 200
            and token_trend.status_code == 200
            and summary_data.get("pv", 0) >= 1
            and summary_data.get("knowledge_count", 0) >= len(EXPECTED_UNITS) - 1,
            f"PV={summary_data.get('pv')} 知识数={summary_data.get('knowledge_count')} "
            f"FAQ命中率={summary_data.get('faq_hit_rate')}",
        )

    report.section("汇总")
    passed = len(report.results) - len(report.failed)
    print(f"  {passed} / {len(report.results)} 项通过")
    for label, _, detail in report.failed:
        print(f"  FAIL  {label} —— {detail}")

    if report.failed:
        print("\n有断言未通过。逐条不变量（P1–P19）的细粒度断言见 backend/test/。")
        return 1

    print("\n全通过。完整口径 = 本脚本 + `pytest` + `pytest -m integration`。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
