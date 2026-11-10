"""Transaction coordination and recovery."""

from timetracker.service.coordinator import CreateResult, EntryCoordinator
from timetracker.service.errors import (
    EntryConflictError,
    EntryDeletedError,
    RecoveryError,
    ServiceError,
    StoreUnavailableError,
)
from timetracker.service.recovery import (
    Finding,
    FindingKind,
    RecoveryReport,
    VerificationOptions,
    open_entry_store,
    verify_and_repair,
)

__all__ = [
    "CreateResult",
    "EntryConflictError",
    "EntryCoordinator",
    "EntryDeletedError",
    "Finding",
    "FindingKind",
    "RecoveryError",
    "RecoveryReport",
    "ServiceError",
    "StoreUnavailableError",
    "VerificationOptions",
    "open_entry_store",
    "verify_and_repair",
]
