"""Time-report calculation over current operational entries."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, JsonValue

from timetracker.api.v0.discovery import json_type
from timetracker.domain.entries import EntryRecord, effective_start


class ReportDefinition(BaseModel):
    id: str
    title: str
    path: str


class ReportGroup(BaseModel):
    missing: bool
    value: JsonValue
    duration_seconds: int
    entry_count: int


class TimeReport(BaseModel):
    group_by: str
    entry_count: int
    duration_seconds: int
    groups: list[ReportGroup]


def _identity(value: JsonValue) -> tuple[object, ...]:
    """Hashable identity with the same type-strict semantics as JSON filters."""
    if isinstance(value, dict):
        return ("object", tuple((key, _identity(item)) for key, item in sorted(value.items())))
    if isinstance(value, list):
        return ("array", tuple(_identity(item) for item in value))
    return (json_type(value), value)


def time_report(
    records: Iterable[EntryRecord],
    *,
    group_by: str,
    start: datetime | None,
    end: datetime | None,
) -> TimeReport:
    """Group each matching entry's full duration by task or an attribute."""
    grouped: dict[tuple[bool, tuple[object, ...]], ReportGroup] = {}
    attribute = group_by.removeprefix("attribute:")
    for record in records:
        when = effective_start(record)
        if (start is not None and when < start) or (end is not None and when >= end):
            continue
        missing = group_by != "task" and attribute not in record.attributes
        value: JsonValue = (
            record.task if group_by == "task" else record.attributes.get(attribute)
        )
        key = (missing, _identity(value))
        if key in grouped:
            grouped[key].entry_count += 1
            grouped[key].duration_seconds += record.duration_seconds
        else:
            grouped[key] = ReportGroup(
                missing=missing,
                value=value,
                duration_seconds=record.duration_seconds,
                entry_count=1,
            )
    groups = list(grouped.values())
    groups.sort(
        key=lambda group: (
            group.missing,
            json_type(group.value),
            json.dumps(group.value, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        )
    )
    return TimeReport(
        group_by=group_by,
        entry_count=sum(group.entry_count for group in groups),
        duration_seconds=sum(group.duration_seconds for group in groups),
        groups=groups,
    )
