"""JSONL operational store organised by the entry's original UTC start month."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError

from timetracker.domain.entries import EntryRecord, effective_start, json_equal, storage_month
from timetracker.storage._fs import fsync_directory
from timetracker.storage.errors import (
    EntryAlreadyExistsError,
    EntryNotFoundError,
    InvalidEntryUpdateError,
    StorageCorruptionError,
)

_MonthKey = tuple[int, int]
_MONTH_GLOB = "[0-9][0-9][0-9][0-9]/[0-9][0-9].jsonl"
_TEMP_GLOB = "[0-9][0-9][0-9][0-9]/.[0-9][0-9].jsonl.*.tmp"


def _month_key(record: EntryRecord) -> _MonthKey:
    return storage_month(record)


def quarantine_entry_files(data_dir: Path | str) -> Path | None:
    """Move the whole `entries/` tree into `quarantine/`; nothing is deleted.

    Used when the operational files cannot be trusted. Returns the new
    location, or None if there was nothing to move.
    """
    root = Path(data_dir)
    entries_dir = root / "entries"
    if not entries_dir.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / "quarantine" / f"entries-{stamp}-{uuid4().hex[:8]}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(entries_dir, destination)
    fsync_directory(destination.parent)
    fsync_directory(root)
    return destination


class JsonlEntryStore:
    """Entry store backed by `<data_dir>/entries/YYYY/MM.jsonl`.

    Each line is one complete current entry, including embedded history. An
    entry is filed by its original `started_at` month (see `storage_month`)
    and never moves. Every write replaces the whole containing file
    atomically, so a crash leaves either the old or the new complete file. Writes are serialized by a process-level
    lock; the store assumes it is the only process using the data directory.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._entries_dir = self._data_dir / "entries"
        self._lock = threading.RLock()
        self._months: dict[_MonthKey, dict[UUID, EntryRecord]] = {}
        self._index: dict[UUID, _MonthKey] = {}
        self.rebuild()

    def rebuild(self) -> None:
        """Discard in-memory state and reload it deterministically from disk."""
        with self._lock:
            self._remove_stale_temp_files()
            months: dict[_MonthKey, dict[UUID, EntryRecord]] = {}
            index: dict[UUID, _MonthKey] = {}
            for path in sorted(self._entries_dir.glob(_MONTH_GLOB)):
                key = (int(path.parent.name), int(path.stem))
                records = self._read_month_file(path, key, index)
                if records:
                    months[key] = records
                    index.update({entry_id: key for entry_id in records})
            self._months = months
            self._index = index

    def get(self, entry_id: UUID) -> EntryRecord | None:
        with self._lock:
            key = self._index.get(entry_id)
            if key is None:
                return None
            return self._months[key][entry_id].model_copy(deep=True)

    def list(
        self,
        *,
        task: str | None = None,
        attributes: Mapping[str, JsonValue] | None = None,
        include_deleted: bool = False,
    ) -> list[EntryRecord]:
        with self._lock:
            matches = [
                record
                for records in self._months.values()
                for record in records.values()
                if (include_deleted or not record.deleted)
                and (task is None or record.task == task)
                and self._attributes_match(record, attributes)
            ]
            matches.sort(key=lambda record: (effective_start(record), str(record.id)))
            return [record.model_copy(deep=True) for record in matches]

    def create(self, record: EntryRecord) -> None:
        with self._lock:
            if record.id in self._index:
                raise EntryAlreadyExistsError(record.id)
            stored = record.model_copy(deep=True)
            key = _month_key(stored)
            records = dict(self._months.get(key, {}))
            records[stored.id] = stored
            self._write_month_file(key, list(records.values()))
            self._months[key] = records
            self._index[stored.id] = key

    def update(self, record: EntryRecord) -> None:
        with self._lock:
            key = self._index.get(record.id)
            if key is None:
                raise EntryNotFoundError(record.id)
            current = self._months[key][record.id]
            if record.created_at != current.created_at:
                raise InvalidEntryUpdateError("created_at is immutable")
            if _month_key(record) != key:
                raise InvalidEntryUpdateError(
                    "an entry stays in the month file chosen when it was created"
                )
            stored = record.model_copy(deep=True)
            records = dict(self._months[key])
            records[stored.id] = stored
            self._write_month_file(key, list(records.values()))
            self._months[key] = records

    def restore(self, records: Iterable[EntryRecord]) -> None:
        """Insert or replace complete records, rewriting each month file once.

        For recovery only: records come from journal replay, so they are
        written as given. An entry whose placement differs from where it is
        currently stored is moved.
        """
        with self._lock:
            incoming = [record.model_copy(deep=True) for record in records]
            months = dict(self._months)
            index = dict(self._index)
            touched: set[_MonthKey] = set()
            for record in incoming:
                key = _month_key(record)
                old_key = index.get(record.id)
                if old_key is not None:
                    old_records = dict(months[old_key])
                    del old_records[record.id]
                    months[old_key] = old_records
                    touched.add(old_key)
                new_records = dict(months.get(key, {}))
                new_records[record.id] = record
                months[key] = new_records
                index[record.id] = key
                touched.add(key)
            try:
                for key in sorted(touched):
                    self._write_month_file(key, list(months[key].values()))
            except BaseException:
                self.rebuild()
                raise
            self._months = {key: value for key, value in months.items() if value}
            self._index = index

    def reset(self, records: Iterable[EntryRecord]) -> Path | None:
        """Quarantine every existing entry file, then store exactly `records`."""
        with self._lock:
            quarantined = quarantine_entry_files(self._data_dir)
            self._months = {}
            self._index = {}
            self.restore(records)
            return quarantined

    @staticmethod
    def _attributes_match(
        record: EntryRecord, attributes: Mapping[str, JsonValue] | None
    ) -> bool:
        if not attributes:
            return True
        return all(
            name in record.attributes and json_equal(record.attributes[name], value)
            for name, value in attributes.items()
        )

    def _path_for(self, key: _MonthKey) -> Path:
        year, month = key
        return self._entries_dir / f"{year:04d}" / f"{month:02d}.jsonl"

    def _read_month_file(
        self, path: Path, key: _MonthKey, seen: Mapping[UUID, _MonthKey]
    ) -> dict[UUID, EntryRecord]:
        records: dict[UUID, EntryRecord] = {}
        with open(path, encoding="utf-8", newline="\n") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = EntryRecord.model_validate_json(line)
                except ValidationError as exc:
                    raise StorageCorruptionError(
                        path, line_number, f"invalid entry record: {exc}"
                    ) from exc
                if _month_key(record) != key:
                    raise StorageCorruptionError(
                        path,
                        line_number,
                        "entry storage month does not match its file",
                    )
                if record.id in records or record.id in seen:
                    raise StorageCorruptionError(
                        path, line_number, f"duplicate entry id {record.id}"
                    )
                records[record.id] = record
        return records

    def _remove_stale_temp_files(self) -> None:
        # Left behind only when a crash interrupted a replacement; the target
        # file was never touched, so the temp file holds no committed data.
        for stale in self._entries_dir.glob(_TEMP_GLOB):
            stale.unlink(missing_ok=True)

    def _write_month_file(self, key: _MonthKey, records: list[EntryRecord]) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = "".join(f"{record.model_dump_json()}\n" for record in records)
        try:
            with open(temp_path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        for directory in (path.parent, self._entries_dir, self._data_dir):
            fsync_directory(directory)
