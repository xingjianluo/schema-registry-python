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
