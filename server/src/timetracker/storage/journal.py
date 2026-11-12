"""Append-only JSONL journal organised by UTC commit month."""

from __future__ import annotations

import hmac
import json
import os
import threading
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from timetracker.domain.entries import JournalEvent
from timetracker.storage._fs import fsync_directory
from timetracker.storage.errors import (
    InvalidJournalEventError,
    JournalFailedError,
    StorageCorruptionError,
)

_MonthKey = tuple[int, int]
_MONTH_GLOB = "[0-9][0-9][0-9][0-9]/[0-9][0-9].jsonl"
_CHECKSUM_PREFIX = "sha256:"
_EVENT_ADAPTER: TypeAdapter[JournalEvent] = TypeAdapter(JournalEvent)


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def event_checksum(event: JournalEvent) -> str:
    """SHA-256 over the event's canonical JSON, excluding the checksum itself."""
    digest = sha256(_canonical(event.model_dump(mode="json"))).hexdigest()
    return f"{_CHECKSUM_PREFIX}{digest}"


def encode_event(event: JournalEvent) -> bytes:
    """Return one newline-terminated journal line for `event`."""
    payload = event.model_dump(mode="json")
    payload["checksum"] = event_checksum(event)
    return _canonical(payload) + b"\n"


def decode_event(line: bytes, *, verify_checksum: bool = True) -> JournalEvent:
    """Parse and verify one journal line (without its newline).

    Raises ValueError describing why the line is not a valid committed event.
    """
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("line is not a JSON object")
    stored = payload.pop("checksum", None)
    if not isinstance(stored, str):
        raise ValueError("missing checksum")
    event = _EVENT_ADAPTER.validate_json(_canonical(payload))
    if verify_checksum and not hmac.compare_digest(stored, event_checksum(event)):
        raise ValueError("checksum mismatch")
    return event


def _month_of(event: JournalEvent) -> _MonthKey:
    return (event.recorded_at.year, event.recorded_at.month)


class JsonlJournal:
    """Journal backed by `<data_dir>/journal/YYYY/MM.jsonl`.

    One checksummed event per line, selected by UTC commit time. Appends only
    report success after the line (including its newline) is fsynced.

    Opening scans the whole journal. A final line in the newest file that lacks
    its newline was never acknowledged: it is copied to `<data_dir>/quarantine`
    and truncated, and its sequence number is reused. Every other fault (bad
    or blank line, checksum mismatch, sequence gap, duplicate event id, event
    in the wrong month file) raises StorageCorruptionError and nothing is
    skipped. The store assumes a single process owns the data directory.
    """

    def __init__(
        self, data_dir: Path | str, quarantine_dir: Path | str | None = None,
        *, verify_integrity: bool = True, recover_tail: bool = True,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._journal_dir = self._data_dir / "journal"
        self._quarantine_dir = (
            Path(quarantine_dir)
            if quarantine_dir is not None
            else self._data_dir / "quarantine"
        )
        self._lock = threading.RLock()
        self._verify_integrity = verify_integrity
        self._recover_tail = recover_tail
        self._last_sequence = 0
        self._last_month: _MonthKey | None = None
        self._event_ids: set[UUID] = set()
        self._failed = False
        self.quarantined: list[Path] = []
        self._load()

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._last_sequence

    def read(self, after_sequence: int = 0) -> list[JournalEvent]:
        """Return committed events with `sequence > after_sequence`, in order."""
        with self._lock:
            events: list[JournalEvent] = []
            for path in sorted(self._journal_dir.glob(_MONTH_GLOB)):
                for _, event in self._parse_file(path, recover=False):
                    if event.sequence > after_sequence:
                        events.append(event)
            return events

    def append(self, event: JournalEvent) -> None:
        with self._lock:
            if self._failed:
                raise JournalFailedError(
                    "an earlier append could not be rolled back; restart to re-verify"
                )
            if event.sequence != self._last_sequence + 1:
                raise InvalidJournalEventError(
                    f"expected sequence {self._last_sequence + 1}, got {event.sequence}"
                )
            if event.event_id in self._event_ids:
                raise InvalidJournalEventError(f"duplicate event id {event.event_id}")
            key = _month_of(event)
            if self._last_month is not None and key < self._last_month:
                raise InvalidJournalEventError(
                    "event commit time is in an earlier month than the journal's latest"
                )

            data = encode_event(event)
            path = self._month_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not path.exists()
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                if is_new:
                    for directory in (path.parent, self._journal_dir, self._data_dir):
                        fsync_directory(directory)
                size_before = os.fstat(descriptor).st_size
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(descriptor, view) :]
                    os.fsync(descriptor)
                except BaseException:
                    self._roll_back(descriptor, size_before)
                    raise
            finally:
                os.close(descriptor)

            self._last_sequence = event.sequence
            self._last_month = key
            self._event_ids.add(event.event_id)

    def _month_path(self, key: _MonthKey) -> Path:
        return self._journal_dir / f"{key[0]:04d}" / f"{key[1]:02d}.jsonl"

    def _roll_back(self, descriptor: int, size: int) -> None:
        try:
            os.ftruncate(descriptor, size)
            os.fsync(descriptor)
        except OSError:
            self._failed = True

    def _load(self) -> None:
        files = sorted(self._journal_dir.glob(_MONTH_GLOB))
        expected = 1
        for index, path in enumerate(files):
            recover = self._recover_tail and index == len(files) - 1
            for line_number, event in self._parse_file(path, recover=recover):
                if self._verify_integrity and event.sequence != expected:
                    raise StorageCorruptionError(
                        path,
                        line_number,
                        f"expected sequence {expected}, found {event.sequence}",
                    )
                if self._verify_integrity and event.event_id in self._event_ids:
                    raise StorageCorruptionError(
                        path, line_number, f"duplicate event id {event.event_id}"
                    )
                self._event_ids.add(event.event_id)
                self._last_month = _month_of(event)
                expected = event.sequence + 1
        self._last_sequence = expected - 1

    def _parse_file(
        self, path: Path, *, recover: bool
    ) -> list[tuple[int, JournalEvent]]:
        key = (int(path.parent.name), int(path.stem))
        data = path.read_bytes()
        *lines, tail = data.split(b"\n")
        if tail:
            if not recover:
                raise StorageCorruptionError(
                    path, len(lines) + 1, "final line is missing its newline"
                )
            self._quarantine_tail(path, key, tail, len(data) - len(tail))

        events: list[tuple[int, JournalEvent]] = []
        for line_number, line in enumerate(lines, start=1):
            try:
                event = decode_event(line, verify_checksum=self._verify_integrity)
            except ValueError as exc:
                raise StorageCorruptionError(
                    path, line_number, f"invalid journal event: {exc}"
                ) from exc
            if self._verify_integrity and _month_of(event) != key:
                raise StorageCorruptionError(
                    path,
                    line_number,
                    "event commit time does not match this file's month",
                )
            events.append((line_number, event))
        return events

    def _quarantine_tail(
        self, path: Path, key: _MonthKey, tail: bytes, keep: int
    ) -> None:
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = (
            self._quarantine_dir
            / f"journal-{key[0]:04d}-{key[1]:02d}-{uuid4().hex}.torn"
        )
        with open(target, "xb") as handle:
            handle.write(tail)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(self._quarantine_dir)
        with open(path, "r+b") as handle:
            handle.truncate(keep)
            handle.flush()
            os.fsync(handle.fileno())
        self.quarantined.append(target)
