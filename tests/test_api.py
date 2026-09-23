"""HTTP API 端到端测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditLogger
from app.registry import SchemaRegistry
from app.store import LocalStore
from main import app


@pytest.fixture()
def client(tmp_path):
    app.state.registry = SchemaRegistry(
        LocalStore(str(tmp_path / "api.json")), audit=AuditLogger("api.test")
    )
    return TestClient(app)


SCHEMA = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "int"}]}


def test_register_and_fetch(client):
    r = client.post("/subjects/users/versions", json={"schema": SCHEMA})
    assert r.status_code == 200
    assert r.json()["version"] == 1
    r = client.get("/subjects/users/versions/1")
    assert r.status_code == 200 and r.json()["schema"]["name"] == "User"
    assert "users" in client.get("/subjects").json()


def test_incompatible_returns_classified_error(client):
    client.post("/subjects/users/versions", json={"schema": SCHEMA})
    changed = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "string"}]}
    r = client.post("/subjects/users/versions", json={"schema": changed})
    assert r.status_code == 409
    body = r.json()
    assert body["error_code"] == "SCHEMA_INCOMPATIBLE"
    codes = {i["code"] for i in body["detail"]["report"]["incompatibilities"]}
    assert codes == {"FIELD_TYPE_CHANGED"}


def test_precheck_does_not_mutate(client):
    client.post("/subjects/users/versions", json={"schema": SCHEMA})
    r = client.post(
        "/compatibility/subjects/users/versions",
        json={"schema": {"type": "record", "name": "User", "fields": [{"name": "id", "type": "bytes"}]}},
    )
    assert r.json()["report"]["compatible"] is False
    assert client.get("/subjects/users/versions").json() == [
        {"subject": "users", "version": 1, "id": r.json()["report"]["baseline_fingerprint"], "created_at": client.get("/subjects/users/versions/1").json()["created_at"]}
    ]


def test_policy_update_and_historical_verdict(client):
    client.post("/subjects/users/versions", json={"schema": SCHEMA})
    assert client.put("/subjects/users/policy", json={"compatibility": "NONE"}).status_code == 200
    assert client.get("/subjects/users/policy").json()["compatibility"] == "NONE"
    changed = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "bytes"}]}
    assert client.post("/subjects/users/versions", json={"schema": changed}).status_code == 200
    verdict = client.get("/subjects/users/versions/2/verdict").json()["report"]
    assert verdict["compatible"] is True and verdict["compatibility_level"] == "NONE"
    replay = client.get(
        "/subjects/users/versions/2/verdict", params={"compatibility": "BACKWARD"}
    ).json()["report"]
    assert replay["compatible"] is False


def test_invalid_policy_and_missing_subject(client):
    r = client.put("/config", json={"compatibility": "BOGUS"})
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_POLICY"
    r = client.get("/subjects/nope/versions")
    assert r.status_code == 404 and r.json()["error_code"] == "SUBJECT_NOT_FOUND"
