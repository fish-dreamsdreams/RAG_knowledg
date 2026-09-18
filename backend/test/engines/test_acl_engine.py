"""ACL 引擎单测与属性测试（tasklist 8.3）。

覆盖 P1（空 ACL 恒拒绝）、P2（不做部门继承）、缓存命中/失效，以及一条属性：
任意 ACL 组合下，`filter` 的结果与逐条纯判定 `allows` 完全一致。
全部用例不依赖 PostgreSQL 与 Redis（loader 与 redis_factory 注入）。
"""

from __future__ import annotations

import random
from uuid import UUID, uuid4

import pytest

from app.engines.acl import AclEngine, AclSubject, UnitAcl, allows
from app.engines.acl import cache

USER = uuid4()
DEPT = uuid4()
OTHER_DEPT = uuid4()
ROLE = uuid4()
OTHER_ROLE = uuid4()


# ---------- 纯判定：P1 / P2 ----------


def test_empty_acl_denies_everyone() -> None:
    """P1：四维全空恒拒绝，空 ACL 不是"公开"。"""

    acl = UnitAcl()

    assert acl.is_empty
    assert allows(acl, AclSubject(user_id=USER)) is False
    assert (
        allows(acl, AclSubject(user_id=USER, department_id=DEPT, role_ids=[ROLE]))
        is False
    )


def test_acl_global_allows_anyone() -> None:
    assert allows(UnitAcl(acl_global=True), AclSubject(user_id=USER)) is True


def test_each_dimension_grants_on_its_own() -> None:
    assert (
        allows(UnitAcl(departments=frozenset({DEPT})), AclSubject(USER, DEPT)) is True
    )
    assert (
        allows(UnitAcl(roles=frozenset({ROLE})), AclSubject(USER, None, [ROLE]))
        is True
    )
    assert allows(UnitAcl(users=frozenset({USER})), AclSubject(USER)) is True


def test_non_matching_subject_is_denied() -> None:
    acl = UnitAcl(
        departments=frozenset({DEPT}),
        roles=frozenset({ROLE}),
        users=frozenset({USER}),
    )

    stranger = AclSubject(uuid4(), uuid4(), [uuid4()])

    assert allows(acl, stranger) is False


def test_missing_acl_is_denied() -> None:
    assert allows(None, AclSubject(user_id=USER)) is False


def test_parent_department_does_not_cover_child() -> None:
    """P2：只比直属部门，不做层级继承——给父部门配的 ACL 不下发给子部门成员。"""

    parent, child = uuid4(), uuid4()
    acl = UnitAcl(departments=frozenset({parent}))

    assert allows(acl, AclSubject(USER, child)) is False
    assert allows(acl, AclSubject(USER, parent)) is True


def test_subject_without_department_never_matches_department_dimension() -> None:
    acl = UnitAcl(departments=frozenset({DEPT}))

    assert allows(acl, AclSubject(USER, None)) is False


def test_role_ids_accepts_plain_list() -> None:
    """`api.deps.CurrentUser.role_ids` 是 list，判定必须照常工作。"""

    acl = UnitAcl(roles=frozenset({ROLE, OTHER_ROLE}))

    assert allows(acl, AclSubject(USER, None, [OTHER_ROLE])) is True


# ---------- 缓存序列化（8.2 / P11） ----------


def test_cache_roundtrip() -> None:
    acl = UnitAcl(
        acl_global=True,
        departments=frozenset({DEPT}),
        roles=frozenset({ROLE}),
        users=frozenset({USER}),
    )

    assert cache.load(cache.dump(acl)) == acl


def test_cache_dump_is_stable() -> None:
    acl = UnitAcl(departments=frozenset({DEPT, OTHER_DEPT}))

    assert cache.dump(acl) == cache.dump(acl)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "{}",
        '{"acl_global": true}',
        '{"acl_global": true, "departments": ["nope"], "roles": [], "users": []}',
        '{"acl_global": true, "departments": [], "roles": [], "users": 3}',
    ],
)
def test_broken_cache_is_treated_as_miss(raw: str) -> None:
    assert cache.load(raw) is None


# ---------- 引擎：批量判定与缓存 ----------


class FakeRedis:
    """只实现引擎用到的最小接口，避免为单测引入真实 Redis。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(key) for key in keys]

    async def delete(self, *keys: str) -> int:
        self.deleted.extend(keys)
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


class BrokenRedis(FakeRedis):
    async def mget(self, keys: list[str]) -> list[str | None]:
        raise RuntimeError("redis down")


def _engine(acls: dict[UUID, UnitAcl], redis: FakeRedis | None = None) -> AclEngine:
    async def loader(
        session: object, unit_ids: list[UUID]
    ) -> dict[UUID, UnitAcl]:
        return {unit_id: acls[unit_id] for unit_id in unit_ids if unit_id in acls}

    return AclEngine(loader=loader, redis_factory=lambda: redis)


async def test_filter_partitions_input() -> None:
    allowed_unit, denied_unit, unknown_unit = uuid4(), uuid4(), uuid4()
    engine = _engine(
        {
            allowed_unit: UnitAcl(users=frozenset({USER})),
            denied_unit: UnitAcl(users=frozenset({uuid4()})),
        },
        FakeRedis(),
    )

    allowed, denied = await engine.filter(
        AclSubject(user_id=USER), [allowed_unit, denied_unit, unknown_unit]
    )

    assert allowed == [allowed_unit]
    # 查不到的单元也进 denied：宁可少给，不能因为查不到而放行
    assert denied == [denied_unit, unknown_unit]


async def test_filter_deduplicates_and_keeps_order() -> None:
    first, second = uuid4(), uuid4()
    engine = _engine(
        {
            first: UnitAcl(users=frozenset({USER})),
            second: UnitAcl(users=frozenset({USER})),
        },
        FakeRedis(),
    )

    allowed, denied = await engine.filter(
        AclSubject(user_id=USER), [second, first, second]
    )

    assert allowed == [second, first]
    assert denied == []


async def test_check_agrees_with_filter() -> None:
    unit = uuid4()
    engine = _engine({unit: UnitAcl(users=frozenset({USER}))}, FakeRedis())

    assert await engine.check(AclSubject(user_id=USER), unit) is True
    assert await engine.check(AclSubject(user_id=uuid4()), unit) is False


async def test_snapshot_uses_cache_and_invalidate_clears_it() -> None:
    redis = FakeRedis()
    calls: list[list[UUID]] = []
    unit = uuid4()

    async def loader(session: object, unit_ids: list[UUID]) -> dict[UUID, UnitAcl]:
        calls.append(list(unit_ids))
        return {item: UnitAcl(users=frozenset({USER})) for item in unit_ids}

    engine = AclEngine(loader=loader, redis_factory=lambda: redis)

    assert (await engine.snapshot([unit]))[unit].users == frozenset({USER})
    assert len(calls) == 1

    # 第二次命中缓存，不再回源
    await engine.snapshot([unit])
    assert len(calls) == 1

    await engine.invalidate(unit)
    assert cache.unit_key(unit) in redis.deleted

    await engine.snapshot([unit])
    assert len(calls) == 2


async def test_missing_unit_is_not_cached() -> None:
    """不存在的单元不写缓存，避免把穿透结果固化下来。"""

    redis = FakeRedis()

    async def loader(session: object, unit_ids: list[UUID]) -> dict[UUID, UnitAcl]:
        return {}

    engine = AclEngine(loader=loader, redis_factory=lambda: redis)

    await engine.snapshot([uuid4()])

    assert redis.store == {}


async def test_cache_failure_falls_back_to_loader() -> None:
    unit = uuid4()
    engine = _engine({unit: UnitAcl(users=frozenset({USER}))}, BrokenRedis())

    snapshot = await engine.snapshot([unit])

    assert snapshot[unit].users == frozenset({USER})


async def test_snapshot_without_unit_ids_needs_no_loader() -> None:
    class ExplodingRedis(FakeRedis):
        async def mget(self, keys: list[str]) -> list[str | None]:
            raise AssertionError("空入参不该访问缓存")

    engine = AclEngine(redis_factory=ExplodingRedis)

    assert await engine.snapshot([]) == {}


# ---------- 属性测试：filter ≡ 逐条 allows ----------


def _random_acl(rng: random.Random, pool: list[UUID]) -> UnitAcl:
    def pick() -> frozenset[UUID]:
        return frozenset(item for item in pool if rng.random() < 0.3)

    return UnitAcl(
        acl_global=rng.random() < 0.2,
        departments=pick(),
        roles=pick(),
        users=pick(),
    )


def _random_subject(rng: random.Random, pool: list[UUID]) -> AclSubject:
    return AclSubject(
        user_id=rng.choice(pool),
        department_id=rng.choice([*pool, None]),
        role_ids=[item for item in pool if rng.random() < 0.3],
    )


async def test_filter_matches_per_unit_allows_property() -> None:
    """任意 ACL 组合下，批量 filter 与逐条纯判定必须完全一致。"""

    rng = random.Random(20260915)
    pool = [uuid4() for _ in range(6)]

    for round_index in range(200):
        acls = {unit_id: _random_acl(rng, pool) for unit_id in pool}
        engine = _engine(acls, FakeRedis() if round_index % 2 else None)
        subject = _random_subject(rng, pool)
        unit_ids = [rng.choice(pool) for _ in range(rng.randint(1, len(pool)))]

        allowed, denied = await engine.filter(subject, unit_ids)

        unique = list(dict.fromkeys(unit_ids))
        expected = [item for item in unique if allows(acls[item], subject)]
        assert allowed == expected, (round_index, subject, acls)
        assert denied == [item for item in unique if item not in allowed]
        assert sorted(allowed + denied, key=str) == sorted(unique, key=str)


async def test_check_matches_filter_property() -> None:
    rng = random.Random(7)
    pool = [uuid4() for _ in range(5)]

    for _ in range(100):
        acls = {unit_id: _random_acl(rng, pool) for unit_id in pool}
        engine = _engine(acls)
        subject = _random_subject(rng, pool)
        target = rng.choice(pool)

        allowed, _ = await engine.filter(subject, [target])

        assert await engine.check(subject, target) == bool(allowed)


async def test_acl_global_is_always_allowed_property() -> None:
    """P1 的补充：只要 acl_global 为真，任何身份都必须放行。"""

    rng = random.Random(11)
    pool = [uuid4() for _ in range(4)]

    for _ in range(50):
        acl = _random_acl(rng, pool)
        acl = UnitAcl(
            acl_global=True,
            departments=acl.departments,
            roles=acl.roles,
            users=acl.users,
        )
        engine = _engine({unit: acl for unit in pool})

        allowed, _ = await engine.filter(_random_subject(rng, pool), pool)

        assert allowed == pool
