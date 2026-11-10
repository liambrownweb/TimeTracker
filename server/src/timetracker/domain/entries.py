"""Validated entry state and domain transitions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    model_validator,
)


OPERATIONAL_SCHEMA_VERSION = 1
JOURNAL_EVENT_SCHEMA_VERSION = 1


def _parse_utc_datetime(value: object) -> datetime:
    if isinstance(value, str):
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("must be an ISO 8601 UTC timestamp") from exc

    if not isinstance(value, datetime):
        raise ValueError("must be an ISO 8601 UTC timestamp")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("must include a UTC timezone")
    return value.astimezone(timezone.utc)


def _format_utc_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    BeforeValidator(_parse_utc_datetime),
    PlainSerializer(_format_utc_datetime, return_type=str, when_used="json"),
]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EntryCreate(DomainModel):
    """Client-submitted, immutable identity and initial entry values."""

    id: UUID
    task: str = Field(min_length=1)
    duration_seconds: int = Field(ge=0)
    started_at: UtcDateTime | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def task_must_not_be_whitespace(self) -> EntryCreate:
        if not self.task.strip():
            raise ValueError("task must contain a non-whitespace character")
        return self


class EntrySnapshot(DomainModel):
    id: UUID
    task: str
    duration_seconds: int = Field(ge=0)
    started_at: UtcDateTime | None
    attributes: dict[str, JsonValue]


class AttributeDelta(DomainModel):
    """Set values and remove keys without conflating removal with JSON null."""

    set: dict[str, JsonValue] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def operations_must_be_unambiguous(self) -> AttributeDelta:
        if len(self.remove) != len(set(self.remove)):
            raise ValueError("attribute keys to remove must be unique")
        if set(self.set).intersection(self.remove):
            raise ValueError("an attribute cannot be both set and removed")
        return self


class EntryDelta(DomainModel):
    task: str | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    started_at: UtcDateTime | None = None
    attributes: AttributeDelta | None = None

    @model_validator(mode="after")
    def changed_fields_must_have_values(self) -> EntryDelta:
        for field_name in ("task", "duration_seconds", "attributes"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} changes cannot be null")
        if (
            "task" in self.model_fields_set
            and self.task is not None
            and not self.task.strip()
        ):
            raise ValueError("task must contain a non-whitespace character")
        return self


class JournalEventModel(DomainModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: int = Field(default=JOURNAL_EVENT_SCHEMA_VERSION, ge=1)
    event_id: UUID
    sequence: int = Field(ge=1)
    entry_id: UUID
    recorded_at: UtcDateTime


class CreateJournalEvent(JournalEventModel):
    event_type: Literal["create"]
    entry: EntryCreate

    @model_validator(mode="after")
    def entry_id_must_match_payload(self) -> CreateJournalEvent:
        if self.entry.id != self.entry_id:
            raise ValueError("entry_id must match the create payload")
        return self


class UpdateJournalEvent(JournalEventModel):
    event_type: Literal["update"]
    changes: EntryDelta

    @model_validator(mode="after")
    def changes_must_not_be_empty(self) -> UpdateJournalEvent:
        if not self.changes.model_fields_set:
            raise ValueError("an update event must contain at least one change")
        if (
            self.changes.attributes is not None
            and not self.changes.attributes.set
            and not self.changes.attributes.remove
        ):
            raise ValueError("an attribute delta must contain an operation")
        return self


class DeleteJournalEvent(JournalEventModel):
    event_type: Literal["delete"]


JournalEvent = Annotated[
    Union[CreateJournalEvent, UpdateJournalEvent, DeleteJournalEvent],
    Field(discriminator="event_type"),
]


class InitialHistory(DomainModel):
    kind: Literal["initial"]
    snapshot: EntrySnapshot


class UpdateHistory(DomainModel):
    kind: Literal["update"]
    recorded_at: UtcDateTime
    event_id: UUID
    changes: EntryDelta


class DeleteHistory(DomainModel):
    kind: Literal["delete"]
    recorded_at: UtcDateTime
    event_id: UUID


HistoryItem = Annotated[
    Union[InitialHistory, UpdateHistory, DeleteHistory],
    Field(discriminator="kind"),
]


class EntryRecord(EntryCreate):
    """Mutable operational read model, separate from the public API schemas."""

    created_at: UtcDateTime
    updated_at: UtcDateTime
    revision: int = Field(ge=1)
    deleted: bool = False
    last_event_sequence: int = Field(ge=1)
    history: list[HistoryItem] = Field(default_factory=list)
    schema_version: int = Field(default=OPERATIONAL_SCHEMA_VERSION, ge=1)

    @model_validator(mode="after")
    def history_snapshot_must_be_first_and_unique(self) -> EntryRecord:
        initial_positions = [
            index
            for index, item in enumerate(self.history)
            if isinstance(item, InitialHistory)
        ]
        if initial_positions and initial_positions != [0]:
            raise ValueError("the initial history snapshot must appear at most once, first")
        return self


class EntryUpdate(DomainModel):
    """Domain update input after mapping from the public PATCH contract."""

    task: str | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    started_at: UtcDateTime | None = None
    attributes: AttributeDelta | None = None

    @model_validator(mode="after")
    def supplied_fields_must_be_valid(self) -> EntryUpdate:
        for field_name in ("task", "duration_seconds", "attributes"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        if (
            "task" in self.model_fields_set
            and self.task is not None
            and not self.task.strip()
        ):
            raise ValueError("task must contain a non-whitespace character")
        return self


def _snapshot(entry: EntryRecord) -> EntrySnapshot:
    return EntrySnapshot(
        id=entry.id,
        task=entry.task,
        duration_seconds=entry.duration_seconds,
        started_at=entry.started_at,
        attributes=deepcopy(entry.attributes),
    )


def _validate_event_sequence(entry: EntryRecord, sequence: int) -> None:
    if sequence <= entry.last_event_sequence:
        raise ValueError("event sequence must be greater than the entry's last sequence")


def apply_entry_update(
    entry: EntryRecord,
    update: EntryUpdate,
    *,
    event_id: UUID,
    recorded_at: UtcDateTime,
    sequence: int,
) -> EntryRecord:
    """Apply a meaningful update, or return the unchanged entry for a no-op."""
    recorded_at = _parse_utc_datetime(recorded_at)
    if entry.deleted:
        raise ValueError("deleted entries cannot be updated")

    changed_fields: dict[str, object] = {}
    for field_name in ("task", "duration_seconds", "started_at"):
        if (
            field_name in update.model_fields_set
            and getattr(update, field_name) != getattr(entry, field_name)
        ):
            changed_fields[field_name] = getattr(update, field_name)

    attributes_delta: AttributeDelta | None = None
    if "attributes" in update.model_fields_set:
        requested = update.attributes
        if requested is None:
            raise ValueError("attributes cannot be null")
        attributes_to_set = {
            key: value
            for key, value in requested.set.items()
            if key not in entry.attributes or entry.attributes[key] != value
        }
        attributes_to_remove = [
            key for key in requested.remove if key in entry.attributes
        ]
        if attributes_to_set or attributes_to_remove:
            attributes_delta = AttributeDelta(
                set=attributes_to_set,
                remove=attributes_to_remove,
            )
            updated_attributes = entry.attributes.copy()
            updated_attributes.update(attributes_to_set)
            for key in attributes_to_remove:
                del updated_attributes[key]
            changed_fields["attributes"] = updated_attributes

    if not changed_fields:
        return entry

    _validate_event_sequence(entry, sequence)
    history = list(entry.history)
    if not history:
        history.append(InitialHistory(kind="initial", snapshot=_snapshot(entry)))
    changes: dict[str, object] = {
        key: (attributes_delta if key == "attributes" else value)
        for key, value in changed_fields.items()
    }
    history.append(
        UpdateHistory(
            kind="update",
            recorded_at=recorded_at,
            event_id=event_id,
            changes=EntryDelta.model_validate(changes),
        )
    )
    return entry.model_copy(
        update={
            **changed_fields,
            "updated_at": recorded_at,
            "revision": entry.revision + 1,
            "last_event_sequence": sequence,
            "history": history,
        },
        deep=True,
    )


def delete_entry(
    entry: EntryRecord,
    *,
    event_id: UUID,
    recorded_at: UtcDateTime,
    sequence: int,
) -> EntryRecord:
    """Logically delete an entry; repeating deletion is a no-op."""
    recorded_at = _parse_utc_datetime(recorded_at)
    if entry.deleted:
        return entry

    _validate_event_sequence(entry, sequence)
    history = list(entry.history)
    if not history:
        history.append(InitialHistory(kind="initial", snapshot=_snapshot(entry)))
    history.append(
        DeleteHistory(
            kind="delete",
            recorded_at=recorded_at,
            event_id=event_id,
        )
    )
    return entry.model_copy(
        update={
            "deleted": True,
            "updated_at": recorded_at,
            "revision": entry.revision + 1,
            "last_event_sequence": sequence,
            "history": history,
        },
        deep=True,
    )
