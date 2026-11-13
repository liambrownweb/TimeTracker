from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from timetracker.api.app import create_app
from timetracker.config import load_config


@pytest.fixture
def client(tmp_path: Path):
    settings = load_config(
        env={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        overrides={"deep_check": "manual"},
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def create(client: TestClient, task: str, attributes: dict[str, object] | None = None) -> str:
    entry_id = str(uuid4())
    response = client.post(
        "/v0/entries",
        json={
            "id": entry_id,
            "task": task,
            "duration_seconds": 60,
            "attributes": attributes or {},
        },
    )
    assert response.status_code == 201
    return entry_id


def test_fields_discover_current_live_attribute_types(client: TestClient) -> None:
    first = create(client, "Writing", {"location": "Home", "billable": True, "count": 1})
    create(client, "Reading", {"location": "Office", "billable": False, "count": 2.5})
    create(client, "Planning", {"nullable": None})
    create(client, "Complex", {"tags": ["a", "b"], "details": {"room": "blue"}})

    fields = client.get("/v0/fields")
    assert fields.status_code == 200
    assert fields.json() == [
        {"name": "billable", "types": ["boolean"]},
        {"name": "count", "types": ["integer", "number"]},
        {"name": "details", "types": ["object"]},
        {"name": "location", "types": ["string"]},
        {"name": "nullable", "types": ["null"]},
        {"name": "tags", "types": ["array"]},
    ]

    assert client.delete(f"/v0/entries/{first}").status_code == 204
    assert "Home" not in str(client.get("/v0/completions", params={"field": "location"}).json())
    assert client.get("/v0/completions", params={"field": "tags"}).json()["values"] == []


def test_completion_is_casefolded_prefix_sorted_deduplicated_and_limited(
    client: TestClient,
) -> None:
    create(client, "Zebra", {"place": "zoo"})
    create(client, "alpha", {"place": "Zone"})
    create(client, "ALPaca", {"place": "zone"})
    create(client, "alphabet", {"place": "Office"})

    response = client.get("/v0/completions", params={"field": "task", "q": "AL", "limit": 2})
    assert response.status_code == 200
    assert response.json() == {"field": "task", "values": ["ALPaca", "alpha"]}

    places = client.get("/v0/completions", params={"field": "place", "q": "zo"})
    assert places.json() == {"field": "place", "values": ["Zone", "zone", "zoo"]}


def test_scalar_attribute_completions_preserve_json_types(client: TestClient) -> None:
    create(client, "one", {"enabled": True, "amount": 1, "nullable": None})
    create(client, "two", {"enabled": False, "amount": 1.0, "nullable": "none"})

    for query, field, expected in (
        ("t", "enabled", [True]),
        ("f", "enabled", [False]),
        ("1", "amount", [1, 1.0]),
        ("nu", "nullable", [None]),
    ):
        response = client.get("/v0/completions", params={"field": field, "q": query})
        assert response.status_code == 200
        assert response.json()["values"] == expected
    numeric_values = client.get(
        "/v0/completions", params={"field": "amount", "q": "1"}
    ).json()["values"]
    assert [type(value) for value in numeric_values] == [int, float]


def test_updates_deletions_and_restart_change_discovery(
    tmp_path: Path,
) -> None:
    settings = load_config(
        env={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        overrides={"deep_check": "manual"},
    )
    with TestClient(create_app(settings)) as client:
        entry_id = create(client, "Old task", {"location": "Old office"})
        response = client.patch(
            f"/v0/entries/{entry_id}",
            json={"attributes": {"remove": ["location"]}},
        )
        assert response.status_code == 200
        response = client.patch(
            f"/v0/entries/{entry_id}",
            json={
                "task": "New task",
                "attributes": {"set": {"location": "New office"}},
            },
        )
        assert response.status_code == 200

    with TestClient(create_app(settings)) as client:
        assert client.get("/v0/completions", params={"field": "task", "q": "old"}).json()["values"] == []
        assert client.get("/v0/completions", params={"field": "task", "q": "new"}).json()["values"] == ["New task"]
        assert client.get("/v0/completions", params={"field": "location", "q": "old"}).json()["values"] == []
        assert client.get("/v0/completions", params={"field": "location", "q": "new"}).json()["values"] == ["New office"]
        assert client.delete(f"/v0/entries/{entry_id}").status_code == 204
        assert client.get("/v0/fields").json() == []
        assert client.get("/v0/completions", params={"field": "task"}).json()["values"] == []


@pytest.mark.parametrize(
    ("params", "expected_status"),
    [
        ({}, 422),
        ({"field": "task", "limit": "0"}, 422),
        ({"field": "task", "limit": "101"}, 422),
        ({"field": "", "q": "x"}, 422),
        ({"field": "task", "q": "x" * 129}, 422),
        ({"field": "missing", "q": "x"}, 200),
    ],
)
def test_completion_validation_and_unknown_field(
    client: TestClient, params: dict[str, str], expected_status: int
) -> None:
    response = client.get("/v0/completions", params=params)
    assert response.status_code == expected_status
    if expected_status == 422:
        assert response.json()["error"]["code"] == "INVALID_REQUEST"
    else:
        assert response.json() == {"field": "missing", "values": []}


def test_openapi_documents_discovery_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths["/v0/fields"]) == {"get"}
    assert set(paths["/v0/completions"]) == {"get"}
