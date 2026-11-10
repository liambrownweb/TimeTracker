import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from timetracker.domain.entries import (
    CreateJournalEvent,
    DeleteJournalEvent,
    EntryCreate,
    EntryDelta,
    UpdateJournalEvent,
)
from timetracker.domain.replay import replay
from timetracker.storage import (
    InvalidJournalEventError,
    JournalFailedError,
    JournalStore,
    JsonlJournal,
    StorageCorruptionError,
)
from timetracker.storage.journal import decode_event, encode_event

NOV = datetime(2026, 11, 30, 23, 59, 59, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 1, 0, 0, 1, tzinfo=timezone.utc)


def create_event(
    sequence: int,
    *,
    recorded_at: datetime = NOV,
    entry_id: UUID | None = None,
    event_id: UUID | None = None,
    attributes: dict[str, object] | None = None,
) -> CreateJournalEvent:
    entry_id = entry_id or uuid4()
    return CreateJournalEvent(
        event_type="create",
        event_id=event_id or uuid4(),
        sequence=sequence,
        entry_id=entry_id,
        recorded_at=recorded_at,
        entry=EntryCreate.model_validate(
            {
                "id": entry_id,
                "task": "Task",
                "duration_seconds": 100,
                "attributes": attributes or {},
            }
        ),
    )


def month_file(root: Path, year: int, month: int) -> Path:
    return root / "journal" / f"{year:04d}" / f"{month:02d}.jsonl"


def test_journal_satisfies_interface_and_starts_empty(tmp_path: Path) -> None:
    journal: JournalStore = JsonlJournal(tmp_path)

    assert journal.last_sequence == 0
    assert journal.read() == []


def test_append_writes_checksummed_line_and_survives_restart(tmp_path: Path) -> None:
    journal = JsonlJournal(tmp_path)
    first, second = create_event(1), create_event(2)

    journal.append(first)
    journal.append(second)

    lines = month_file(tmp_path, 2026, 11).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["checksum"].startswith("sha256:")
    assert json.loads(lines[0])["recorded_at"] == "2026-11-30T23:59:59Z"
    reopened = JsonlJournal(tmp_path)
    assert reopened.last_sequence == 2
    assert reopened.read() == [first, second]
    assert reopened.read(after_sequence=1) == [second]


def test_events_are_filed_by_commit_month_and_ordered_across_months(
    tmp_path: Path,
) -> None:
    journal = JsonlJournal(tmp_path)
    events = [create_event(1), create_event(2, recorded_at=DEC), create_event(3, recorded_at=DEC)]
    for event in events:
        journal.append(event)

    assert month_file(tmp_path, 2026, 11).exists()
    assert len(month_file(tmp_path, 2026, 12).read_text().splitlines()) == 2
    assert JsonlJournal(tmp_path).read() == events


def test_append_rejects_invalid_sequence_duplicate_id_and_earlier_month(
    tmp_path: Path,
) -> None:
    journal = JsonlJournal(tmp_path)
    shared = uuid4()
    journal.append(create_event(1, recorded_at=DEC, event_id=shared))
    before = month_file(tmp_path, 2026, 12).read_bytes()

    with pytest.raises(InvalidJournalEventError):
        journal.append(create_event(3, recorded_at=DEC))
    with pytest.raises(InvalidJournalEventError):
        journal.append(create_event(1, recorded_at=DEC))
    with pytest.raises(InvalidJournalEventError):
        journal.append(create_event(2, recorded_at=DEC, event_id=shared))
    with pytest.raises(InvalidJournalEventError):
        journal.append(create_event(2, recorded_at=NOV))

    assert month_file(tmp_path, 2026, 12).read_bytes() == before
    assert journal.last_sequence == 1


def test_json_types_and_unicode_round_trip_exactly(tmp_path: Path) -> None:
    journal = JsonlJournal(tmp_path)
    attributes = {"i": 1, "f": 1.0, "b": True, "s": "naïve ✓", "n": None, "l": [1, 1.0]}
    event = create_event(1, attributes=attributes)
    journal.append(event)

    (stored,) = JsonlJournal(tmp_path).read()

    assert isinstance(stored, CreateJournalEvent)
    stored_attrs = stored.entry.attributes
    assert {k: type(v) for k, v in stored_attrs.items()} == {
        k: type(v) for k, v in attributes.items()
    }
    assert stored_attrs == attributes


def test_update_and_delete_events_round_trip(tmp_path: Path) -> None:
    entry_id = uuid4()
    events = [
        create_event(1, entry_id=entry_id),
        UpdateJournalEvent(
            event_type="update",
            event_id=uuid4(),
            sequence=2,
            entry_id=entry_id,
            recorded_at=NOV,
            changes=EntryDelta.model_validate({"started_at": None, "task": "New"}),
        ),
        DeleteJournalEvent(
            event_type="delete",
            event_id=uuid4(),
            sequence=3,
            entry_id=entry_id,
            recorded_at=DEC,
        ),
    ]
    journal = JsonlJournal(tmp_path)
    for event in events:
        journal.append(event)

    loaded = JsonlJournal(tmp_path).read()

    assert loaded == events
    assert "started_at" in loaded[1].changes.model_fields_set  # type: ignore[union-attr]
    assert replay(loaded).records[entry_id].deleted is True


def test_checksum_detects_valid_json_bit_flip(tmp_path: Path) -> None:
    journal = JsonlJournal(tmp_path)
    journal.append(create_event(1))
    journal.append(create_event(2))
    path = month_file(tmp_path, 2026, 11)
    path.write_bytes(path.read_bytes().replace(b'"duration_seconds":100', b'"duration_seconds":900', 1))

    with pytest.raises(StorageCorruptionError, match="checksum") as caught:
        JsonlJournal(tmp_path)

    assert caught.value.path == path
    assert caught.value.line_number == 1


def test_event_without_checksum_is_rejected() -> None:
    line = encode_event(create_event(1)).rstrip(b"\n")
    payload = json.loads(line)
    del payload["checksum"]

    with pytest.raises(ValueError, match="missing checksum"):
        decode_event(json.dumps(payload).encode())


def test_torn_tail_is_quarantined_truncated_and_its_sequence_reused(
    tmp_path: Path,
) -> None:
    journal = JsonlJournal(tmp_path)
    committed = [create_event(1), create_event(2)]
    for event in committed:
        journal.append(event)
    path = month_file(tmp_path, 2026, 11)
    intact = path.read_bytes()
    torn = encode_event(create_event(3)).rstrip(b"\n")[:40]
    path.write_bytes(intact + torn)

    recovered = JsonlJournal(tmp_path)

    assert path.read_bytes() == intact
    assert recovered.last_sequence == 2
    assert recovered.read() == committed
    assert [p.read_bytes() for p in recovered.quarantined] == [torn]
    replacement = create_event(3)
    recovered.append(replacement)
    assert JsonlJournal(tmp_path).read() == [*committed, replacement]
    assert JsonlJournal(tmp_path).quarantined == []


def test_complete_but_unterminated_final_line_is_still_unacknowledged(
    tmp_path: Path,
) -> None:
    journal = JsonlJournal(tmp_path)
    journal.append(create_event(1))
    path = month_file(tmp_path, 2026, 11)
    path.write_bytes(path.read_bytes() + encode_event(create_event(2)).rstrip(b"\n"))

    recovered = JsonlJournal(tmp_path)

    assert recovered.last_sequence == 1
    assert len(recovered.quarantined) == 1


def test_torn_only_line_leaves_a_usable_empty_file(tmp_path: Path) -> None:
    path = month_file(tmp_path, 2026, 11)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"half')

    journal = JsonlJournal(tmp_path)

    assert journal.last_sequence == 0
    journal.append(create_event(1))
    assert JsonlJournal(tmp_path).last_sequence == 1


def test_unterminated_line_in_an_older_file_is_corruption(tmp_path: Path) -> None:
    journal = JsonlJournal(tmp_path)
    journal.append(create_event(1))
    journal.append(create_event(2, recorded_at=DEC))
    november = month_file(tmp_path, 2026, 11)
    november.write_bytes(november.read_bytes().rstrip(b"\n"))

    with pytest.raises(StorageCorruptionError, match="newline"):
        JsonlJournal(tmp_path)


def test_terminated_bad_final_line_halts_instead_of_being_dropped(
    tmp_path: Path,
) -> None:
    journal = JsonlJournal(tmp_path)
    journal.append(create_event(1))
    path = month_file(tmp_path, 2026, 11)
    path.write_bytes(path.read_bytes() + b'{"not": "an event"}\n')
    before = path.read_bytes()

    with pytest.raises(StorageCorruptionError) as caught:
        JsonlJournal(tmp_path)

    assert caught.value.line_number == 2
    assert path.read_bytes() == before
    assert not (tmp_path / "quarantine").exists()


def test_sequence_gap_duplicate_event_id_blank_line_and_wrong_month_halt(
    tmp_path: Path,
) -> None:
    path = month_file(tmp_path, 2026, 11)
    path.parent.mkdir(parents=True)
    shared = uuid4()
    first = encode_event(create_event(1, event_id=shared))

    path.write_bytes(first + encode_event(create_event(3)))
    with pytest.raises(StorageCorruptionError, match="expected sequence 2"):
        JsonlJournal(tmp_path)

    path.write_bytes(first + encode_event(create_event(2, event_id=shared)))
    with pytest.raises(StorageCorruptionError, match="duplicate event id"):
        JsonlJournal(tmp_path)

    path.write_bytes(first + b"\n" + encode_event(create_event(2)))
    with pytest.raises(StorageCorruptionError, match="invalid journal event"):
        JsonlJournal(tmp_path)

    path.write_bytes(encode_event(create_event(1, recorded_at=DEC)))
    with pytest.raises(StorageCorruptionError, match="month"):
        JsonlJournal(tmp_path)


def test_partial_write_is_rolled_back_and_journal_stays_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = JsonlJournal(tmp_path)
    journal.append(create_event(1))
    path = month_file(tmp_path, 2026, 11)
    before = path.read_bytes()
    real_write = os.write

    def short_write(descriptor: int, data: object) -> int:
        real_write(descriptor, bytes(data)[:10])  # type: ignore[arg-type]
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", short_write)
    with pytest.raises(OSError, match="disk full"):
        journal.append(create_event(2))
    monkeypatch.undo()

    assert path.read_bytes() == before
    assert journal.last_sequence == 1
    journal.append(create_event(2))
    assert JsonlJournal(tmp_path).last_sequence == 2


def test_failed_fsync_is_not_acknowledged_and_blocks_further_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = JsonlJournal(tmp_path)
    journal.append(create_event(1))

    def explode(descriptor: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(os, "fsync", explode)
    with pytest.raises(OSError, match="injected"):
        journal.append(create_event(2))
    monkeypatch.undo()

    assert journal.last_sequence == 1
    with pytest.raises(JournalFailedError):
        journal.append(create_event(2))
    reopened = JsonlJournal(tmp_path)
    assert reopened.last_sequence == 1
    reopened.append(create_event(2))
    assert JsonlJournal(tmp_path).last_sequence == 2
