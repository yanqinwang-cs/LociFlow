from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from organizer.application.activity_service import list_activity_entries
from organizer.application.undo_service import (
    OrganizationUndoError,
    preview_organization_undo,
    undo_organization_operation,
)
from organizer.web.forms import FormDataError, read_urlencoded_form
from organizer.web.security import (
    WebSecurityError,
    is_authenticated,
    session_id_for_session,
    validate_csrf_token,
    validate_same_origin,
)


_ERRORS = {
    "operation_not_found": (404, "That organization move is unavailable."),
    "operation_not_undoable": (409, "This organization move cannot be undone."),
    "operation_superseded": (409, "A later organization move superseded this one."),
    "undo_already_started": (409, "Undo has already started for this move."),
    "undo_already_completed": (409, "This organization move was already restored."),
    "undo_destination_occupied": (409, "A previous file location is occupied."),
    "undo_source_missing": (409, "A moved file is no longer available."),
    "undo_source_changed": (409, "A moved file changed after organization."),
    "unsafe_undo_path": (409, "The restore path is no longer safe."),
    "movement_or_undo_in_progress": (409, "Another file operation is in progress."),
    "undo_failed": (409, "Undo could not be prepared."),
}


def create_activity_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/activity", include_in_schema=False)
    async def activity(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        try:
            result = list_activity_entries(
                operation_repository=request.app.state.organization_operations,
                undo_repository=request.app.state.organization_undos,
            )
        except (OSError, TypeError, ValueError):
            return _error_response(
                "activity_unavailable",
                "Organization Activity is unavailable.",
                409,
            )
        return _json_response(result.to_dict())

    @router.post(
        "/api/activity/{operation_id}/undo/preview",
        include_in_schema=False,
    )
    async def preview_undo(operation_id: str, request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        try:
            form = await _validated_form(request, {"csrf_token"})
            if set(form) != {"csrf_token"}:
                raise FormDataError("undo preview fields are invalid")
            preview = preview_organization_undo(
                operation_id=operation_id,
                browser_session_id=session_id_for_session(request.session),
                coordinator=request.app.state.organization_execution,
                operation_repository=request.app.state.organization_operations,
                undo_repository=request.app.state.organization_undos,
            )
        except OrganizationUndoError as error:
            return _undo_error(error)
        except WebSecurityError:
            return _error_response(
                "request_authentication_failed",
                "The Activity request could not be authenticated.",
                403,
            )
        except (FormDataError, TypeError, ValueError):
            return _error_response("invalid_request", "The Undo request is invalid.", 400)
        return _json_response(preview.to_dict())

    @router.post("/api/activity/{operation_id}/undo", include_in_schema=False)
    async def execute_undo(operation_id: str, request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        try:
            form = await _validated_form(
                request,
                {"csrf_token", "confirmation_token", "confirmation"},
            )
            if set(form) != {"csrf_token", "confirmation_token", "confirmation"}:
                raise FormDataError("undo fields are invalid")
            outcome = undo_organization_operation(
                operation_id=operation_id,
                browser_session_id=session_id_for_session(request.session),
                confirmation_token=_required(form.get("confirmation_token")),
                confirmation=_required(form.get("confirmation")),
                coordinator=request.app.state.organization_execution,
                operation_repository=request.app.state.organization_operations,
                undo_repository=request.app.state.organization_undos,
            )
            activity_list = list_activity_entries(
                operation_repository=request.app.state.organization_operations,
                undo_repository=request.app.state.organization_undos,
            )
            entry = next(
                item for item in activity_list.activity if item.id == outcome.undo_id
            )
        except OrganizationUndoError as error:
            return _undo_error(error)
        except WebSecurityError:
            return _error_response(
                "request_authentication_failed",
                "The Activity request could not be authenticated.",
                403,
            )
        except (FormDataError, StopIteration, TypeError, ValueError):
            return _error_response("invalid_request", "The Undo request is invalid.", 400)
        return _json_response({"undo": outcome.to_dict(), "activity": entry.to_dict()})

    return router


def _validate_common_request(request: Request) -> Response | None:
    if not is_authenticated(request.session):
        return _error_response(
            "authentication_required",
            "An authenticated bootAI session is required.",
            403,
        )
    if request.query_params:
        return _error_response(
            "invalid_query",
            "This endpoint does not accept query parameters.",
            400,
        )
    return None


async def _validated_form(
    request: Request,
    allowed_fields: set[str],
) -> dict[str, str]:
    form = await read_urlencoded_form(request, allowed_fields=allowed_fields)
    validate_same_origin(request)
    validate_csrf_token(request.session, form.get("csrf_token"))
    return form


def _required(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise FormDataError("required Undo field is unavailable")
    return value


def _undo_error(error: OrganizationUndoError) -> JSONResponse:
    status, message = _ERRORS.get(
        error.code,
        (400, "The Undo request is invalid."),
    )
    return _error_response(error.code, message, status)


def _json_response(payload: object, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return _json_response(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )
