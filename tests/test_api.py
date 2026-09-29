"""End-to-end HTTP tests."""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.migration import seal_bundle
from app.models import CompatibilityLevel
from app.service import RegistryService

V1 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"}]}
V2_BAD = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "string"}]}
V2_OK = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "long"}]}


@pytest.fixture
def client():
    return TestClient(create_app(RegistryService()))


def test_register_and_fetch(client, judge):
    r = client.post("/subjects/user/versions", json={"schema": V1})
    body = r.json()
    judge("http-register", {"schema": V1}, "default BACKWARD, first version",
          {"status": r.status_code, "body": body})
    assert r.status_code == 200 and body["version"] == 1
    got = client.get("/subjects/user/versions/1").json()
    assert got["schema"] == V1


def test_incompatible_returns_409_with_categorized_reasons(client, judge):
    client.post("/subjects/user/versions", json={"schema": V1})
    r = client.post("/subjects/user/versions", json={"schema": V2_BAD})
    body = r.json()
    judge("http-409", {"candidate": V2_BAD},
          "BACKWARD int->string -> FIELD_TYPE_CHANGED",
          {"status": r.status_code, "body": body})
    assert r.status_code == 409
    assert body["error"] == "INCOMPATIBLE_SCHEMA"
    assert body["incompatibilities"][0]["code"] == "FIELD_TYPE_CHANGED"
    assert body["incompatibilities"][0]["path"] == "/id"


def test_invalid_schema_returns_422(client):
    r = client.post("/subjects/user/versions",
                    json={"schema": {"type": "record"}})
    assert r.status_code == 422
    assert r.json()["error"] == "SCHEMA_INVALID"


def test_precheck_is_readonly(client, judge):
    client.post("/subjects/user/versions", json={"schema": V1})
    r1 = client.post("/subjects/user/compatibility", json={"schema": V2_BAD})
    r2 = client.post("/subjects/user/compatibility", json={"schema": V2_BAD})
    versions = client.get("/subjects/user/versions").json()
    judge("http-precheck", {}, "precheck never registers",
          {"is_compatible": r1.json()["is_compatible"],
           "cached": r1.json() == r2.json(), "versions": len(versions)})
    assert r1.status_code == 200 and r1.json()["is_compatible"] is False
    assert len(versions) == 1


def test_policy_change_then_register_and_recheck(client, judge):
    client.post("/subjects/user/versions", json={"schema": V1})
    ok = client.post("/subjects/user/versions", json={"schema": V2_OK})
    assert ok.status_code == 200  # v2 under BACKWARD (int->long promoted)

    put = client.put("/subjects/user/compatibility/policy",
                     json={"compatibility": "FULL"})
    assert put.status_code == 200
    assert put.json()["effective_version"] == 3

    # historical v2 reproduced under BACKWARD despite current FULL
    rc = client.get("/subjects/user/compatibility/recheck/2").json()
    judge("http-recheck", {"version": 2},
          "recheck uses policy effective at v2 registration",
          rc)
    assert rc["is_compatible"] is True
    assert rc["compatibility"] == "BACKWARD"


def test_unknown_policy_level_422(client):
    r = client.put("/subjects/user/compatibility/policy",
                   json={"compatibility": "SIDEWAYS"})
    assert r.status_code == 422


def test_missing_version_404(client):
    assert client.get("/subjects/user/versions/9").status_code == 404


# -- migration endpoints -------------------------------------------------------


def _seed(client):
    client.post("/subjects/user/versions", json={"schema": V1})
    client.put("/subjects/user/compatibility/policy",
               json={"compatibility": "NONE"})
    client.post("/subjects/user/versions", json={"schema": V2_BAD})


def test_http_export_import_roundtrip(client, judge):
    _seed(client)
    exported = client.post("/migration/export",
                           json={"subjects": ["user"]})
    assert exported.status_code == 200
    bundle = exported.json()

    other = TestClient(create_app(RegistryService()))
    imported = other.post("/migration/import",
                          json={"bundle": bundle, "on_conflict": "reject"})
    recheck = other.get("/subjects/user/compatibility/recheck/2").json()
    judge("http-migration-roundtrip", {"subjects": ["user"]},
          "bundle over HTTP reproduces versions, policy and conclusions",
          {"import": imported.json(), "recheck_v2": recheck})
    assert imported.status_code == 200
    assert imported.json()["imported_versions"] == 2
    assert recheck["is_compatible"] is True
    assert recheck["compatibility"] == "NONE"


def test_http_import_conflict_409(client, judge):
    _seed(client)
    bundle = client.post("/migration/export",
                         json={"subjects": ["user"]}).json()
    r = client.post("/migration/import",
                    json={"bundle": bundle, "on_conflict": "reject"})
    judge("http-import-conflict", {"mode": "reject"},
          "subject already exists -> 409 IMPORT_CONFLICT",
          {"status": r.status_code, "body": r.json()})
    assert r.status_code == 409
    assert r.json()["error"] == "IMPORT_CONFLICT"


def test_http_import_error_categories_422(client, judge):
    _seed(client)
    bundle = client.post("/migration/export",
                         json={"subjects": ["user"]}).json()

    def _post(b):
        return client.post("/migration/import",
                           json={"bundle": b, "on_conflict": "reject"})

    # 1) corrupt: hand-written object without an integrity envelope
    no_envelope = _post({"format": "nope"})
    # 2) corrupt: real package modified after export (digest mismatch)
    tampered = _post({**bundle, "subjects": [
        {**bundle["subjects"][0],
         "versions": [{**bundle["subjects"][0]["versions"][0],
                       "registered_at": "2000-01-01T00:00:00+00:00"}]
                      + bundle["subjects"][0]["versions"][1:]}]})
    # 3) corrupt: truncated raw document uploaded as a string
    truncated = _post(json.dumps(bundle)[:150])
    # 4) malformed: authentic envelope, wrong format
    malformed = _post(seal_bundle({**bundle, "format": "other"}))
    # 5) incomplete: authentic envelope, base policy missing
    subj = {k: v for k, v in bundle["subjects"][0].items()
            if k != "base_policy"}
    incomplete = _post(seal_bundle(
        {**bundle, "subjects": [subj]}))
    # 6) inconsistent: authentic envelope, schema_id contradicts schema
    bad_id = copy.deepcopy(bundle)
    bad_id["subjects"][0]["versions"][0]["schema_id"] = "0" * 16
    inconsistent = _post(seal_bundle(bad_id))

    bodies = {
        "no_envelope": no_envelope.json(), "tampered": tampered.json(),
        "truncated": truncated.json(), "malformed": malformed.json(),
        "incomplete": incomplete.json(), "inconsistent": inconsistent.json()}
    judge("http-import-422",
          {"cases": list(bodies)},
          "corrupt (no envelope / tampered / truncated) -> BUNDLE_CORRUPT; "
          "malformed -> BUNDLE_MALFORMED; missing data -> "
          "BUNDLE_INCOMPLETE; contradiction -> BUNDLE_INCONSISTENT; all 422",
          bodies)
    assert no_envelope.status_code == 422
    assert no_envelope.json()["error"] == "BUNDLE_CORRUPT"
    assert tampered.status_code == 422
    assert tampered.json()["error"] == "BUNDLE_CORRUPT"
    assert truncated.status_code == 422
    assert truncated.json()["error"] == "BUNDLE_CORRUPT"
    assert malformed.json()["error"] == "BUNDLE_MALFORMED"
    assert incomplete.json()["error"] == "BUNDLE_INCOMPLETE"
    assert inconsistent.json()["error"] == "BUNDLE_INCONSISTENT"
    for r in (no_envelope, tampered, truncated, malformed, incomplete,
              inconsistent):
        assert r.status_code == 422


def test_http_import_across_different_default_policies(judge):
    """HTTP end-to-end: a FORWARD-default source's subject imports into a
    BACKWARD-default instance and keeps its FORWARD historical conclusion."""
    src_app = create_app(RegistryService(
        default_level=CompatibilityLevel.FORWARD))
    src = TestClient(src_app)
    v_required_add = {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "int"},
        {"name": "nick", "type": "string"}]}  # required field: FORWARD-only
    src.post("/subjects/user/versions", json={"schema": V1})
    accepted = src.post("/subjects/user/versions",
                        json={"schema": v_required_add})
    assert accepted.status_code == 200
    bundle = src.post("/migration/export",
                      json={"subjects": ["user"]}).json()

    tgt = TestClient(create_app(RegistryService(
        default_level=CompatibilityLevel.BACKWARD)))
    imported = tgt.post("/migration/import",
                        json={"bundle": bundle, "on_conflict": "reject"})
    recheck = tgt.get("/subjects/user/compatibility/recheck/2").json()
    judge("http-cross-default", {"src": "FORWARD", "tgt": "BACKWARD"},
          "import succeeds and v2 keeps FORWARD compatibility on target",
          {"import": imported.json(), "recheck": recheck})
    assert imported.status_code == 200
    assert imported.json()["imported_versions"] == 2
    assert recheck["is_compatible"] is True
    assert recheck["compatibility"] == "FORWARD"


def test_http_export_unknown_subject_404(client):
    r = client.post("/migration/export", json={"subjects": ["ghost"]})
    assert r.status_code == 404
