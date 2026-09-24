"""知识维护用例的集成测试（tasklist 7.5）。

覆盖：未授权单元默认不可读（列表与详情）、ACL 保存后缓存失效、乐观锁冲突、
重试重置状态、切片预览的父子归组。用例造真实 PostgreSQL 行并在结束时清理。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import numpy as np

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from app.common.config import EMBED_DIM
from app.common.db import SessionLocal
from app.common.errors import AppError, ErrorCode
from app.engines.acl import AclEngine, AclSubject
from app.engines.acl import cache as acl_cache
from app.models.knowledge import Chunk, KnowledgeUnit, KnowledgeUnitAcl
from app.repositories import knowledge as knowledge_repo
from app.repositories import org as org_repo
from app.schemas.knowledge import AclPayload, ChunkUpdate, UnitUpdate
from app.services import knowledge as knowledge_service

pytestmark = pytest.mark.integration


class FakeRedis:
    """ACL 引擎用到的最小接口，用来验证「保存后主动删缓存」（P11）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(key) for key in keys]

    async def delete(self, *keys: str) -> int:
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, str]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._ops.append((key, value))

    async def execute(self) -> list[bool]:
        for key, value in self._ops:
            self._redis.store[key] = value
        return [True] * len(self._ops)


@pytest.fixture
def fake_acl_redis(monkeypatch) -> FakeRedis:
    """把服务的全局 ACL 引擎换成走假 Redis 的实例，不依赖真实缓存。"""

    redis = FakeRedis()
    monkeypatch.setattr(
        knowledge_service, "acl_engine", AclEngine(redis_factory=lambda: redis)
    )
    return redis


@pytest.fixture
async def unit_factory():
    """建知识单元（可带 ACL 条目），用例结束删行。"""

    created: list[UUID] = []

    async def _make(
        *,
        acl: Sequence[tuple[str, UUID]] = (),
        acl_global: bool = False,
        parse_status: str = "indexed",
    ) -> KnowledgeUnit:
        async with SessionLocal() as session:
            unit = KnowledgeUnit(
                id=uuid4(),
                title=f"维护测试 {uuid4().hex[:6]}",
                format="md",
                source_path="test/维护.md",
                source_filename="维护.md",
                file_size=32,
                status="enabled",
                parse_status=parse_status,
                acl_global=acl_global,
            )
            session.add(unit)
            session.add_all(
                KnowledgeUnitAcl(
                    unit_id=unit.id, principal_type=kind, principal_id=principal
                )
                for kind, principal in acl
            )
            await session.commit()
            created.append(unit.id)
            session.expunge(unit)
        return unit

    yield _make

    for unit_id in created:
        async with SessionLocal() as session:
            unit = await session.get(KnowledgeUnit, unit_id)
            if unit is not None:
                await session.delete(unit)
                await session.commit()


async def test_list_filters_by_acl_and_total(unit_factory) -> None:
    """未授权单元默认不可读，也不计入 total（否则 total 会泄露单元总数）。"""

    owner = uuid4()
    mine = await unit_factory(acl=[("user", owner)])
    others = await unit_factory(acl=[("user", uuid4())])

    subject = AclSubject(user_id=owner)
    async with SessionLocal() as session:
        items, total = await knowledge_service.list_units(
            session, subject, offset=0, limit=100
        )
        _, total_all = await knowledge_service.list_units(
            session, subject, offset=0, limit=100, bypass_acl=True
        )

    visible = {item.unit_id for item in items}
    assert mine.id in visible
    assert others.id not in visible
    assert total == len(items), "limit 足够大时 total 必须等于返回条数"
    assert total_all > total, "管理视角必须能看到被过滤掉的单元"


async def test_detail_of_denied_unit_is_not_found(unit_factory) -> None:
    """无权读取与不存在同样返回 404，不泄露单元是否存在（P9）。"""

    unit = await unit_factory(acl=[("user", uuid4())])

    async with SessionLocal() as session:
        with pytest.raises(AppError) as excinfo:
            await knowledge_service.get_unit_detail(
                session, AclSubject(user_id=uuid4()), unit.id
            )

    assert excinfo.value.http_status == 404
    assert excinfo.value.code == ErrorCode.KB_NOT_FOUND


async def test_acl_global_makes_unit_readable(unit_factory) -> None:
    unit = await unit_factory(acl_global=True)

    async with SessionLocal() as session:
        detail = await knowledge_service.get_unit_detail(
            session, AclSubject(user_id=uuid4()), unit.id
        )

    assert detail.unit_id == unit.id


async def test_list_carries_acl_labels_with_names(unit_factory) -> None:
    """列表行要带权限标签用的**名称**（PRD §6.3）：只给 id 的话前端拼不出「财务部」。"""

    async with SessionLocal() as session:
        departments = await org_repo.list_departments(session)
        roles = await org_repo.list_roles(session)
        users, _ = await org_repo.list_users(session, offset=0, limit=1)
    assert departments and roles and users, "集成用例依赖已跑过种子（README §测试）"

    department, role, user = departments[0], roles[0], users[0]
    unit = await unit_factory(
        acl=[
            ("department", department.id),
            ("role", role.id),
            ("user", user.id),
        ]
    )

    async with SessionLocal() as session:
        items, _ = await knowledge_service.list_units(
            session, AclSubject(user_id=uuid4()), offset=0, limit=100, bypass_acl=True
        )

    row = next(item for item in items if item.unit_id == unit.id)
    assert row.acl_departments == [department.name]
    assert row.acl_roles == [role.name]
    assert row.acl_users == [user.display_name]
    assert row.acl_global is False


async def test_acl_label_falls_back_to_short_id(unit_factory) -> None:
    """主体已被删时退化成短 id，而不是静默消失——少一条标签会让人以为权限被改了。"""

    ghost = uuid4()
    unit = await unit_factory(acl=[("user", ghost)])

    async with SessionLocal() as session:
        detail = await knowledge_service.get_unit_detail(
            session, AclSubject(user_id=uuid4()), unit.id, bypass_acl=True
        )

    assert detail.acl_users == [str(ghost)[:8]]
    assert detail.acl_departments == []


async def test_acl_view_carries_labels_for_initial_values(unit_factory) -> None:
    """权限弹窗初值要显示名称（PRD §6.3）：只给 id 就只能给用户看 UUID。"""

    async with SessionLocal() as session:
        departments = await org_repo.list_departments(session)
        roles = await org_repo.list_roles(session)
        users, _ = await org_repo.list_users(session, offset=0, limit=1)
    department, role, user = departments[0], roles[0], users[0]
    unit = await unit_factory(
        acl=[
            ("department", department.id),
            ("role", role.id),
            ("user", user.id),
        ]
    )

    async with SessionLocal() as session:
        view = await knowledge_service.get_acl(session, unit.id)

    assert view.departments == [department.id]
    assert [(item.id, item.name) for item in view.labels.departments] == [
        (department.id, department.name)
    ]
    assert [(item.id, item.name) for item in view.labels.roles] == [(role.id, role.name)]
    assert [(item.id, item.name) for item in view.labels.users] == [
        (user.id, user.display_name)
    ]


async def test_save_acl_persists_and_bumps_version(
    unit_factory, fake_acl_redis
) -> None:
    unit = await unit_factory()
    department, role, user = uuid4(), uuid4(), uuid4()

    async with SessionLocal() as session:
        view = await knowledge_service.save_acl(
            session,
            unit.id,
            AclPayload(
                acl_global=False,
                departments=[department, department],
                roles=[role],
                users=[user],
                version=unit.version,
            ),
        )

    assert view.version == unit.version + 1
    assert set(view.departments) == {department}, "重复主体必须去重"
    assert set(view.roles) == {role}
    assert set(view.users) == {user}

    async with SessionLocal() as session:
        again = await knowledge_service.get_acl(session, unit.id)

    assert again.version == unit.version + 1
    assert set(again.departments) == {department}


async def test_save_acl_invalidates_cache(unit_factory, fake_acl_redis) -> None:
    """保存后必须主动删 `kb:acl:unit:{id}`，不能等 TTL（P11）。"""

    owner = uuid4()
    unit = await unit_factory(acl=[("user", owner)])
    key = acl_cache.unit_key(unit.id)

    async with SessionLocal() as session:
        snapshot = await knowledge_service.acl_engine.snapshot([unit.id], session=session)

    assert snapshot[unit.id].users == frozenset({owner})
    assert key in fake_acl_redis.store, "首次读取应回填缓存"

    async with SessionLocal() as session:
        await knowledge_service.save_acl(
            session, unit.id, AclPayload(acl_global=True, version=unit.version)
        )

    assert key not in fake_acl_redis.store, "保存 ACL 后缓存必须立即失效"


async def test_save_acl_with_stale_version_conflicts(
    unit_factory, fake_acl_redis
) -> None:
    unit = await unit_factory()

    async with SessionLocal() as session:
        await knowledge_service.save_acl(
            session, unit.id, AclPayload(acl_global=True, version=unit.version)
        )
        with pytest.raises(AppError) as excinfo:
            await knowledge_service.save_acl(
                session, unit.id, AclPayload(acl_global=False, version=unit.version)
            )

    assert excinfo.value.http_status == 409
    assert excinfo.value.code == ErrorCode.KB_VERSION_CONFLICT


async def test_update_unit_uses_optimistic_lock(unit_factory) -> None:
    unit = await unit_factory()

    async with SessionLocal() as session:
        detail = await knowledge_service.update_unit(
            session, unit.id, UnitUpdate(title="新标题", version=unit.version)
        )

    assert detail.title == "新标题"
    assert detail.version == unit.version + 1

    async with SessionLocal() as session:
        with pytest.raises(AppError) as excinfo:
            await knowledge_service.update_unit(
                session, unit.id, UnitUpdate(title="并发标题", version=unit.version)
            )

    assert excinfo.value.code == ErrorCode.KB_VERSION_CONFLICT


async def test_update_unit_clears_category_with_explicit_null(unit_factory) -> None:
    unit = await unit_factory()

    async with SessionLocal() as session:
        first = await knowledge_service.update_unit(
            session, unit.id, UnitUpdate(category="制度", version=unit.version)
        )
    assert first.category == "制度"

    async with SessionLocal() as session:
        second = await knowledge_service.update_unit(
            session, unit.id, UnitUpdate(category=None, version=first.version)
        )

    assert second.category is None, "显式传 null 表示清空，不传才是保持原值"


async def test_retry_requeues_and_resets_error(unit_factory) -> None:
    unit = await unit_factory(parse_status="failed")
    dispatched: list[list[tuple[UUID, str]]] = []

    async with SessionLocal() as session:
        row = await session.get(KnowledgeUnit, unit.id)
        assert row is not None
        row.parse_error = "MinerU 超时"
        await session.commit()

    async with SessionLocal() as session:
        result = await knowledge_service.retry_unit(
            session, unit.id, dispatcher=dispatched.append
        )

    assert result.parse_status == "queued"
    assert dispatched == [[(unit.id, result.task_id)]]

    async with SessionLocal() as session:
        row = await session.get(KnowledgeUnit, unit.id)
        assert row is not None
        assert row.parse_status == "queued"
        assert row.parse_error is None, "重试必须清掉上一次的错误信息"


@pytest.mark.parametrize("parse_status", ["parsing", "queued"])
async def test_retry_rejected_while_busy(unit_factory, parse_status: str) -> None:
    unit = await unit_factory(parse_status=parse_status)

    async with SessionLocal() as session:
        with pytest.raises(AppError) as excinfo:
            await knowledge_service.retry_unit(
                session, unit.id, dispatcher=lambda _: None
            )

    assert excinfo.value.code == ErrorCode.SYS_VALIDATION


class _FakeEmbedding:
    def __init__(self, count: int) -> None:
        dense = np.zeros((count, EMBED_DIM), dtype=np.float32)
        dense[:, 0] = 1.0
        self.dense = dense
        self.sparse = [{1: 0.5} for _ in range(count)]


def _fake_encode(texts: list[str]) -> _FakeEmbedding:
    return _FakeEmbedding(len(texts))


async def _seed_parent_with_children(unit_id: UUID, contents: Sequence[str]) -> tuple[UUID, list[UUID]]:
    async with SessionLocal() as session:
        parent = Chunk(
            unit_id=unit_id,
            level="parent",
            ordinal=0,
            content="\n\n".join(contents),
            char_count=sum(len(item) for item in contents),
        )
        session.add(parent)
        await session.flush()
        children = [
            Chunk(
                unit_id=unit_id,
                parent_id=parent.id,
                level="child",
                ordinal=index,
                content=text,
                char_count=len(text),
            )
            for index, text in enumerate(contents)
        ]
        session.add_all(children)
        await session.commit()
        return parent.id, [row.id for row in children]


async def test_chunks_preview_lists_children_only(unit_factory) -> None:
    unit = await unit_factory()
    await _seed_parent_with_children(unit.id, ["子块 0", "子块 1"])

    async with SessionLocal() as session:
        preview = await knowledge_service.list_chunks(
            session, unit.id, offset=0, limit=10
        )

    assert preview.child_total == 2
    assert [chunk.ordinal for chunk in preview.chunks] == [0, 1]
    assert all(chunk.level == "child" for chunk in preview.chunks)
    assert all(len(chunk.excerpt) <= 201 for chunk in preview.chunks)
    assert all("父块" not in chunk.excerpt for chunk in preview.chunks)


async def test_update_chunk_reembeds_and_rebuilds_parent(unit_factory) -> None:
    unit = await unit_factory()
    parent_id, child_ids = await _seed_parent_with_children(unit.id, ["旧文甲", "旧文乙"])
    indexed: list[list] = []

    async with SessionLocal() as session:
        detail = await knowledge_service.update_chunk(
            session,
            unit.id,
            child_ids[0],
            ChunkUpdate(content="新的检索正文"),
            encoder=_fake_encode,
            indexer=indexed.append,
        )

    assert detail.content == "新的检索正文"
    assert indexed and indexed[0][0].chunk_id == str(child_ids[0])
    async with SessionLocal() as session:
        child = await session.get(Chunk, child_ids[0])
        parent = await session.get(Chunk, parent_id)
        sibling = await session.get(Chunk, child_ids[1])
        assert child is not None and child.content == "新的检索正文"
        assert sibling is not None and sibling.content == "旧文乙"
        assert parent is not None
        assert parent.content == "新的检索正文\n\n旧文乙"


async def test_update_chunk_encode_failure_does_not_persist(unit_factory) -> None:
    unit = await unit_factory()
    _, child_ids = await _seed_parent_with_children(unit.id, ["保持原样"])

    def boom(_texts: list[str]) -> _FakeEmbedding:
        raise RuntimeError("gpu down")

    async with SessionLocal() as session:
        with pytest.raises(AppError) as excinfo:
            await knowledge_service.update_chunk(
                session,
                unit.id,
                child_ids[0],
                ChunkUpdate(content="不该落库"),
                encoder=boom,
                indexer=lambda _: None,
            )
    assert excinfo.value.http_status == 503

    async with SessionLocal() as session:
        child = await session.get(Chunk, child_ids[0])
        assert child is not None
        assert child.content == "保持原样"


async def test_update_chunk_rejects_empty_and_busy(unit_factory) -> None:
    idle = await unit_factory()
    _, idle_children = await _seed_parent_with_children(idle.id, ["可改"])
    busy = await unit_factory(parse_status="parsing")
    _, busy_children = await _seed_parent_with_children(busy.id, ["解析中"])

    async with SessionLocal() as session:
        with pytest.raises(AppError) as empty:
            await knowledge_service.update_chunk(
                session,
                idle.id,
                idle_children[0],
                ChunkUpdate(content="   "),
                encoder=_fake_encode,
                indexer=lambda _: None,
            )
        with pytest.raises(AppError) as parsing:
            await knowledge_service.update_chunk(
                session,
                busy.id,
                busy_children[0],
                ChunkUpdate(content="改一下"),
                encoder=_fake_encode,
                indexer=lambda _: None,
            )
    assert empty.value.code == ErrorCode.SYS_VALIDATION
    assert parsing.value.code == ErrorCode.SYS_VALIDATION


def _deadlock() -> DBAPIError:
    """真机删除时撞到的那个错：`DELETE FROM knowledge_units` → 40P01。"""

    return DBAPIError(
        "DELETE FROM knowledge_units",
        None,
        asyncpg.exceptions.DeadlockDetectedError("deadlock detected"),
    )


async def test_delete_retries_after_deadlock(unit_factory, monkeypatch) -> None:
    """解析任务正在写切片时删除会撞死锁：回滚重放要能把删除做完，不是丢个 500 给用户。"""

    unit = await unit_factory()
    real_delete = knowledge_repo.delete_unit_row
    rows: list[UUID] = []

    async def flaky_delete(session, row) -> None:
        rows.append(row.id)
        if len(rows) == 1:
            raise _deadlock()
        await real_delete(session, row)

    monkeypatch.setattr(knowledge_repo, "delete_unit_row", flaky_delete)

    async with SessionLocal() as session:
        await knowledge_service.delete_unit(session, unit.id)

    assert len(rows) == 2, "第一次死锁后必须整段重放"
    assert rows[0] == rows[1] == unit.id, "重放时取回的必须是同一个单元"
    async with SessionLocal() as session:
        assert await session.get(KnowledgeUnit, unit.id) is None


async def test_delete_conflict_that_never_clears_becomes_503(
    unit_factory, monkeypatch
) -> None:
    """一直冲突就别再转圈：转 503 让用户稍后重试，且必须回滚干净不留下半删状态。"""

    unit = await unit_factory()

    async def always_deadlock(session, row) -> None:
        raise _deadlock()

    monkeypatch.setattr(knowledge_repo, "delete_unit_row", always_deadlock)

    async with SessionLocal() as session:
        with pytest.raises(AppError) as excinfo:
            await knowledge_service.delete_unit(session, unit.id)

    assert excinfo.value.http_status == 503
    assert excinfo.value.code == ErrorCode.SYS_DEPENDENCY_UNAVAILABLE
    async with SessionLocal() as session:
        assert await session.get(KnowledgeUnit, unit.id) is not None, "冲突未解决时不能删掉行"
