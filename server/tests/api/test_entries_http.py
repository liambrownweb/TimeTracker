from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
import httpx
from fastapi.testclient import TestClient

from timetracker.api.app import create_app
from timetracker.config import load_config
from timetracker.service.errors import StoreUnavailableError
from timetracker.verification import VerificationError, verify


@pytest.fixture
def settings(tmp_path: Path):
    return load_config(
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")},
        overrides={"deep_check": "manual"},
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def payload(**changes: object) -> dict[str, object]:
    return {"id": str(uuid4()), "task": "work", "duration_seconds": 60, **changes}


def test_entry_lifecycle_retry_and_persistence(client: TestClient, settings) -> None:
    entry = payload(started_at="2026-10-01T10:00:00Z", attributes={"project": "a"})
    created = client.post("/v0/entries", json=entry)
    assert created.status_code == 201
    location = created.headers["location"]
    assert location == f"/v0/entries/{entry['id']}"
    assert created.json()["revision"] == 1
    assert not {"history", "last_event_sequence", "schema_version"} & created.json().keys()
    assert client.get(location).json() == created.json()

    updated = client.patch(location, json={"task": "updated", "attributes": {"set": {"project": "b"}}})
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    retried = client.post("/v0/entries", json=entry)
    assert retried.status_code == 200 and retried.json() == updated.json()
    assert client.post("/v0/entries", json={**entry, "task": "different"}).status_code == 409
    assert client.delete(location).status_code == 204
    assert client.delete(location).status_code == 204
    assert client.get(location).status_code == 410
    assert client.patch(location, json={"task": "x"}).status_code == 410
    assert client.post("/v0/entries", json=entry).json()["deleted"] is True
    assert client.get("/v0/entries").json() == []
    with pytest.raises(VerificationError, match="already in use"):
        verify(settings)


def test_restart_persists_entry(settings) -> None:
    entry = payload()
    with TestClient(create_app(settings)) as first:
        assert first.post("/v0/entries", json=entry).status_code == 201
    with TestClient(create_app(settings)) as second:
        assert second.get(f"/v0/entries/{entry['id']}").json()["task"] == "work"


def test_list_chronology_and_typed_filters(client: TestClient) -> None:
    later = payload(started_at="2026-10-03T00:00:00Z", attributes={"value": 1})
    early = payload(started_at="2026-10-01T00:00:00Z", attributes={"value": True})
    decimal = payload(started_at="2026-10-02T00:00:00Z", task="other", attributes={"value": 1.0})
    for entry in (later, early, decimal):
        assert client.post("/v0/entries", json=entry).status_code == 201
    assert [r["id"] for r in client.get("/v0/entries").json()] == [early["id"], decimal["id"], later["id"]]
    for value, expected in ((True, early), (1, later), (1.0, decimal)):
        response = client.get("/v0/entries", params={"attributes": json.dumps({"value": value})})
        assert [r["id"] for r in response.json()] == [expected["id"]]
    assert [r["id"] for r in client.get("/v0/entries", params={"task": "other"}).json()] == [decimal["id"]]
    assert client.get("/v0/entries", params={"task": "work", "attributes": '{"value":1}'}).json()[0]["id"] == later["id"]


@pytest.mark.parametrize("value", ['[]', '{', '{"x":NaN}', 'null', '1'])
def test_invalid_attribute_filter(client: TestClient, value: str) -> None:
    response = client.get("/v0/entries", params={"attributes": value})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_unknown_ids_and_invalid_requests_never_create(client: TestClient) -> None:
    entry_id = uuid4()
    for method in (client.get, client.patch, client.delete):
        response = method(f"/v0/entries/{entry_id}", **({"json": {"task": "x"}} if method == client.patch else {}))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ENTRY_NOT_FOUND"
    for body in (payload(id="bad"), payload(duration_seconds=True), payload(task=" "), payload(extra=1)):
        response = client.post("/v0/entries", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert client.get("/v0/entries").json() == []


def test_nonfinite_json_attribute_is_rejected(client: TestClient) -> None:
    entry = payload(attributes={"nested": [float("nan")]})
    response = client.post("/v0/entries", content=json.dumps(entry), headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert client.get("/v0/entries").json() == []
    valid = payload()
    assert client.post("/v0/entries", json=valid).status_code == 201
    update = {"attributes": {"set": {"nested": [float("inf")]}}}
    response = client.patch(f"/v0/entries/{valid['id']}", content=json.dumps(update), headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert client.get(f"/v0/entries/{valid['id']}").json()["revision"] == 1


def test_patch_set_remove_null_and_noop(client: TestClient) -> None:
    entry = payload(started_at="2026-10-01T00:00:00Z", attributes={"old": 1})
    assert client.post("/v0/entries", json=entry).status_code == 201
    url = f"/v0/entries/{entry['id']}"
    assert client.patch(url, json={"task": None}).status_code == 422
    changed = client.patch(url, json={"started_at": None, "attributes": {"set": {"new": None}, "remove": ["old"]}})
    assert changed.status_code == 200
    assert changed.json()["started_at"] is None and changed.json()["attributes"] == {"new": None}
    assert client.patch(url, json={}).json()["revision"] == 2


def test_503_is_safe_and_machine_readable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise StoreUnavailableError("private path /secret")
    monkeypatch.setattr(client.app.state.coordinator, "list", fail)
    response = client.get("/v0/entries")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORE_UNAVAILABLE"
    assert "/secret" not in response.text


def test_retry_after_committed_create_and_failed_record_write(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = payload()
    coordinator = client.app.state.coordinator
    store = coordinator._entries
    original = store.create

    def fail(record):
        raise OSError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "create", fail)
        assert client.post("/v0/entries", json=entry).status_code == 503
    assert store.create == original
    retry = client.post("/v0/entries", json=entry)
    assert retry.status_code == 200 and retry.json()["id"] == entry["id"]
    assert coordinator._journal.last_sequence == 1


def test_health_and_openapi(client: TestClient) -> None:
    assert client.get("/v0/health").json() == {"status": "ok"}
    assert client.get("/v0/entries/not-a-uuid").json()["error"]["code"] == "INVALID_REQUEST"
    assert client.get("/v0/missing").json()["error"]["code"] == "NOT_FOUND"
    wrong_method = client.put("/v0/entries")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "allow" in wrong_method.headers
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]["/v0/entries"]) == {"get", "post"}
    assert set(schema["paths"]["/v0/entries/{entry_id}"]) == {"get", "patch", "delete"}
    assert "201" in schema["paths"]["/v0/entries"]["post"]["responses"]
    assert "ErrorResponse" in schema["components"]["schemas"]


def test_serve_command_starts_an_http_server(settings) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "timetracker.cli", "serve", "--config",
         str(settings.data_dir.parent / "serve.toml"), "--data-dir",
         str(settings.data_dir), "--port", str(port), "--deep-check", "manual"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        with httpx.Client(trust_env=False) as http:
            while True:
                try:
                    response = http.get(f"http://127.0.0.1:{port}/v0/health", timeout=0.5)
                    break
                except httpx.ConnectError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError("server failed to start")
                    time.sleep(0.05)
            assert response.status_code == 200 and response.json() == {"status": "ok"}
    finally:
        process.terminate()
        process.communicate(timeout=10)
