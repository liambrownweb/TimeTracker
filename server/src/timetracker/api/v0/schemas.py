"""Public v0 entry request and response schemas."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from timetracker.domain.entries import (
    AttributeDelta,
    DomainModel,
    EntryRecord,
    EntryUpdate,
    UtcDateTime,
)


class EntryCreateRequest(DomainModel):
    id: UUID
    task: str = Field(min_length=1)
    duration_seconds: int = Field(ge=0)
    started_at: UtcDateTime | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def task_must_not_be_whitespace(self) -> EntryCreateRequest:
        if not self.task.strip():
            raise ValueError("task must contain a non-whitespace character")
        return self


class AttributePatch(DomainModel):
    """Explicitly set attribute values or remove attribute keys."""

    set: dict[str, JsonValue] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def operations_must_be_unambiguous(self) -> AttributePatch:
        if len(self.remove) != len(set(self.remove)):
            raise ValueError("attribute keys to remove must be unique")
        if set(self.set).intersection(self.remove):
            raise ValueError("an attribute cannot be both set and removed")
        return self


class EntryPatchRequest(DomainModel):
    task: str | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    started_at: UtcDateTime | None = None
    attributes: AttributePatch | None = None

    @model_validator(mode="after")
    def supplied_fields_must_be_valid(self) -> EntryPatchRequest:
        for field_name in ("task", "duration_seconds", "attributes"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        if (
            "task" in self.model_fields_set
            and self.task is not None
            and not self.task.strip()
        ):
            raise ValueError("task must contain a non-whitespace character")
        return self

    def to_domain_update(self) -> EntryUpdate:
        values = self.model_dump(exclude_unset=True)
        if "attributes" in values:
            values["attributes"] = AttributeDelta.model_validate(values["attributes"])
        return EntryUpdate.model_validate(values)


class EntryResponse(DomainModel):
    id: UUID
    task: str
    duration_seconds: int = Field(ge=0)
    started_at: UtcDateTime | None
    attributes: dict[str, JsonValue]
    created_at: UtcDateTime
    updated_at: UtcDateTime
    revision: int = Field(ge=1)
    deleted: bool

    @classmethod
    def from_record(cls, record: EntryRecord) -> EntryResponse:
        return cls(
            id=record.id,
            task=record.task,
            duration_seconds=record.duration_seconds,
            started_at=record.started_at,
            attributes=record.attributes,
            created_at=record.created_at,
            updated_at=record.updated_at,
            revision=record.revision,
            deleted=record.deleted,
        )
