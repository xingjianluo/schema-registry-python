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


# ---------------------------------------------------------------------------
# Migration endpoints
# ---------------------------------------------------------------------------

MV1 = {"type": "record", "name": "Mig", "fields": [{"name": "id", "type": "int"}]}
MV2 = {"type": "record", "name": "Mig", "fields": [
    {"name": "id", "type": "long"}]}


def _client_with_subject():
    svc = RegistryService()
    svc.register("mig", MV1)
    svc.register("mig", MV2)
    return TestClient(create_app(svc))


def test_http_export_import_roundtrip(judge):
    src = _client_with_subject()
    r = src.post("/migration/export", json={"subjects": ["mig"]})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    package = r.content

    dst = TestClient(create_app(RegistryService()))
    imp = dst.post("/migration/import?mode=REJECT", content=package,
                   headers={"content-type": "application/octet-stream"})
    body = imp.json()
    judge("http-migration", {"export": ["mig"], "mode": "REJECT"},
          "binary package moves across HTTP into empty instance",
          {"status": imp.status_code, "body": body})
    assert imp.status_code == 200
    assert body["imported"][0]["added_versions"] == [1, 2]
    got = dst.get("/subjects/mig/versions/2").json()
    assert got["schema"] == MV2
    # history reproducible over HTTP
    rc = dst.get("/subjects/mig/compatibility/recheck/2").json()
    assert rc["is_compatible"] is True


def test_http_export_all_default_body():
    src = _client_with_subject()
    r = src.post("/migration/export", json={})  # empty body -> all subjects
    assert r.status_code == 200 and len(r.content) > 0


def test_http_import_conflict_and_merge(judge):
    package = _client_with_subject().post(
        "/migration/export", json={"subjects": ["mig"]}).content

    dst = TestClient(create_app(RegistryService()))
    # pre-existing different v1
    dst.post("/subjects/mig/versions",
             json={"schema": {"type": "record", "name": "Mig", "fields": [
                 {"name": "id", "type": "string"}]}})

    reject = dst.post("/migration/import?mode=REJECT", content=package,
                      headers={"content-type": "application/octet-stream"})
    merge = dst.post("/migration/import?mode=MERGE", content=package,
                     headers={"content-type": "application/octet-stream"})
    judge("http-conflict-merge", {"target_v1": "id:string", "incoming": "id:int"},
          "REJECT -> 409 IMPORT_CONFLICT; MERGE -> 409 same-number diff content",
          {"reject": reject.status_code, "merge": merge.status_code,
           "reject_err": reject.json()["error"]})
    assert reject.status_code == 409
    assert reject.json()["error"] == "IMPORT_CONFLICT"
    assert merge.status_code == 409
    # nothing got through
    versions = dst.get("/subjects/mig/versions").json()
    assert [v["version"] for v in versions] == [1]


def test_http_import_corrupted_package(judge):
    dst = TestClient(create_app(RegistryService()))
    r = dst.post("/migration/import", content=b"###not-json###",
                 headers={"content-type": "application/octet-stream"})
    judge("http-corrupt", {"bytes": "###not-json###"},
          "parse failure -> 422 PACKAGE_CORRUPTED", r.json())
    assert r.status_code == 422
    assert r.json()["error"] == "PACKAGE_CORRUPTED"


def test_http_import_bad_mode():
    dst = TestClient(create_app(RegistryService()))
    r = dst.post("/migration/import?mode=WHATEVER", content=b"{}",
                 headers={"content-type": "application/octet-stream"})
    assert r.status_code == 422
