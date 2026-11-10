"""Operational storage adapters."""

from timetracker.storage.errors import (
    EntryAlreadyExistsError,
    EntryNotFoundError,
    InvalidEntryUpdateError,
    StorageCorruptionError,
    StorageError,
)
from timetracker.storage.interface import EntryStore
from timetracker.storage.jsonl import JsonlEntryStore

__all__ = [
    "EntryAlreadyExistsError",
    "EntryNotFoundError",
    "EntryStore",
    "InvalidEntryUpdateError",
    "JsonlEntryStore",
    "StorageCorruptionError",
    "StorageError",
]
