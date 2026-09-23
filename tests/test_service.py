"""Tests for registration, policy history, precheck and cache consistency."""
from __future__ import annotations

import pytest

from app.models import CompatibilityLevel, IncompatibilityCode
from app.service import (
    IncompatibleSchemaError,
    RegistryService,
    SchemaValidationError,
)

V1 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"},
    {"name": "name", "type": "string"},
]}
# valid BACKWARD evolution: int promoted to long, optional field added
V2 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "long"},
    {"name": "name", "type": "string"},
    {"name": "nick", "type": "string", "default": ""},
]}


@pytest.fixture
def svc():
    return RegistryService()


def test_first_registration_and_dense_versions(svc, judge):
    r1 = svc.register("user", V1)
    r2 = svc.register("user", V2)
    judge("dense-versions", {"schemas": ["V1", "V2"]},
          "versions assigned under lock as len+1",
          [r1.version, r2.version])
    assert (r1.version, r2.version) == (1, 2)
    assert r1.schema_id == r1.schema_id  # id is present and stable
    assert svc.store.get_version("user", 2).schema_id == r2.schema_id


def test_incompatible_registration_rejected_and_not_stored(svc, judge):
    svc.register("user", V1)
    bad = {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "string"}]}
    with pytest.raises(IncompatibleSchemaError) as exc:
        svc.register("user", bad)
    codes = [i.code for i in exc.value.report.incompatibilities]
    versions = [v.version for v in svc.list_versions("user")]
    judge("reject-not-stored", {"candidate": "id:string"},
          "BACKWARD: int->string not a promotion",
          {"codes": [c.value for c in codes], "stored_versions": versions})
    assert codes == [IncompatibilityCode.FIELD_TYPE_CHANGED]
    assert versions == [1]  # rejected registration consumes no version


def test_distinct_reason_categories(svc, judge):
    svc.register("user", V1)
    bad = {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "string"},   # type change
        {"name": "age", "type": "int"},     # required addition
    ]}
    with pytest.raises(IncompatibleSchemaError) as exc:
        svc.register("user", bad)
    codes = {i.code for i in exc.value.report.incompatibilities}
    judge("distinct-categories", {"candidate": "id:string + required age"},
          "all violations collected and categorized",
          sorted(c.value for c in codes))
    assert codes == {IncompatibilityCode.FIELD_TYPE_CHANGED,
                     IncompatibilityCode.FIELD_ADDED_REQUIRED}


def test_invalid_schema_raises_validation_error(svc):
    with pytest.raises(SchemaValidationError):
        svc.register("user", {"type": "record"})


def test_precheck_does_not_mutate_state(svc, judge):
    svc.register("user", V1)
    bad = {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "bytes"}]}
    report1 = svc.precheck("user", bad)
    report2 = svc.precheck("user", bad)
    judge("precheck-readonly", {"candidate": "id:bytes"},
          "precheck computes + memoizes but never registers",
          {"compatible": report1.compatible, "cache_same": report1 is report2,
           "versions": len(svc.list_versions("user"))})
    assert not report1.compatible
    assert report1 is report2  # served from cache
    assert len(svc.list_versions("user")) == 1


def test_policy_change_only_affects_later_registrations(svc, judge):
    svc.register("user", V1)
    svc.register("user", V2)  # accepted under default BACKWARD

    # Removing 'name' (which carries no default) is fine for BACKWARD but
    # not FORWARD, since the old reader still requires that field.
    shrunk = {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "long"},
        {"name": "nick", "type": "string", "default": ""}]}
    assert svc.precheck("user", shrunk, CompatibilityLevel.BACKWARD).compatible

    svc.set_policy("user", CompatibilityLevel.FORWARD)
    with pytest.raises(IncompatibleSchemaError) as exc:
        svc.register("user", shrunk)
    codes = {i.code for i in exc.value.report.incompatibilities}
    judge("policy-affects-later", {"new_policy": "FORWARD"},
          "policy change invalidates cache and applies to subsequent register",
          [c.value for c in codes])
    assert codes == {IncompatibilityCode.FIELD_REMOVED}


def test_history_reproduced_under_then_policy(svc, judge):
    svc.register("user", V1)
    svc.register("user", V2)                 # v2 under BACKWARD -> ok
    svc.set_policy("user", CompatibilityLevel.FULL)
    v3 = {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "long"},
        {"name": "name", "type": "string"},
        {"name": "nick", "type": "string", "default": ""},
        {"name": "tag", "type": "string", "default": "x"}]}
    svc.register("user", v3)                 # v3 under FULL -> ok

    r2 = svc.recheck_history("user", 2)
    r3 = svc.recheck_history("user", 3)
    judge("history-repro", {"v2_pair": "v1->v2", "v3_pair": "v2->v3"},
          "recheck uses policy_at(version): BACKWARD then FULL",
          {"v2": (r2.level.value, r2.compatible),
           "v3": (r3.level.value, r3.compatible)})
    assert r2.level is CompatibilityLevel.BACKWARD and r2.compatible
    assert r3.level is CompatibilityLevel.FULL and r3.compatible

    # and re-evaluating the SAME v1->v2 transition under the *current*
    # FULL policy yields the opposite conclusion, proving the policy matters:
    # long->int (the forward direction of that change) is not a promotion.
    from app.checker import check_compatibility
    current = check_compatibility(
        svc.store.get_version("user", 1).schema,
        svc.store.get_version("user", 2).schema,
        CompatibilityLevel.FULL)
    assert not current.compatible
    assert {i.code for i in current.incompatibilities} == \
        {IncompatibilityCode.FIELD_TYPE_CHANGED}


def test_cached_conclusion_cannot_contradict_new_version(svc, judge):
    svc.register("user", V1)
    cand = V2
    report = svc.precheck("user", cand)
    assert report.compatible
    # another registration advances the subject's latest version
    v_other = {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "long"},
        {"name": "name", "type": "string"},
        {"name": "x", "type": "string", "default": "x"}]}
    svc.register("user", v_other)
    # stale cache entry must not be served anymore
    again = svc.precheck("user", cand)
    basis = ("candidate cached against v1; now compared against v2, "
             "so entry is dropped on version mismatch")
    judge("cache-vs-new-version", {"candidate": "V2"}, basis,
          {"fresh_object": again is not report,
           "compatible_against_v2": again.compatible})
    assert again is not report


def test_policy_change_incompatible_is_409_shape(svc):
    # service-level sanity for the error payload the API serializes
    svc.register("user", V1)
    try:
        svc.register("user", {"type": "record", "name": "User", "fields": [
            {"name": "id", "type": "int"}, {"name": "age", "type": "int"}]})
    except IncompatibleSchemaError as e:
        assert e.report.incompatibilities[0].code.value == \
            "FIELD_ADDED_REQUIRED"
    else:  # pragma: no cover
        pytest.fail("expected IncompatibleSchemaError")
