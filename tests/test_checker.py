"""兼容性规则的分类化测试。"""
from __future__ import annotations

from app.checker import check_compatibility
from app.models import CompatibilityLevel as L
from app.models import IncompatibilityCode as C


def rec(*fields, name: str = "U") -> dict:
    return {"type": "record", "name": name, "fields": list(fields)}


def f(name: str, t, default=..., **extra) -> dict:
    field = {"name": name, "type": t}
    if default is not ...:
        field["default"] = default
    field.update(extra)
    return field


def codes(report) -> set[str]:
    return {item.code.value for item in report.incompatibilities}


def paths(report, code: str) -> set[str]:
    return {item.path for item in report.incompatibilities if item.code.value == code}


def test_backward_allows_optional_addition_and_removal():
    base = rec(f("id", "int"), f("name", "string"))
    candidate = rec(f("id", "int"), f("age", "int", 0))
    report = check_compatibility(base, candidate, L.BACKWARD)
    assert report.compatible, report.to_dict()


def test_backward_required_addition_is_distinguished():
    base = rec(f("id", "int"))
    candidate = rec(f("id", "int"), f("age", "int"))
    report = check_compatibility(base, candidate, L.BACKWARD)
    assert codes(report) == {C.REQUIRED_FIELD_ADDED.value}
    assert paths(report, C.REQUIRED_FIELD_ADDED.value) == {"$.age"}


def test_forward_field_removal_is_distinguished():
    base = rec(f("id", "int"), f("name", "string"))
    candidate = rec(f("id", "int"))
    report = check_compatibility(base, candidate, L.FORWARD)
    assert codes(report) == {C.FIELD_REMOVED.value, C.REQUIRED_FIELD_REMOVED.value}
    # 删除带 default 的字段只算 FIELD_REMOVED
    base2 = rec(f("id", "int"), f("name", "string", "x"))
    report2 = check_compatibility(base2, rec(f("id", "int")), L.FORWARD)
    assert codes(report2) == {C.FIELD_REMOVED.value}


def test_type_change_is_distinguished_and_promotion_directional():
    base = rec(f("id", "int"))
    assert not check_compatibility(base, rec(f("id", "string")), L.BACKWARD).compatible
    # int->long 仅新读旧（BACKWARD）合法；旧读新不合法，故 FULL 不兼容
    assert check_compatibility(base, rec(f("id", "long")), L.BACKWARD).compatible
    assert not check_compatibility(base, rec(f("id", "long")), L.FORWARD).compatible
    assert not check_compatibility(base, rec(f("id", "long")), L.FULL).compatible
    report = check_compatibility(base, rec(f("id", "boolean")), L.BACKWARD)
    assert codes(report) == {C.FIELD_TYPE_CHANGED.value}
    assert report.incompatibilities[0].detail == {"from": "int", "to": "boolean"}


def test_enum_symbol_changes_are_directional():
    e = lambda syms: rec(f("s", {"type": "enum", "name": "S", "symbols": syms}))
    assert check_compatibility(e(["A"]), e(["A", "B"]), L.BACKWARD).compatible
    back = check_compatibility(e(["A", "B"]), e(["A"]), L.BACKWARD)
    assert codes(back) == {C.ENUM_SYMBOL_REMOVED.value}
    fwd = check_compatibility(e(["A"]), e(["A", "B"]), L.FORWARD)
    assert codes(fwd) == {C.ENUM_SYMBOL_ADDED.value}


def test_invalid_schema_classified():
    report = check_compatibility(rec(f("id", "int")), {"type": "record", "name": "X"}, L.BACKWARD)
    assert codes(report) == {C.SCHEMA_INVALID.value}


def test_full_combines_both_directions_and_paths_precise():
    base = rec(f("id", "int"), f("name", "string"))
    candidate = rec(f("id", "int"), f("age", "int"))  # 删 name + 加必填 age
    report = check_compatibility(base, candidate, L.FULL)
    found = codes(report)
    assert C.REQUIRED_FIELD_ADDED.value in found
    assert C.FIELD_REMOVED.value in found
    assert C.REQUIRED_FIELD_REMOVED.value in found


def test_nested_record_removal_path():
    a = {"type": "record", "name": "A", "fields": [f("city", "string"), f("zip", "string")]}
    b = {"type": "record", "name": "A", "fields": [f("city", "string")]}
    report = check_compatibility(rec(f("addr", a)), rec(f("addr", b)), L.FORWARD)
    assert "$.addr.zip" in paths(report, C.FIELD_REMOVED.value)


def test_none_policy_skips_checks():
    base = rec(f("id", "int"))
    assert check_compatibility(base, rec(f("id", "bytes"), f("x", "int")), L.NONE).compatible
