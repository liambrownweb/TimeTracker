"""Deep-check scheduling and managed startup/verification workflows."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Generator
from uuid import uuid4

from timetracker.config import Settings
from timetracker.service.coordinator import EntryCoordinator
from timetracker.service.recovery import (
    Finding, VerificationOptions, compare, load_state, open_entry_store,
)
from timetracker.storage import JsonlEntryStore, JsonlJournal
from timetracker.storage._fs import fsync_directory

log = logging.getLogger(__name__)


class VerificationError(ValueError):
    """Verification or its persistent schedule state is invalid."""


@dataclass(frozen=True)
class CheckState:
    completed_at: datetime
    sequence: int


def read_check_state(data_dir: Path) -> CheckState | None:
    path = data_dir / "verification-state.json"
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise VerificationError(f"invalid verification state {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"completed_at", "sequence"}:
        raise VerificationError(f"invalid verification state {path}: expected completed_at and sequence")
    try:
        timestamp = data["completed_at"]
        if not isinstance(timestamp, str):
            raise ValueError("completed_at must be a timestamp")
        completed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if completed_at.tzinfo is None or completed_at.utcoffset() != timedelta(0):
            raise ValueError("completed_at must be UTC")
        sequence = data["sequence"]
        if type(sequence) is not int or sequence < 0:
            raise ValueError("sequence must be a nonnegative integer")
    except (ValueError, TypeError) as exc:
        raise VerificationError(f"invalid verification state {path}: {exc}") from exc
    return CheckState(completed_at, sequence)


def write_check_state(data_dir: Path, state: CheckState) -> None:
    path = data_dir / "verification-state.json"
    temporary = data_dir / f".verification-state.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump({"completed_at": state.completed_at.isoformat().replace("+00:00", "Z"),
                       "sequence": state.sequence}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(data_dir)
    finally:
        temporary.unlink(missing_ok=True)


def deep_check_due(settings: Settings, sequence: int, now: datetime) -> bool:
    state = read_check_state(settings.data_dir)
    if state is not None and state.sequence > sequence:
        raise VerificationError("verification state sequence is ahead of the journal")
    if settings.deep_check == "startup":
        return True
    if settings.deep_check == "manual":
        return False
    if state is None:
        return True
    return (
        settings.deep_check_days is not None
        and now - state.completed_at >= timedelta(days=settings.deep_check_days)
    ) or (
        settings.deep_check_events is not None
        and sequence - state.sequence >= settings.deep_check_events
    )


@contextmanager
def data_directory_lock(path: Path, *, create: bool) -> Generator[None]:
    """Lock the directory inode across managed processes without modifying data."""
    if create:
        path.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VerificationError(f"data directory {path} is already in use") from exc
        yield
    finally:
        os.close(descriptor)


def _findings(entries: JsonlEntryStore, journal: JsonlJournal, *, deep: bool) -> list[Finding]:
    state = load_state(journal)
    for event_id in state.replayed.skipped_no_ops:
        log.warning("Journal event %s was skipped as a provable no-op", event_id)
    return compare(state.replayed.records, entries.list(include_deleted=True), deep=deep)


def verify(settings: Settings, *, deep: bool = False) -> list[Finding]:
    """Check all journal and record data without repairing it."""
    with data_directory_lock(settings.data_dir, create=False):
        journal = JsonlJournal(settings.data_dir, verify_integrity=True, recover_tail=False)
        previous = read_check_state(settings.data_dir)
        if previous is not None and previous.sequence > journal.last_sequence:
            raise VerificationError("verification state sequence is ahead of the journal")
        entries = JsonlEntryStore(settings.data_dir, clean_temps=False)
        findings = _findings(entries, journal, deep=deep)
        if deep and not findings:
            write_check_state(settings.data_dir, CheckState(datetime.now(timezone.utc), journal.last_sequence))
        return findings


@contextmanager
def open_server(settings: Settings) -> Generator[EntryCoordinator]:
    """Own a data directory for the lifetime of a configured coordinator."""
    with data_directory_lock(settings.data_dir, create=True):
        # A scheduled deep check also requires full journal verification.
        journal = JsonlJournal(settings.data_dir, verify_integrity=settings.verify_journal_on_startup)
        now = datetime.now(timezone.utc)
        due = deep_check_due(settings, journal.last_sequence, now)
        if due and not settings.verify_journal_on_startup:
            journal = JsonlJournal(settings.data_dir, verify_integrity=True)
        # Do not quarantine operational data until the journal is replayable.
        load_state(journal)
        entries, quarantined = open_entry_store(settings.data_dir)
        if quarantined is not None:
            log.warning("Corrupt operational data quarantined at %s", quarantined)
        coordinator = EntryCoordinator(
            entries, journal,
            options=VerificationOptions(cross_check=settings.verify_cross_check_on_startup, deep_check=False),
        )
        if coordinator.recovery_report.repaired:
            log.warning("Repaired %d operational record(s) from the journal",
                        len(coordinator.recovery_report.findings))
        for event_id in coordinator.recovery_report.skipped_no_ops:
            log.warning("Journal event %s was skipped as a provable no-op", event_id)
        if due:
            findings = _findings(entries, journal, deep=True)
            if findings:
                first = findings[0]
                raise VerificationError(
                    f"deep verification failed: {first.entry_id}: {first.kind.value}: {first.detail}"
                )
            write_check_state(settings.data_dir, CheckState(datetime.now(timezone.utc), journal.last_sequence))
        yield coordinator
