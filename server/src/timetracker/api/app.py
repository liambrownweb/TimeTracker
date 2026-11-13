"""HTTP API and managed storage lifetime."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from timetracker.api.v0.schemas import EntryCreateRequest, EntryPatchRequest, EntryResponse
from timetracker.api.v0.discovery import (
    CompletionResponse,
    FieldInfo,
    completions,
    discover_fields,
)
from timetracker.config import Settings
from timetracker.domain.entries import EntryCreate
from timetracker.service.coordinator import EntryCoordinator
from timetracker.service.errors import EntryConflictError, EntryDeletedError, StoreUnavailableError
from timetracker.storage import EntryNotFoundError, StorageError
from timetracker.verification import open_server

log = logging.getLogger(__name__)
_attributes = TypeAdapter(dict[str, JsonValue])


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: str


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def _coordinator(request: Request) -> EntryCoordinator:
    return request.app.state.coordinator


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _parse_attributes(value: str | None) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value, parse_constant=_reject_constant)
        return _attributes.validate_python(parsed, strict=True)
    except (ValueError, ValidationError) as exc:
        raise RequestValidationError([{"loc": ("query", "attributes"), "msg": str(exc), "type": "value_error"}]) from exc


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        with open_server(settings) as coordinator:
            app.state.coordinator = coordinator
            yield

    app = FastAPI(title="TimeTracker", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(EntryNotFoundError)
    async def not_found(request: Request, exc: EntryNotFoundError) -> JSONResponse:
        return _error("ENTRY_NOT_FOUND", "No entry exists with the specified ID.", 404)

    @app.exception_handler(EntryDeletedError)
    async def deleted(request: Request, exc: EntryDeletedError) -> JSONResponse:
        return _error("ENTRY_DELETED", "The entry has been deleted.", 410)

    @app.exception_handler(EntryConflictError)
    async def conflict(request: Request, exc: EntryConflictError) -> JSONResponse:
        return _error("ENTRY_CONFLICT", "The entry ID already exists with different content.", 409)

    @app.exception_handler(StoreUnavailableError)
    async def unavailable(request: Request, exc: StoreUnavailableError) -> JSONResponse:
        log.error("Operational storage unavailable: %s", exc, exc_info=exc)
        return _error("STORE_UNAVAILABLE", "The store is unavailable; retry the request.", 503)

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError) -> JSONResponse:
        log.error("Storage failure: %s", exc, exc_info=exc)
        return _error("STORE_UNAVAILABLE", "The store is unavailable; retry the request.", 503)

    @app.exception_handler(OSError)
    async def io_error(request: Request, exc: OSError) -> JSONResponse:
        log.error("Storage I/O failure: %s", exc, exc_info=exc)
        return _error("STORE_UNAVAILABLE", "The store is unavailable; retry the request.", 503)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error("INVALID_REQUEST", "The request contains invalid data.", 422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = {
            404: ("NOT_FOUND", "The requested resource does not exist."),
            405: ("METHOD_NOT_ALLOWED", "The method is not allowed for this resource."),
        }.get(exc.status_code, ("HTTP_ERROR", "The request could not be completed."))
        response = _error(code, message, exc.status_code)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    common: dict[int | str, dict[str, object]] = {
        422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}
    }
    missing: dict[int | str, dict[str, object]] = {
        404: {"model": ErrorResponse}, 410: {"model": ErrorResponse}
    }

    @app.post("/v0/entries", response_model=EntryResponse, status_code=201,
              responses={200: {"model": EntryResponse}, 409: {"model": ErrorResponse}, **common})
    def create_entry(body: EntryCreateRequest, response: Response,
                     coordinator: Annotated[EntryCoordinator, Depends(_coordinator)]) -> EntryResponse:
        result = coordinator.create(EntryCreate.model_validate(body.model_dump()))
        if result.created:
            response.headers["Location"] = f"/v0/entries/{result.record.id}"
        else:
            response.status_code = status.HTTP_200_OK
        return EntryResponse.from_record(result.record)

    @app.get("/v0/entries", response_model=list[EntryResponse], responses=common)
    def list_entries(
        coordinator: Annotated[EntryCoordinator, Depends(_coordinator)],
        task: str | None = None,
        attributes: Annotated[str | None, Query(description="JSON object of exact, type-strict attribute filters")] = None,
    ) -> list[EntryResponse]:
        return [EntryResponse.from_record(record) for record in
                coordinator.list(task=task, attributes=_parse_attributes(attributes))]

    @app.get("/v0/entries/{entry_id}", response_model=EntryResponse, responses={**missing, **common})
    def get_entry(entry_id: UUID, coordinator: Annotated[EntryCoordinator, Depends(_coordinator)]) -> EntryResponse:
        return EntryResponse.from_record(coordinator.get(entry_id))

    @app.patch("/v0/entries/{entry_id}", response_model=EntryResponse, responses={**missing, **common})
    def patch_entry(entry_id: UUID, body: EntryPatchRequest,
                    coordinator: Annotated[EntryCoordinator, Depends(_coordinator)]) -> EntryResponse:
        return EntryResponse.from_record(coordinator.update(entry_id, body.to_domain_update()))

    @app.delete("/v0/entries/{entry_id}", status_code=204, responses={404: {"model": ErrorResponse}, **common})
    def delete_entry(entry_id: UUID, coordinator: Annotated[EntryCoordinator, Depends(_coordinator)]) -> Response:
        coordinator.delete(entry_id)
        return Response(status_code=204)

    @app.get("/v0/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/v0/fields", response_model=list[FieldInfo], responses=common)
    def fields(
        coordinator: Annotated[EntryCoordinator, Depends(_coordinator)],
    ) -> list[FieldInfo]:
        return discover_fields(coordinator.list())

    @app.get("/v0/completions", response_model=CompletionResponse, responses=common)
    def field_completions(
        coordinator: Annotated[EntryCoordinator, Depends(_coordinator)],
        field: Annotated[str, Query(min_length=1, max_length=128)],
        q: Annotated[str, Query(max_length=128)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> CompletionResponse:
        return completions(
            coordinator.list(),
            field=field,
            query=q,
            limit=limit,
        )

    return app
