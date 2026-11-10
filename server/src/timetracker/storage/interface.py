"""Storage interface used by services; API handlers never touch files."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from timetracker.domain.entries import EntryRecord, JournalEvent


class EntryStore(Protocol):
    """Current-state read model for entries.

    Records are persisted as complete states. Updates and logical deletions
    are both expressed as a replacement record produced by the domain layer
    (`apply_entry_update` / `delete_entry`); deleted records stay stored.
    """

    def get(self, entry_id: UUID) -> EntryRecord | None:
        """Return the entry, including a logically deleted one, or None."""

    def list(
        self,
        *,
        task: str | None = None,
        attributes: Mapping[str, JsonValue] | None = None,
        include_deleted: bool = False,
    ) -> list[EntryRecord]:
        """Return entries ordered by `created_at`, then `id`.

        `task` and each attribute value are matched exactly, with JSON types
        distinguished (`1` does not match `true`). Deleted entries are
        excluded unless `include_deleted` is true.
        """

    def create(self, record: EntryRecord) -> None:
        """Persist a new entry; raise EntryAlreadyExistsError on a duplicate id."""

    def update(self, record: EntryRecord) -> None:
        """Replace an existing entry's state; raise EntryNotFoundError if absent."""


class JournalStore(Protocol):
    """Immutable, globally ordered log of committed transaction events."""

    @property
    def last_sequence(self) -> int:
        """Sequence of the latest committed event, or 0 for an empty journal."""

    def append(self, event: JournalEvent) -> None:
        """Durably commit the next event; return only after it is fsynced.

        The event's sequence must be exactly last_sequence + 1 and its event id
        unused, otherwise InvalidJournalEventError is raised.
        """

    def read(self, after_sequence: int = 0) -> list[JournalEvent]:
        """Return committed events with a greater sequence, in order."""
