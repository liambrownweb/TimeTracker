from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from timetracker.api.app import create_app
from timetracker.config import load_config


@pytest.fixture
def client(tmp_path: Path):
    settings = load_config(
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")},
        overrides={"deep_check": "manual"},
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def create(client: TestClient, task: str, duration: int, **fields: object) -> dict:
    response = client.post(
        "/v0/entries",
        json={"id": str(uuid4()), "task": task, "duration_seconds": duration, **fields},
    )
    assert response.status_code == 201
    return response.json()


def test_report_discovery_and_empty_report(client: TestClient) -> None:
    assert client.get("/v0/reports").json() == [
        {"id": "time", "title": "Time by group", "path": "/v0/reports/time"}
    ]
    assert client.get("/v0/reports/time").json() == {
        "group_by": "task", "entry_count": 0, "duration_seconds": 0, "groups": []
    }
    assert "/v0/reports/time" in client.get("/openapi.json").json()["paths"]


def test_task_totals_follow_updates_and_deletes(client: TestClient) -> None:
    first = create(client, "Writing", 90)
    second = create(client, "Writing", 20)
    create(client, "Reading", 30)
    report = client.get("/v0/reports/time").json()
    assert report["entry_count"] == 3 and report["duration_seconds"] == 140
    assert report["groups"] == [
        {"missing": False, "value": "Reading", "duration_seconds": 30, "entry_count": 1},
        {"missing": False, "value": "Writing", "duration_seconds": 110, "entry_count": 2},
    ]
    assert client.patch(f"/v0/entries/{first['id']}", json={"duration_seconds": 10, "task": "Reading"}).status_code == 200
    assert client.delete(f"/v0/entries/{second['id']}").status_code == 204
    report = client.get("/v0/reports/time").json()
    assert report["entry_count"] == 2 and report["duration_seconds"] == 40
    assert report["groups"] == [
        {"missing": False, "value": "Reading", "duration_seconds": 40, "entry_count": 2}
    ]


def test_dates_are_utc_start_inclusive_end_exclusive_and_use_full_duration(client: TestClient) -> None:
    create(client, "before", 999, started_at="2026-10-31T23:59:59Z")
    create(client, "included", 90000, started_at="2026-11-01T00:00:00Z")
    create(client, "included", 10, started_at="2026-11-30T23:59:59Z")
    create(client, "excluded", 15, started_at="2026-12-01T00:00:00Z")
    response = client.get("/v0/reports/time", params={
        "from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z"
    })
    assert response.status_code == 200
    assert response.json()["entry_count"] == 2
    assert response.json()["duration_seconds"] == 90010


def test_undated_entry_uses_actual_created_at_and_edits_change_range(client: TestClient) -> None:
    entry = create(client, "undated", 12)
    created = entry["created_at"]
    response = client.get("/v0/reports/time", params={"from": created})
    assert response.status_code == 200 and response.json()["entry_count"] == 1
    response = client.get("/v0/reports/time", params={"to": created})
    assert response.status_code == 200 and response.json()["entry_count"] == 0
    assert client.patch(f"/v0/entries/{entry['id']}", json={"started_at": "2020-01-01T00:00:00Z"}).status_code == 200
    assert client.get("/v0/reports/time", params={"from": created}).json()["entry_count"] == 0
    assert client.patch(f"/v0/entries/{entry['id']}", json={"started_at": None}).status_code == 200
    assert client.get("/v0/reports/time", params={"from": created}).json()["entry_count"] == 1


def test_attribute_grouping_preserves_types_and_missing_distinct_from_null(client: TestClient) -> None:
    create(client, "a", 1, attributes={"x": 1})
    create(client, "b", 2, attributes={"x": 1.0})
    create(client, "c", 3, attributes={"x": True})
    create(client, "d", 4, attributes={"x": None})
    create(client, "e", 5)
    create(client, "f", 6, attributes={"x": {"b": 2, "a": [1]}})
    create(client, "g", 7, attributes={"x": {"a": [1], "b": 2}})
    report = client.get("/v0/reports/time", params={"group_by": "x"}).json()
    assert (report["entry_count"], report["duration_seconds"]) == (7, 28)
    groups = report["groups"]
    assert len(groups) == 6
    assert next(g for g in groups if g["missing"])["duration_seconds"] == 5
    assert next(g for g in groups if not g["missing"] and g["value"] is None)["duration_seconds"] == 4
    assert next(g for g in groups if type(g["value"]) is bool)["duration_seconds"] == 3
    assert next(g for g in groups if type(g["value"]) is int)["duration_seconds"] == 1
    assert next(g for g in groups if type(g["value"]) is float)["duration_seconds"] == 2
    assert next(g for g in groups if isinstance(g["value"], dict))["entry_count"] == 2


def test_task_named_attribute_and_type_strict_filters(client: TestClient) -> None:
    create(client, "work", 10, attributes={"task": "attribute", "n": True})
    create(client, "work", 20, attributes={"task": "other", "n": 1})
    params = {"group_by": "attribute:task", "task": "work", "attributes": json.dumps({"n": 1})}
    report = client.get("/v0/reports/time", params=params).json()
    assert report["duration_seconds"] == 20
    assert report["groups"][0]["value"] == "other"


def test_nested_json_groups_are_type_strict_and_zero_float_is_equal(client: TestClient) -> None:
    create(client, "a", 1, attributes={"x": {"n": [0.0]}})
    create(client, "b", 2, attributes={"x": {"n": [-0.0]}})
    create(client, "c", 4, attributes={"x": {"n": [0]}})
    create(client, "d", 8, attributes={"x": {"n": [False]}})
    groups = client.get("/v0/reports/time", params={"group_by": "x"}).json()["groups"]
    assert len(groups) == 3
    assert sorted(group["duration_seconds"] for group in groups) == [3, 4, 8]


def test_reports_do_not_read_journal_during_requests(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(client, "work", 30)
    assert isinstance(client.app, FastAPI)
    journal = client.app.state.coordinator._journal

    def fail_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected journal read")

    monkeypatch.setattr(journal, "read", fail_read)
    assert client.get("/v0/reports/time").json()["duration_seconds"] == 30


def test_report_after_restart(tmp_path: Path) -> None:
    settings = load_config(
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")},
        overrides={"deep_check": "manual"},
    )
    with TestClient(create_app(settings)) as first:
        create(first, "work", 10, attributes={"project": "home"})
        expected = first.get("/v0/reports/time", params={"group_by": "project"}).json()
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/v0/reports/time", params={"group_by": "project"}).json() == expected


@pytest.mark.parametrize("params", [
    {"from": "2026-11-01"}, {"from": "2026-11-01T00:00:00+02:00"},
    {"from": "nonsense"}, {"from": "2026-11-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
    {"from": "2026-12-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
    {"group_by": ""}, {"attributes": "[]"}, {"attributes": '{"x":NaN}'},
])
def test_invalid_parameters_return_standard_422(client: TestClient, params: dict[str, str]) -> None:
    response = client.get("/v0/reports/time", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
