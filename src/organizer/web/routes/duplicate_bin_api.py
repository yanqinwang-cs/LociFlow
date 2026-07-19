from __future__ import annotations

import json
import uuid

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from organizer.application.duplicate_bin_movement_service import (
    DuplicateBinMovementError,
    execute_duplicate_bin_movement,
    preview_duplicate_bin_movement,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationBlockedError,
)
from organizer.application.duplicate_bin_restore_service import (
    DuplicateBinRestoreError,
    execute_duplicate_bin_restore,
    list_duplicate_bin_items,
    preview_duplicate_bin_restore,
)
from organizer.application.runtime_lifecycle import ApplicationStoppingError
from organizer.web.forms import FormDataError, read_urlencoded_form
from organizer.web.security import (
    WebSecurityError,
    is_authenticated,
    session_id_for_session,
    validate_csrf_token,
    validate_same_origin,
)


_ERRORS = {
    "duplicate_batch_not_found": (404, "That Duplicate Bin selection is unavailable."),
    "duplicate_batch_not_approved": (409, "That Duplicate Bin selection is not ready."),
    "duplicate_batch_stale": (409, "The approved duplicate copies changed."),
    "invalid_confirmation": (400, "Duplicate Bin confirmation is invalid."),
    "invalid_confirmation_token": (409, "Duplicate Bin confirmation is unavailable."),
    "confirmation_expired": (409, "Duplicate Bin confirmation expired."),
    "duplicate_bin_movement_already_started": (
        409,
        "Duplicate Bin movement already started.",
    ),
    "movement_or_undo_in_progress": (409, "Another file action is in progress."),
    "selected_copy_changed": (409, "A selected copy changed after approval."),
    "retained_copy_changed": (409, "A retained copy changed after approval."),
    "retained_copy_missing": (409, "A retained matching copy is unavailable."),
    "duplicate_member_unsafe": (409, "An approved duplicate copy is no longer safe."),
    "duplicate_member_changed_during_hash": (
        409,
        "A duplicate copy changed while being checked.",
    ),
    "unsafe_duplicate_bin_destination": (
        409,
        "A Duplicate Bin destination is unavailable.",
    ),
    "duplicate_bin_recovery_required": (409, "Duplicate Bin movement needs recovery."),
}

_RESTORE_ERRORS = {
    "no_restorable_items": (400, "Select at least one restorable copy."),
    "bin_item_not_found": (404, "That Duplicate Bin copy is unavailable."),
    "bin_item_not_restorable": (409, "That Duplicate Bin copy cannot be restored."),
    "bin_item_already_owned": (409, "That copy already has a restore operation."),
    "bin_item_missing": (409, "A Duplicate Bin copy is missing."),
    "bin_item_changed": (409, "A Duplicate Bin copy changed."),
    "restore_destination_occupied": (409, "A previous file location is occupied."),
    "unsafe_restore_destination": (409, "A previous file location is unsafe."),
    "unsafe_restore_parent": (409, "A previous folder location is unsafe."),
    "invalid_confirmation_token": (409, "Restore confirmation is unavailable."),
    "invalid_restore_confirmation": (400, "Restore confirmation is invalid."),
    "confirmation_expired": (409, "Restore confirmation expired."),
    "movement_or_undo_in_progress": (409, "Another file action is in progress."),
    "restore_authorization_stale": (409, "The selected copies changed."),
    "restore_recovery_required": (409, "Duplicate Bin restore needs recovery."),
}


def create_duplicate_bin_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/duplicate-bin", include_in_schema=False)
    async def duplicate_bin(request: Request) -> Response:
        common = _common(request)
        if common is not None:
            return common
        try:
            items = list_duplicate_bin_items(
                request.app.state.duplicate_bin_batches,
                request.app.state.duplicate_bin_operations,
                request.app.state.duplicate_bin_restores,
            )
        except (OSError, TypeError, ValueError):
            return _error(
                "duplicate_bin_unavailable", "Duplicate Bin is unavailable.", 409
            )
        return _json({"items": [item.to_dict() for item in items]})

    @router.post("/api/duplicate-bin/move/preview", include_in_schema=False)
    async def preview_move(request: Request) -> Response:
        common = _common(request)
        if common is not None:
            return common
        try:
            form = await _form(request, {"csrf_token", "batch_id"})
            if set(form) != {"csrf_token", "batch_id"}:
                raise FormDataError("Duplicate Bin preview fields are invalid")
            result = preview_duplicate_bin_movement(
                batch_id=_server_id(form.get("batch_id")),
                browser_session_id=session_id_for_session(request.session),
                batch_repository=request.app.state.duplicate_bin_batches,
                authorization_repository=request.app.state.duplicate_bin_authorizations,
                operation_repository=request.app.state.duplicate_bin_operations,
                coordinator=request.app.state.organization_execution,
            )
        except DuplicateBinMovementError as error:
            return _movement_error(error)
        except ApplicationStoppingError as error:
            return _error("application_stopping", str(error), 409)
        except WebSecurityError:
            return _error(
                "request_authentication_failed",
                "The request could not be authenticated.",
                403,
            )
        except (FormDataError, TypeError, ValueError):
            return _error(
                "invalid_request", "The Duplicate Bin request is invalid.", 400
            )
        return _json(result.to_dict())

    @router.post("/api/duplicate-bin/move", include_in_schema=False)
    async def execute_move(request: Request) -> Response:
        common = _common(request)
        if common is not None:
            return common
        expected = {"csrf_token", "confirmation_token", "confirmation"}
        try:
            form = await _form(request, expected)
            if set(form) != expected:
                raise FormDataError("Duplicate Bin movement fields are invalid")
            result = execute_duplicate_bin_movement(
                browser_session_id=session_id_for_session(request.session),
                confirmation_token=_server_id(form.get("confirmation_token")),
                confirmation=_required(form.get("confirmation")),
                batch_repository=request.app.state.duplicate_bin_batches,
                authorization_repository=request.app.state.duplicate_bin_authorizations,
                operation_repository=request.app.state.duplicate_bin_operations,
                coordinator=request.app.state.organization_execution,
                filesystem_operation_admission=(
                    request.app.state.filesystem_operation_admission
                ),
            )
        except FilesystemOperationBlockedError as error:
            return _error(error.code, str(error), 409)
        except DuplicateBinMovementError as error:
            return _movement_error(error)
        except ApplicationStoppingError as error:
            return _error("application_stopping", str(error), 409)
        except WebSecurityError:
            return _error(
                "request_authentication_failed",
                "The request could not be authenticated.",
                403,
            )
        except (FormDataError, TypeError, ValueError):
            return _error(
                "invalid_request", "The Duplicate Bin request is invalid.", 400
            )
        return _json({"operation": result.to_dict()})

    @router.post("/api/duplicate-bin/restore/preview", include_in_schema=False)
    async def preview_restore(request: Request) -> Response:
        common = _common(request)
        if common is not None:
            return common
        try:
            form = await _form(request, {"csrf_token", "item_ids"})
            if set(form) != {"csrf_token", "item_ids"}:
                raise FormDataError("Duplicate Bin restore preview fields are invalid")
            result = preview_duplicate_bin_restore(
                item_ids=_item_ids(form.get("item_ids")),
                browser_session_id=session_id_for_session(request.session),
                batch_repository=request.app.state.duplicate_bin_batches,
                movement_repository=request.app.state.duplicate_bin_operations,
                restore_repository=request.app.state.duplicate_bin_restores,
                coordinator=request.app.state.organization_execution,
            )
        except DuplicateBinRestoreError as error:
            return _restore_error(error)
        except ApplicationStoppingError as error:
            return _error("application_stopping", str(error), 409)
        except WebSecurityError:
            return _error(
                "request_authentication_failed",
                "The request could not be authenticated.",
                403,
            )
        except (FormDataError, TypeError, ValueError):
            return _error(
                "invalid_request", "The Duplicate Bin restore request is invalid.", 400
            )
        return _json(result.to_dict())

    @router.post("/api/duplicate-bin/restore", include_in_schema=False)
    async def execute_restore(request: Request) -> Response:
        common = _common(request)
        if common is not None:
            return common
        expected = {"csrf_token", "confirmation_token", "confirmation"}
        try:
            form = await _form(request, expected)
            if set(form) != expected:
                raise FormDataError("Duplicate Bin restore fields are invalid")
            result = execute_duplicate_bin_restore(
                browser_session_id=session_id_for_session(request.session),
                confirmation_token=_server_id(form.get("confirmation_token")),
                confirmation=_required(form.get("confirmation")),
                batch_repository=request.app.state.duplicate_bin_batches,
                movement_repository=request.app.state.duplicate_bin_operations,
                restore_repository=request.app.state.duplicate_bin_restores,
                coordinator=request.app.state.organization_execution,
                filesystem_operation_admission=(
                    request.app.state.filesystem_operation_admission
                ),
            )
        except FilesystemOperationBlockedError as error:
            return _error(error.code, str(error), 409)
        except DuplicateBinRestoreError as error:
            return _restore_error(error)
        except ApplicationStoppingError as error:
            return _error("application_stopping", str(error), 409)
        except WebSecurityError:
            return _error(
                "request_authentication_failed",
                "The request could not be authenticated.",
                403,
            )
        except (FormDataError, TypeError, ValueError):
            return _error(
                "invalid_request", "The Duplicate Bin restore request is invalid.", 400
            )
        return _json({"restore": result.to_dict()})

    return router


def _common(request: Request) -> Response | None:
    if not is_authenticated(request.session):
        return _error(
            "authentication_required",
            "An authenticated bootAI session is required.",
            403,
        )
    if request.query_params:
        return _error(
            "invalid_query", "This endpoint does not accept query parameters.", 400
        )
    return None


async def _form(request: Request, allowed: set[str]) -> dict[str, str]:
    validate_same_origin(request)
    form = await read_urlencoded_form(request, allowed_fields=allowed)
    validate_csrf_token(request.session, form.get("csrf_token"))
    return form


def _server_id(value: str | None) -> str:
    text = _required(value)
    parsed = uuid.UUID(text)
    if str(parsed) != text:
        raise FormDataError("server ID is invalid")
    return text


def _required(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise FormDataError("required field is missing")
    return value


def _item_ids(value: str | None) -> tuple[str, ...]:
    text = _required(value)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise FormDataError("item_ids must be JSON") from error
    if not isinstance(data, list) or not data:
        raise FormDataError("item_ids must be a non-empty array")
    result = tuple(_server_id(item) for item in data)
    if len(set(result)) != len(result):
        raise FormDataError("item_ids must be unique")
    return result


def _movement_error(error: DuplicateBinMovementError) -> JSONResponse:
    status, message = _ERRORS.get(
        error.code, (400, "The Duplicate Bin request is invalid.")
    )
    return _error(error.code, message, status)


def _restore_error(error: DuplicateBinRestoreError) -> JSONResponse:
    status, message = _RESTORE_ERRORS.get(
        error.code, (409, "Duplicate Bin restore is unavailable.")
    )
    return _error(error.code, message, status)


def _json(payload: object, status: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(code: str, message: str, status: int) -> JSONResponse:
    return _json({"error": {"code": code, "message": message}}, status)
