import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from timetracker.domain.entries import (
    EntryRecord,
    EntryUpdate,
    apply_entry_update,
    delete_entry,
)
from timetracker.storage import (
    EntryAlreadyExistsError,
    EntryNotFoundError,
    EntryStore,
    InvalidEntryUpdateError,
    JsonlEntryStore,
    StorageCorruptionError,
)

NOV = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 2, 8, 30, tzinfo=timezone.utc)
OCT = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)


def make_record(
    *,
    entry_id: UUID | None = None,
    created_at: datetime = NOV,
    task: str = "Task",
    attributes: dict[str, object] | None = None,
) -> EntryRecord:
    return EntryRecord.model_validate(
        {
            "id": entry_id or uuid4(),
            "task": task,
            "duration_seconds": 60,
            "started_at": None,
            "attributes": attributes or {},
            "created_at": created_at,
            "updated_at": created_at,
            "revision": 1,
            "deleted": False,
            "last_event_sequence": 1,
        }
    )


def month_file(root: Path, year: int, month: int) -> Path:
    return root / "entries" / f"{year:04d}" / f"{month:02d}.jsonl"


def read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_store_satisfies_interface(tmp_path: Path) -> None:
    store: EntryStore = JsonlEntryStore(tmp_path)

    assert store.get(uuid4()) is None
    assert store.list() == []


def test_create_writes_one_jsonl_line_in_original_utc_month(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    record = make_record(task="Nonempty", attributes={"billable": True})

    store.create(record)

    lines = read_lines(month_file(tmp_path, 2026, 11))
    assert len(lines) == 1
    assert lines[0]["id"] == str(record.id)
    assert lines[0]["created_at"] == "2026-11-06T12:00:00Z"
    assert lines[0]["history"] == []
    assert store.get(record.id) == record


def test_month_boundary_uses_utc_created_at(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    store.create(make_record(created_at=OCT))
    store.create(make_record(created_at=DEC))

    assert month_file(tmp_path, 2026, 10).exists()
    assert month_file(tmp_path, 2026, 12).exists()
    assert not month_file(tmp_path, 2026, 11).exists()


def test_restart_rebuilds_identical_state(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    records = [
        make_record(created_at=DEC, task="later"),
        make_record(created_at=NOV, task="earlier"),
    ]
    for record in records:
        store.create(record)

    reopened = JsonlEntryStore(tmp_path)

    assert reopened.list() == store.list()
    assert [record.task for record in reopened.list()] == ["earlier", "later"]
    for record in records:
        assert reopened.get(record.id) == record


def test_update_preserves_unrelated_records_and_original_month(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    first = make_record(task="first")
    second = make_record(task="second")
    other_month = make_record(created_at=DEC, task="december")
    for record in (first, second, other_month):
        store.create(record)
    december_before = month_file(tmp_path, 2026, 12).read_bytes()

    updated = apply_entry_update(
        first,
        EntryUpdate(task="renamed"),
        event_id=uuid4(),
        recorded_at=DEC,
        sequence=2,
    )
    store.update(updated)

    assert month_file(tmp_path, 2026, 12).read_bytes() == december_before
    november = read_lines(month_file(tmp_path, 2026, 11))
    assert [line["task"] for line in november] == ["renamed", "second"]
    assert store.get(first.id) == updated
    assert store.get(second.id) == second
    assert JsonlEntryStore(tmp_path).get(first.id) == updated


def test_history_is_readable_from_the_record_line_alone(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    record = make_record(attributes={"location": "home"})
    store.create(record)
    event_id = uuid4()
    store.update(
        apply_entry_update(
            record,
            EntryUpdate(duration_seconds=120),
            event_id=event_id,
            recorded_at=DEC,
            sequence=2,
        )
    )

    (line,) = read_lines(month_file(tmp_path, 2026, 11))

    history = line["history"]
    assert isinstance(history, list)
    assert [item["kind"] for item in history] == ["initial", "update"]
    assert history[0]["snapshot"]["duration_seconds"] == 60
    assert history[1]["event_id"] == str(event_id)
    assert history[1]["changes"] == {"duration_seconds": 120}


def test_logical_deletion_is_persisted_and_hidden_from_default_list(
    tmp_path: Path,
) -> None:
    store = JsonlEntryStore(tmp_path)
    keep = make_record(task="keep")
    remove = make_record(task="remove")
    store.create(keep)
    store.create(remove)

    store.update(delete_entry(remove, event_id=uuid4(), recorded_at=DEC, sequence=2))

    for current in (store, JsonlEntryStore(tmp_path)):
        assert [record.id for record in current.list()] == [keep.id]
        assert {record.id for record in current.list(include_deleted=True)} == {
            keep.id,
            remove.id,
        }
        stored = current.get(remove.id)
        assert stored is not None and stored.deleted is True
        assert [item.kind for item in stored.history] == ["initial", "delete"]


def test_duplicate_create_and_missing_update_are_rejected(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    record = make_record()
    store.create(record)

    with pytest.raises(EntryAlreadyExistsError):
        store.create(record)
    with pytest.raises(EntryNotFoundError):
        store.update(make_record())
    assert len(read_lines(month_file(tmp_path, 2026, 11))) == 1


def test_update_cannot_move_an_entry_to_another_month(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    record = make_record()
    store.create(record)

    with pytest.raises(InvalidEntryUpdateError):
        store.update(record.model_copy(update={"created_at": DEC}))
    assert store.get(record.id) == record


def test_list_filters_by_task_and_typed_attributes(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    flag = make_record(task="a", attributes={"billable": True, "n": 1})
    number = make_record(task="a", attributes={"billable": 1, "n": 1.0})
    other = make_record(task="b", attributes={"billable": True})
    for record in (flag, number, other):
        store.create(record)

    assert {r.id for r in store.list(task="a")} == {flag.id, number.id}
    assert {r.id for r in store.list(attributes={"billable": True})} == {
        flag.id,
        other.id,
    }
    assert {r.id for r in store.list(attributes={"billable": 1})} == {number.id}
    assert {r.id for r in store.list(attributes={"n": 1.0})} == {number.id}
    assert store.list(task="a", attributes={"billable": True}) == [flag]
    assert store.list(attributes={"missing": None}) == []


def test_returned_records_are_copies(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    record = make_record(attributes={"tags": ["x"]})
    store.create(record)

    fetched = store.get(record.id)
    assert fetched is not None
    fetched.task = "mutated"
    fetched.attributes["tags"] = ["changed"]
    record.task = "also mutated"

    stored = store.get(record.id)
    assert stored is not None
    assert stored.task == "Task"
    assert stored.attributes == {"tags": ["x"]}


@pytest.mark.parametrize("failing_call", ["replace", "fsync"])
def test_failed_replacement_leaves_old_file_and_state_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_call: str
) -> None:
    store = JsonlEntryStore(tmp_path)
    record = make_record()
    store.create(record)
    path = month_file(tmp_path, 2026, 11)
    before = path.read_bytes()

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr(os, failing_call, explode)
    updated = apply_entry_update(
        record, EntryUpdate(task="never stored"), event_id=uuid4(), recorded_at=DEC, sequence=2
    )
    with pytest.raises(OSError, match="injected"):
        store.update(updated)
    with pytest.raises(OSError, match="injected"):
        store.create(make_record())
    monkeypatch.undo()

    assert path.read_bytes() == before
    assert store.get(record.id) == record
    assert len(store.list()) == 1
    assert [p.name for p in path.parent.iterdir()] == ["11.jsonl"]
    assert JsonlEntryStore(tmp_path).list() == [record]


def test_stale_temp_file_from_a_crash_is_ignored_and_removed(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    record = make_record()
    store.create(record)
    path = month_file(tmp_path, 2026, 11)
    stale = path.with_name(f".{path.name}.deadbeef.tmp")
    stale.write_text('{"id": "half-written', encoding="utf-8")

    reopened = JsonlEntryStore(tmp_path)

    assert reopened.list() == [record]
    assert not stale.exists()


def test_corrupt_line_is_reported_with_location_not_skipped(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    store.create(make_record())
    path = month_file(tmp_path, 2026, 11)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"id": "not complete\n')

    with pytest.raises(StorageCorruptionError) as caught:
        JsonlEntryStore(tmp_path)

    assert caught.value.path == path
    assert caught.value.line_number == 2


def test_duplicate_ids_and_misplaced_records_are_corruption(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    record = make_record()
    store.create(record)
    nov = month_file(tmp_path, 2026, 11)
    line = nov.read_text(encoding="utf-8")

    nov.write_text(line + line, encoding="utf-8")
    with pytest.raises(StorageCorruptionError, match="duplicate"):
        JsonlEntryStore(tmp_path)

    nov.write_text(line, encoding="utf-8")
    december = month_file(tmp_path, 2026, 12)
    december.parent.mkdir(parents=True, exist_ok=True)
    december.write_text(line, encoding="utf-8")
    with pytest.raises(StorageCorruptionError, match="does not match"):
        JsonlEntryStore(tmp_path)


def test_concurrent_creates_and_updates_lose_nothing(tmp_path: Path) -> None:
    store = JsonlEntryStore(tmp_path)
    seeded = [make_record(task=f"seed-{n}") for n in range(8)]
    for record in seeded:
        store.create(record)
    errors: list[BaseException] = []

    def create_many(worker: int) -> None:
        try:
            for n in range(10):
                store.create(make_record(task=f"worker-{worker}-{n}"))
        except BaseException as exc:
            errors.append(exc)

    def update_one(index: int) -> None:
        try:
            store.update(
                apply_entry_update(
                    seeded[index],
                    EntryUpdate(duration_seconds=1000 + index),
                    event_id=uuid4(),
                    recorded_at=DEC,
                    sequence=2,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=create_many, args=(w,)) for w in range(8)]
    threads += [threading.Thread(target=update_one, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    reopened = JsonlEntryStore(tmp_path)
    assert len(reopened.list()) == 8 + 8 * 10
    for index, record in enumerate(seeded):
        stored = reopened.get(record.id)
        assert stored is not None and stored.duration_seconds == 1000 + index
    assert len(read_lines(month_file(tmp_path, 2026, 11))) == 88
