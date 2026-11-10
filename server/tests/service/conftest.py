from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from timetracker.domain.entries import EntryCreate, EntryRecord, JournalEvent
from timetracker.service import EntryCoordinator, VerificationOptions
from timetracker.storage import JsonlEntryStore, JsonlJournal

START = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    """Advances one second per reading unless a time is forced."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        value = self.now
        self.now = value + timedelta(seconds=1)
        return value


def make_create(
    *,
    entry_id: UUID | None = None,
    task: str = "Task",
    duration_seconds: int = 60,
    started_at: datetime | None = None,
    attributes: dict[str, object] | None = None,
) -> EntryCreate:
    return EntryCreate.model_validate(
        {
            "id": entry_id or uuid4(),
            "task": task,
            "duration_seconds": duration_seconds,
            "started_at": started_at,
            "attributes": attributes or {},
        }
    )


@dataclass
class World:
    """A data directory with its journal, entry store and coordinator."""

    root: Path
    clock: FakeClock = field(default_factory=FakeClock)

    def journal(self) -> JsonlJournal:
        return JsonlJournal(self.root)

    def store(self) -> JsonlEntryStore:
        return JsonlEntryStore(self.root)

    def coordinator(
        self, options: VerificationOptions = VerificationOptions()
    ) -> EntryCoordinator:
        return EntryCoordinator(
            self.store(), self.journal(), options=options, clock=self.clock
        )

    def events(self) -> list[JournalEvent]:
        return self.journal().read()

    def records(self) -> list[EntryRecord]:
        return self.store().list(include_deleted=True)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)
