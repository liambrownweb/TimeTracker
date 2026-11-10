"""Write path: journal first, then operational state (spec section 04)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import JsonValue

from timetracker.domain.entries import (
    CreateJournalEvent,
    DeleteJournalEvent,
    EntryCreate,
    EntryRecord,
    EntryUpdate,
    JournalEvent,
    UpdateHistory,
    UpdateJournalEvent,
    apply_entry_update,
    json_equal,
)
from timetracker.domain.replay import apply_journal_event
from timetracker.service.errors import (
    EntryConflictError,
    EntryDeletedError,
    StoreUnavailableError,
)
from timetracker.service.recovery import (
    JournalState,
    RecoveryReport,
    VerificationOptions,
    verify_and_repair,
)
from timetracker.storage import EntryNotFoundError, JournalStore
from timetracker.storage.interface import EntryStore


@dataclass(frozen=True)
class CreateResult:
    record: EntryRecord
    # False when an identical earlier submission was recognized (a retry).
    created: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _same_submission(original: EntryCreate, retry: EntryCreate) -> bool:
    return json_equal(original.model_dump(mode="json"), retry.model_dump(mode="json"))


class EntryCoordinator:
    """Serializes mutations and keeps the journal and entry store in step.

    Every mutation holds one lock and follows the same order: validate against
    current state, compute the meaningful delta, durably append the journal
    event, then apply that same event to the record and persist it. Once the
    journal append returns, the mutation is committed whatever happens next.
    If persisting the record then fails, the store is flagged and repaired from
    the journal before any later request is served.
    """

    def __init__(
        self,
        entries: EntryStore,
        journal: JournalStore,
        *,
        options: VerificationOptions = VerificationOptions(),
        clock: Callable[[], datetime] = _utc_now,
        new_event_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._entries = entries
        self._journal = journal
        self._clock = clock
        self._new_event_id = new_event_id
        self._lock = threading.RLock()
        self._needs_repair = False
        self._creates: dict[UUID, CreateJournalEvent] = {}
        self._last_sequence: dict[UUID, int] = {}
        self._last_recorded_at: datetime | None = None
        self.recovery_report: RecoveryReport = verify_and_repair(
            entries, journal, options
        )
        self._adopt(self.recovery_report.state)

    def _adopt(self, state: JournalState) -> None:
        self._creates = {
            event.entry_id: event
            for event in state.events
            if isinstance(event, CreateJournalEvent)
        }
        self._last_sequence = {
            entry_id: record.last_event_sequence
            for entry_id, record in state.replayed.records.items()
        }
        self._last_recorded_at = state.events[-1].recorded_at if state.events else None

    def _flag(self, message: str) -> StoreUnavailableError:
        self._needs_repair = True
        return StoreUnavailableError(message)

    def _ensure_consistent(self) -> None:
        if not self._needs_repair:
            return
        try:
            report = verify_and_repair(
                self._entries, self._journal, VerificationOptions(deep_check=False)
            )
        except Exception as exc:
            raise StoreUnavailableError(
                f"repair of operational state failed: {exc}"
            ) from exc
        self._adopt(report.state)
        self.recovery_report = report
        self._needs_repair = False

    def _stamp(self) -> datetime:
        """Commit time; never earlier than the previous event, even if the clock steps back."""
        now = self._clock()
        if self._last_recorded_at is not None and now < self._last_recorded_at:
            return self._last_recorded_at
        return now

    def _load(self, entry_id: UUID) -> EntryRecord:
        """The entry's record, or EntryNotFoundError; also the runtime guard.

        A record whose `last_event_sequence` is not the sequence the journal
        says it should have means a write was lost or the store was altered.
        """
        record = self._entries.get(entry_id)
        known = entry_id in self._creates
        if record is None and not known:
            raise EntryNotFoundError(entry_id)
        if record is None or not known:
            raise self._flag(f"entry {entry_id} and the journal disagree on existence")
        if record.last_event_sequence != self._last_sequence[entry_id]:
            raise self._flag(
                f"entry {entry_id} is at sequence {record.last_event_sequence}, "
                f"the journal expects {self._last_sequence[entry_id]}"
            )
        return record

    def _commit(
        self, event: JournalEvent, record: EntryRecord, *, is_create: bool
    ) -> EntryRecord:
        self._journal.append(event)
        if isinstance(event, CreateJournalEvent):
            self._creates[event.entry_id] = event
        self._last_sequence[event.entry_id] = event.sequence
        self._last_recorded_at = event.recorded_at
        try:
            if is_create:
                self._entries.create(record)
            else:
                self._entries.update(record)
        except Exception as exc:
            raise self._flag(
                f"event {event.event_id} is committed but the record was not saved: {exc}"
            ) from exc
        return record

    def create(self, entry: EntryCreate) -> CreateResult:
        with self._lock:
            self._ensure_consistent()
            original = self._creates.get(entry.id)
            if original is not None:
                if not _same_submission(original.entry, entry):
                    raise EntryConflictError(entry.id)
                return CreateResult(self._load(entry.id), created=False)
            if self._entries.get(entry.id) is not None:
                raise self._flag(
                    f"entry {entry.id} exists in the store but not in the journal"
                )
            event = CreateJournalEvent(
                event_type="create",
                event_id=self._new_event_id(),
                sequence=self._journal.last_sequence + 1,
                entry_id=entry.id,
                recorded_at=self._stamp(),
                entry=entry,
            )
            record = apply_journal_event(None, event)
            return CreateResult(
                self._commit(event, record, is_create=True), created=True
            )

    def update(self, entry_id: UUID, update: EntryUpdate) -> EntryRecord:
        """Apply a PATCH. A request that changes nothing writes nothing."""
        with self._lock:
            self._ensure_consistent()
            record = self._load(entry_id)
            if record.deleted:
                raise EntryDeletedError(entry_id)
            event_id = self._new_event_id()
            sequence = self._journal.last_sequence + 1
            recorded_at = self._stamp()
            candidate = apply_entry_update(
                record,
                update,
                event_id=event_id,
                recorded_at=recorded_at,
                sequence=sequence,
            )
            if candidate is record:
                return record
            applied = candidate.history[-1]
            assert isinstance(applied, UpdateHistory)
            event = UpdateJournalEvent(
                event_type="update",
                event_id=event_id,
                sequence=sequence,
                entry_id=entry_id,
                recorded_at=recorded_at,
                changes=applied.changes,
            )
            # Persist what replay would build from the event, so both paths agree.
            return self._commit(
                event, apply_journal_event(record, event), is_create=False
            )

    def delete(self, entry_id: UUID) -> None:
        """Logically delete; repeating the request is a no-op."""
        with self._lock:
            self._ensure_consistent()
            record = self._load(entry_id)
            if record.deleted:
                return
            event = DeleteJournalEvent(
                event_type="delete",
                event_id=self._new_event_id(),
                sequence=self._journal.last_sequence + 1,
                entry_id=entry_id,
                recorded_at=self._stamp(),
            )
            self._commit(event, apply_journal_event(record, event), is_create=False)

    def get(self, entry_id: UUID) -> EntryRecord:
        """Return a live entry; EntryNotFoundError or EntryDeletedError otherwise."""
        with self._lock:
            self._ensure_consistent()
            record = self._load(entry_id)
            if record.deleted:
                raise EntryDeletedError(entry_id)
            return record

    def list(
        self,
        *,
        task: str | None = None,
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> list[EntryRecord]:
        with self._lock:
            self._ensure_consistent()
            return self._entries.list(task=task, attributes=attributes)
