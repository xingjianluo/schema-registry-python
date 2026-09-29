"""Migration tests: export/import round-trip, conflicts, idempotency,
concurrency, integrity protection and malformed/incomplete/inconsistent
bundles."""
from __future__ import annotations

import copy
import json
import threading

import pytest

from app.migration import (
    BundleCorruptError,
    BundleIncompleteError,
    BundleInconsistentError,
    BundleMalformedError,
    ImportConflictError,
    seal_bundle,
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
# FORWARD-only evolution: a required field is added (rejected by BACKWARD
# when that is the default on the source).
V2_REQUIRED_ADDED = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"},
    {"name": "name", "type": "string"},
    {"name": "nick", "type": "string"},
]}
ORDER_V1 = {"type": "record", "name": "Order", "fields": [
    {"name": "oid", "type": "long"}]}


def _source(default: CompatibilityLevel = CompatibilityLevel.BACKWARD
            ) -> RegistryService:
    """A source instance with two subjects and a non-trivial policy history."""
    svc = RegistryService(default_level=default)
    svc.register("user", V1)                       # v1 under source default
    svc.register("user", V2)                       # v2 under source default
    svc.set_policy("user", CompatibilityLevel.NONE)
    svc.register("user", V3_BREAKING)              # v3 under NONE
    svc.register("order", ORDER_V1)
    svc.set_policy("order", CompatibilityLevel.FULL)
    return svc


def _reseal(payload: dict) -> dict:
    """Recompute the integrity envelope after deliberately constructing an
    (authentic-shape) malformed/incomplete/inconsistent document."""
    return seal_bundle({k: v for k, v in payload.items()
                        if k != "integrity"})


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


# -- differing default policies between instances -------------------------------


@pytest.mark.parametrize(
    "src_default,tgt_default",
    [(s, t) for s in CompatibilityLevel for t in CompatibilityLevel
     if s != t],
)
def test_import_independent_of_importer_default_policy(
        judge, src_default, tgt_default):
    """A subject exported under one default policy imports cleanly into an
    instance configured with a different default; every version keeps its
    original effective policy and its original compatibility conclusion.

    The v2 evolution is chosen so that it is genuinely accepted under the
    *source* default, including evolutions the target default would have
    rejected at registration time.
    """
    v2_by_default = {
        CompatibilityLevel.FORWARD: V2_REQUIRED_ADDED,
        CompatibilityLevel.NONE: V3_BREAKING,
        CompatibilityLevel.BACKWARD: V2,
        CompatibilityLevel.FULL: V2,
    }
    src = RegistryService(default_level=src_default)
    src.register("user", V1)
    src.register("user", v2_by_default[src_default])

    bundle = src.export_subjects(["user"])
    assert bundle["subjects"][0]["base_policy"] == src_default.value

    tgt = RegistryService(default_level=tgt_default)
    summary = tgt.import_bundle(bundle, on_conflict="reject")

    before = _conclusions(src, "user")
    after = _conclusions(tgt, "user")
    judge("cross-default", {"source_default": src_default.value,
                            "target_default": tgt_default.value},
          "imported per-version policies/conclusions match the source "
          "regardless of the importer's default",
          {"source": before, "target": after,
           "imported": summary.imported_versions})
    assert summary.imported_versions == 2
    assert after == before
    # The importer default must not leak onto the imported subject.
    assert tgt.store.policy_at("user", 1).level is src_default
    assert tgt.store.policy_at("user", 2).level is src_default
    assert all(
        tgt.recheck_history("user", v).compatible
        == src.recheck_history("user", v).compatible
        for v in (1, 2))


def test_explicit_policy_history_survives_cross_default_import(judge):
    """Mixed history: base policy plus a mid-history change must reproduce
    per-version levels even when both defaults differ from BACKWARD."""
    src = RegistryService(default_level=CompatibilityLevel.FORWARD)
    src.register("user", V1)
    src.register("user", V2_REQUIRED_ADDED)           # v2 under FORWARD
    src.set_policy("user", CompatibilityLevel.NONE)
    src.register("user", V3_BREAKING)                 # v3 under NONE

    tgt = RegistryService(default_level=CompatibilityLevel.FULL)
    bundle = src.export_subjects(["user"])
    summary = tgt.import_bundle(bundle)

    before = _conclusions(src, "user")
    after = _conclusions(tgt, "user")
    judge("cross-default-mixed",
          {"source_default": "FORWARD", "target_default": "FULL"},
          "base policy seeded explicitly; mid-history NONE change intact; "
          "all three versions reproduce source conclusions",
          {"source": before, "target": after,
           "imported": summary.imported_versions,
           "target_policy_records": [
               (p.level.value, p.effective_version)
               for p in tgt.store.list_policies("user")]})
    assert after == before
    assert tgt.store.policy_at("user", 1).level is CompatibilityLevel.FORWARD
    assert tgt.store.policy_at("user", 3).level is CompatibilityLevel.NONE


def test_policy_only_subject_roundtrips_across_defaults(judge):
    """A subject that only has a policy history (zero registered versions)
    is a legitimate snapshot and must import, carrying its base policy."""
    src = RegistryService(default_level=CompatibilityLevel.FORWARD)
    src.set_policy("reserved", CompatibilityLevel.FULL)
    bundle = src.export_subjects(["reserved"])

    tgt = RegistryService(default_level=CompatibilityLevel.NONE)
    summary = tgt.import_bundle(bundle)
    judge("policy-only", {"source_default": "FORWARD", "policy": "FULL",
                          "target_default": "NONE"},
          "policy-only subject imports; its effective policy is FULL "
          "(from history), not the target default NONE",
          {"new_policies": len(summary.plans[0].new_policies),
           "policy": tgt.get_policy("reserved").level.value})
    assert tgt.list_versions("reserved") == []
    # History record effective from v1 is reproduced verbatim.
    assert [p.level for p in tgt.store.list_policies("reserved")] == \
           [CompatibilityLevel.FULL]
    assert tgt.get_policy("reserved").level is CompatibilityLevel.FULL


def test_merge_across_different_defaults_governs_appended_versions(judge):
    """Target holds an identical prefix under its own default; appended
    versions must still be governed by the *bundle's* policies."""
    src = RegistryService(default_level=CompatibilityLevel.FORWARD)
    src.register("user", V1)
    src.register("user", V2_REQUIRED_ADDED)
    src.set_policy("user", CompatibilityLevel.NONE)
    src.register("user", V3_BREAKING)
    bundle = src.export_subjects(["user"])

    tgt = RegistryService(default_level=CompatibilityLevel.BACKWARD)
    tgt.register("user", V1)
    # Target accepted the identical v2 under an explicit FORWARD policy;
    # the target instance's own default stays BACKWARD.
    tgt.set_policy("user", CompatibilityLevel.FORWARD)
    tgt.register("user", V2_REQUIRED_ADDED)  # identical content prefix
    summary = tgt.import_bundle(bundle, on_conflict="merge")

    after = _conclusions(tgt, "user")
    judge("cross-default-merge", {"overlap": 2},
          "only v3 appended; it rechecks under NONE from the bundle, "
          "not the BACKWARD target default",
          {"target": after, "already_present": summary.plans[0].already_present})
    assert summary.plans[0].already_present == 2
    assert [v.version for v in tgt.list_versions("user")] == [1, 2, 3]
    assert tgt.store.policy_at("user", 3).level is CompatibilityLevel.NONE
    assert tgt.recheck_history("user", 3).compatible is True


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
    # tamper with one version deep inside the bundle WITHOUT re-sealing:
    # the integrity envelope must reject it before any content check.
    bundle["subjects"][1]["versions"][0]["schema"] = {"type": "record"}

    tgt = RegistryService()
    with pytest.raises(BundleCorruptError):
        tgt.import_bundle(bundle)

    judge("rollback", {"corruption": "subjects[1].versions[0].schema"},
          "tampered package fails the digest check: target stays empty",
          {"user": len(tgt.list_versions("user")),
           "order": len(tgt.list_versions("order"))})
    assert tgt.list_versions("user") == []
    assert tgt.list_versions("order") == []


# -- corrupt / malformed / incomplete / inconsistent bundles -------------------


def test_corrupt_bundles_rejected(judge):
    """Transport/tampering problems are BUNDLE_CORRUPT: the package bytes
    themselves cannot be trusted; the fix is to re-export/re-transfer."""
    src = _source()
    good = src.export_subjects(["user"])
    cases: dict[str, object] = {
        "not-json-bytes": "{not valid json",
        "truncated-document": json.dumps(good)[:120],
        "not-an-object": [1, 2, 3],
        "envelope-stripped": {k: v for k, v in good.items()
                              if k != "integrity"},
    }
    b = copy.deepcopy(good)  # field tampering after export
    b["exported_at"] = "1999-01-01T00:00:00+00:00"
    cases["tampered-exported-at"] = b
    b = copy.deepcopy(good)
    b["subjects"][0]["versions"][0]["registered_at"] = "2000-01-01T00:00:00+00:00"
    cases["tampered-registered-at"] = b
    b = copy.deepcopy(good)
    b["subjects"][0]["versions"][0]["schema_id"] = "0" * 16
    cases["tampered-schema-id"] = b
    b = copy.deepcopy(good)  # digest flipped, payload intact
    b["integrity"] = {**b["integrity"], "digest": "0" * 64}
    cases["forged-digest"] = b

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        if isinstance(bundle, str):
            from app.migration import parse_bundle
            with pytest.raises(BundleCorruptError):
                parse_bundle(bundle)
        else:
            with pytest.raises(BundleCorruptError):
                tgt.import_bundle(bundle)
        results[name] = "BUNDLE_CORRUPT"
    judge("corrupt", {"cases": sorted(cases)},
          "unparseable/truncated bytes and any un-re-sealed modification "
          "are BUNDLE_CORRUPT, detected before content validation",
          results)
    assert tgt.list_versions("user") == []


def test_malformed_bundles_rejected(judge):
    """Authentic, freshly sealed documents that still have the wrong
    shape/types/format are BUNDLE_MALFORMED (no healthy exporter could
    have made them): human investigation, not just a re-export."""
    src = _source()
    good = src.export_subjects(["user"])
    cases = {
        "wrong-format": None,
        "bad-format-version": None,
        "version-not-int": None,
        "unknown-level": None,
    }
    cases["wrong-format"] = _reseal({**good, "format": "other"})
    cases["bad-format-version"] = _reseal({**good, "format_version": 99})
    v_bad = copy.deepcopy(good)
    v_bad["subjects"][0]["versions"][0]["version"] = "1"
    cases["version-not-int"] = _reseal(v_bad)
    l_bad = copy.deepcopy(good)
    l_bad["subjects"][0]["versions"][0]["policy"] = "SIDEWAYS"
    cases["unknown-level"] = _reseal(l_bad)

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleMalformedError):
            tgt.import_bundle(bundle)
        results[name] = "BUNDLE_MALFORMED"
    judge("malformed", {"cases": sorted(cases)},
          "structural/type problems in authentic packages are "
          "BUNDLE_MALFORMED (never partially applied)",
          results)
    assert tgt.list_versions("user") == []


def test_incomplete_bundles_rejected(judge):
    """Authentic shape, but required data is absent: BUNDLE_INCOMPLETE.
    The actionable difference from malformed/corrupt is: redo the export
    or supply the missing data rather than debug the transport."""
    src = _source()
    good = src.export_subjects(["user"])
    cases = {
        "missing-subjects-block": None,
        "missing-schema": None,
        "missing-policy-info": None,
        "missing-policies-block": None,
        "missing-base-policy": None,
        "missing-conclusion": None,
    }
    cases["missing-subjects-block"] = _reseal(
        {k: v for k, v in good.items() if k != "subjects"})

    one = src.export_subjects(["order"])
    one["subjects"][0]["versions"] = []
    # 'order' also carries a policy record; strip it so emptiness is a
    # missing-content failure rather than an effective_version range error.
    one["subjects"][0]["policies"] = []
    cases["missing-version-body"] = _reseal(one)

    b = copy.deepcopy(good)
    del b["subjects"][0]["versions"][0]["schema"]
    cases["missing-schema"] = _reseal(b)

    b = copy.deepcopy(good)  # per-version policy info absent
    del b["subjects"][0]["versions"][0]["policy"]
    cases["missing-policy-info"] = _reseal(b)

    b = copy.deepcopy(good)
    del b["subjects"][0]["policies"]
    cases["missing-policies-block"] = _reseal(b)

    b = copy.deepcopy(good)
    del b["subjects"][0]["base_policy"]
    cases["missing-base-policy"] = _reseal(b)

    b = copy.deepcopy(good)
    del b["subjects"][0]["versions"][0]["conclusion"]
    cases["missing-conclusion"] = _reseal(b)

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleIncompleteError):
            tgt.import_bundle(bundle)
        results[name] = "BUNDLE_INCOMPLETE"
    judge("incomplete", {"cases": sorted(cases)},
          "missing version content or policy information is "
          "BUNDLE_INCOMPLETE, distinct from corrupt/malformed/inconsistent",
          results)
    assert tgt.list_versions("user") == []


def test_inconsistent_bundles_rejected(judge):
    """Complete, well-shaped, authentic packages whose contents contradict
    each other are BUNDLE_INCONSISTENT (re-signed by a buggy producer):
    human investigation."""
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

    tgt = RegistryService()
    results = {}
    for name, bundle in cases.items():
        with pytest.raises(BundleInconsistentError):
            tgt.import_bundle(bundle)
        results[name] = "BUNDLE_INCONSISTENT"
    judge("inconsistent", {"cases": sorted(cases)},
          "self-contradictory contents are BUNDLE_INCONSISTENT, distinct "
          "from corrupt/malformed/incomplete",
          results)
    assert tgt.list_versions("user") == []


def test_failure_categories_are_distinct(judge):
    """One representative case per category must raise a distinct type, so
    callers can branch on what to do."""
    src = _source()
    good = src.export_subjects(["user"])

    b = copy.deepcopy(good)
    b["exported_at"] = "tampered"
    corrupt = b
    malformed = _reseal({**good, "format": "other"})
    incomplete = _reseal(
        {**good, "subjects": [
            {k: v for k, v in good["subjects"][0].items()
             if k != "base_policy"}]})
    b = copy.deepcopy(good)
    b["subjects"][0]["versions"][0]["schema_id"] = "0" * 16
    inconsistent = _reseal(b)

    tgt = RegistryService()
    categories = (
        ("corrupt", corrupt, BundleCorruptError),
        ("malformed", malformed, BundleMalformedError),
        ("incomplete", incomplete, BundleIncompleteError),
        ("inconsistent", inconsistent, BundleInconsistentError),
    )
    codes = {}
    for label, bundle, exc in categories:
        with pytest.raises(exc) as caught:
            tgt.import_bundle(bundle)
        codes[label] = caught.value.code
    judge("distinct-categories", {},
          "corrupt/malformed/incomplete/inconsistent are distinguishable "
          "by exception type and by .code, all rejected before apply",
          codes)
    assert len(set(codes.values())) == 4
    assert tgt.list_versions("user") == []


def test_rejected_import_leaves_unrelated_subjects_untouched(judge):
    """A corrupt package must not affect subjects outside the package nor
    leave partial state for subjects inside it."""
    src = _source()
    good = src.export_subjects(["user", "order"])

    tgt = RegistryService()
    tgt.register("untouched", ORDER_V1)
    untouched_before = list(tgt.list_versions("untouched"))

    # truncate the on-wire bytes
    raw = json.dumps(good)[:200]
    from app.migration import parse_bundle
    with pytest.raises(BundleCorruptError):
        parse_bundle(raw)

    # and a resealed-but-missing-field package, through the service
    missing = copy.deepcopy(good)
    del missing["subjects"][0]["versions"][0]["schema_id"]
    missing = _reseal(missing)
    with pytest.raises(BundleIncompleteError):
        tgt.import_bundle(missing)

    judge("untouched", {"subjects_in_package": ["user", "order"]},
          "rejected imports change nothing: package subjects absent, "
          "unrelated subject exactly as before",
          {"user": len(tgt.list_versions("user")),
           "order": len(tgt.list_versions("order")),
           "untouched_same": list(tgt.list_versions("untouched"))
                             == untouched_before})
    assert tgt.list_versions("user") == []
    assert tgt.list_versions("order") == []
    assert list(tgt.list_versions("untouched")) == untouched_before


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
