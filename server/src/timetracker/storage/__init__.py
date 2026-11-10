"""Storage adapters."""

from timetracker.storage.errors import (
    EntryAlreadyExistsError,
    EntryNotFoundError,
    InvalidEntryUpdateError,
    InvalidJournalEventError,
    JournalFailedError,
    StorageCorruptionError,
    StorageError,
)
from timetracker.storage.interface import EntryStore, JournalStore
from timetracker.storage.jsonl import JsonlEntryStore
from timetracker.storage.journal import JsonlJournal

__all__ = [
    "EntryAlreadyExistsError",
    "EntryNotFoundError",
    "EntryStore",
    "InvalidEntryUpdateError",
    "InvalidJournalEventError",
    "JournalFailedError",
    "JournalStore",
    "JsonlEntryStore",
    "JsonlJournal",
    "StorageCorruptionError",
    "StorageError",
]
