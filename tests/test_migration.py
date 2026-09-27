"""Migration tests: export/import round-trip fidelity.

Covers versions, per-version effective policy and historical conclusions
being reproduced byte-for-conclusion on a fresh instance.
"""
from __future__ import annotations

import pytest

from app.models import CompatibilityLevel
from app.service import RegistryService

V1 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"},
    {"name": "name", "type": "string"},
]}
V2_NONE = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "bytes"},   # would break BACKWARD/FORWARD/FULL
    {"name": "name", "type": "string"},
    {"name": "nick", "type": "int"},
]}
V3_FULL = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "bytes"},
    {"name": "name", "type": "string"},
    {"name": "nick", "type": "int"},
    {"name": "tag", "type": "string", "default": "x"},
]}


def _populated() -> RegistryService:
    """v1 under BACKWARD(default), v2 under NONE (a breaking pair),
    v3 under FULL with one optional field added."""
    svc = RegistryService()
    svc.register("user", V1)
    svc.set_policy("user", CompatibilityLevel.NONE)
    svc.register("user", V2_NONE)
    svc.set_policy("user", CompatibilityLevel.FULL)
    svc.register("user", V3_FULL)
    svc.register("order", {"type": "record", "name": "Order", "fields": [
        {"name": "oid", "type": "long"}]})
    return svc


@pytest.fixture
def source():
    return _populated()


def test_roundtrip_reproduces_versions_policies_conclusions(source, judge):
    data = source.export_subjects(["user"])
    dst = RegistryService()
    dst.import_package(data)

    src_records = source.list_versions("user")
    dst_records = dst.list_versions("user")

    # versions: numbering, content, ids and timestamps all preserved
    assert [r.version for r in dst_records] == [1, 2, 3]
    assert [(r.schema, r.schema_id, r.registered_at) for r in dst_records] == \
           [(r.schema, r.schema_id, r.registered_at) for r in src_records]

    # policy history: explicit changes keep their coordinates, and the
    # source's implicit default (BACKWARD@1) is materialized explicitly so
    # a target with a different instance default still reproduces history.
    assert [(p.level, p.effective_version)
            for p in dst.store.list_policies("user")] == [
        (CompatibilityLevel.BACKWARD, 1),
        (CompatibilityLevel.NONE, 2),
        (CompatibilityLevel.FULL, 3)]
    for v in (1, 2, 3):
        assert dst.store.policy_at("user", v).level == \
               source.store.policy_at("user", v).level

    # historical conclusions reproduce under the then-effective policy
    reproduced = {v: dst.recheck_history("user", v) for v in (1, 2, 3)}
    basis = ("v1 BACKWARD(ok), v2 NONE(ok despite breaking shape), "
             "v3 FULL(optional field addition, ok)")
    judge("roundtrip-fidelity",
          {"subject": "user", "versions": 3,
           "policies": ["BACKWARD@1", "NONE@2", "FULL@3"]},
          basis,
          {v: (r.level.value, r.compatible) for v, r in reproduced.items()})
    assert reproduced[1].level is CompatibilityLevel.BACKWARD
    assert reproduced[1].compatible
    assert reproduced[2].level is CompatibilityLevel.NONE
    assert reproduced[2].compatible
    assert reproduced[3].level is CompatibilityLevel.FULL
    assert reproduced[3].compatible


def test_recheck_on_source_and_target_identical(source, judge):
    data = source.export_subjects(["user"])
    dst = RegistryService()
    dst.import_package(data)
    for v in (1, 2, 3):
        a = source.recheck_history("user", v)
        b = dst.recheck_history("user", v)
        assert (a.level, a.compatible,
                [(i.code.value, i.path) for i in a.incompatibilities]) == \
               (b.level, b.compatible,
                [(i.code.value, i.path) for i in b.incompatibilities])
    judge("recheck-identical", {"versions": [1, 2, 3]},
          "same schemas + same policy history => same report object content",
          "all three rechecks match level/compatible/codes/paths")


def test_export_subset_does_not_carry_other_subjects(source, judge):
    data = source.export_subjects(["user"])
    dst = RegistryService()
    dst.import_package(data)
    judge("export-subset", {"requested": ["user"], "source_has": ["order", "user"]},
          "only named subjects are serialized",
          {"dst_subjects": dst.store.list_subjects()})
    assert dst.store.list_subjects() == ["user"]


def test_export_all_subjects(source, judge):
    data = source.export_subjects()
    dst = RegistryService()
    dst.import_package(data)
    judge("export-all", None, "None means every subject",
          dst.store.list_subjects())
    assert dst.store.list_subjects() == ["order", "user"]


def test_export_unknown_subject_is_keyerror(source):
    with pytest.raises(KeyError):
        source.export_subjects(["ghost"])


def test_default_policy_subject_roundtrips(judge):
    """A subject that never had an explicit set_policy still carries its
    then-effective default policy after import (no 'only versions' import)."""
    svc = RegistryService()
    svc.register("plain", V1)
    dst = RegistryService()
    dst.import_package(svc.export_subjects(["plain"]))
    policy = dst.store.policy_at("plain", 1)
    judge("default-policy-roundtrip", {"explicit_policies": 0},
          "implicit default is synthesized into an explicit v1 record",
          policy.level.value)
    assert policy.level is CompatibilityLevel.BACKWARD
    assert dst.recheck_history("plain", 1).level is CompatibilityLevel.BACKWARD


# ---------------------------------------------------------------------------
# Conflict and merge rules
# ---------------------------------------------------------------------------

S1 = {"type": "record", "name": "S", "fields": [{"name": "id", "type": "int"}]}
S2 = {"type": "record", "name": "S", "fields": [
    {"name": "id", "type": "int"},
    {"name": "a", "type": "string", "default": ""}]}
S3 = {"type": "record", "name": "S", "fields": [
    {"name": "id", "type": "int"},
    {"name": "a", "type": "string", "default": ""},
    {"name": "b", "type": "string", "default": ""}]}


def _pkg(schemas):
    svc = RegistryService()
    for s in schemas:
        svc.register("s", s)
    return svc.export_subjects(["s"])


def test_reject_mode_refuses_existing_even_when_identical(judge):
    dst = RegistryService()
    dst.import_package(_pkg([S1]))
    from app.migration import ImportConflictError
    with pytest.raises(ImportConflictError) as exc:
        dst.import_package(_pkg([S1]))  # default mode REJECT
    judge("reject-identical", {"target": ["v1"], "incoming": ["v1"]},
          "REJECT refuses any name collision, identical content included",
          {"content_differs": exc.value.content_differs})
    assert exc.value.content_differs is False


def test_merge_idempotent_no_duplicates(judge):
    dst = RegistryService()
    dst.import_package(_pkg([S1, S2]))
    r = dst.import_package(_pkg([S1, S2]), "MERGE")
    judge("merge-idempotent", {"target": [1, 2], "incoming": [1, 2]},
          "same content at same number = already present; nothing appended",
          {"added": r[0].added_versions, "skipped": r[0].skipped_versions,
           "versions": [v.version for v in dst.list_versions("s")]})
    assert r[0].added_versions == ()
    assert list(r[0].skipped_versions) == [1, 2]
    assert [v.version for v in dst.list_versions("s")] == [1, 2]


def test_merge_appends_only_missing_versions(judge):
    dst = RegistryService()
    dst.import_package(_pkg([S1]))
    r = dst.import_package(_pkg([S1, S2, S3]), "MERGE")
    judge("merge-append", {"target": [1], "incoming": [1, 2, 3]},
          "prefix equal -> skip; dense append of v2,v3",
          {"added": r[0].added_versions,
           "versions": [v.version for v in dst.list_versions("s")]})
    assert list(r[0].added_versions) == [2, 3]
    recs = dst.list_versions("s")
    assert [v.version for v in recs] == [1, 2, 3]
    assert recs[1].schema == S2 and recs[2].schema == S3
    assert recs[0].schema == S1  # target version untouched


def test_merge_conflict_same_number_different_content(judge):
    from app.migration import ImportConflictError
    dst = RegistryService()
    dst.import_package(_pkg([S1]))
    other_v2_content = {"type": "record", "name": "S", "fields": [
        {"name": "id", "type": "int"},
        {"name": "x", "type": "string", "default": ""}]}
    # package v1 identical, v2 differs from target's future... build a
    # target with [S1, S2] and incoming [S1, other_v2_content]
    dst.import_package(_pkg([S1, S2]), "MERGE")
    pkg = _pkg([S1, other_v2_content])
    with pytest.raises(ImportConflictError) as exc:
        dst.import_package(pkg, "MERGE")
    versions = [v.version for v in dst.list_versions("s")]
    judge("merge-conflict",
          {"target_v2": "field a", "incoming_v2": "field x"},
          "same version number, different content => conflict",
          {"conflict_version": exc.value.version, "versions_after": versions})
    assert exc.value.version == 2
    assert versions == [1, 2]  # unchanged


def test_reject_mode_rolls_back_all_subjects(judge):
    """Multi-subject package: a later collision must undo the earlier
    subject's create — no half-imported state."""
    src = RegistryService()
    src.register("a", S1)
    src.register("b", S1)
    data = src.export_subjects()  # [a, b]

    dst = RegistryService()
    dst.register("b", S2)  # target b diverges (has v2 too, v1 identical)
    # make b v1 different to force conflict
    dst2 = RegistryService()
    b1_different = {"type": "record", "name": "S",
                    "fields": [{"name": "id", "type": "long"}]}
    dst2.register("b", b1_different)
    from app.migration import ImportConflictError
    with pytest.raises(ImportConflictError):
        dst2.import_package(data)  # REJECT: b collides
    judge("reject-rollback", {"package": ["a", "b"], "target_has": ["b"]},
          "all planning happens before any write; collision on b rejects a too",
          {"subjects_after": dst2.store.list_subjects()})
    assert dst2.store.list_subjects() == ["b"]
    assert dst2.store.get_version("b", 1).schema == b1_different


def test_merge_batch_atomicity_on_second_subject(judge):
    """MERGE: first subject would append cleanly but the second conflicts;
    the whole batch must be rolled back."""
    src = RegistryService()
    src.register("a", S1)
    src.register("a", S2)
    src.register("b", S1)
    data = src.export_subjects()  # a:[1,2], b:[1]

    dst = RegistryService()
    dst.register("b", {"type": "record", "name": "S",
                       "fields": [{"name": "id", "type": "string"}]})  # v1 differs
    from app.migration import ImportConflictError
    with pytest.raises(ImportConflictError) as exc:
        dst.import_package(data, "MERGE")
    judge("merge-batch-atomic", {"package": ["a:1,2", "b:1"]},
          "plan a(append) + b(conflict) => nothing committed",
          {"conflict": exc.value.subject,
           "subjects_after": dst.store.list_subjects()})
    assert exc.value.subject == "b"
    assert "a" not in dst.store.list_subjects()
    assert [v.version for v in _versions_of(dst, "b")] == [1]


def test_unmigrated_subjects_never_touched(judge):
    src = RegistryService()
    src.register("a", S1)
    data = src.export_subjects()

    dst = RegistryService()
    dst.register("keep", S1)
    dst.register("keep", S2)
    dst.import_package(data)
    recs = _versions_of(dst, "keep")
    judge("untouched-subjects", {"migrated": ["a"], "resident": ["keep"]},
          "import only names its own subjects",
          {"keep_versions": [v.version for v in recs],
           "keep_v2": recs[1].schema})
    assert [v.version for v in recs] == [1, 2]
    assert recs[1].schema == S2


def _versions_of(svc, subject):
    return svc.list_versions(subject)


def test_repeated_full_package_import_merge_keeps_dense_versions(judge):
    """Importing the same package three times under MERGE never duplicates
    or gaps version numbers."""
    data = _pkg([S1, S2, S3])
    dst = RegistryService()
    for _ in range(3):
        dst.import_package(data, "MERGE")
    versions = [v.version for v in dst.list_versions("s")]
    judge("repeated-import", {"rounds": 3},
          "identical prefix skipped each time => idempotent",
          versions)
    assert versions == [1, 2, 3]


# ---------------------------------------------------------------------------
# MERGE edge cases: target/source version-set asymmetries, policy rewrite
# ---------------------------------------------------------------------------


def test_merge_when_target_has_more_versions_is_noop(judge):
    """target [v1..v3], incoming [v1,v2] identical prefix => nothing added."""
    dst = RegistryService()
    dst.import_package(_pkg([S1, S2, S3]))
    r = dst.import_package(_pkg([S1, S2]), "MERGE")
    versions = [v.version for v in dst.list_versions("s")]
    judge("merge-target-ahead", {"target": 3, "incoming": 2},
          "incoming is a prefix => no writes, target policy history untouched",
          {"added": r[0].added_versions, "versions": versions,
           "policies": [(p.level.value, p.effective_version)
                        for p in dst.store.list_policies("s")]})
    assert list(r[0].added_versions) == []
    assert versions == [1, 2, 3]


def test_merge_appended_policy_coordinates_rewritten(judge):
    """Source policy change in the appended range keeps its relative
    coordinate; baseline policy for the first appended version is carried."""
    # source: v1 BACKWARD; NONE from v2 (lets v2 be a breaking shape); and
    # a still-pending FORWARD change made after v2 for its next registration.
    breaking_v2 = {"type": "record", "name": "S", "fields": [
        {"name": "id", "type": "bytes"}]}
    src_svc = RegistryService()
    src_svc.register("s", S1)
    src_svc.set_policy("s", CompatibilityLevel.NONE)
    src_svc.register("s", breaking_v2)
    src_svc.set_policy("s", CompatibilityLevel.FORWARD)  # pending at v3
    pkg = src_svc.export_subjects(["s"])

    dst = RegistryService()
    dst.import_package(_pkg([S1]))                 # target only has v1
    r = dst.import_package(pkg, "MERGE")           # append v2 from source
    versions = [v.version for v in dst.list_versions("s")]
    policies = [(p.level.value, p.effective_version)
                for p in dst.store.list_policies("s")]
    # v2's historical conclusion on target must reproduce as NONE/compatible,
    # even though the target's own default is BACKWARD.
    report = dst.recheck_history("s", 2)
    judge("merge-policy-rewrite",
          {"target": ["v1@BACKWARD"],
           "source": ["v1@BACKWARD", "v2@NONE", "pending FORWARD@v3"]},
          "appended range carries governing NONE; source's pending policy is "
          "dropped so it cannot override the target's own future intent; "
          "current policy rests at the last landed policy NONE@v2",
          {"added": r[0].added_versions, "versions": versions,
           "policies": policies,
           "v2_recheck": (report.level.value, report.compatible),
           "current": dst.store.get_policy("s").level.value})
    assert list(r[0].added_versions) == [2]
    assert versions == [1, 2]
    assert report.level is CompatibilityLevel.NONE
    assert report.compatible is True
    # current policy is the last one that actually governed imported data;
    # the source's still-pending FORWARD never made it across.
    assert dst.store.get_policy("s").level is CompatibilityLevel.NONE
    assert ("FORWARD", 3) not in policies


def test_merge_preserves_target_policy_history_prefix(judge):
    """Target set FULL before having versions (pending at v1), then created
    v1 itself; a merge append must not overwrite that FULL intent for v1."""
    from app.models import CompatibilityLevel as L

    dst = RegistryService()
    dst.set_policy("s", L.FORWARD)   # pending before first registration
    dst.register("s", S1)            # v1 registered under FORWARD
    pkg = _pkg([S1, S2])             # source used BACKWARD throughout
    r = dst.import_package(pkg, "MERGE")
    v1_policy = dst.store.policy_at("s", 1)
    v2_policy = dst.store.policy_at("s", 2)
    judge("merge-keeps-target-prefix",
          {"target_v1_policy": "FORWARD", "source_v2_policy": "BACKWARD"},
          "merge preserves target history over shared prefix; appended v2 "
          "inherits the source governing policy",
          {"added": r[0].added_versions,
           "v1": v1_policy.level.value, "v2": v2_policy.level.value,
           "history": [(p.level.value, p.effective_version)
                       for p in dst.store.list_policies("s")]})
    assert v1_policy.level is L.FORWARD
    assert v2_policy.level is L.BACKWARD


def test_export_then_import_bytes_are_deterministic():
    svc = RegistryService()
    svc.register("s", S1)
    a = svc.export_subjects(["s"])
    b = svc.export_subjects(["s"])
    # exported_at timestamp may differ; compare everything else
    import json
    ja, jb = json.loads(a), json.loads(b)
    ja.pop("exported_at"); jb.pop("exported_at")
    ja.pop("checksum"); jb.pop("checksum")
    assert ja == jb
