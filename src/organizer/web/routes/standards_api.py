from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from organizer.application.standards_models import StandardsApplicationError
from organizer.application.standards_read_models import StandardsReadProjection
from organizer.web.forms import FormDataError, read_urlencoded_form
from organizer.web.security import (
    WebSecurityError,
    is_authenticated,
    validate_csrf_token,
    validate_same_origin,
)


_ERRORS = {
    "standard_revision_mismatch": (409, "Standards changed. Reload the latest list."),
    "standard_conflict": (409, "This Standard conflicts with another active Standard."),
    "active_plan_conflict": (
        409,
        "Finish or restart the current organization before changing Standards.",
    ),
    "standards_storage_unavailable": (409, "Standards could not be loaded."),
    "unknown_standard": (404, "This Standard is no longer available."),
    "immutable_standard": (409, "This built-in Standard cannot be paused."),
}


def create_standards_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/standards", include_in_schema=False)
    async def list_standards(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        try:
            projection = request.app.state.standards.list_standards()
        except StandardsApplicationError as error:
            return _standards_error(error)
        except (OSError, TypeError, ValueError):
            return _error_response(
                "standards_storage_unavailable",
                "Standards could not be loaded.",
                409,
            )
        return _json_response(_projection_payload(projection))

    @router.post("/api/standards/state", include_in_schema=False)
    async def set_standard_state(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        expected = {"csrf_token", "expected_revision", "standard_id", "active"}
        try:
            form = await _validated_form(request, expected)
            if set(form) != expected:
                raise FormDataError("Standards state fields are invalid")
            result = request.app.state.standards.set_active(
                _required(form.get("standard_id")),
                active=_strict_boolean(form.get("active")),
                expected_revision=_revision(form.get("expected_revision")),
            )
        except StandardsApplicationError as error:
            return _standards_error(error)
        except WebSecurityError:
            return _error_response(
                "request_authentication_failed",
                "The Standards request could not be authenticated.",
                403,
            )
        except (FormDataError, OSError, TypeError, ValueError):
            return _error_response(
                "invalid_request",
                "The Standards request is invalid.",
                400,
            )
        return _json_response(
            {
                **_projection_payload(result.projection),
                "fresh_scan_required": result.fresh_scan_required,
            }
        )

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
    if not isinstance(value, str) or not value or len(value) > 256:
        raise FormDataError("required Standards field is unavailable")
    return value


def _strict_boolean(value: str | None) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise FormDataError("Standards state must be true or false")


def _revision(value: str | None) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise FormDataError("Standards revision is invalid")
    revision = int(value)
    if revision < 0 or revision > 2**63 - 1:
        raise FormDataError("Standards revision is invalid")
    return revision


def _projection_payload(projection: StandardsReadProjection) -> dict[str, object]:
    return {
        "revision": projection.revision,
        "items": [item.to_dict() for item in projection.items],
    }


def _standards_error(error: StandardsApplicationError) -> JSONResponse:
    mapped = _ERRORS.get(error.code)
    if mapped is None:
        return _error_response(
            "standards_storage_unavailable",
            "Standards could not be loaded.",
            409,
        )
    status, message = mapped
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
