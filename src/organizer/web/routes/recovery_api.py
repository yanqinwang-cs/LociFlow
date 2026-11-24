from __future__ import annotations

import uuid

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from organizer.application.recovery_check_service import RecoveryCheckError
from organizer.application.runtime_lifecycle import ApplicationStoppingError
from organizer.web.forms import FormDataError, read_urlencoded_form
from organizer.web.security import (
    WebSecurityError,
    is_authenticated,
    session_id_for_session,
    validate_csrf_token,
    validate_same_origin,
)


def create_recovery_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/recovery/current", include_in_schema=False)
    async def current_recovery(request: Request) -> Response:
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
        try:
            projection = request.app.state.recovery_projection_service.current()
        except Exception:
            return _error_response(
                "recovery_unavailable",
                "The unfinished file work could not be reviewed safely.",
                503,
            )
        return _json_response(projection.to_dict())

    @router.post("/api/recovery/check-context", include_in_schema=False)
    async def create_check_context(request: Request) -> Response:
        common = _validate_mutation_request(request)
        if common is not None:
            return common
        try:
            form = await _validated_form(request, {"csrf_token"})
            if set(form) != {"csrf_token"}:
                raise FormDataError("recovery Check context fields are invalid")
            result = request.app.state.recovery_check_service.create_context(
                browser_session_id=session_id_for_session(request.session)
            )
        except ApplicationStoppingError:
            return _error_response(
                "application_stopping",
                "Loci Flow is closing. No new work can start.",
                409,
            )
        except RecoveryCheckError as error:
            return _recovery_check_error(error)
        except WebSecurityError:
            return _error_response(
                "request_authentication_failed",
                "The recovery request could not be authenticated.",
                403,
            )
        except (FormDataError, TypeError, ValueError):
            return _error_response(
                "invalid_request",
                "The recovery request is invalid.",
                400,
            )
        return _json_response(result.to_dict())

    @router.post("/api/recovery/check-result", include_in_schema=False)
    async def check_result(request: Request) -> Response:
        common = _validate_mutation_request(request)
        if common is not None:
            return common
        try:
            expected = {"csrf_token", "case_id", "action_id"}
            form = await _validated_form(request, expected)
            if set(form) != expected:
                raise FormDataError("recovery Check result fields are invalid")
            result = request.app.state.recovery_check_service.check_result(
                browser_session_id=session_id_for_session(request.session),
                case_id=_opaque_id(form.get("case_id")),
                action_id=_opaque_id(form.get("action_id")),
            )
        except ApplicationStoppingError:
            return _error_response(
                "application_stopping",
                "Loci Flow is closing. No new work can start.",
                409,
            )
        except RecoveryCheckError as error:
            return _recovery_check_error(error)
        except WebSecurityError:
            return _error_response(
                "request_authentication_failed",
                "The recovery request could not be authenticated.",
                403,
            )
        except (FormDataError, TypeError, ValueError):
            return _error_response(
                "invalid_request",
                "The recovery request is invalid.",
                400,
            )
        return _json_response(result.to_dict())

    return router


def _validate_mutation_request(request: Request) -> Response | None:
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
    validate_same_origin(request)
    form = await read_urlencoded_form(
        request,
        allowed_fields=allowed_fields,
    )
    validate_csrf_token(request.session, form.get("csrf_token"))
    return form


def _opaque_id(value: str | None) -> str:
    if not isinstance(value, str):
        raise FormDataError("recovery Check ID is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise FormDataError("recovery Check ID is invalid") from error
    if str(parsed) != value:
        raise FormDataError("recovery Check ID is invalid")
    return value


def _recovery_check_error(error: RecoveryCheckError) -> JSONResponse:
    code, message = {
        "check_unavailable": (
            "recovery_check_unavailable",
            "Loci Flow cannot check this result right now.",
        ),
        "context_unavailable": (
            "recovery_context_unavailable",
            "The recovery check is no longer available. Open the review again.",
        ),
        "recovery_details_changed": (
            "recovery_details_changed",
            "The recovery details changed. Open the review again.",
        ),
    }[error.code]
    return _error_response(code, message, 409)


def _json_response(payload: object, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return _json_response(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )
