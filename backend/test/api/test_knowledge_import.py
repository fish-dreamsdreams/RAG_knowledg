"""批量导入与导入进度接口测试（tasklist 6.4 / 6.5）。

需要 PostgreSQL + Redis（导入受理会写行、投队列被替换成假投递器）。用例自建自清。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.common.db import SessionLocal
from app.engines.storage import object_store
from app.models.knowledge import IngestTask, KnowledgeUnit
from app.services import knowledge as knowledge_service

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "Demo@123456"


async def login_headers(client: AsyncClient, username: str = "admin") -> dict[str, str]:
    """用演示账号登录。

    导入要写 `knowledge_units.created_by`（外键指向 users），所以这几个用例不能用
    `fake_identity` 的随机 user_id，必须走真实登录（admin 是 system_admin，带 `*` 权限）。
    """

    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def upload(name: str, content: bytes | str = "# 制度\n\n正文内容。\n") -> tuple:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return ("files", (name, data, "application/octet-stream"))


@pytest.fixture
def dispatched(monkeypatch) -> list[tuple[UUID, str]]:
    """替换真实投递器，记录 (unit_id, task_id)，避免测试连 broker。"""

    calls: list[tuple[UUID, str]] = []

    def _record(pairs: list[tuple[UUID, str]]) -> None:
        calls.extend(pairs)

    monkeypatch.setattr(knowledge_service, "dispatch_ingest", _record)
    return calls


@pytest.fixture
async def created_units():
    """登记用例创建的单元，结束时清理行与对象存储里的原件。"""

    ids: list[UUID] = []
    yield ids

    for unit_id in ids:
        async with SessionLocal() as session:
            unit = await session.get(KnowledgeUnit, unit_id)
            if unit is None:
                continue
            await session.delete(unit)
            await session.commit()
        # 原件与解析产物都在对象存储，按单元前缀整体清理
        await object_store.delete_unit_prefix(unit_id)


async def test_import_accepts_files_and_enqueues_tasks(
    client: AsyncClient, dispatched, created_units
) -> None:
    headers = await login_headers(client)

    response = await client.post(
        "/api/v1/knowledge-units/import",
        headers=headers,
        files=[upload("员工手册.md"), upload("差旅制度.txt", "差旅报销上限 2000 元。")],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == "OK"

    data = payload["data"]
    assert len(data["items"]) == 2
    assert UUID(data["upload_batch_id"])
    created_units.extend(UUID(item["unit_id"]) for item in data["items"])

    async with SessionLocal() as session:
        for item in data["items"]:
            unit = await session.get(KnowledgeUnit, UUID(item["unit_id"]))
            assert unit is not None
            # 导入即未授权，等管理员配 ACL 后再启用
            assert unit.status == "disabled"
            assert unit.parse_status == "queued"
            assert unit.upload_batch_id == UUID(data["upload_batch_id"])
            assert unit.format in {"md", "txt"}
            assert unit.created_by is not None, "导入人要落库"
            # 原件写对象存储：key 落在本单元前缀内，客户端文件名不外泄路径
            assert unit.source_path == f"kb/units/{unit.id}/source/{unit.source_filename}"
            assert await object_store.exists(unit.source_path), "原件必须已写入对象存储"

            task = await session.get(IngestTask, item["task_id"])
            assert task is not None
            assert task.unit_id == unit.id
            assert task.stage == "queued" and task.progress == 0

    assert [unit_id for unit_id, _ in dispatched] == [
        UUID(item["unit_id"]) for item in data["items"]
    ]
    assert all(task_id for _, task_id in dispatched)


async def test_import_applies_batch_category(
    client: AsyncClient, dispatched, created_units
) -> None:
    """导入抽屉选的分类要落到这批单元上，否则抽屉里那个输入框就是个摆设。"""

    headers = await login_headers(client)

    with_category = await client.post(
        "/api/v1/knowledge-units/import",
        headers=headers,
        data={"category": "  财务制度  "},
        files=[upload("报销细则.md")],
    )
    without_category = await client.post(
        "/api/v1/knowledge-units/import",
        headers=headers,
        data={"category": "   "},
        files=[upload("无分类.md")],
    )

    for response in (with_category, without_category):
        assert response.status_code == 200, response.text
        created_units.extend(
            UUID(item["unit_id"]) for item in response.json()["data"]["items"]
        )

    trimmed = UUID(with_category.json()["data"]["items"][0]["unit_id"])
    blank = UUID(without_category.json()["data"]["items"][0]["unit_id"])
    async with SessionLocal() as session:
        assert (await session.get(KnowledgeUnit, trimmed)).category == "财务制度"
        # 全空白当作没填：列表筛选里不该多出一个空字符串分类
        assert (await session.get(KnowledgeUnit, blank)).category is None


async def test_import_rejects_unsupported_extension(
    client: AsyncClient, fake_identity, dispatched, created_units
) -> None:
    fake_identity(permissions=["kb:import"])
    before = await object_store.list_keys(f"{object_store.UNIT_PREFIX}/")

    response = await client.post(
        "/api/v1/knowledge-units/import",
        files=[upload("员工手册.md"), upload("病毒.exe", b"MZ")],
    )

    assert response.status_code == 422
    assert response.json()["code"] == "KB_UNSUPPORTED_FORMAT"
    assert dispatched == [], "校验失败不得投递"
    assert (
        await object_store.list_keys(f"{object_store.UNIT_PREFIX}/") == before
    ), "校验失败不得写入原件对象"


async def test_import_requests_without_files_are_rejected(
    client: AsyncClient, fake_identity
) -> None:
    fake_identity(permissions=["kb:import"])

    response = await client.post("/api/v1/knowledge-units/import")

    assert response.status_code == 400
    assert response.json()["code"] == "SYS_VALIDATION"


async def test_import_requires_kb_import_permission(client: AsyncClient, fake_identity) -> None:
    fake_identity(permissions=["kb:view"])

    response = await client.post(
        "/api/v1/knowledge-units/import", files=[upload("员工手册.md")]
    )

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_FORBIDDEN"


async def test_import_rejects_too_many_files(
    client: AsyncClient, fake_identity, monkeypatch, dispatched
) -> None:
    fake_identity(permissions=["kb:import"])
    monkeypatch.setattr(knowledge_service, "MAX_BATCH_FILES", 2)

    response = await client.post(
        "/api/v1/knowledge-units/import",
        files=[upload(f"制度{index}.md") for index in range(3)],
    )

    assert response.status_code == 400
    assert "最多" in response.json()["message"]
    assert dispatched == []


async def test_import_rejects_oversize_file(
    client: AsyncClient, fake_identity, monkeypatch, dispatched
) -> None:
    fake_identity(permissions=["kb:import"])
    monkeypatch.setattr(knowledge_service, "MAX_FILE_BYTES", 16)

    response = await client.post(
        "/api/v1/knowledge-units/import",
        files=[upload("大文件.md", "# " + "长" * 64)],
    )

    assert response.status_code == 400
    assert "不得超过" in response.json()["message"]
    assert dispatched == []


async def test_dispatch_failure_marks_units_failed(
    client: AsyncClient, monkeypatch, created_units
) -> None:
    """队列不可用时不能让单元卡在 queued：标 failed 并返回 503。"""

    headers = await login_headers(client)

    def _boom(pairs) -> None:
        raise OSError("broker down")

    monkeypatch.setattr(knowledge_service, "dispatch_ingest", _boom)

    response = await client.post(
        "/api/v1/knowledge-units/import", headers=headers, files=[upload("员工手册.md")]
    )

    assert response.status_code == 503
    assert response.json()["code"] == "SYS_DEPENDENCY_UNAVAILABLE"

    # 接口返回 503，但单元已经落库（可重试），状态必须是 failed 而不是 queued
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                KnowledgeUnit.__table__.select()
                .where(KnowledgeUnit.parse_status == "failed")
                .order_by(KnowledgeUnit.created_at.desc())
                .limit(1)
            )
        ).first()
        assert rows is not None
        created_units.append(rows.id)
        unit = await session.get(KnowledgeUnit, rows.id)
        assert unit is not None and unit.parse_error and "投递失败" in unit.parse_error


async def test_filename_path_traversal_is_neutralized(
    client: AsyncClient, dispatched, created_units
) -> None:
    headers = await login_headers(client)

    response = await client.post(
        "/api/v1/knowledge-units/import", headers=headers, files=[upload(r"..\..\evil.md")]
    )

    assert response.status_code == 200
    unit_id = UUID(response.json()["data"]["items"][0]["unit_id"])
    created_units.append(unit_id)

    async with SessionLocal() as session:
        unit = await session.get(KnowledgeUnit, unit_id)
        assert unit is not None
        assert unit.source_filename == "evil.md", "只保留文件名部分"
        # 路径部分被剥掉，key 仍落在本单元前缀内，不会写到别的单元
        assert unit.source_path == f"kb/units/{unit_id}/source/evil.md"
        assert await object_store.exists(unit.source_path)


async def test_import_status_reports_progress(client: AsyncClient, fake_identity) -> None:
    fake_identity(permissions=["kb:view"])

    async with SessionLocal() as session:
        unit = KnowledgeUnit(
            title="进度查询测试",
            format="md",
            source_path="var/uploads/status.md",
            source_filename="status.md",
            parse_status="chunking",
            status="disabled",
        )
        session.add(unit)
        await session.flush()
        session.add(
            IngestTask(id=uuid4().hex, unit_id=unit.id, stage="chunking", progress=50)
        )
        await session.commit()
        unit_id = unit.id

    try:
        response = await client.get(f"/api/v1/knowledge-units/{unit_id}/import-status")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["unit_id"] == str(unit_id)
        assert data["parse_status"] == "chunking"
        assert data["stage"] == "chunking"
        assert data["progress"] == 50
        assert data["error"] is None
    finally:
        async with SessionLocal() as session:
            row = await session.get(KnowledgeUnit, unit_id)
            if row is not None:
                await session.delete(row)
                await session.commit()


async def test_import_status_requires_view_permission(
    client: AsyncClient, fake_identity
) -> None:
    fake_identity(permissions=["kb:import"])

    response = await client.get(f"/api/v1/knowledge-units/{uuid4()}/import-status")

    assert response.status_code == 403


async def test_import_status_unknown_unit_returns_404(
    client: AsyncClient, fake_identity
) -> None:
    fake_identity(permissions=["kb:view"])

    response = await client.get(f"/api/v1/knowledge-units/{uuid4()}/import-status")

    assert response.status_code == 404
    assert response.json()["code"] == "KB_NOT_FOUND"


async def test_import_status_falls_back_to_unit_when_task_missing(
    client: AsyncClient, fake_identity
) -> None:
    """任务行缺失（如历史数据）时按单元状态回答，不能 500。"""

    fake_identity(permissions=["kb:view"])

    async with SessionLocal() as session:
        unit = KnowledgeUnit(
            title="无任务行测试",
            format="md",
            source_path="var/uploads/no-task.md",
            source_filename="no-task.md",
            parse_status="failed",
            parse_error="MinerU 业务错误 code=1002",
            status="disabled",
        )
        session.add(unit)
        await session.commit()
        unit_id = unit.id

    try:
        response = await client.get(f"/api/v1/knowledge-units/{unit_id}/import-status")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["stage"] == "failed"
        assert data["progress"] == 100
        assert "1002" in data["error"]
    finally:
        async with SessionLocal() as session:
            row = await session.get(KnowledgeUnit, unit_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
