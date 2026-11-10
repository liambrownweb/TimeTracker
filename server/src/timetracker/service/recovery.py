"""Journal-to-record verification and targeted repair (spec decision #7).

The journal is the authority. `load_state` replays it in memory to obtain the
expected record for every entry; `compare` checks the stored records against
that expectation; `verify_and_repair` rewrites only what the comparison
flagged and then re-verifies completely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from timetracker.domain.entries import EntryRecord, JournalEvent, json_equal
from timetracker.domain.replay import ReplayError, ReplayResult, replay
from timetracker.service.errors import RecoveryError
from timetracker.storage import (
    JournalStore,
    JsonlEntryStore,
    StorageCorruptionError,
)
from timetracker.storage.interface import EntryStore
from timetracker.storage.jsonl import quarantine_entry_files


class FindingKind(StrEnum):
    MISSING_RECORD = "missing_record"
    ORPHAN_RECORD = "orphan_record"
    TAIL_MISSED = "tail_missed"
    MIDDLE_MISSED = "middle_missed"
    METADATA_MISMATCH = "metadata_mismatch"
    FIELD_MISMATCH = "field_mismatch"


@dataclass(frozen=True)
class Finding:
    entry_id: UUID
    kind: FindingKind
    detail: str


@dataclass(frozen=True)
class VerificationOptions:
    """Which comparisons run. The tail check and repair-on-detection always do."""

    cross_check: bool = True
    deep_check: bool = True


@dataclass
class JournalState:
    events: list[JournalEvent]
    replayed: ReplayResult


@dataclass
class RecoveryReport:
    state: JournalState
    findings: list[Finding] = field(default_factory=list)
    repaired: bool = False
    quarantined: list[Path] = field(default_factory=list)
    ran_cross_check: bool = False
    ran_deep_check: bool = False

    @property
    def skipped_no_ops(self) -> list[UUID]:
        """Journal events replay proved to be no-ops (the live path never writes one)."""
        return self.state.replayed.skipped_no_ops


def load_state(journal: JournalStore) -> JournalState:
    events = journal.read()
    try:
        replayed = replay(events)
    except ReplayError as exc:
        raise RecoveryError(f"the journal cannot be replayed: {exc}") from exc
    return JournalState(events=events, replayed=replayed)


def _history_event_ids(record: EntryRecord) -> list[UUID]:
    return [item.event_id for item in record.history if item.kind != "initial"]


def _is_prefix(shorter: list[UUID], longer: list[UUID]) -> bool:
    return len(shorter) < len(longer) and longer[: len(shorter)] == shorter


def _compare_one(
    expected: EntryRecord, actual: EntryRecord, *, deep: bool
) -> Finding | None:
    expected_ids = _history_event_ids(expected)
    actual_ids = _history_event_ids(actual)
    if actual_ids != expected_ids:
        kind = (
            FindingKind.TAIL_MISSED
            if _is_prefix(actual_ids, expected_ids)
            else FindingKind.MIDDLE_MISSED
        )
        return Finding(expected.id, kind, "history does not match the journal's events")
    for name in ("last_event_sequence", "revision", "deleted"):
        if getattr(actual, name) != getattr(expected, name):
            return Finding(
                expected.id,
                FindingKind.METADATA_MISMATCH,
                f"{name} is {getattr(actual, name)!r}, journal says {getattr(expected, name)!r}",
            )
    if deep:
        left = expected.model_dump(mode="json")
        right = actual.model_dump(mode="json")
        differing = [name for name in left if not json_equal(left[name], right.get(name))]
        if differing:
            return Finding(
                expected.id,
                FindingKind.FIELD_MISMATCH,
                f"fields differ from the journal: {', '.join(sorted(differing))}",
            )
    return None


def compare(
    expected: dict[UUID, EntryRecord],
    actual_records: list[EntryRecord],
    *,
    deep: bool,
) -> list[Finding]:
    """Compare stored records with the journal's expectation.

    The cross-check (`deep=False`) verifies existence in both directions and
    each entry's event history, revision, deletion state and last sequence.
    `deep=True` additionally compares every field, type-strictly.
    """
    actual = {record.id: record for record in actual_records}
    findings: list[Finding] = []
    for entry_id, want in expected.items():
        have = actual.get(entry_id)
        if have is None:
            findings.append(
                Finding(entry_id, FindingKind.MISSING_RECORD, "no operational record")
            )
            continue
        finding = _compare_one(want, have, deep=deep)
        if finding is not None:
            findings.append(finding)
    for entry_id in actual.keys() - expected.keys():
        findings.append(
            Finding(entry_id, FindingKind.ORPHAN_RECORD, "record has no create event")
        )
    return findings


def _tail_matches(entries: EntryStore, state: JournalState) -> bool:
    """Cheap check: the journal's last event is reflected in the records."""
    if not state.events:
        return not entries.list(include_deleted=True)
    tail = state.events[-1]
    record = entries.get(tail.entry_id)
    if record is None or record.last_event_sequence != tail.sequence:
        return False
    return all(
        r.last_event_sequence <= tail.sequence for r in entries.list(include_deleted=True)
    )


def open_entry_store(data_dir: Path | str) -> tuple[JsonlEntryStore, Path | None]:
    """Open the operational store; if its files are unreadable, quarantine and start empty.

    The empty store is then filled from the journal by `verify_and_repair`.
    """
    try:
        return JsonlEntryStore(data_dir), None
    except StorageCorruptionError:
        quarantined = quarantine_entry_files(data_dir)
        return JsonlEntryStore(data_dir), quarantined


def verify_and_repair(
    entries: EntryStore,
    journal: JournalStore,
    options: VerificationOptions = VerificationOptions(),
) -> RecoveryReport:
    """Verify operational state against the journal and repair what differs.

    A failed tail check escalates to the cross-check whatever `options` say.
    Repairs write only flagged entries, taken from journal replay and never
    layered on later state; an orphan record forces a full quarantine and
    rebuild. After any repair the full deep comparison must come back clean,
    otherwise RecoveryError is raised.
    """
    state = load_state(journal)
    report = RecoveryReport(state=state)
    expected = state.replayed.records

    tail_ok = _tail_matches(entries, state)
    report.ran_cross_check = options.cross_check or options.deep_check or not tail_ok
    report.ran_deep_check = options.deep_check
    if not report.ran_cross_check:
        return report

    report.findings = compare(
        expected, entries.list(include_deleted=True), deep=options.deep_check
    )
    if not report.findings:
        return report

    if any(f.kind is FindingKind.ORPHAN_RECORD for f in report.findings):
        quarantined = entries.reset(expected.values())
        if quarantined is not None:
            report.quarantined.append(quarantined)
    else:
        entries.restore(expected[f.entry_id] for f in report.findings)
    report.repaired = True

    remaining = compare(expected, entries.list(include_deleted=True), deep=True)
    if remaining:
        raise RecoveryError(
            f"{len(remaining)} entries still differ from the journal after repair; "
            f"first: {remaining[0].entry_id} ({remaining[0].kind.value})"
        )
    return report
