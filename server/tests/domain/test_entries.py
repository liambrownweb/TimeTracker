from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from timetracker.domain.entries import (
    AttributeDelta,
    CreateJournalEvent,
    DeleteHistory,
    DeleteJournalEvent,
    EntryCreate,
    EntryRecord,
    EntryUpdate,
    InitialHistory,
    UpdateJournalEvent,
    UpdateHistory,
    apply_entry_update,
    delete_entry,
)


ENTRY_ID = UUID("c4d15d1a-6e25-4e27-97c8-1a9020ed33d2")
EVENT_ID = UUID("a01f85ce-ea99-4595-92c7-ea2638c22929")
CREATED_AT = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def create_record(**overrides: object) -> EntryRecord:
    values: dict[str, object] = {
        "id": ENTRY_ID,
        "task": "Review specification",
        "duration_seconds": 900,
        "started_at": None,
        "attributes": {"custom": {"nested": True}},
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "revision": 1,
        "deleted": False,
        "last_event_sequence": 1,
    }
    values.update(overrides)
    return EntryRecord.model_validate(values)


def test_manual_and_stopwatch_entries_are_valid() -> None:
    manual = EntryCreate(
        id=ENTRY_ID,
        task="Manual activity",
        duration_seconds=60,
    )
    stopwatch = EntryCreate(
        id=ENTRY_ID,
        task="Timed activity",
        duration_seconds=61,
        started_at="2026-11-06T19:00:00Z",
    )

    assert manual.started_at is None
    assert stopwatch.started_at == CREATED_AT + timedelta(hours=7)


def test_unknown_typed_attributes_are_accepted_and_serialized() -> None:
    record = EntryCreate(
        id=ENTRY_ID,
        task="Flexible metadata",
        duration_seconds=1,
        attributes={"new-field": "text", "nullable": None, "billable": True},
    )

    assert record.attributes == {
        "new-field": "text",
        "nullable": None,
        "billable": True,
    }
    assert '"started_at":null' in record.model_dump_json()


def test_missing_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="id"):
        EntryCreate(task="Missing identity", duration_seconds=1)


@pytest.mark.parametrize(
    "duration",
    ["60", -1, True],
)
def test_invalid_duration_types_and_values_are_rejected(duration: object) -> None:
    with pytest.raises(ValidationError):
        EntryCreate(id=ENTRY_ID, task="Bad duration", duration_seconds=duration)


@pytest.mark.parametrize(
    "started_at",
    ["not-a-timestamp", "2026-11-06T19:00:00", "2026-11-06T20:00:00+01:00"],
)
def test_malformed_or_non_utc_timestamps_are_rejected(started_at: str) -> None:
    with pytest.raises(ValidationError):
        EntryCreate(
            id=ENTRY_ID,
            task="Bad timestamp",
            duration_seconds=1,
            started_at=started_at,
        )


def test_timestamp_json_uses_z_notation() -> None:
    record = EntryCreate(
        id=ENTRY_ID,
        task="UTC timestamp",
        duration_seconds=1,
        started_at="2026-11-06T19:00:00+00:00",
    )

    assert record.model_dump(mode="json")["started_at"] == "2026-11-06T19:00:00Z"


def test_no_op_update_does_not_change_revision_or_history() -> None:
    record = create_record()
    result = apply_entry_update(
        record,
        EntryUpdate(
            task=record.task,
            attributes=AttributeDelta(set={"custom": {"nested": True}}),
        ),
        event_id=EVENT_ID,
        recorded_at=CREATED_AT,
        sequence=2,
    )

    assert result is record
    assert result.revision == 1
    assert result.history == []


@pytest.mark.parametrize(
    ("old", "new"),
    [(1, True), (True, 1), (1, 1.0), ({"a": 1}, {"a": True}), ([1], [1.0])],
)
def test_attribute_updates_distinguish_json_types(old: object, new: object) -> None:
    record = create_record(attributes={"flag": old})

    result = apply_entry_update(
        record,
        EntryUpdate(attributes=AttributeDelta(set={"flag": new})),
        event_id=EVENT_ID,
        recorded_at=CREATED_AT,
        sequence=2,
    )

    assert result is not record
    assert result.revision == 2
    assert type(result.attributes["flag"]) is type(new)


def test_first_update_snapshots_initial_state_and_records_attribute_delta() -> None:
    record = create_record(attributes={"nullable": "old", "remove-me": 5})
    result = apply_entry_update(
        record,
        EntryUpdate(
            task="Updated task",
            attributes=AttributeDelta(
                set={"nullable": None, "new-key": "new"},
                remove=["remove-me"],
            ),
        ),
        event_id=EVENT_ID,
        recorded_at=CREATED_AT,
        sequence=2,
    )

    assert result.task == "Updated task"
    assert result.attributes == {"nullable": None, "new-key": "new"}
    assert result.revision == 2
    assert isinstance(result.history[0], InitialHistory)
    assert result.history[0].snapshot.task == record.task
    assert isinstance(result.history[1], UpdateHistory)
    assert result.history[1].event_id == EVENT_ID
    assert result.history[1].changes.attributes == AttributeDelta(
        set={"nullable": None, "new-key": "new"},
        remove=["remove-me"],
    )


def test_later_update_appends_only_the_delta() -> None:
    record = apply_entry_update(
        create_record(),
        EntryUpdate(task="First update"),
        event_id=EVENT_ID,
        recorded_at=CREATED_AT,
        sequence=2,
    )
    second_event_id = UUID("cf6c0eb2-3f5b-4b56-a691-844fd1cfcb45")
    result = apply_entry_update(
        record,
        EntryUpdate(duration_seconds=1200),
        event_id=second_event_id,
        recorded_at=CREATED_AT,
        sequence=3,
    )

    assert len(result.history) == 3
    assert isinstance(result.history[0], InitialHistory)
    assert isinstance(result.history[1], UpdateHistory)
    assert isinstance(result.history[2], UpdateHistory)
    assert result.history[2].changes.model_dump(exclude_unset=True) == {
        "duration_seconds": 1200
    }


def test_delete_is_a_recorded_change_and_repeat_is_no_op() -> None:
    record = create_record()
    deleted = delete_entry(
        record,
        event_id=EVENT_ID,
        recorded_at=CREATED_AT,
        sequence=2,
    )
    repeated = delete_entry(
        deleted,
        event_id=UUID("cf6c0eb2-3f5b-4b56-a691-844fd1cfcb45"),
        recorded_at=CREATED_AT,
        sequence=3,
    )

    assert deleted.deleted is True
    assert deleted.revision == 2
    assert isinstance(deleted.history[0], InitialHistory)
    assert isinstance(deleted.history[1], DeleteHistory)
    assert deleted.history[1].event_id == EVENT_ID
    assert repeated is deleted
    assert repeated.revision == 2


def test_journal_event_types_are_versioned_immutable_and_replayable() -> None:
    create_payload = EntryCreate(
        id=ENTRY_ID,
        task="Journaled entry",
        duration_seconds=90,
        attributes={"project": "prototype"},
    )
    create_event = CreateJournalEvent(
        event_type="create",
        event_id=EVENT_ID,
        sequence=1,
        entry_id=ENTRY_ID,
        recorded_at=CREATED_AT,
        entry=create_payload,
    )
    update_event = UpdateJournalEvent(
        event_type="update",
        event_id=UUID("cf6c0eb2-3f5b-4b56-a691-844fd1cfcb45"),
        sequence=2,
        entry_id=ENTRY_ID,
        recorded_at=CREATED_AT,
        changes={"duration_seconds": 120},
    )
    delete_event = DeleteJournalEvent(
        event_type="delete",
        event_id=UUID("7be5b048-1751-4c64-9fd0-28c7418c7837"),
        sequence=3,
        entry_id=ENTRY_ID,
        recorded_at=CREATED_AT,
    )

    assert create_event.schema_version == 1
    assert create_event.entry == create_payload
    assert update_event.changes.duration_seconds == 120
    assert delete_event.event_type == "delete"
    with pytest.raises(ValidationError):
        create_event.sequence = 4


def test_update_journal_event_rejects_empty_change_payload() -> None:
    with pytest.raises(ValidationError, match="at least one change"):
        UpdateJournalEvent(
            event_type="update",
            event_id=EVENT_ID,
            sequence=2,
            entry_id=ENTRY_ID,
            recorded_at=CREATED_AT,
            changes={},
        )


def test_create_journal_event_requires_matching_entry_id() -> None:
    with pytest.raises(ValidationError, match="entry_id"):
        CreateJournalEvent(
            event_type="create",
            event_id=EVENT_ID,
            sequence=1,
            entry_id=ENTRY_ID,
            recorded_at=CREATED_AT,
            entry={
                "id": UUID("cf6c0eb2-3f5b-4b56-a691-844fd1cfcb45"),
                "task": "Mismatched ID",
                "duration_seconds": 1,
            },
        )


def test_deleted_entries_cannot_be_updated() -> None:
    deleted = delete_entry(
        create_record(),
        event_id=EVENT_ID,
        recorded_at=CREATED_AT,
        sequence=2,
    )

    with pytest.raises(ValueError, match="deleted entries"):
        apply_entry_update(
            deleted,
            EntryUpdate(task="Not allowed"),
            event_id=UUID("cf6c0eb2-3f5b-4b56-a691-844fd1cfcb45"),
            recorded_at=CREATED_AT,
            sequence=3,
        )
