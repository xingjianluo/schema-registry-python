"""End-to-end HTTP tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
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


def test_http_import_corrupt_returns_400(client, judge):
    """Tampered content, truncated JSON or missing signature => 400."""
    _seed(client)
    bundle = client.post("/migration/export",
                         json={"subjects": ["user"]}).json()
    import copy
    import json as _json
    cases = {}

    tampered = copy.deepcopy(bundle)
    tampered["subjects"][0]["versions"][0]["registered_at"] = "1999"
    cases["tampered"] = tampered

    cases["truncated-raw"] = _json.dumps(bundle)[:40]

    unsigned = {k: v for k, v in bundle.items() if k != "integrity"}
    cases["unsigned"] = unsigned

    results = {}
    for name, body in cases.items():
        r = client.post("/migration/import",
                        json={"bundle": body, "on_conflict": "reject"})
        results[name] = (r.status_code, r.json().get("error"))
    judge("http-import-400", {"cases": list(cases)},
          "tampered/truncated/unsigned bundles are rejected before any "
          "write with 400 BUNDLE_CORRUPT",
          results)
    assert all(v == (400, "BUNDLE_CORRUPT") for v in results.values())
    # target instance completely unchanged by rejected imports
    assert len(client.get("/subjects/user/versions").json()) == 2


def test_http_import_malformed_and_inconsistent_422(client, judge):
    _seed(client)
    bundle = client.post("/migration/export",
                         json={"subjects": ["user"]}).json()
    from app.migration import compute_integrity

    def reseal(b):
        b["integrity"] = compute_integrity(b)
        return b

    bad_structure = client.post(
        "/migration/import", json={"bundle": {"format": "nope"}})

    malformed = reseal({**bundle, "format_version": 99})
    r_malformed = client.post(
        "/migration/import", json={"bundle": malformed})

    tampered = {
        **bundle, "subjects": [
            {**bundle["subjects"][0],
             "versions": [{**bundle["subjects"][0]["versions"][0],
                           "schema_id": "0" * 16}
                          ] + bundle["subjects"][0]["versions"][1:]}]}
    # content deliberately contradicts itself, but it is properly signed so
    # the request reaches semantic validation (otherwise it would be 400).
    r_tampered = client.post("/migration/import",
                             json={"bundle": reseal(tampered)})

    incomplete = reseal({
        **bundle, "subjects": [
            {**bundle["subjects"][0],
             "versions": [{k: v for k, v in
                           bundle["subjects"][0]["versions"][0].items()
                           if k != "policy"}]
             + bundle["subjects"][0]["versions"][1:]}]})
    r_incomplete = client.post(
        "/migration/import", json={"bundle": incomplete})

    judge("http-import-422",
          {"cases": ["bad structure", "malformed", "tampered id",
                     "missing policy"]},
          "unsigned envelope -> 400; shape/type -> BUNDLE_MALFORMED; "
          "schema_id contradiction -> BUNDLE_INCONSISTENT; missing content "
          "-> BUNDLE_INCOMPLETE",
          {"unsigned": bad_structure.status_code,
           "malformed": r_malformed.json(),
           "tampered": r_tampered.json(),
           "incomplete": r_incomplete.json()})
    assert bad_structure.status_code == 400
    assert bad_structure.json()["error"] == "BUNDLE_CORRUPT"
    assert r_malformed.status_code == 422
    assert r_malformed.json()["error"] == "BUNDLE_MALFORMED"
    assert r_tampered.status_code == 422
    assert r_tampered.json()["error"] == "BUNDLE_INCONSISTENT"
    assert r_incomplete.status_code == 422
    assert r_incomplete.json()["error"] == "BUNDLE_INCOMPLETE"


def test_http_export_unknown_subject_404(client):
    r = client.post("/migration/export", json={"subjects": ["ghost"]})
    assert r.status_code == 404
