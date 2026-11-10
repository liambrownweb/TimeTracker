"""Deterministic reconstruction of entry state from journal events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
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
    json_equal,
)


class ReplayError(ValueError):
    """A journal event cannot be applied to the state built so far."""


def _is_provable_no_op(record: EntryRecord, event: UpdateJournalEvent) -> bool:
    """True only if the event's changes demonstrably leave `record` unchanged.

    This deliberately does not reuse `apply_entry_update`'s detector. It
    applies the delta directly and compares the content fields type-strictly,
    so a flaw in that detector can never cause a real change to be skipped.
    """
    changes = event.changes
    for name in ("task", "duration_seconds", "started_at"):
        if name in changes.model_fields_set and not json_equal(
            getattr(changes, name), getattr(record, name)
        ):
            return False
    delta = changes.attributes
    if delta is not None:
        for key, value in delta.set.items():
            if key not in record.attributes or not json_equal(
                record.attributes[key], value
            ):
                return False
        if any(key in record.attributes for key in delta.remove):
            return False
    return True


def apply_journal_event(
    record: EntryRecord | None, event: JournalEvent
) -> EntryRecord:
    """Return the entry state after `event`, or raise ReplayError.

    A no-op event is an error here; `replay` is the only place that may skip
    one, and only when it is provably a no-op.
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


@dataclass
class ReplayResult:
    records: dict[UUID, EntryRecord]
    skipped_no_ops: list[UUID] = field(default_factory=list)


def replay(events: Iterable[JournalEvent]) -> ReplayResult:
    """Rebuild every entry by applying events in the order given.

    An update event proven to change nothing is skipped and reported in
    `skipped_no_ops`: the live path never journals one, so its presence signals
    an upstream fault that verification should surface. Any other event that
    cannot be applied raises ReplayError.
    """
    result = ReplayResult(records={})
    for event in events:
        current = result.records.get(event.entry_id)
        if (
            isinstance(event, UpdateJournalEvent)
            and current is not None
            and not current.deleted
            and _is_provable_no_op(current, event)
        ):
            result.skipped_no_ops.append(event.event_id)
            continue
        result.records[event.entry_id] = apply_journal_event(current, event)
    return result
