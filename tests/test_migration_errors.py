"""Malformed migration packages: every problem gets a distinct category."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from app.migration import (
    ImportConflictError,
    MigrationError,
    PackageContentMissingError,
    PackageCorruptedError,
    PackageInconsistentError,
    decode_package,
)
from app.service import RegistryService

V1 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"}]}
V2 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "long"}]}


@pytest.fixture
def valid_envelope():
    svc = RegistryService()
    svc.register("u", V1)
    svc.register("u", V2)
    return json.loads(svc.export_subjects(["u"]).decode("utf-8"))


def _canon(o) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def reencode(env: dict) -> bytes:
    return json.dumps(env, sort_keys=True).encode("utf-8")


def reseal(env: dict) -> dict:
    """Recompute subject + root checksums after a semantic body edit.

    Models a package that is structurally intact (valid checksums) but
    contains self-contradictory data, so decoding reaches semantic
    validation instead of stopping at checksum verification.
    """
    for entry in env["subjects"]:
        entry["checksum"] = hashlib.sha256(_canon(entry["body"])).hexdigest()
    env["checksum"] = hashlib.sha256(_canon(
        {k: v for k, v in env.items() if k != "checksum"})).hexdigest()
    return env


def body(env):
    return env["subjects"][0]["body"]


def expect(exc_type, env, judge, case, basis, *, seal=False):
    if seal:
        reseal(env)
    with pytest.raises(exc_type) as exc:
        decode_package(reencode(env))
    judge(case, "mutated valid package", basis,
          {"category": exc.value.category, "message": str(exc.value)})


# -- corruption --------------------------------------------------------------

def test_garbage_bytes_rejected(judge):
    for data in (b"", b"\x00\x01binary", b"{not json"):
        with pytest.raises(PackageCorruptedError) as exc:
            decode_package(data)
        assert exc.value.category == "PACKAGE_CORRUPTED"
    judge("garbage", {"samples": 3}, "not parseable as JSON / empty",
          "PACKAGE_CORRUPTED")


def test_wrong_format_and_version(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    env["format"] = "something-else"
    expect(PackageCorruptedError, env, judge, "wrong-format",
           "format marker does not identify a migration package")
    env = copy.deepcopy(valid_envelope)
    env["package_version"] = 999
    expect(PackageCorruptedError, env, judge, "wrong-version",
           "unsupported package_version")


def test_root_checksum_detects_tampering(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    env["exported_at"] = "1999-01-01T00:00:00+00:00"  # edit w/o re-hashing
    expect(PackageCorruptedError, env, judge, "root-tamper",
           "any metadata byte change invalidates the root checksum")


def test_subject_checksum_detects_inner_tampering(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    body(env)["default_level"] = "NONE"  # edit body, leave its checksum stale
    expect(PackageCorruptedError, env, judge, "subject-tamper",
           "body edited but subject checksum not refreshed")


def test_root_rehash_cannot_hide_stale_subject_checksum(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    body(env)["default_level"] = "NONE"
    # attacker re-hashes only the root; the inner checksum still fails.
    env["checksum"] = hashlib.sha256(_canon(
        {k: v for k, v in env.items() if k != "checksum"})).hexdigest()
    expect(PackageCorruptedError, env, judge, "layered-checksum",
           "root rehash cannot mask a stale subject checksum")


def test_truncated_entry(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    del env["subjects"][0]["body"]
    expect(PackageCorruptedError, env, judge, "truncated-entry",
           "entry missing its body")


# -- content missing ---------------------------------------------------------

def test_missing_schema_document(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    del body(env)["versions"][0]["schema"]
    expect(PackageContentMissingError, env, judge, "missing-schema",
           "a version without its schema content cannot be imported",
           seal=True)


def test_missing_policies_or_conclusions(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    del body(env)["policies"]
    expect(PackageContentMissingError, env, judge, "missing-policies",
           "versions without policy history are refused, not imported",
           seal=True)
    env = copy.deepcopy(valid_envelope)
    del body(env)["conclusions"]
    expect(PackageContentMissingError, env, judge, "missing-conclusions",
           "historical conclusions are a required part of the package",
           seal=True)


# -- inconsistency -----------------------------------------------------------

def test_version_gap_rejected(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    body(env)["versions"] = [v for v in body(env)["versions"]
                             if v["version"] != 1]
    expect(PackageInconsistentError, env, judge, "version-gap",
           "versions must be dense 1..N", seal=True)


def test_schema_id_mismatch_rejected(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    body(env)["versions"][0]["schema_id"] = "0" * 16
    expect(PackageInconsistentError, env, judge, "id-mismatch",
           "schema_id must be derived from the schema content", seal=True)


def test_invalid_schema_document_rejected(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    body(env)["versions"][0]["schema"] = {"type": "record"}  # no fields/name
    expect(PackageInconsistentError, env, judge, "bad-schema",
           "schema document fails structural validation", seal=True)


def test_unknown_policy_level_rejected(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    body(env)["default_level"] = "SIDEWAYS"
    expect(PackageInconsistentError, env, judge, "bad-level",
           "unknown compatibility level", seal=True)


def test_policy_effective_version_beyond_last(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    body(env)["policies"].append(
        {"level": "NONE", "effective_version": 9, "changed_at": "t"})
    expect(PackageInconsistentError, env, judge, "policy-out-of-range",
           "policy effective_version cannot point past last+1", seal=True)


def test_conclusion_level_disagrees_with_history(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    body(env)["conclusions"][0]["level"] = "NONE"
    expect(PackageInconsistentError, env, judge, "conclusion-level-lie",
           "conclusion policy must equal policy_at(version)", seal=True)


def test_conclusion_verdict_does_not_reproduce(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    c = body(env)["conclusions"][1]  # int->long is compatible under BACKWARD
    assert c["compatible"] is True
    c["compatible"] = False
    c["incompatibility_codes"] = ["FIELD_TYPE_CHANGED"]
    expect(PackageInconsistentError, env, judge, "conclusion-verdict-lie",
           "recorded verdict must reproduce from schemas+policy", seal=True)


def test_duplicate_subject_in_package(valid_envelope, judge):
    env = copy.deepcopy(valid_envelope)
    env["subjects"].append(copy.deepcopy(env["subjects"][0]))
    expect(PackageInconsistentError, env, judge, "dup-subject",
           "one package cannot name the same subject twice", seal=True)


def test_import_failure_changes_nothing(valid_envelope, judge):
    """A corrupted/inconsistent import leaves the target exactly as before."""
    svc = RegistryService()
    svc.register("resident", V1)
    before = [(r.version, r.schema_id) for r in svc.list_versions("resident")]
    with pytest.raises(PackageCorruptedError):
        svc.import_package(b"nonsense")
    env = copy.deepcopy(valid_envelope)
    body(env)["versions"][0]["schema_id"] = "f" * 16
    with pytest.raises(PackageCorruptedError):  # stale subject checksum
        svc.import_package(reencode(env))
    with pytest.raises(PackageInconsistentError):  # valid checksum, lies
        svc.import_package(reencode(reseal(env)))
    after = [(r.version, r.schema_id) for r in svc.list_versions("resident")]
    judge("no-partial-import", {"resident": "v1 before"},
          "validation precedes every mutation",
          {"subjects": svc.store.list_subjects(), "resident": after})
    assert before == after
    assert "u" not in svc.store.list_subjects()


def test_all_migration_errors_share_category():
    for cls in (PackageCorruptedError, PackageContentMissingError,
                PackageInconsistentError, ImportConflictError):
        assert issubclass(cls, MigrationError)
