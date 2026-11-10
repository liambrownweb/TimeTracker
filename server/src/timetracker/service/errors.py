"""Service-layer errors; the HTTP layer maps each to a status code."""

from __future__ import annotations

from uuid import UUID


class ServiceError(Exception):
    """Base class for coordinator failures."""


class EntryConflictError(ServiceError):
    """The UUID exists with different content (HTTP 409)."""

    def __init__(self, entry_id: UUID) -> None:
        super().__init__(f"Entry {entry_id} already exists with different content.")
        self.entry_id = entry_id


class EntryDeletedError(ServiceError):
    """The entry is logically deleted (HTTP 410)."""

    def __init__(self, entry_id: UUID) -> None:
        super().__init__(f"Entry {entry_id} has been deleted.")
        self.entry_id = entry_id


class StoreUnavailableError(ServiceError):
    """Operational state is out of sync with the journal (HTTP 503).

    The store is flagged for repair; the next request repairs it before being
    served. A mutation that raised this after its journal commit is committed
    and will not be lost.
    """


class RecoveryError(ServiceError):
    """The journal and operational state could not be reconciled."""
