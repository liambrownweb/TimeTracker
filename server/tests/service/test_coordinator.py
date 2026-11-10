import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import FakeClock, World, make_create

from timetracker.domain.entries import (
    AttributeDelta,
    EntryRecord,
    EntryUpdate,
    JournalEvent,
)
from timetracker.domain.replay import replay
from timetracker.service import (
    EntryConflictError,
    EntryCoordinator,
    EntryDeletedError,
    StoreUnavailableError,
)
from timetracker.storage import EntryNotFoundError, JsonlEntryStore, JsonlJournal


def set_attr(**values: object) -> EntryUpdate:
    return EntryUpdate(attributes=AttributeDelta(set=values))


class FailingJournal:
    """Delegates to a real journal but fails appends on demand."""

    def __init__(self, inner: JsonlJournal) -> None:
        self.inner = inner
        self.fail_appends = False

    @property
    def last_sequence(self) -> int:
        return self.inner.last_sequence

    def append(self, event: JournalEvent) -> None:
        if self.fail_appends:
            raise OSError("disk unplugged")
        self.inner.append(event)

    def read(self, after_sequence: int = 0) -> list[JournalEvent]:
        return self.inner.read(after_sequence)


class FailingStore:
    """Delegates to a real store but fails writes on demand."""

    def __init__(self, inner: JsonlEntryStore) -> None:
        self.inner = inner
        self.fail_writes = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def create(self, record: EntryRecord) -> None:
        if self.fail_writes:
            raise OSError("write failed")
        self.inner.create(record)

    def update(self, record: EntryRecord) -> None:
        if self.fail_writes:
            raise OSError("write failed")
        self.inner.update(record)


def test_mutations_are_journaled_and_match_replay(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create(attributes={"project": "a"})

    created = coordinator.create(entry)
    updated = coordinator.update(entry.id, set_attr(project="b"))
    coordinator.delete(entry.id)

    assert created.created and created.record.revision == 1
    assert updated.revision == 2
    events = world.events()
    assert [e.event_type for e in events] == ["create", "update", "delete"]
    assert [e.sequence for e in events] == [1, 2, 3]
    assert world.records() == list(replay(events).records.values())


def test_no_op_requests_write_nothing(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create(attributes={"project": "a"})
    coordinator.create(entry)

    same = coordinator.update(entry.id, set_attr(project="a"))
    assert same.revision == 1
    coordinator.delete(entry.id)
    coordinator.delete(entry.id)

    assert [e.event_type for e in world.events()] == ["create", "delete"]


def test_unknown_and_deleted_entries(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create()
    coordinator.create(entry)
    coordinator.delete(entry.id)

    with pytest.raises(EntryNotFoundError):
        coordinator.get(uuid4())
    with pytest.raises(EntryNotFoundError):
        coordinator.update(uuid4(), set_attr(a=1))
    with pytest.raises(EntryNotFoundError):
        coordinator.delete(uuid4())
    with pytest.raises(EntryDeletedError):
        coordinator.get(entry.id)
    with pytest.raises(EntryDeletedError):
        coordinator.update(entry.id, set_attr(a=1))
    assert coordinator.list() == []


def test_identical_retry_is_recognized_even_after_edit_and_delete(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create(attributes={"n": 1})
    coordinator.create(entry)
    coordinator.update(entry.id, set_attr(n=2))

    retry = coordinator.create(entry)
    assert not retry.created
    assert retry.record.attributes == {"n": 2}

    coordinator.delete(entry.id)
    assert not coordinator.create(entry).created
    assert [e.event_type for e in world.events()] == ["create", "update", "delete"]


def test_retry_with_different_content_conflicts_type_strictly(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create(attributes={"n": 1})
    coordinator.create(entry)

    for changed in (
        make_create(entry_id=entry.id, attributes={"n": True}),
        make_create(entry_id=entry.id, attributes={"n": 1.0}),
        make_create(entry_id=entry.id, attributes={"n": 1}, task="Other"),
    ):
        with pytest.raises(EntryConflictError):
            coordinator.create(changed)
    assert len(world.events()) == 1


def test_failure_before_journal_append_leaves_no_trace(world: World) -> None:
    journal = FailingJournal(world.journal())
    coordinator = EntryCoordinator(world.store(), journal, clock=world.clock)
    entry = make_create()

    journal.fail_appends = True
    with pytest.raises(OSError):
        coordinator.create(entry)
    assert world.events() == []
    assert world.records() == []

    journal.fail_appends = False
    assert coordinator.create(entry).created
    assert len(world.events()) == 1


def test_failure_after_journal_commit_is_resolved_by_retry(world: World) -> None:
    store = FailingStore(world.store())
    coordinator = EntryCoordinator(store, world.journal(), clock=world.clock)
    entry = make_create()

    store.fail_writes = True
    with pytest.raises(StoreUnavailableError):
        coordinator.create(entry)
    store.fail_writes = False
    assert len(world.events()) == 1

    result = coordinator.create(entry)

    assert not result.created
    assert result.record.id == entry.id
    assert len(world.events()) == 1
    assert [r.id for r in world.records()] == [entry.id]


def test_failure_after_journal_commit_is_resolved_by_restart(world: World) -> None:
    store = FailingStore(world.store())
    coordinator = EntryCoordinator(store, world.journal(), clock=world.clock)
    entry = make_create()
    coordinator.create(entry)

    store.fail_writes = True
    with pytest.raises(StoreUnavailableError):
        coordinator.update(entry.id, set_attr(a=1))

    restarted = world.coordinator()

    assert restarted.recovery_report.repaired
    assert restarted.get(entry.id).attributes == {"a": 1}
    assert len(world.events()) == 2


def test_failed_patch_retry_resolves_to_the_committed_result(world: World) -> None:
    store = FailingStore(world.store())
    coordinator = EntryCoordinator(store, world.journal(), clock=world.clock)
    entry = make_create()
    coordinator.create(entry)
    store.fail_writes = True
    with pytest.raises(StoreUnavailableError):
        coordinator.update(entry.id, set_attr(a=1))
    store.fail_writes = False

    retried = coordinator.update(entry.id, set_attr(a=1))

    assert retried.attributes == {"a": 1}
    assert retried.revision == 2
    assert len(world.events()) == 2


def test_failure_during_operational_replacement_is_repaired(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = world.coordinator()
    entry = make_create()
    coordinator.create(entry)

    def broken_replace(*args: object, **kwargs: object) -> None:
        raise OSError("crash during replace")

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", broken_replace)
        with pytest.raises(StoreUnavailableError):
            coordinator.update(entry.id, set_attr(a=1))

    restarted = world.coordinator()

    assert restarted.get(entry.id).attributes == {"a": 1}
    assert [e.event_type for e in world.events()] == ["create", "update"]
    assert world.records() == list(replay(world.events()).records.values())


def test_concurrent_updates_lose_nothing(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create()
    coordinator.create(entry)
    workers = 16
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            coordinator.update(entry.id, set_attr(**{f"k{index}": index}))
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    record = coordinator.get(entry.id)
    assert record.attributes == {f"k{i}": i for i in range(workers)}
    assert record.revision == workers + 1
    assert [e.sequence for e in world.events()] == list(range(1, workers + 2))


def test_concurrent_identical_posts_create_once(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create()
    results = []

    def work() -> None:
        results.append(coordinator.create(entry))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result.created for result in results) == 1
    assert len(world.events()) == 1


def test_clock_stepping_back_across_a_month_never_breaks_the_journal(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, FakeClock(datetime(2026, 12, 1, 0, 0, 5, tzinfo=timezone.utc)))
    coordinator = world.coordinator()
    entry = make_create()
    coordinator.create(entry)

    world.clock.now = datetime(2026, 11, 30, 23, 59, 0, tzinfo=timezone.utc)
    coordinator.update(entry.id, set_attr(a=1))

    first, second = world.events()
    assert second.recorded_at >= first.recorded_at
    assert second.recorded_at.month == 12


def test_runtime_guard_returns_503_once_then_repairs(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create()
    coordinator.create(entry)
    stale = world.store().get(entry.id)
    assert stale is not None
    coordinator.update(entry.id, set_attr(a=1))
    tampering_store = coordinator._entries  # noqa: SLF001 - deliberate fault injection
    tampering_store.restore([stale])

    with pytest.raises(StoreUnavailableError):
        coordinator.update(entry.id, set_attr(b=2))
    assert len(world.events()) == 2

    assert coordinator.update(entry.id, set_attr(b=2)).attributes == {"a": 1, "b": 2}
    assert len(world.events()) == 3


def test_startup_repairs_a_store_that_missed_committed_events(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create()
    coordinator.create(entry)
    coordinator.update(entry.id, set_attr(a=1))
    for path in (world.root / "entries").rglob("*.jsonl"):
        path.unlink()

    restarted = world.coordinator()

    assert restarted.recovery_report.repaired
    assert restarted.get(entry.id).attributes == {"a": 1}
