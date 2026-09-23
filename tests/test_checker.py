"""Tests for compatibility rules and error classification."""
from __future__ import annotations


from app.checker import check_compatibility, validate_schema
from app.models import CompatibilityLevel, IncompatibilityCode


def record(*fields, name="U"):
    return {"type": "record", "name": name,
            "fields": [{"name": n, "type": t} if not isinstance(t, dict)
                       and not isinstance(t, tuple)
                       else _field(n, t) for n, t in fields]}


def _field(name, spec):
    if isinstance(spec, tuple):
        ftype, default = spec
        return {"name": name, "type": ftype, "default": default}
    return {"name": name, "type": spec}


BASE = {"type": "record", "name": "U", "fields": [
    {"name": "id", "type": "int"},
    {"name": "name", "type": "string"},
]}


def test_none_accepts_everything(judge):
    cand = {"type": "record", "name": "U", "fields": [
        {"name": "totally", "type": "bytes"}]}
    report = check_compatibility(BASE, cand, CompatibilityLevel.NONE)
    judge("none-accepts", {"existing": BASE, "candidate": cand,
                           "level": "NONE"}, "NONE skips all checks",
          report.compatible)
    assert report.compatible


def test_backward_field_removal_ok(judge):
    cand = {"type": "record", "name": "U", "fields": [
        {"name": "id", "type": "int"}]}
    report = check_compatibility(BASE, cand, CompatibilityLevel.BACKWARD)
    judge("backward-removal", {"candidate": cand},
          "backward: reader ignores writer-only fields", report.compatible)
    assert report.compatible


def test_backward_required_addition_rejected(judge):
    cand = {"type": "record", "name": "U", "fields": BASE["fields"] + [
        {"name": "age", "type": "int"}]}
    report = check_compatibility(BASE, cand, CompatibilityLevel.BACKWARD)
    codes = {i.code for i in report.incompatibilities}
    judge("backward-required-add", {"candidate": cand},
          "backward: reader-only field without default -> FIELD_ADDED_REQUIRED",
          [c.value for c in codes])
    assert not report.compatible
    assert codes == {IncompatibilityCode.FIELD_ADDED_REQUIRED}
    assert report.incompatibilities[0].path == "/age"


def test_backward_optional_addition_ok(judge):
    cand = {"type": "record", "name": "U", "fields": BASE["fields"] + [
        {"name": "age", "type": "int", "default": 0}]}
    report = check_compatibility(BASE, cand, CompatibilityLevel.BACKWARD)
    judge("backward-optional-add", {"candidate": cand},
          "reader-only field with default is fillable", report.compatible)
    assert report.compatible


def test_type_change_categories(judge):
    cases = [
        ("int->long promotes",
         {"type": "record", "name": "U", "fields": [
             {"name": "id", "type": "long"}]}, True),
        ("int->string not promotable",
         {"type": "record", "name": "U", "fields": [
             {"name": "id", "type": "string"}]}, False),
    ]
    for title, cand, expected in cases:
        report = check_compatibility(
            {"type": "record", "name": "U", "fields": [
                {"name": "id", "type": "int"}]},
            cand, CompatibilityLevel.BACKWARD)
        judge(title, {"candidate": cand},
              "Avro promotions int->{long,float,double}", report.compatible)
        assert report.compatible is expected
        if not expected:
            assert report.incompatibilities[0].code is \
                IncompatibilityCode.FIELD_TYPE_CHANGED
            assert report.incompatibilities[0].detail == {
                "from": "int", "to": "string"}


def test_forward_rejects_removal_without_default(judge):
    cand = {"type": "record", "name": "U", "fields": [
        {"name": "id", "type": "int"}]}
    report = check_compatibility(BASE, cand, CompatibilityLevel.FORWARD)
    codes = {i.code for i in report.incompatibilities}
    judge("forward-removal", {"candidate": cand},
          "forward: reader still needs 'name', which had no default",
          [c.value for c in codes])
    assert not report.compatible
    assert IncompatibilityCode.FIELD_REMOVED in codes


def test_forward_removal_ok_when_default_existed(judge):
    existing = {"type": "record", "name": "U", "fields": [
        {"name": "id", "type": "int"},
        {"name": "name", "type": "string", "default": "anon"}]}
    cand = {"type": "record", "name": "U", "fields": [
        {"name": "id", "type": "int"}]}
    report = check_compatibility(existing, cand, CompatibilityLevel.FORWARD)
    judge("forward-removal-default", {"existing": existing, "candidate": cand},
          "removed field had default -> old reader fills it", report.compatible)
    assert report.compatible


def test_full_requires_both_directions(judge):
    # added with default: fine backward; type int->long only promotes one way,
    # so FULL must fail via the forward type check.
    cand = {"type": "record", "name": "U", "fields": [
        {"name": "id", "type": "long"},
        {"name": "name", "type": "string"}]}
    report = check_compatibility(BASE, cand, CompatibilityLevel.FULL)
    codes = {i.code for i in report.incompatibilities}
    judge("full-both-ways", {"candidate": cand},
          "FULL = BACKWARD and FORWARD; long->int is not a promotion",
          [c.value for c in codes])
    assert not report.compatible
    assert codes == {IncompatibilityCode.FIELD_TYPE_CHANGED}


def test_enum_symbol_categories(judge):
    old = {"type": "record", "name": "U", "fields": [{"name": "c", "type": {
        "type": "enum", "name": "Color", "symbols": ["RED", "GREEN"]}}]}
    shrunk = {"type": "record", "name": "U", "fields": [{"name": "c", "type": {
        "type": "enum", "name": "Color", "symbols": ["RED"]}}]}
    grown = {"type": "record", "name": "U", "fields": [{"name": "c", "type": {
        "type": "enum", "name": "Color",
        "symbols": ["RED", "GREEN", "BLUE"]}}]}

    rb = check_compatibility(old, shrunk, CompatibilityLevel.BACKWARD)
    rf = check_compatibility(old, grown, CompatibilityLevel.FORWARD)
    judge("enum-removed", {"old_symbols": ["RED", "GREEN"],
                           "candidate": ["RED"]},
          "backward: writer emits GREEN, reader cannot resolve it",
          [i.code.value for i in rb.incompatibilities])
    judge("enum-added", {"old_symbols": ["RED", "GREEN"],
                         "candidate": ["RED", "GREEN", "BLUE"]},
          "forward: writer may emit BLUE, old reader cannot resolve it",
          [i.code.value for i in rf.incompatibilities])
    assert {i.code for i in rb.incompatibilities} == \
        {IncompatibilityCode.ENUM_SYMBOL_REMOVED}
    assert {i.code for i in rf.incompatibilities} == \
        {IncompatibilityCode.ENUM_SYMBOL_ADDED}


def test_nested_record_path(judge):
    old = {"type": "record", "name": "U", "fields": [{"name": "addr", "type": {
        "type": "record", "name": "Addr",
        "fields": [{"name": "city", "type": "string"}]}}]}
    new = {"type": "record", "name": "U", "fields": [{"name": "addr", "type": {
        "type": "record", "name": "Addr",
        "fields": [{"name": "city", "type": "int"}]}}]}
    report = check_compatibility(old, new, CompatibilityLevel.BACKWARD)
    judge("nested-path", {}, "nested records recurse with field path",
          [(i.path, i.code.value) for i in report.incompatibilities])
    assert report.incompatibilities[0].path == "/addr/city"


def test_invalid_schema_shapes():
    assert validate_schema({"type": "record", "name": "U"}) is not None
    assert validate_schema({"type": "record", "name": "U",
                            "fields": [{"name": "x", "type": "weird"}]}) \
        is not None
    assert validate_schema(BASE) is None
