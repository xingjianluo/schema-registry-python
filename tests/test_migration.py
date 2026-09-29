"""Migration tests: export/import round-trip, conflicts, idempotency,
concurrency, cross-default-policy reproduction, tamper/truncation
protection, and the corrupt/malformed/incomplete/inconsistent split."""
from __future__ import annotations

import copy
import json
import threading

import pytest

from app import service as service_mod
from app import storage as storage_mod
from app.migration import (
    BundleCorruptError,
    BundleIncompleteError,
    BundleInconsistentError,
    BundleMalformedError,
    ImportConflictError,
    compute_integrity,
)
from app.models import CompatibilityLevel
from app.service import IncompatibleSchemaError, RegistryService

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
# FORWARD-compatible evolution of V1: a newly added REQUIRED field is
# legal under FORWARD but rejected under BACKWARD.
V2_REQUIRED_ADDED = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"},
    {"name": "name", "type": "string"},
    {"name": "tag", "type": "string"},
]}
ORDER_V1 = {"type": "record", "name": "Order", "fields": [
    {"name": "oid", "type": "long"}]}


def _subject(bundle: dict, name: str) -> dict:
    return next(s for s in bundle["subjects"] if s["name"] == name)


def _reseal(bundle: dict) -> dict:
    """Recompute the integrity section after building a *valid* mutation.

    Used so a structurally modified bundle passes authentication and can
    reach the semantic validation stage being tested.
    """
    bundle["integrity"] = compute_integrity(bundle)
    return bundle


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


def test_export_records_baseline_policy_and_integrity(judge):
    src = _source()
    bundle = src.export_subjects(["user"])
    subj = bundle["subjects"][0]
    judge("bundle-shape", {"first_policy": subj["policies"][0],
                           "integrity_alg": bundle["integrity"]["alg"]},
          "every subject carries an explicit effective_version=1 baseline "
          "and a SHA-256 integrity section",
          {"policies": subj["policies"], "has_sha256":
           isinstance(bundle["integrity"].get("sha256"), str)})
    assert subj["policies"][0]["effective_version"] == 1
    assert subj["policies"][0]["level"] == "BACKWARD"
    assert bundle["integrity"]["alg"] == "SHA-256"
    assert len(bundle["integrity"]["sha256"]) == 64


def test_exported_bundle_is_json_roundtrippable(judge):
    src = _source()
    bundle = json.loads(json.dumps(src.export_subjects(["user"])))
    tgt = RegistryService()
    tgt.import_bundle(bundle)
    judge("json-roundtrip", {"subjects": ["user"]},
          "bundle survives JSON serialization unchanged (digest stable)",
          {"versions": [v.version for v in tgt.list_versions("user")]})
    assert _conclusions(tgt, "user") == _conclusions(src, "user")


def test_export_unknown_subject_404(judge):
    svc = _source()
    with pytest.raises(KeyError):
        svc.export_subjects(["user", "ghost"])
    judge("export-unknown", {"subjects": ["user", "ghost"]},
          "unknown subject names abort the export", "KeyError")


# -- differing default compatibility between instances ----------------------------


@pytest.fixture
def isolated_defaults(monkeypatch):
    """Force each constructed service/store to use an explicit default.

    Real deployments differ via SCHEMA_REGISTRY_COMPATIBILITY per process;
    here the two phases swap the module-level default so exporter and
    importer genuinely run under different configured levels.
    """
    def set_default(level: CompatibilityLevel) -> None:
        monkeypatch.setattr(service_mod, "DEFAULT_COMPATIBILITY", level)
        monkeypatch.setattr(storage_mod, "DEFAULT_COMPATIBILITY", level)
    return set_default


def test_import_between_instances_with_different_defaults(judge,
                                                          isolated_defaults):
    """A subject exported under one default must import into an instance
    with a different default, reproducing the recorded policies and
    conclusions instead of failing the whole batch."""
    # exporter default FORWARD: adding a required field is accepted
    isolated_defaults(CompatibilityLevel.FORWARD)
    src = RegistryService()
    src.register("user", V1)
    src.register("user", V2_REQUIRED_ADDED)  # required field: FORWARD-legal
    bundle = src.export_subjects(["user"])
    src_conclusions = _conclusions(src, "user")

    # importer default BACKWARD: the same evolution would be rejected
    isolated_defaults(CompatibilityLevel.BACKWARD)
    tgt = RegistryService()
    tgt.register("probe", V1)
    with pytest.raises(IncompatibleSchemaError):
        tgt.register("probe", V2_REQUIRED_ADDED)  # local default really differs
    summary = tgt.import_bundle(bundle, on_conflict="reject")

    tgt_conclusions = _conclusions(tgt, "user")
    judge("cross-default", {"exporter_default": "FORWARD",
                            "importer_default": "BACKWARD"},
          "import result must not depend on the importer's default: "
          "baseline policy is restored and history conclusions match 1:1",
          {"source": src_conclusions, "target": tgt_conclusions,
           "imported": summary.imported_versions,
           "policy_v1": tgt.store.policy_at("user", 1).level.value})
    assert summary.imported_versions == 2
    assert tgt.store.policy_at("user", 1).level is CompatibilityLevel.FORWARD
    assert tgt.store.policy_at("user", 2).level is CompatibilityLevel.FORWARD
    assert tgt_conclusions == src_conclusions
    assert tgt.recheck_history("user", 2).compatible is True


def test_import_none_default_into_backward_default_instance(judge,
                                                            isolated_defaults):
    """A NONE-governed breaking version must stay accepted after import."""
    isolated_defaults(CompatibilityLevel.NONE)
    src = RegistryService()
    src.register("user", V1)
    src.register("user", V3_BREAKING)  # accepted under NONE default
    bundle = src.export_subjects(["user"])

    isolated_defaults(CompatibilityLevel.BACKWARD)
    tgt = RegistryService()
    tgt.import_bundle(bundle)
    judge("none-into-backward", {"src": "NONE", "tgt": "BACKWARD"},
          "NONE baseline is explicit in the bundle; recheck v2 stays "
          "compatible regardless of the importer's configured default",
          _conclusions(tgt, "user"))
    assert tgt.store.policy_at("user", 2).level is CompatibilityLevel.NONE
    assert tgt.recheck_history("user", 2).compatible is True


def test_merge_across_different_defaults_reproduces_appended(judge,
                                                             isolated_defaults):
    """Target holds v1 with no explicit policy under its own default; the
    appended versions' policies/conclusions must come from the bundle,
    never from the importer's default."""
    # exporter BACKWARD, switches to NONE at v3
    isolated_defaults(CompatibilityLevel.BACKWARD)
    src = RegistryService()
    src.register("user", V1)
    src.register("user", V2)
    src.set_policy("user", CompatibilityLevel.NONE)
    src.register("user", V3_BREAKING)
    bundle = src.export_subjects(["user"])
    # capture while the exporter-side default is still in force: the
    # store's implicit v1 baseline otherwise follows the module default.
    src_appended = _conclusions(src, "user")[1:]

    # importer runs a different default and already has identical v1
    isolated_defaults(CompatibilityLevel.FORWARD)
    tgt = RegistryService()
    tgt.register("user", V1)
    summary = tgt.import_bundle(bundle, on_conflict="merge")
    judge("merge-cross-default",
          {"exporter_default": "BACKWARD", "importer_default": "FORWARD",
           "target_versions_before": 1},
          "appended v2 must recheck under the exporter's BACKWARD baseline "
          "and v3 under NONE; importer default FORWARD never leaks in",
          {"imported": summary.imported_versions,
           "conclusions": _conclusions(tgt, "user")})
    assert [v.version for v in tgt.list_versions("user")] == [1, 2, 3]
    assert tgt.store.policy_at("user", 2).level is CompatibilityLevel.BACKWARD
    assert tgt.store.policy_at("user", 3).level is CompatibilityLevel.NONE
    assert tgt.recheck_history("user", 2).compatible is True
    assert tgt.recheck_history("user", 3).compatible is True
    # appended versions (v2+) reproduce the source 1:1; the overlap v1
    # belongs to the target and keeps the target's own recorded policy.
    assert _conclusions(tgt, "user")[1:] == src_appended
    # idempotent under the importer default too
    assert tgt.import_bundle(bundle, on_conflict="merge").imported_versions == 0


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
    # corrupt exactly one version deep inside the bundle (digest goes stale)
    bundle["subjects"][1]["versions"][0]["schema"] = {"type": "record"}

    tgt = RegistryService()
    tgt.register("untouched", ORDER_V1)  # subject outside the migration set
    with pytest.raises(BundleCorruptError):
        tgt.import_bundle(bundle)

    judge("rollback", {"corruption": "subjects[1].versions[0].schema"},
          "a tampered entry fails authentication before mutation: target "
          "stays empty for bundle subjects; unrelated subject untouched",
          {"user": len(tgt.list_versions("user")),
           "order": len(tgt.list_versions("order")),
           "untouched": len(tgt.list_versions("untouched"))})
    assert tgt.list_versions("user") == []
    assert tgt.list_versions("order") == []
    assert len(tgt.list_versions("untouched")) == 1


# -- corrupt bundles: tampering / truncation / bad signature ----------------------


def test_corrupt_bundles_rejected_before_any_write(judge):
    src = _source()
    good = src.export_subjects(["user", "order"])
    tgt = RegistryService()
    tgt.register("keep", ORDER_V1)
    before = {s: [v.version for v in tgt.list_versions(s)]
              for s in ("user", "order", "keep")}
    cases: dict[str, object] = {}

    b = copy.deepcopy(good)
    b["exported_at"] = "1999-01-01T00:00:00+00:00"  # metadata edited
    cases["exported_at-tampered"] = b

    b = copy.deepcopy(good)  # the exact field the bug report mentioned
    b["subjects"][0]["versions"][0]["registered_at"] = "2000-01-01T00:00:00+00:00"
    cases["registered_at-tampered"] = b

    b = copy.deepcopy(good)
    _subject(b, "user")["versions"][1]["conclusion"]["compatible"] = False
    cases["conclusion-tampered"] = b

    b = copy.deepcopy(good)  # digest present but wrong
    b["integrity"] = {"alg": "SHA-256", "sha256": "0" * 64}
    cases["forged-digest"] = b

    b = copy.deepcopy(good)  # unknown signature algorithm
    b["integrity"] = {"alg": "SHA-1", "sha256": "0" * 40}
    cases["unknown-algorithm"] = b

    cases["no-integrity-section"] = {k: v for k, v in good.items()
                                     if k != "integrity"}
    cases["not-an-object"] = [1, 2, 3]
    cases["truncated-json"] = json.dumps(good)[:len(json.dumps(good)) // 2]
    cases["invalid-json"] = "{not json"

    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleCorruptError) as exc:
            tgt.import_bundle(copy.deepcopy(bundle))
        results[name] = exc.value.code
    after = {s: [v.version for v in tgt.list_versions(s)]
             for s in ("user", "order", "keep")}
    judge("corrupt", {"cases": sorted(cases)},
          "tampered, truncated, unsigned or unparseable bundles are "
          "BUNDLE_CORRUPT, rejected before validation/writes, and leave "
          "every subject (including unrelated ones) unchanged",
          {"results": results, "unchanged": before == after})
    assert set(results.values()) == {"BUNDLE_CORRUPT"}
    assert after == before


def test_corrupt_rejection_is_complete_before_application(judge):
    """A large bundle whose digest fails must not apply its first subjects."""
    src = RegistryService()
    for i in range(50):
        src.register("big", {"type": "record", "name": "Big", "fields":
                             [{"name": "id", "type": "int"}] +
                             [{"name": f"f{k}", "type": "string",
                               "default": ""} for k in range(i + 1)]})
    src.register("other", ORDER_V1)
    bundle = src.export_subjects(["big", "other"])
    # mutate the tail subject and forget to re-seal
    bundle["subjects"][1]["versions"][0]["registered_at"] = "x"

    tgt = RegistryService()
    with pytest.raises(BundleCorruptError):
        tgt.import_bundle(bundle)
    judge("corrupt-no-partial", {"subjects": ["big", "other"]},
          "authentication failure aborts the whole batch atomically",
          {"big": len(tgt.list_versions("big")),
           "other": len(tgt.list_versions("other"))})
    assert tgt.list_versions("big") == []
    assert tgt.list_versions("other") == []


# -- malformed envelopes -------------------------------------------------------------


def test_malformed_bundles_rejected(judge):
    src = _source()
    good = src.export_subjects(["user"])
    cases = {
        "wrong-format": _reseal({**good, "format": "other"}),
        "bad-format-version": _reseal({**good, "format_version": 99}),
        "exported-at-not-string": _reseal({**good, "exported_at": 123}),
        "subjects-not-list": _reseal({**good, "subjects": "nope"}),
    }
    v_bad = copy.deepcopy(good)
    v_bad["subjects"][0]["versions"][0]["version"] = "1"
    cases["version-not-int"] = _reseal(v_bad)
    l_bad = copy.deepcopy(good)
    l_bad["subjects"][0]["versions"][0]["policy"] = "SIDEWAYS"
    cases["unknown-level"] = _reseal(l_bad)
    s_bad = copy.deepcopy(good)
    s_bad["subjects"][0]["name"] = 42
    cases["name-not-string"] = _reseal(s_bad)

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleMalformedError) as exc:
            tgt.import_bundle(bundle)
        results[name] = exc.value.code
    judge("malformed", {"cases": sorted(cases)},
          "shape/type/enum problems are BUNDLE_MALFORMED, distinct from "
          "BUNDLE_CORRUPT and BUNDLE_INCOMPLETE (never partially applied)",
          results)
    assert set(results.values()) == {"BUNDLE_MALFORMED"}
    assert tgt.list_versions("user") == []


# -- incomplete bundles: required content absent ------------------------------------


def test_incomplete_bundles_rejected(judge):
    src = _source()
    good = src.export_subjects(["user"])

    cases = {}
    for key in ("schema", "schema_id", "registered_at", "policy",
                "conclusion"):
        b = copy.deepcopy(good)
        b["subjects"][0]["versions"][0].pop(key)
        cases[f"version-missing-{key}"] = _reseal(b)

    b = copy.deepcopy(good)
    b["subjects"][0]["policies"][0].pop("level")
    cases["policy-missing-level"] = _reseal(b)
    b = copy.deepcopy(good)
    b["subjects"][0]["policies"][0].pop("effective_version")
    cases["policy-missing-effective_version"] = _reseal(b)
    b = copy.deepcopy(good)
    b["subjects"][0]["versions"][0]["conclusion"].pop("incompatibilities")
    cases["conclusion-missing-incompatibilities"] = _reseal(b)
    b = copy.deepcopy(good)
    b["subjects"][0]["versions"][0]["conclusion"].pop("compatible")
    cases["conclusion-missing-compatible"] = _reseal(b)

    # empty subject cut: neither versions nor policies
    b = copy.deepcopy(good)
    b["subjects"].append({"name": "ghost", "versions": [], "policies": []})
    cases["empty-subject"] = _reseal(b)

    # policy history that does not cover version 1 (simulated old export):
    # information needed to reproduce v1's governing level is missing.
    b = copy.deepcopy(good)
    b["subjects"][0]["policies"] = [
        p for p in b["subjects"][0]["policies"]
        if p["effective_version"] != 1]
    cases["no-baseline-policy"] = _reseal(b)

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleIncompleteError) as exc:
            tgt.import_bundle(bundle)
        results[name] = exc.value.code
    judge("incomplete", {"cases": sorted(cases)},
          "missing version content or policy information is "
          "BUNDLE_INCOMPLETE (re-export), distinct from corrupt structure "
          "and from self-contradiction; target never mutated",
          results)
    assert set(results.values()) == {"BUNDLE_INCOMPLETE"}
    assert tgt.list_versions("user") == []


# -- inconsistent bundles: present content contradicts itself ------------------------


def test_inconsistent_bundles_rejected(judge):
    src = _source()
    good = src.export_subjects(["user"])
    cases = {}

    b = copy.deepcopy(good)  # gap in version numbering
    b["subjects"][0]["versions"][1]["version"] = 7
    cases["non-dense-versions"] = _reseal(b)

    b = copy.deepcopy(good)  # schema_id no longer matches content
    b["subjects"][0]["versions"][0]["schema_id"] = "0" * 16
    cases["schema-id-mismatch"] = _reseal(b)

    b = copy.deepcopy(good)  # recorded policy contradicts policy history
    b["subjects"][0]["versions"][0]["policy"] = "FULL"
    cases["policy-contradiction"] = _reseal(b)

    b = copy.deepcopy(good)  # recorded conclusion contradicts recomputation
    b["subjects"][0]["versions"][1]["conclusion"]["compatible"] = False
    cases["conclusion-contradiction"] = _reseal(b)

    b = copy.deepcopy(good)  # policy effective_version out of range
    b["subjects"][0]["policies"][0]["effective_version"] = 99
    cases["policy-out-of-range"] = _reseal(b)

    b = copy.deepcopy(good)  # invalid schema document
    b["subjects"][0]["versions"][0]["schema"] = {"type": "record"}
    cases["schema-invalid"] = _reseal(b)

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleInconsistentError) as exc:
            tgt.import_bundle(bundle)
        results[name] = exc.value.code
    judge("inconsistent", {"cases": sorted(cases)},
          "complete-but-self-contradictory contents are "
          "BUNDLE_INCONSISTENT, distinct from BUNDLE_MALFORMED / "
          "BUNDLE_INCOMPLETE / BUNDLE_CORRUPT",
          results)
    assert set(results.values()) == {"BUNDLE_INCONSISTENT"}
    assert tgt.list_versions("user") == []


def test_error_classes_are_distinct(judge):
    """Callers can switch on the exception type to pick a remediation."""
    classes = (BundleCorruptError, BundleMalformedError,
               BundleIncompleteError, BundleInconsistentError)
    assert len({c.code for c in classes}) == 4
    judge("error-codes", {c.__name__: c.code for c in classes},
          "corrupt vs malformed vs incomplete vs inconsistent are "
          "distinguishable to callers", {c.__name__: c.code for c in classes})


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
