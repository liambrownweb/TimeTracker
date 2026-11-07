from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from timetracker.api.v0.schemas import (
    EntryCreateRequest,
    EntryPatchRequest,
    EntryResponse,
)
from timetracker.domain.entries import AttributeDelta, EntryRecord


ENTRY_ID = UUID("c4d15d1a-6e25-4e27-97c8-1a9020ed33d2")
RECORDED_AT = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)


def test_public_create_schema_accepts_extensible_attributes() -> None:
    request = EntryCreateRequest(
        id=ENTRY_ID,
        task="Capture without setup",
        duration_seconds=0,
        attributes={"new-location": "home", "billable": False},
    )

    assert request.attributes == {"new-location": "home", "billable": False}


def test_public_create_schema_excludes_server_managed_fields() -> None:
    with pytest.raises(ValidationError):
        EntryCreateRequest(
            id=ENTRY_ID,
            task="Client cannot assign revision",
            duration_seconds=1,
            revision=10,
        )


def test_patch_schema_distinguishes_setting_null_from_removing_attribute() -> None:
    patch = EntryPatchRequest(
        attributes={
            "set": {"nullable": None, "new-field": "value"},
            "remove": ["old-field"],
        }
    )
    domain_update = patch.to_domain_update()

    assert domain_update.attributes == AttributeDelta(
        set={"nullable": None, "new-field": "value"},
        remove=["old-field"],
    )


def test_patch_schema_preserves_omitted_fields_and_allows_clearing_started_at() -> None:
    patch = EntryPatchRequest(started_at=None)
    domain_update = patch.to_domain_update()

    assert domain_update.model_fields_set == {"started_at"}
    assert domain_update.started_at is None


@pytest.mark.parametrize(
    "patch",
    [
        {"task": None},
        {"task": "   "},
        {"duration_seconds": None},
        {"duration_seconds": -1},
        {"attributes": None},
        {"attributes": {"set": {"same": None}, "remove": ["same"]}},
        {"attributes": {"remove": ["same", "same"]}},
    ],
)
def test_invalid_patch_requests_are_rejected(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EntryPatchRequest.model_validate(patch)


def test_response_schema_excludes_operational_and_journal_internals() -> None:
    record = EntryRecord(
        id=ENTRY_ID,
        task="Public representation",
        duration_seconds=1,
        started_at=None,
        attributes={"location": "home"},
        created_at=RECORDED_AT,
        updated_at=RECORDED_AT,
        revision=1,
        deleted=False,
        last_event_sequence=1,
        history=[],
        schema_version=1,
    )

    payload = EntryResponse.from_record(record).model_dump(mode="json")

    assert payload["id"] == str(ENTRY_ID)
    assert payload["created_at"] == "2026-11-06T19:00:00Z"
    assert "history" not in payload
    assert "last_event_sequence" not in payload
    assert "schema_version" not in payload
