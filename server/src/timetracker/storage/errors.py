"""Storage-layer errors."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID


class StorageError(Exception):
    """Base class for operational storage failures."""


class EntryAlreadyExistsError(StorageError):
    def __init__(self, entry_id: UUID) -> None:
        super().__init__(f"An entry already exists with id {entry_id}.")
        self.entry_id = entry_id


class EntryNotFoundError(StorageError):
    def __init__(self, entry_id: UUID) -> None:
        super().__init__(f"No entry exists with id {entry_id}.")
        self.entry_id = entry_id


class InvalidEntryUpdateError(StorageError):
    """A replacement record changed identity or placement fields."""


class StorageCorruptionError(StorageError):
    """Stored operational data is unreadable or inconsistent."""

    def __init__(self, path: Path, line_number: int | None, reason: str) -> None:
        location = f"{path}" if line_number is None else f"{path}:{line_number}"
        super().__init__(f"Corrupt operational data at {location}: {reason}")
        self.path = path
        self.line_number = line_number
        self.reason = reason


class InvalidJournalEventError(StorageError):
    """An event cannot be appended because it would break journal invariants."""


class JournalFailedError(StorageError):
    """A failed append could not be rolled back; the journal refuses further writes."""
