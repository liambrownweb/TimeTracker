from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from timetracker.domain.entries import (
    AttributeDelta,
    CreateJournalEvent,
    DeleteJournalEvent,
    EntryCreate,
    EntryDelta,
    EntryRecord,
    EntryUpdate,
    UpdateJournalEvent,
    apply_entry_update,
    delete_entry,
)
from timetracker.domain.replay import ReplayError, apply_journal_event, replay

ENTRY_ID = UUID("11111111-1111-4111-8111-111111111111")
T1 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
T3 = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


def create_event(sequence: int = 1, entry_id: UUID = ENTRY_ID) -> CreateJournalEvent:
    return CreateJournalEvent(
        event_type="create",
        event_id=uuid4(),
        sequence=sequence,
        entry_id=entry_id,
        recorded_at=T1,
        entry=EntryCreate(
            id=entry_id,
            task="Write",
            duration_seconds=60,
            attributes={"place": "home"},
        ),
    )


def update_event(sequence: int, changes: dict[str, object]) -> UpdateJournalEvent:
    return UpdateJournalEvent(
        event_type="update",
        event_id=uuid4(),
        sequence=sequence,
        entry_id=ENTRY_ID,
        recorded_at=T2,
        changes=EntryDelta.model_validate(changes),
    )


def delete_event(sequence: int) -> DeleteJournalEvent:
    return DeleteJournalEvent(
        event_type="delete",
        event_id=uuid4(),
        sequence=sequence,
        entry_id=ENTRY_ID,
        recorded_at=T3,
    )


def test_replay_matches_live_application() -> None:
    created = create_event()
    update = update_event(
        2,
        {
            "duration_seconds": 120,
            "attributes": AttributeDelta(set={"mood": "ok"}, remove=["place"]),
        },
    )
    deleted = delete_event(3)

    replayed = replay([created, update, deleted])[ENTRY_ID]

    live = EntryRecord.model_validate(
        {
            **created.entry.model_dump(),
            "created_at": T1,
            "updated_at": T1,
            "revision": 1,
            "last_event_sequence": 1,
        }
    )
    live = apply_entry_update(
        live,
        EntryUpdate(
            duration_seconds=120,
            attributes=AttributeDelta(set={"mood": "ok"}, remove=["place"]),
        ),
        event_id=update.event_id,
        recorded_at=T2,
        sequence=2,
    )
    live = delete_entry(live, event_id=deleted.event_id, recorded_at=T3, sequence=3)
    assert replayed == live
    assert replayed.revision == 3
    assert [item.kind for item in replayed.history] == ["initial", "update", "delete"]


def test_replay_applies_explicit_null_started_at() -> None:
    first = update_event(2, {"started_at": T2})
    second = update_event(3, {"started_at": None})

    record = replay([create_event(), first, second])[ENTRY_ID]

    assert record.started_at is None
    assert record.revision == 3


def test_replay_keeps_entries_independent() -> None:
    other = UUID("22222222-2222-4222-8222-222222222222")

    records = replay([create_event(1), create_event(2, other)])

    assert set(records) == {ENTRY_ID, other}


@pytest.mark.parametrize(
    "events",
    [
        [update_event(1, {"task": "x"})],
        [create_event(1), create_event(2)],
        [create_event(1), update_event(2, {"task": "Write"})],
        [create_event(1), delete_event(2), update_event(3, {"task": "x"})],
        [create_event(1), delete_event(2), delete_event(3)],
    ],
    ids=[
        "update-missing",
        "create-twice",
        "no-op-update",
        "update-after-delete",
        "repeat-delete",
    ],
)
def test_replay_rejects_events_that_do_not_apply_cleanly(
    events: list[CreateJournalEvent | UpdateJournalEvent | DeleteJournalEvent],
) -> None:
    with pytest.raises(ReplayError):
        replay(events)


def test_apply_journal_event_does_not_mutate_input() -> None:
    record = apply_journal_event(None, create_event())
    snapshot = record.model_copy(deep=True)

    apply_journal_event(record, update_event(2, {"task": "Changed"}))

    assert record == snapshot
