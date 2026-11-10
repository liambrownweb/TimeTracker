from pathlib import Path
from uuid import uuid4

import pytest
from conftest import World, make_create

from timetracker.domain.entries import (
    AttributeDelta,
    EntryRecord,
    EntryUpdate,
    UpdateJournalEvent,
    EntryDelta,
)
from timetracker.service import (
    FindingKind,
    RecoveryError,
    VerificationOptions,
    open_entry_store,
    verify_and_repair,
)

NONE = VerificationOptions(cross_check=False, deep_check=False)
CROSS_ONLY = VerificationOptions(cross_check=True, deep_check=False)
FULL = VerificationOptions()


def bump(coordinator, entry_id, **values) -> EntryRecord:
    return coordinator.update(
        entry_id, EntryUpdate(attributes=AttributeDelta(set=values))
    )


def kinds(report) -> list[FindingKind]:
    return [finding.kind for finding in report.findings]


def run(world: World, options: VerificationOptions):
    store = world.store()
    return store, verify_and_repair(store, world.journal(), options)


def three_event_entry(world: World):
    coordinator = world.coordinator()
    entry = make_create(attributes={"a": 0})
    coordinator.create(entry)
    first = coordinator.get(entry.id)
    bump(coordinator, entry.id, a=1)
    middle = coordinator.get(entry.id)
    bump(coordinator, entry.id, a=2)
    return entry, first, middle, coordinator.get(entry.id)


def test_consistent_state_has_no_findings(world: World) -> None:
    three_event_entry(world)

    _, report = run(world, FULL)

    assert report.findings == []
    assert not report.repaired
    assert report.ran_cross_check and report.ran_deep_check


def test_missing_record_is_rebuilt(world: World) -> None:
    entry, *_, final = three_event_entry(world)
    for path in (world.root / "entries").rglob("*.jsonl"):
        path.unlink()

    store, report = run(world, CROSS_ONLY)

    assert kinds(report) == [FindingKind.MISSING_RECORD]
    assert store.get(entry.id) == final


def test_missed_tail_events_are_applied(world: World) -> None:
    entry, first, _, final = three_event_entry(world)
    world.store().restore([first])

    store, report = run(world, CROSS_ONLY)

    assert kinds(report) == [FindingKind.TAIL_MISSED]
    assert store.get(entry.id) == final


def test_missed_middle_event_rebuilds_from_the_journal_not_on_later_state(
    world: World,
) -> None:
    entry, _, _, final = three_event_entry(world)
    gappy = final.model_copy(update={"history": [final.history[0], final.history[2]]})
    world.store().restore([gappy])

    store, report = run(world, CROSS_ONLY)

    assert kinds(report) == [FindingKind.MIDDLE_MISSED]
    assert store.get(entry.id) == final


def test_wrong_metadata_is_flagged(world: World) -> None:
    entry, *_, final = three_event_entry(world)
    world.store().restore([final.model_copy(update={"revision": 9})])

    store, report = run(world, CROSS_ONLY)

    assert kinds(report) == [FindingKind.METADATA_MISMATCH]
    assert store.get(entry.id) == final


def test_orphan_record_forces_quarantined_rebuild(world: World) -> None:
    entry, *_, final = three_event_entry(world)
    ghost = final.model_copy(update={"id": uuid4(), "task": "ghost"})
    world.store().restore([ghost])

    store, report = run(world, CROSS_ONLY)

    assert FindingKind.ORPHAN_RECORD in kinds(report)
    assert store.get(ghost.id) is None
    assert store.get(entry.id) == final
    assert len(report.quarantined) == 1 and report.quarantined[0].exists()


def test_deep_check_catches_what_the_cross_check_cannot(world: World) -> None:
    entry, *_, final = three_event_entry(world)
    world.store().restore([final.model_copy(update={"task": "tampered"})])

    _, cross = run(world, CROSS_ONLY)
    assert cross.findings == []
    assert world.store().get(entry.id).task == "tampered"

    store, deep = run(world, FULL)
    assert kinds(deep) == [FindingKind.FIELD_MISMATCH]
    assert "task" in deep.findings[0].detail
    assert store.get(entry.id) == final


def test_deep_check_distinguishes_json_types(world: World) -> None:
    entry, *_, final = three_event_entry(world)
    tampered = final.model_copy(update={"attributes": {"a": 2.0}})
    world.store().restore([tampered])

    _, report = run(world, FULL)

    assert kinds(report) == [FindingKind.FIELD_MISMATCH]


def test_disabled_checks_do_not_run_while_the_tail_is_intact(world: World) -> None:
    entry, *_, final = three_event_entry(world)
    world.store().restore([final.model_copy(update={"task": "tampered"})])

    store, report = run(world, NONE)

    assert not report.ran_cross_check and not report.ran_deep_check
    assert report.findings == []
    assert store.get(entry.id).task == "tampered"


def test_failed_tail_check_escalates_even_when_checks_are_disabled(
    world: World,
) -> None:
    entry, first, _, final = three_event_entry(world)
    world.store().restore([first])

    store, report = run(world, NONE)

    assert report.ran_cross_check and not report.ran_deep_check
    assert store.get(entry.id) == final


def test_store_ahead_of_journal_is_reset_to_the_journal(
    world: World, tmp_path: Path
) -> None:
    entry, *_ = three_event_entry(world)
    shorter = World(tmp_path / "shorter")
    shorter_journal = shorter.journal()
    for event in world.events()[:2]:
        shorter_journal.append(event)

    store = world.store()
    report = verify_and_repair(store, shorter_journal, NONE)

    assert report.repaired
    assert store.get(entry.id).revision == 2


def test_no_op_event_is_classified_like_replay(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create(attributes={"a": 1})
    coordinator.create(entry)
    journal = world.journal()
    no_op = UpdateJournalEvent(
        event_type="update",
        event_id=uuid4(),
        sequence=2,
        entry_id=entry.id,
        recorded_at=world.clock(),
        changes=EntryDelta(attributes=AttributeDelta(set={"a": 1})),
    )
    journal.append(no_op)

    store = world.store()
    report = verify_and_repair(store, journal, FULL)

    assert report.skipped_no_ops == [no_op.event_id]
    assert report.findings == []
    assert not report.repaired
    assert store.get(entry.id).revision == 1


def test_a_changed_type_is_not_treated_as_a_no_op(world: World) -> None:
    coordinator = world.coordinator()
    entry = make_create(attributes={"a": 1})
    coordinator.create(entry)
    journal = world.journal()
    journal.append(
        UpdateJournalEvent(
            event_type="update",
            event_id=uuid4(),
            sequence=2,
            entry_id=entry.id,
            recorded_at=world.clock(),
            changes=EntryDelta(attributes=AttributeDelta(set={"a": True})),
        )
    )

    store = world.store()
    report = verify_and_repair(store, journal, FULL)

    assert report.skipped_no_ops == []
    assert store.get(entry.id).attributes == {"a": True}


def test_unreplayable_journal_is_a_recovery_error(world: World) -> None:
    journal = world.journal()
    journal.append(
        UpdateJournalEvent(
            event_type="update",
            event_id=uuid4(),
            sequence=1,
            entry_id=uuid4(),
            recorded_at=world.clock(),
            changes=EntryDelta(task="x"),
        )
    )

    with pytest.raises(RecoveryError):
        verify_and_repair(world.store(), journal, FULL)


def test_corrupt_operational_files_are_quarantined_and_rebuilt(world: World) -> None:
    entry, *_, final = three_event_entry(world)
    target = next((world.root / "entries").rglob("*.jsonl"))
    target.write_text("not json\n", encoding="utf-8")

    store, quarantined = open_entry_store(world.root)
    assert quarantined is not None and quarantined.exists()
    assert store.list(include_deleted=True) == []

    report = verify_and_repair(store, world.journal(), CROSS_ONLY)

    assert kinds(report) == [FindingKind.MISSING_RECORD]
    assert store.get(entry.id) == final
    assert (quarantined / target.parent.name / target.name).read_text() == "not json\n"
