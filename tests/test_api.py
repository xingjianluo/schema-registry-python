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


def test_http_import_malformed_and_inconsistent_422(client, judge):
    _seed(client)
    bundle = client.post("/migration/export",
                         json={"subjects": ["user"]}).json()

    bad_structure = client.post("/migration/import",
                                json={"bundle": {"format": "nope"}})
    tampered = client.post("/migration/import", json={"bundle": {
        **bundle, "subjects": [
            {**bundle["subjects"][0],
             "versions": [{**bundle["subjects"][0]["versions"][0],
                           "schema_id": "0" * 16}
                          ] + bundle["subjects"][0]["versions"][1:]}]}})
    judge("http-import-422", {"cases": ["bad structure", "tampered id"]},
          "structural -> BUNDLE_MALFORMED, contradiction -> "
          "BUNDLE_INCONSISTENT, both 422",
          {"structure": bad_structure.json(), "tampered": tampered.json()})
    assert bad_structure.status_code == 422
    assert bad_structure.json()["error"] == "BUNDLE_MALFORMED"
    assert tampered.status_code == 422
    assert tampered.json()["error"] == "BUNDLE_INCONSISTENT"


def test_http_export_unknown_subject_404(client):
    r = client.post("/migration/export", json={"subjects": ["ghost"]})
    assert r.status_code == 404
