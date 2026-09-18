"""演示脚本共用的后端 HTTP 客户端（场景 A / B 都用它）。

抽出来的理由：两个场景都要"登录 → 上传 → 轮询解析 → 配 ACL → 启用 → 看切片"，复制两份会在
两处慢慢走偏（比如只有一个脚本记得启用，另一个的单元就永远检索不到——这个坑真踩过）。

**为什么走 HTTP 而不是直接调 service**：脚本要复现的是知识管理员在控制台上点的那条路
（上传 → Celery 解析 → 轮询 → 配权限 → 启用）。直接写库只能证明数据库能写，证明不了接口能用。

账户分工（与种子一致）：
- `kbadm`（知识管理员）有 `kb:import`/`kb:acl`/`faq:review`/`faq:publish`，用它做导入与发布；
- 部门树的读取要 `org:dept`，只有系统管理员 `admin` 有，所以查部门 id 另开一个会话。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8000/api/v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
DOC_DIR_A = KNOWLEDGE_DIR / "scenario-a"
DOC_DIR_B = KNOWLEDGE_DIR / "scenario-b"
KB_ADMIN = ("kbadm", "Demo@123456")
SYS_ADMIN = ("admin", "Demo@123456")

POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class Plan:
    """一份文档的导入口径：ACL 怎么配、为什么这么配。"""

    filename: str
    departments: tuple[str, ...]
    acl_global: bool
    reason: str


def login(client: httpx.Client, username: str, password: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"username": username, "password": password})
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def department_ids(
    client: httpx.Client, headers: dict[str, str], names: set[str]
) -> dict[str, str]:
    """按名称在部门树里找 id（`GET /departments` 返回的是树，不是平铺列表）。"""

    response = client.get("/departments", headers=headers)
    response.raise_for_status()

    found: dict[str, str] = {}

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            if node.get("name") in names:
                found[node["name"]] = node["id"]
            walk(node.get("children") or [])

    walk(response.json()["data"])
    missing = names - found.keys()
    if missing:
        raise SystemExit(f"种子部门里找不到：{'、'.join(sorted(missing))}")
    return found


def upload(client: httpx.Client, headers: dict[str, str], path: Path) -> str:
    with path.open("rb") as handle:
        response = client.post(
            "/knowledge-units/import",
            headers=headers,
            files={"files": (path.name, handle, "application/octet-stream")},
            timeout=120,
        )
    response.raise_for_status()
    return str(response.json()["data"]["items"][0]["unit_id"])


def retry_import(client: httpx.Client, headers: dict[str, str], unit_id: str) -> None:
    """重投解析任务（控制台「重试」按钮的同一条路）。已有单元但是 `failed` 时必须走这里。"""

    response = client.post(f"/knowledge-units/{unit_id}/retry", headers=headers)
    response.raise_for_status()


def wait_indexed(
    client: httpx.Client, headers: dict[str, str], unit_id: str, *, label: str = ""
) -> str:
    """轮询到 `indexed`；失败/超时直接抛，让脚本以退出码表达失败。"""

    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        response = client.get(f"/knowledge-units/{unit_id}/import-status", headers=headers)
        response.raise_for_status()
        status = response.json()["data"]
        stage = f"{status['stage']}({status['progress']}%)"
        if stage != last:
            print(f"    {label}{stage}".rstrip())
            last = stage
        if status["parse_status"] == "indexed":
            return status["parse_status"]
        if status["parse_status"] == "failed":
            raise SystemExit(f"单元 {unit_id} 解析失败：{status['error']}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"单元 {unit_id} 超过 {POLL_TIMEOUT_SECONDS}s 仍未 indexed")


def apply_acl(
    client: httpx.Client,
    headers: dict[str, str],
    unit_id: str,
    department_id_map: dict[str, str],
    plan: Plan,
) -> None:
    """按计划覆写四维权限。`version` 原样带回，被并发改过就 409。"""

    current = client.get(f"/knowledge-units/{unit_id}/acl", headers=headers)
    current.raise_for_status()

    payload = {
        "acl_global": plan.acl_global,
        "departments": [department_id_map[name] for name in plan.departments],
        "roles": [],
        "users": [],
        "version": current.json()["data"]["version"],
    }
    response = client.put(f"/knowledge-units/{unit_id}/acl", headers=headers, json=payload)
    response.raise_for_status()


def enable_unit(client: httpx.Client, headers: dict[str, str], unit_id: str) -> None:
    """启用检索。导入默认是 `disabled`：不启用，配好权限也一条都召不回。"""

    response = client.put(
        f"/knowledge-units/{unit_id}/enabled", headers=headers, json={"enabled": True}
    )
    response.raise_for_status()


def chunk_counts(client: httpx.Client, headers: dict[str, str], unit_id: str) -> int:
    response = client.get(f"/knowledge-units/{unit_id}/chunks", headers=headers)
    response.raise_for_status()
    data = response.json()["data"]
    return data["child_total"]


def unit_detail(client: httpx.Client, headers: dict[str, str], unit_id: str) -> dict:
    response = client.get(f"/knowledge-units/{unit_id}", headers=headers)
    response.raise_for_status()
    return response.json()["data"]


def units_by_filename(client: httpx.Client, headers: dict[str, str]) -> dict[str, dict]:
    """已导入单元按原文件名索引，供幂等（重复跑不重复导入）与 `--verify` 使用。"""

    response = client.get(
        "/knowledge-units", headers=headers, params={"page": 1, "page_size": 100}
    )
    response.raise_for_status()
    units: dict[str, dict] = {}
    for item in response.json()["data"]["items"]:
        detail = unit_detail(client, headers, item["unit_id"])
        units[detail["source_filename"]] = detail
    return units


def import_plan(
    client: httpx.Client,
    kb_headers: dict[str, str],
    admin_headers: dict[str, str],
    plans: tuple[Plan, ...],
    doc_dir: Path,
    *,
    configure: bool = True,
) -> None:
    """把一组计划跑一遍：上传（或复用）→ 等解析 → 配 ACL + 启用 → 打印切片与权限。

    `configure=False` 用于 `--verify`：只看现状，不改权限与启用状态。
    """

    dept_ids = department_ids(
        client, admin_headers, {name for plan in plans for name in plan.departments}
    )
    existing = units_by_filename(client, kb_headers)

    for plan in plans:
        path = doc_dir / plan.filename
        if not path.exists():
            raise SystemExit(f"缺少文档：{path}")

        print(f"[{plan.filename}] {plan.reason}")
        if plan.filename in existing:
            unit = existing[plan.filename]
            unit_id = str(unit["unit_id"])
            if unit["parse_status"] == "failed" and configure:
                print(f"    unit={unit_id} 已存在但解析失败，重投：{unit['parse_error']}")
                retry_import(client, kb_headers, unit_id)
                state = "重投，等待解析"
            else:
                state = "已存在，跳过导入"
        else:
            unit_id = upload(client, kb_headers, path)
            state = "已受理，等待解析"
        print(f"    unit={unit_id} {state}")

        parse_status = wait_indexed(client, kb_headers, unit_id)
        if configure:
            apply_acl(client, kb_headers, unit_id, dept_ids, plan)
            enable_unit(client, kb_headers, unit_id)

        children = chunk_counts(client, kb_headers, unit_id)
        detail = unit_detail(client, kb_headers, unit_id)
        acl = client.get(f"/knowledge-units/{unit_id}/acl", headers=kb_headers).json()["data"]
        acl_text = "全局" if acl["acl_global"] else "、".join(plan.departments) or "未授权"
        print(
            f"    {parse_status} | 子块 {children} | "
            f"状态 {detail['status']} | 权限 {acl_text} | version {acl['version']}"
        )
