"""Migration tests: export/import round-trip, conflicts, idempotency,
concurrency and malformed bundles."""
from __future__ import annotations

import copy
import threading

import pytest

from app.migration import (
    BundleInconsistentError,
    BundleMalformedError,
    ImportConflictError,
)
from app.models import CompatibilityLevel
from app.service import RegistryService

V1 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"},
    {"name": "name", "type": "string"},
]}
# BACKWARD-compatible evolution of V1 (optional field added)
V2 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"},
    {"name": "name", "type": "string"},
    {"name": "nick", "type": "string", "default": ""},
]}
# only valid under NONE/FORWARD at v2->v3 boundary depending on policy
V3_BREAKING = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "string"},
    {"name": "name", "type": "string"},
    {"name": "nick", "type": "string", "default": ""},
]}
ORDER_V1 = {"type": "record", "name": "Order", "fields": [
    {"name": "oid", "type": "long"}]}


def _source() -> RegistryService:
    """A source instance with two subjects and a non-trivial policy history."""
    svc = RegistryService()
    svc.register("user", V1)                       # v1 under default BACKWARD
    svc.register("user", V2)                       # v2 under BACKWARD
    svc.set_policy("user", CompatibilityLevel.NONE)
    svc.register("user", V3_BREAKING)              # v3 under NONE
    svc.register("order", ORDER_V1)
    svc.set_policy("order", CompatibilityLevel.FULL)
    return svc


def _conclusions(svc: RegistryService, subject: str) -> list[tuple]:
    return [
        (r.version,
         svc.store.policy_at(subject, r.version).level.value,
         svc.recheck_history(subject, r.version).compatible)
        for r in svc.list_versions(subject)
    ]


# -- round trip -----------------------------------------------------------------


def test_roundtrip_preserves_versions_policies_conclusions(judge):
    src = _source()
    bundle = src.export_subjects(["user", "order"])

    tgt = RegistryService()
    summary = tgt.import_bundle(bundle, on_conflict="reject")

    before = {s: _conclusions(src, s) for s in ("user", "order")}
    after = {s: _conclusions(tgt, s) for s in ("user", "order")}
    judge("roundtrip", {"subjects": ["user", "order"], "mode": "reject"},
          "imported versions + policy history must reproduce the exact "
          "per-version policy and conclusion of the source",
          {"source": before, "target": after,
           "imported_versions": summary.imported_versions})

    assert summary.imported_versions == 4
    assert after == before
    for s in ("user", "order"):
        assert [v.schema_id for v in tgt.list_versions(s)] == \
               [v.schema_id for v in src.list_versions(s)]
        assert [v.registered_at for v in tgt.list_versions(s)] == \
               [v.registered_at for v in src.list_versions(s)]


def test_exported_bundle_is_json_roundtrippable(judge):
    import json
    src = _source()
    bundle = json.loads(json.dumps(src.export_subjects(["user"])))
    tgt = RegistryService()
    tgt.import_bundle(bundle)
    judge("json-roundtrip", {"subjects": ["user"]},
          "bundle survives JSON serialization unchanged",
          {"versions": [v.version for v in tgt.list_versions("user")]})
    assert _conclusions(tgt, "user") == _conclusions(src, "user")


def test_export_unknown_subject_404(judge):
    svc = _source()
    with pytest.raises(KeyError):
        svc.export_subjects(["user", "ghost"])
    judge("export-unknown", {"subjects": ["user", "ghost"]},
          "unknown subject names abort the export", "KeyError")


# -- conflict modes ----------------------------------------------------------------


def test_reject_mode_aborts_on_existing_subject(judge):
    src = _source()
    bundle = src.export_subjects(["user", "order"])

    tgt = RegistryService()
    tgt.register("order", ORDER_V1)  # 'order' already exists in target
    before = {s: list(tgt.list_versions(s)) for s in ("user", "order")}

    with pytest.raises(ImportConflictError):
        tgt.import_bundle(bundle, on_conflict="reject")

    after = {s: list(tgt.list_versions(s)) for s in ("user", "order")}
    judge("reject-mode", {"existing": "order", "mode": "reject"},
          "any existing subject aborts the whole import; nothing is applied",
          {"before": {k: len(v) for k, v in before.items()},
           "after": {k: len(v) for k, v in after.items()}})
    assert after == before  # 'user' not imported either: all-or-nothing


def test_merge_appends_only_missing_versions(judge):
    src = _source()
    bundle = src.export_subjects(["user"])

    tgt = RegistryService()
    tgt.register("user", V1)
    tgt.register("user", V2)  # identical prefix, target has no policy records
    summary = tgt.import_bundle(bundle, on_conflict="merge")

    versions = [v.version for v in tgt.list_versions("user")]
    judge("merge-append", {"target_versions": [1, 2], "bundle_versions": [1, 2, 3]},
          "identical overlap kept, only v3 appended, numbering stays dense; "
          "v3 must still recheck under NONE (its source policy)",
          {"versions": versions, "already_present": summary.plans[0].already_present,
           "recheck3": tgt.recheck_history("user", 3).level.value})
    assert versions == [1, 2, 3]
    assert summary.plans[0].already_present == 2
    assert tgt.store.policy_at("user", 3).level is CompatibilityLevel.NONE
    assert tgt.recheck_history("user", 3).compatible is True


def test_merge_same_number_different_content_is_conflict(judge):
    src = _source()
    bundle = src.export_subjects(["user", "order"])

    tgt = RegistryService()
    tgt.register("user", V1)
    tgt.set_policy("user", CompatibilityLevel.NONE)
    tgt.register("user", V3_BREAKING)  # v2 differs from bundle's v2
    tgt_before = list(tgt.list_versions("user"))

    with pytest.raises(ImportConflictError):
        tgt.import_bundle(bundle, on_conflict="merge")

    judge("merge-content-conflict", {"subject": "user", "version": 2},
          "same version number with different content => conflict; "
          "whole import rolled back, other subjects untouched",
          {"user_versions": [v.version for v in tgt.list_versions("user")],
           "order_versions": [v.version for v in tgt.list_versions("order")]})
    assert tgt.list_versions("user") == tgt_before
    assert tgt.list_versions("order") == []  # rolled back, not half-imported


def test_merge_is_idempotent(judge):
    src = _source()
    bundle = src.export_subjects(["user", "order"])

    tgt = RegistryService()
    tgt.import_bundle(bundle, on_conflict="merge")
    state_once = {s: (list(tgt.list_versions(s)), list(tgt.store.list_policies(s)))
                  for s in ("user", "order")}
    summary = tgt.import_bundle(bundle, on_conflict="merge")  # same bundle again
    state_twice = {s: (list(tgt.list_versions(s)), list(tgt.store.list_policies(s)))
                   for s in ("user", "order")}

    judge("merge-idempotent", {"bundles": 2},
          "re-importing the same bundle adds no versions and no policy records",
          {"imported_on_second": summary.imported_versions,
           "identical_state": state_once == state_twice})
    assert summary.imported_versions == 0
    assert state_once == state_twice


def test_failed_import_rolls_back_everything(judge):
    src = _source()
    bundle = src.export_subjects(["user", "order"])
    # corrupt exactly one version deep inside the bundle
    bundle["subjects"][1]["versions"][0]["schema"] = {"type": "record"}

    tgt = RegistryService()
    with pytest.raises(BundleInconsistentError):
        tgt.import_bundle(bundle)

    judge("rollback", {"corruption": "subjects[1].versions[0].schema"},
          "any invalid entry aborts before mutation: target stays empty",
          {"user": len(tgt.list_versions("user")),
           "order": len(tgt.list_versions("order"))})
    assert tgt.list_versions("user") == []
    assert tgt.list_versions("order") == []


# -- malformed / inconsistent bundles ------------------------------------------


def test_malformed_bundles_rejected(judge):
    src = _source()
    good = src.export_subjects(["user"])
    cases = {
        "not-an-object": [1, 2, 3],
        "wrong-format": {**good, "format": "other"},
        "bad-format-version": {**good, "format_version": 99},
        "missing-subjects": {k: v for k, v in good.items() if k != "subjects"},
        "version-not-int": None,  # filled below
        "unknown-level": None,
    }
    v_bad = copy.deepcopy(good)
    v_bad["subjects"][0]["versions"][0]["version"] = "1"
    cases["version-not-int"] = v_bad
    l_bad = copy.deepcopy(good)
    l_bad["subjects"][0]["versions"][0]["policy"] = "SIDEWAYS"
    cases["unknown-level"] = l_bad

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleMalformedError):
            tgt.import_bundle(bundle)
        results[name] = "BUNDLE_MALFORMED"
    judge("malformed", {"cases": sorted(cases)},
          "structural problems are BUNDLE_MALFORMED (never partially applied)",
          results)
    assert tgt.list_versions("user") == []


def test_inconsistent_bundles_rejected(judge):
    src = _source()
    good = src.export_subjects(["user"])
    cases = {}

    b = copy.deepcopy(good)  # gap in version numbering
    b["subjects"][0]["versions"][1]["version"] = 7
    cases["non-dense-versions"] = b

    b = copy.deepcopy(good)  # schema_id no longer matches content
    b["subjects"][0]["versions"][0]["schema_id"] = "0" * 16
    cases["schema-id-mismatch"] = b

    b = copy.deepcopy(good)  # recorded policy contradicts policy history
    b["subjects"][0]["versions"][0]["policy"] = "FULL"
    cases["policy-contradiction"] = b

    b = copy.deepcopy(good)  # recorded conclusion contradicts recomputation
    b["subjects"][0]["versions"][1]["conclusion"]["compatible"] = False
    cases["conclusion-contradiction"] = b

    b = copy.deepcopy(good)  # policy effective_version out of range
    b["subjects"][0]["policies"][0]["effective_version"] = 99
    cases["policy-out-of-range"] = b

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleInconsistentError):
            tgt.import_bundle(bundle)
        results[name] = "BUNDLE_INCONSISTENT"
    judge("inconsistent", {"cases": sorted(cases)},
          "self-contradictory contents are BUNDLE_INCONSISTENT, distinct "
          "from structural BUNDLE_MALFORMED",
          results)
    assert tgt.list_versions("user") == []


def test_unknown_conflict_mode_rejected(judge):
    src = _source()
    with pytest.raises(ValueError):
        RegistryService().import_bundle(src.export_subjects(["user"]),
                                        on_conflict="yolo")
    judge("bad-mode", {"on_conflict": "yolo"},
          "unknown modes are rejected before touching the store", "ValueError")


# -- concurrency --------------------------------------------------------------------


def test_import_racing_reads_and_writes(judge):
    """Readers see the state before OR after the import, never between;
    concurrent registrations on other subjects are undisturbed."""
    src = RegistryService()
    for i in range(200):  # sizeable subject so the apply is observable
        src.register("big", {"type": "record", "name": "Big", "fields":
                             [{"name": "id", "type": "int"}] +
                             [{"name": f"f{k}", "type": "string",
                               "default": ""} for k in range(i + 1)]})
    bundle = src.export_subjects(["big"])

    tgt = RegistryService()
    tgt.register("other", ORDER_V1)
    anomalies: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            n = len(tgt.list_versions("big"))
            if n not in (0, 200):
                anomalies.append(f"intermediate version count {n}")
            r = tgt.recheck_history("other", 1)
            if not r.compatible:
                anomalies.append("other subject recheck flipped")

    def registrator():
        for i in range(30):
            tgt.register("other", {"type": "record", "name": "Order",
                                   "fields": [{"name": "oid", "type": "long"},
                                              {"name": f"x{i}", "type": "int",
                                               "default": 0}]})

    def flipper():
        for _ in range(10):
            tgt.set_policy("other", CompatibilityLevel.FULL)
            tgt.set_policy("other", CompatibilityLevel.BACKWARD)

    threads = ([threading.Thread(target=reader) for _ in range(4)]
               + [threading.Thread(target=registrator),
                  threading.Thread(target=flipper)])
    for t in threads:
        t.start()
    tgt.import_bundle(bundle, on_conflict="reject")
    stop.set()
    for t in threads:
        t.join()

    big = tgt.list_versions("big")
    other_versions = [v.version for v in tgt.list_versions("other")]
    judge("import-concurrency",
          {"bundle_versions": 200, "reader_threads": 4,
           "concurrent": "register+policy-flip on 'other'"},
          "apply is one critical section: counts are only 0 or 200; "
          "untouched subjects keep dense versions and reproducible history",
          {"anomalies": anomalies, "big_versions": len(big),
           "other_versions_dense": other_versions == list(range(1, 32))})
    assert anomalies == []
    assert len(big) == 200
    assert other_versions == list(range(1, 31 + 1))
    # imported history fully reproducible in target
    assert _conclusions(tgt, "big") == _conclusions(src, "big")


def _race_import_with_register(bundle: dict) -> tuple[list[int],
                                                      list[Exception]]:
    """One import vs one registration on the same subject, released together."""
    tgt = RegistryService()
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def importer():
        barrier.wait()
        try:
            tgt.import_bundle(copy.deepcopy(bundle), on_conflict="merge")
        except Exception as exc:  # noqa: BLE001 - any failure is recorded
            errors.append(exc)

    def registrator():
        barrier.wait()
        try:
            tgt.register("user", V1)  # re-registering V1 content
        except Exception as exc:  # noqa: BLE001 - any failure is recorded
            errors.append(exc)

    t1 = threading.Thread(target=importer)
    t2 = threading.Thread(target=registrator)
    t1.start(); t2.start(); t1.join(); t2.join()
    versions = [v.version for v in tgt.list_versions("user")]
    return versions, errors


def test_import_racing_registration_same_subject_is_serialized(judge):
    """A registration on the imported subject lands entirely before or
    after the import; versions stay dense and nothing is lost."""
    src = RegistryService()
    src.register("user", V1)
    src.register("user", V2)
    bundle = src.export_subjects(["user"])

    outcomes = set()
    for _ in range(20):
        versions, errors = _race_import_with_register(bundle)
        assert versions == list(range(1, len(versions) + 1))  # dense
        assert not errors or all(
            isinstance(e, ImportConflictError) for e in errors)
        outcomes.add(len(versions))

    judge("import-register-race", {"rounds": 20},
          "import and register serialize on one lock: final state is one of "
          "the two legal interleavings, versions always dense",
          {"final_version_counts": sorted(outcomes)})
    assert outcomes <= {2, 3}
