"""图片代理接口测试（tasklist 17.4，P19）。

需要 PostgreSQL + Redis + MinIO（`docker compose up -d`）：用例自己造单元行、资产行与真实对象，
再走 HTTP 取图。自建自清。

安全属性在这里锁死：桶私有、前端不直连对象存储，取图必须过 ACL，且**无权与不存在返回同一个
404**——否则接口就成了"按 id 探测内部资料是否存在"的探针。
"""

from __future__ import annotations

import base64
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.db import SessionLocal
from app.common.errors import AppError
from app.engines.acl import AclSubject
from app.engines.storage import object_store
from app.models.knowledge import KnowledgeUnit, KnowledgeUnitAsset
from app.models.org import User
from app.services import knowledge as knowledge_service

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "Demo@123456"
# 1×1 透明 PNG：接口只做字节转发与 Content-Type 标注，图片内容本身不参与判定
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
NAME = "img_1.png"


async def login_token(client: AsyncClient, username: str = "admin") -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


async def make_unit(
    session: AsyncSession, *, title: str, acl_global: bool = True
) -> KnowledgeUnit:
    """造一个已入库的单元；`acl_global=False` 且无 ACL 行 = 所有人无权（P1）。"""

    unit = KnowledgeUnit(
        title=title,
        format="pdf",
        source_path=f"kb/units/{uuid4()}/source/{title}.pdf",
        source_filename=f"{title}.pdf",
        status="enabled",
        parse_status="indexed",
        acl_global=acl_global,
    )
    session.add(unit)
    await session.flush()
    return unit


async def make_asset(
    session: AsyncSession, unit: KnowledgeUnit, *, name: str = NAME, content: bytes = PNG_BYTES
) -> KnowledgeUnitAsset:
    """写对象 + 登记资产行（与 `services/ingest.py::_ingest_assets` 同口径）。"""

    key = object_store.asset_key(unit.id, name)
    assert key is not None
    await object_store.put_bytes(key, content, content_type=object_store.media_type_for(name))
    asset = KnowledgeUnitAsset(
        unit_id=unit.id,
        rel_path=f"{object_store.ASSETS_DIR}/{name}",
        name=name,
        media_type=object_store.media_type_for(name) or "image/png",
        byte_size=len(content),
        sha256=None,
        caption=None,
        ordinal=0,
    )
    session.add(asset)
    await session.flush()
    return asset


@pytest.fixture
async def unit_factory():
    """按需造单元，结束时删行并清掉该单元前缀下的对象。"""

    created: list[UUID] = []

    async def _make(*, title: str = "审批流程", acl_global: bool = True) -> KnowledgeUnit:
        async with SessionLocal() as session:
            unit = await make_unit(session, title=title, acl_global=acl_global)
            await session.commit()
            created.append(unit.id)
            return unit

    yield _make

    for unit_id in created:
        async with SessionLocal() as session:
            unit = await session.get(KnowledgeUnit, unit_id)
            if unit is not None:
                await session.delete(unit)
                await session.commit()
        await object_store.delete_unit_prefix(unit_id)


@pytest.fixture
async def unit_with_asset(unit_factory) -> KnowledgeUnit:
    unit = await unit_factory()
    async with SessionLocal() as session:
        row = await session.get(KnowledgeUnit, unit.id)
        assert row is not None
        await make_asset(session, row)
        await session.commit()
    return unit


def asset_url(unit_id: UUID, name: str = NAME) -> str:
    return f"/api/v1/knowledge-units/{unit_id}/assets/{name}"


# ---------- 正常取图 ----------


async def test_serves_bytes_with_hardening_headers(
    client: AsyncClient, unit_with_asset: KnowledgeUnit
) -> None:
    token = await login_token(client)

    response = await client.get(asset_url(unit_with_asset.id), params={"access_token": token})

    assert response.status_code == 200, response.text
    assert response.content == PNG_BYTES
    # 显式 Content-Type 来自资产表白名单；禁嗅探 + 只允许私有缓存（TECH_SPEC §8.0）
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private"


async def test_bearer_header_also_works(
    client: AsyncClient, unit_with_asset: KnowledgeUnit
) -> None:
    """查询参数是给 `<img>` 用的；脚本与调试仍可用标准的 Bearer 头。"""

    token = await login_token(client)

    response = await client.get(
        asset_url(unit_with_asset.id), headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.content == PNG_BYTES


async def test_url_from_citation_assets_is_the_real_route(
    client: AsyncClient, unit_with_asset: KnowledgeUnit
) -> None:
    """答案侧下发的地址必须就是本接口的路径，否则前端引用卡片会永远裂图。"""

    token = await login_token(client)
    proxy_path = object_store.asset_proxy_path(unit_with_asset.id, NAME)

    response = await client.get(proxy_path, params={"access_token": token})

    assert response.status_code == 200, response.text
    assert response.content == PNG_BYTES


# ---------- 鉴权 ----------


async def test_missing_token_is_unauthorized(
    client: AsyncClient, unit_with_asset: KnowledgeUnit
) -> None:
    response = await client.get(asset_url(unit_with_asset.id))

    assert response.status_code == 401
    assert PNG_BYTES not in response.content


async def test_invalid_token_is_unauthorized(
    client: AsyncClient, unit_with_asset: KnowledgeUnit
) -> None:
    response = await client.get(
        asset_url(unit_with_asset.id), params={"access_token": "not-a-jwt"}
    )

    assert response.status_code == 401
    assert PNG_BYTES not in response.content


# ---------- ACL：无权与不存在不可区分（P19） ----------


async def test_denied_unit_returns_404_without_bytes(
    client: AsyncClient, unit_factory
) -> None:
    unit = await unit_factory(title="机密薪酬表", acl_global=False)
    async with SessionLocal() as session:
        row = await session.get(KnowledgeUnit, unit.id)
        assert row is not None
        await make_asset(session, row)
        await session.commit()
    token = await login_token(client)

    response = await client.get(asset_url(unit.id), params={"access_token": token})

    assert response.status_code == 404
    assert PNG_BYTES not in response.content


async def test_acl_admin_gets_no_bypass(client: AsyncClient, unit_factory) -> None:
    """能配权限不等于能看内容：`kb:acl` 管理员看得到单元列表，但取不到它的图。

    否则"越权读一张图"只需要一个管理员账号，四维 ACL 在图片这条路径上就白设了。
    """

    unit = await unit_factory(title="仅财务可见", acl_global=False)
    async with SessionLocal() as session:
        row = await session.get(KnowledgeUnit, unit.id)
        assert row is not None
        await make_asset(session, row)
        await session.commit()
    headers = {"Authorization": f"Bearer {await login_token(client)}"}

    listing = await client.get("/api/v1/knowledge-units", headers=headers)
    assert listing.status_code == 200
    assert str(unit.id) in listing.text, "前置条件：管理员确实能在列表里看到这个单元"

    response = await client.get(asset_url(unit.id), headers=headers)

    assert response.status_code == 404
    assert PNG_BYTES not in response.content


async def test_unknown_unit_is_404(client: AsyncClient, unit_with_asset: KnowledgeUnit) -> None:
    token = await login_token(client)

    response = await client.get(
        asset_url(uuid4()), params={"access_token": token}
    )

    assert response.status_code == 404


# ---------- 名字与对象本身 ----------


async def test_unregistered_object_is_not_served(
    client: AsyncClient, unit_factory
) -> None:
    """对象存储里有字节、但资产表没登记：不吐。登记行才是"这张图属于本单元"的凭据。"""

    unit = await unit_factory()
    key = object_store.asset_key(unit.id, "orphan.png")
    assert key is not None
    await object_store.put_bytes(key, PNG_BYTES, content_type="image/png")
    token = await login_token(client)

    response = await client.get(
        asset_url(unit.id, "orphan.png"), params={"access_token": token}
    )

    assert response.status_code == 404
    assert PNG_BYTES not in response.content


@pytest.mark.parametrize(
    "name",
    [
        "nope.png",  # 不存在
        "img_1.svg",  # 白名单外（svg 可含脚本）
        "img_1.png.exe",  # 伪造扩展名
        "CON.png",  # Windows 保留名
    ],
)
async def test_rejected_names_are_404(
    client: AsyncClient, unit_with_asset: KnowledgeUnit, name: str
) -> None:
    token = await login_token(client)

    response = await client.get(asset_url(unit_with_asset.id, name), params={"access_token": token})

    assert response.status_code == 404
    assert PNG_BYTES not in response.content


async def test_dot_segment_name_is_rejected_by_service(
    client: AsyncClient, unit_with_asset: KnowledgeUnit
) -> None:
    """`..` 走到 URL 层就被规范化掉了（浏览器与 httpx 都会折叠点段），所以直接量 service：
    用 `curl --path-as-is` 原样送进来时，必须 404，而不是拼出一个能出前缀的 key。
    """

    async with SessionLocal() as session:
        admin = (
            await session.execute(select(User).where(User.username == "admin"))
        ).scalar_one()
        subject = AclSubject(
            user_id=admin.id, department_id=admin.department_id, role_ids=[]
        )
        # 该单元 acl_global=True，所以 404 只能来自名字本身
        with pytest.raises(AppError) as excinfo:
            await knowledge_service.read_unit_asset(
                session, subject, unit_with_asset.id, ".."
            )

    assert excinfo.value.http_status == 404


async def test_traversal_in_path_never_reaches_other_prefixes(
    client: AsyncClient, unit_with_asset: KnowledgeUnit
) -> None:
    """`../` 拼出的路径既不能命中单元内的其他前缀，也不该命中别的对象。"""

    token = await login_token(client)

    response = await client.get(
        f"/api/v1/knowledge-units/{unit_with_asset.id}/assets/../source/secret.pdf",
        params={"access_token": token},
    )

    assert response.status_code in {400, 404}
    assert PNG_BYTES not in response.content


async def test_asset_is_gone_after_unit_deleted(
    client: AsyncClient, unit_factory
) -> None:
    """单元删除后：对象与资产行都被清掉，取图 404（P10 的收尾）。"""

    unit = await unit_factory()
    async with SessionLocal() as session:
        row = await session.get(KnowledgeUnit, unit.id)
        assert row is not None
        await make_asset(session, row)
        await session.commit()
    headers = {"Authorization": f"Bearer {await login_token(client)}"}
    assert (
        await client.get(asset_url(unit.id), headers=headers)
    ).status_code == 200, "前置条件：删除前能取到图"

    deleted = await client.delete(f"/api/v1/knowledge-units/{unit.id}", headers=headers)
    assert deleted.status_code == 200, deleted.text

    response = await client.get(asset_url(unit.id), headers=headers)

    assert response.status_code == 404
    assert await object_store.list_keys(object_store.assets_prefix(unit.id)) == []
