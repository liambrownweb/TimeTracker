"""Field discovery and current-value completion responses."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from timetracker.domain.entries import EntryRecord

JsonType = Literal["null", "boolean", "integer", "number", "string", "array", "object"]


class FieldInfo(BaseModel):
    name: str
    types: list[JsonType]


class CompletionResponse(BaseModel):
    field: str
    values: list[JsonValue]


def json_type(value: object) -> JsonType:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def discover_fields(records: Iterable[EntryRecord]) -> list[FieldInfo]:
    observed: dict[str, set[JsonType]] = {}
    for record in records:
        for name, value in record.attributes.items():
            observed.setdefault(name, set()).add(json_type(value))
    return [
        FieldInfo(name=name, types=sorted(types))
        for name, types in sorted(observed.items())
    ]


def _canonical(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _match_text(value: JsonValue) -> str | None:
    if isinstance(value, (dict, list)):
        return None
    return value if isinstance(value, str) else _canonical(value)


def completions(
    records: Iterable[EntryRecord],
    *,
    field: str,
    query: str,
    limit: int,
) -> CompletionResponse:
    """Return unique current scalar values matching a case-insensitive prefix."""
    candidates: dict[str, JsonValue] = {}
    for record in records:
        value: JsonValue | None
        if field == "task":
            value = record.task
        elif field in record.attributes:
            value = record.attributes[field]
        else:
            continue
        match = _match_text(value)
        if match is None or not match.casefold().startswith(query.casefold()):
            continue
        candidates.setdefault(_canonical(value), value)
    ordered = sorted(
        candidates.values(),
        key=lambda value: (
            (_match_text(value) or "").casefold(),
            json_type(value),
            _canonical(value),
        ),
    )
    return CompletionResponse(field=field, values=ordered[:limit])
