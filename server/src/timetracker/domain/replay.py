"""Deterministic reconstruction of entry state from journal events."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from timetracker.domain.entries import (
    CreateJournalEvent,
    DeleteJournalEvent,
    EntryRecord,
    EntryUpdate,
    JournalEvent,
    UpdateJournalEvent,
    apply_entry_update,
    delete_entry,
)


class ReplayError(ValueError):
    """A journal event cannot be applied to the state built so far."""


def apply_journal_event(
    record: EntryRecord | None, event: JournalEvent
) -> EntryRecord:
    """Return the entry state after `event`.

    Committed events always change state, so an event that would be a no-op,
    that targets a missing or deleted entry, or that re-creates an entry is a
    replay error rather than being silently tolerated.
    """
    if isinstance(event, CreateJournalEvent):
        if record is not None:
            raise ReplayError(f"entry {event.entry_id} is created twice")
        return EntryRecord.model_validate(
            {
                **event.entry.model_dump(),
                "created_at": event.recorded_at,
                "updated_at": event.recorded_at,
                "revision": 1,
                "deleted": False,
                "last_event_sequence": event.sequence,
            }
        )

    if record is None:
        raise ReplayError(
            f"event {event.event_id} targets missing entry {event.entry_id}"
        )
    if record.deleted:
        raise ReplayError(
            f"event {event.event_id} targets deleted entry {event.entry_id}"
        )

    if isinstance(event, UpdateJournalEvent):
        update = EntryUpdate.model_validate(
            {
                name: getattr(event.changes, name)
                for name in event.changes.model_fields_set
            }
        )
        result = apply_entry_update(
            record,
            update,
            event_id=event.event_id,
            recorded_at=event.recorded_at,
            sequence=event.sequence,
        )
    elif isinstance(event, DeleteJournalEvent):
        result = delete_entry(
            record,
            event_id=event.event_id,
            recorded_at=event.recorded_at,
            sequence=event.sequence,
        )
    else:  # pragma: no cover - the union is closed
        raise ReplayError(f"unsupported event type {type(event).__name__}")

    if result is record:
        raise ReplayError(f"event {event.event_id} does not change entry state")
    return result


def replay(events: Iterable[JournalEvent]) -> dict[UUID, EntryRecord]:
    """Rebuild every entry by applying events in the order given."""
    records: dict[UUID, EntryRecord] = {}
    for event in events:
        records[event.entry_id] = apply_journal_event(
            records.get(event.entry_id), event
        )
    return records
