"""注册中心：注册、预检、错误分类、策略变更与历史复现。"""
from __future__ import annotations

import threading

import pytest

from app.models import (
    CompatibilityLevel,
    IncompatibilityCode,
    InvalidSchemaError,
    SchemaIncompatibleError,
    SubjectNotFoundError,
)


def rec(*fields, name: str = "User") -> dict:
    return {"type": "record", "name": name, "fields": list(fields)}


def field(name: str, t, default=...) -> dict:
    out = {"name": name, "type": t}
    if default is not ...:
        out["default"] = default
    return out


def test_register_assigns_continuous_versions_and_dedups(registry):
    r1 = registry.register("users", rec(field("id", "int")))
    r2 = registry.register(
        "users", rec(field("id", "int"), field("email", "string", "a@b.c"))
    )
    assert (r1.version, r2.version) == (1, 2)
    assert r1.fingerprint != r2.fingerprint
    dup = registry.register(
        "users", rec(field("id", "int"), field("email", "string", "a@b.c"))
    )
    assert dup.already_registered and dup.version == 2
    assert [v.version for v in registry.list_versions("users")] == [1, 2]


def test_incompatible_registration_rejected_with_distinguishable_codes(registry):
    registry.register("users", rec(field("id", "int"), field("name", "string")))
    # FULL 策略下同时触发：必填新增 + 字段删除（必填），两类原因可区分
    registry.set_policy("users", CompatibilityLevel.FULL.value)
    candidate = rec(field("id", "int"), field("age", "int"))
    with pytest.raises(SchemaIncompatibleError) as exc:
        registry.register("users", candidate)
    error = exc.value
    assert error.http_status == 409
    codes = {i.code.value for i in error.report.incompatibilities}
    assert IncompatibilityCode.REQUIRED_FIELD_ADDED.value in codes
    assert IncompatibilityCode.FIELD_REMOVED.value in codes
    assert IncompatibilityCode.REQUIRED_FIELD_REMOVED.value in codes
    paths = {i.path for i in error.report.incompatibilities}
    assert "$.name" in paths and "$.age" in paths
    # 被拒绝后状态不变
    assert [v.version for v in registry.list_versions("users")] == [1]


def test_invalid_schema_is_a_separate_error_class(registry):
    with pytest.raises(InvalidSchemaError) as exc:
        registry.register("users", {"type": "record", "fields": []})
    assert exc.value.code == "INVALID_SCHEMA"
    with pytest.raises(SubjectNotFoundError):
        registry.latest("users")


def test_precheck_is_readonly_and_explains_sources(registry):
    registry.register("users", rec(field("id", "int"), field("name", "string")))
    candidate = rec(field("id", "string"))
    report = registry.precheck("users", candidate)
    assert report.compatible is False
    codes = {i.code.value for i in report.incompatibilities}
    assert codes == {IncompatibilityCode.FIELD_TYPE_CHANGED.value}
    # 预检不产生版本
    assert [v.version for v in registry.list_versions("users")] == [1]
    # 空主体预检视为可注册，且不创建主体
    empty = registry.precheck("brand-new", rec(field("id", "int")))
    assert empty.compatible is True
    with pytest.raises(SubjectNotFoundError):
        registry.latest("brand-new")


def test_concurrent_registration_versions_are_continuous_and_unique(registry):
    base = rec(field("id", "int"))
    registry.register("orders", base)

    def make(i: int) -> dict:
        return rec(field("id", "int"), field(f"f{i}", "int", i))

    results: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            outcome = registry.register("orders", make(i))
            results.append(outcome.version)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert sorted(results) == list(range(2, 10))
    versions = [v.version for v in registry.list_versions("orders")]
    assert versions == list(range(1, 10))


def test_concurrent_incompatible_and_compatible_registrations(registry):
    registry.register("p", rec(field("id", "int")))
    barrier = threading.Barrier(3)
    accepted: list[int] = []
    rejected: list[str] = []

    def candidate_compatible() -> None:
        barrier.wait()
        try:
            accepted.append(
                registry.register("p", rec(field("id", "int"), field("a", "int", 0))).version
            )
        except SchemaIncompatibleError:
            rejected.append("compatible_one")

    def candidate_bad_type() -> None:
        barrier.wait()
        try:
            registry.register("p", rec(field("id", "string")))
        except SchemaIncompatibleError:
            rejected.append("bad_type")

    def candidate_required() -> None:
        barrier.wait()
        try:
            registry.register("p", rec(field("id", "int"), field("b", "int")))
        except SchemaIncompatibleError:
            rejected.append("required_add")

    threads = [
        threading.Thread(target=candidate_compatible),
        threading.Thread(target=candidate_bad_type),
        threading.Thread(target=candidate_required),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert accepted == [2]
    assert set(rejected) == {"bad_type", "required_add"}
    assert [v.version for v in registry.list_versions("p")] == [1, 2]


def test_policy_change_only_affects_future_registrations_and_history_reproduces(registry):
    # 默认 BACKWARD：v1/v2 登记
    registry.register("svc", rec(field("id", "int"), field("name", "string")))
    registry.register(
        "svc", rec(field("id", "int"), field("name", "string"), field("tag", "string", ""))
    )
    # BACKWARD 下 v2 新增带默认字段兼容
    registry.register(
        "svc", rec(field("id", "int"), field("name", "string"), field("tag", "string", ""))
    )
    # 演示“同一变更在不同策略下结论相反”：新增必填字段 age
    growth = rec(field("id", "int"), field("name", "string"), field("tag", "string", ""), field("age", "int"))
    # BACKWARD 拒绝：必填新增
    with pytest.raises(SchemaIncompatibleError) as exc_info:
        registry.register("svc", growth)
    reject_codes = {i.code.value for i in exc_info.value.report.incompatibilities}
    assert reject_codes == {IncompatibilityCode.REQUIRED_FIELD_ADDED.value}
    # 被拒绝不落版本
    assert [v.version for v in registry.list_versions("svc")] == [1, 2]
    # FORWARD 允许新增字段，登记为 v3
    registry.set_policy("svc", CompatibilityLevel.FORWARD.value)
    v3 = registry.register("svc", growth)
    assert v3.version == 3
    # 历史复现：v2 按当时 BACKWARD 兼容；v3 快照为 FORWARD 兼容，强制 BACKWARD 复现则不兼容
    assert registry.reproduce("svc", 2).compatible is True
    assert registry.reproduce("svc", 2).level == CompatibilityLevel.BACKWARD
    assert registry.reproduce("svc", 3).compatible is True
    assert registry.reproduce("svc", 3).level == CompatibilityLevel.FORWARD
    replayed = registry.reproduce("svc", 3, CompatibilityLevel.BACKWARD)
    assert replayed.compatible is False
    assert {i.code.value for i in replayed.incompatibilities} == {
        IncompatibilityCode.REQUIRED_FIELD_ADDED.value
    }
    assert registry.reproduce("svc", 1).compatible is True  # 首版本永远兼容
    # 策略历史可读：FORWARD(rev1)
    history = registry.list_policy_history("svc")
    assert [p.level for p in history] == [CompatibilityLevel.FORWARD]


def test_global_policy_change_influences_subject_without_override(registry):
    assert registry.get_policy().level == CompatibilityLevel.BACKWARD
    registry.register("g", rec(field("id", "int"), field("name", "string")))
    registry.set_global_policy(CompatibilityLevel.NONE.value)
    assert registry.get_policy("g").level == CompatibilityLevel.NONE
    result = registry.register("g", rec(field("id", "bytes")))
    assert result.version == 2


def test_cached_precheck_verdict_stays_consistent_with_latest_after_policy_change(registry):
    """同一候选：缓存命中后，策略变更或新版本登记，结论必须以最新状态重算。"""
    registry.register("c", rec(field("id", "int")))
    candidate = rec(field("id", "int"), field("name", "string"))
    # FORWARD 下新增字段合法
    registry.set_policy("c", CompatibilityLevel.FORWARD.value)
    first = registry.precheck("c", candidate)
    second = registry.precheck("c", candidate)
    assert first.compatible and second.compatible
    # 切到 BACKWARD：新增必填字段非法，旧缓存不得返回兼容
    registry.set_policy("c", CompatibilityLevel.BACKWARD.value)
    after_policy = registry.precheck("c", candidate)
    assert after_policy.compatible is False
    codes = {i.code.value for i in after_policy.incompatibilities}
    assert codes == {IncompatibilityCode.REQUIRED_FIELD_ADDED.value}
    # 登记一个带 default name 的新版本，使候选变为删除字段；缓存也必须以新版本为基线
    registry.set_policy("c", CompatibilityLevel.FORWARD.value)
    registry.register(
        "c", rec(field("id", "int"), field("name", "string", "x"), field("age", "int", 0))
    )
    latest_report = registry.precheck("c", candidate)
    assert latest_report.baseline_fingerprint == registry.latest("c").fingerprint
    codes = {i.code.value for i in latest_report.incompatibilities}
    assert codes == {IncompatibilityCode.FIELD_REMOVED.value}


def test_state_persists_across_restart(tmp_path):
    from app.audit import AuditLogger
    from app.registry import SchemaRegistry
    from app.store import LocalStore

    db = tmp_path / "db.json"
    reg = SchemaRegistry(LocalStore(str(db)), audit=AuditLogger("restart.a"))
    reg.register("u", rec(field("id", "int")))
    reg.set_policy("u", CompatibilityLevel.FULL.value)

    reloaded = SchemaRegistry(LocalStore(str(db)), audit=AuditLogger("restart.b"))
    assert reloaded.latest("u").version == 1
    assert reloaded.get_policy("u").level == CompatibilityLevel.FULL
    assert reloaded.reproduce("u", 1).compatible is True


def test_audit_log_carries_input_basis_and_conclusion(registry):
    registry.register("z", rec(field("id", "int")))
    registry.precheck("z", rec(field("id", "string")))
    events = {e["event"] for e in registry.audit.entries()}
    assert "register_accepted" in events and "precheck" in events
    precheck_entries = [e for e in registry.audit.entries() if e["event"] == "precheck"]
    entry = precheck_entries[-1]
    assert "input_schema" in entry
    assert entry["mutated_state"] is False
    assert entry["conclusion"] == "incompatible"
    assert entry["incompatibilities"][0]["code"] == IncompatibilityCode.FIELD_TYPE_CHANGED.value
