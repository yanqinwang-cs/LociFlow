from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from organizer.application.standards_models import StandardsApplicationError
from organizer.application.standards_read_models import StandardsReadProjection
from organizer.organization_rules import (
    ORGANIZATION_RULES_STATUS_UNAVAILABLE,
    STANDARD_KEEP_FOLDER_UNCHANGED,
    STANDARD_KEEP_MATCHING_FILES_TOGETHER,
)
from organizer.web.forms import FormDataError, read_urlencoded_form
from organizer.web.security import (
    WebSecurityError,
    is_authenticated,
    session_id_for_session,
    validate_csrf_token,
    validate_same_origin,
)
from organizer.web.scan_jobs import ScanJobSnapshot


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
    "wrong_root_scope": (409, "Check files again and choose the folder again."),
    "folder_scope_stale": (409, "Check files again and choose the folder again."),
    "preview_expired": (409, "This preview expired. Preview the Standard again."),
    "preview_superseded": (409, "A newer preview is available. Preview the Standard again."),
    "invalid_preset": (400, "Choose one of the available Standard types."),
    "invalid_matcher": (400, "Enter a valid course or project code."),
    "duplicate_standard": (409, "This Standard is already saved."),
}

_PRESETS = (
    {
        "id": STANDARD_KEEP_FOLDER_UNCHANGED,
        "name": "Keep a folder unchanged",
        "description": "Files in this folder stay where they are.",
    },
    {
        "id": STANDARD_KEEP_MATCHING_FILES_TOGETHER,
        "name": "Keep matching files together",
        "description": "Files with one course or project code stay grouped.",
    },
)

_NO_SCAN_MESSAGE = "Check files before adding a Standard."
_ACTIVE_PLAN_MESSAGE = (
    "Finish or restart the current organization before changing Standards."
)
_STORAGE_MESSAGE = "Standards could not be loaded."


def create_standards_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/standards", include_in_schema=False)
    async def list_standards(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        try:
            projection = request.app.state.standards.list_standards()
            creation = _creation_payload(request)
        except StandardsApplicationError as error:
            return _standards_error(error)
        except WebSecurityError:
            return _authentication_error()
        except (OSError, TypeError, ValueError):
            return _error_response(
                "standards_storage_unavailable",
                "Standards could not be loaded.",
                409,
            )
        return _json_response({**_projection_payload(projection), **creation})

    @router.post("/api/standards/preview", include_in_schema=False)
    async def preview_standard(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        allowed = {
            "csrf_token", "expected_revision", "preset_type", "scope_id",
            "matcher_kind", "matcher_value",
        }
        try:
            form = await _validated_form(request, allowed)
            preset_type = _required(form.get("preset_type"))
            expected = (
                {"csrf_token", "expected_revision", "preset_type", "scope_id"}
                if preset_type == STANDARD_KEEP_FOLDER_UNCHANGED
                else {
                    "csrf_token", "expected_revision", "preset_type", "scope_id",
                    "matcher_kind", "matcher_value",
                }
                if preset_type == STANDARD_KEEP_MATCHING_FILES_TOGETHER
                else None
            )
            if expected is None:
                raise StandardsApplicationError("invalid_preset")
            if set(form) != expected:
                raise FormDataError("Standards preview fields are invalid")
            scan = _current_scan(request)
            session_id = session_id_for_session(request.session)
            service = request.app.state.standards
            service.ensure_creation_available()
            root_identity = service.repository.configured_root_identity
            scope_kind, folder, scope_label = (
                request.app.state.standards_creation.resolve_scope(
                    scope_id=_required(form.get("scope_id")),
                    session_id=session_id,
                    root_identity=root_identity,
                    scan=scan,
                )
            )
            if scope_kind == "root":
                scope = service.resolve_root_scope(scan_generation=scan.job_id)
            else:
                if folder is None:
                    raise StandardsApplicationError("folder_scope_stale")
                scope = service.resolve_folder_scope(
                    folder,
                    scan_generation=scan.job_id,
                )
            preview = service.preview(
                preset_type=preset_type,
                scope=scope,
                expected_revision=_revision(form.get("expected_revision")),
                matcher_kind=form.get("matcher_kind"),
                matcher_value=form.get("matcher_value"),
                scan_result=scan.result,
                owner_key=session_id,
            )
            token = request.app.state.standards_creation.issue_preview_token(
                session_id=session_id,
                root_identity=root_identity,
                scan_generation=scan.job_id,
                expected_revision=preview.expected_revision,
                application_preview_id=preview.preview_id,
            )
        except StandardsApplicationError as error:
            return _standards_error(error)
        except WebSecurityError:
            return _authentication_error()
        except (FormDataError, OSError, TypeError, ValueError):
            return _invalid_request()
        return _json_response(
            {
                "preview_token": token,
                "expires_at": preview.expires_at,
                "preview": {
                    "name": preview.name,
                    "description": preview.description,
                    "scope_label": scope_label,
                    "effect_summary": preview.effect_summary,
                    "matching_files": preview.matching_file_count,
                    "fresh_scan_required": preview.fresh_scan_required,
                    "attention_message": _attention_message(preview.attention_code),
                },
            }
        )

    @router.post("/api/standards", include_in_schema=False)
    async def save_standard(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        expected_fields = {"csrf_token", "expected_revision", "preview_token"}
        try:
            form = await _validated_form(request, expected_fields)
            if set(form) != expected_fields:
                raise FormDataError("Standards save fields are invalid")
            expected_revision = _revision(form.get("expected_revision"))
            scan = _current_scan(request)
            session_id = session_id_for_session(request.session)
            service = request.app.state.standards
            application_preview_id = (
                request.app.state.standards_creation.resolve_preview_token(
                    token=_required(form.get("preview_token")),
                    session_id=session_id,
                    root_identity=service.repository.configured_root_identity,
                    scan=scan,
                    expected_revision=expected_revision,
                )
            )
            result = service.save_preview(
                application_preview_id,
                expected_revision=expected_revision,
                owner_key=session_id,
            )
        except StandardsApplicationError as error:
            return _standards_error(error)
        except WebSecurityError:
            return _authentication_error()
        except (FormDataError, OSError, TypeError, ValueError):
            return _invalid_request()
        return _json_response(
            {
                **_projection_payload(result.projection),
                "fresh_scan_required": result.fresh_scan_required,
                "save_message": (
                    "Standard saved."
                    if result.created
                    else "This Standard is already saved."
                ),
            }
        )

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


def _current_scan(request: Request) -> ScanJobSnapshot:
    scan = request.app.state.scan_jobs.snapshot()
    if scan.status != "completed" or scan.job_id is None or scan.result is None:
        raise StandardsApplicationError("folder_scope_stale")
    if (
        scan.result.organization_rules_load_result is not None
        and scan.result.organization_rules_load_result.status
        == ORGANIZATION_RULES_STATUS_UNAVAILABLE
    ):
        raise StandardsApplicationError("standards_storage_unavailable")
    return scan


def _creation_payload(request: Request) -> dict[str, object]:
    scan = request.app.state.scan_jobs.snapshot()
    if scan.status != "completed" or scan.job_id is None or scan.result is None:
        return {
            "presets": list(_PRESETS),
            "scope_choices": [],
            "creation_available": False,
            "creation_message": _NO_SCAN_MESSAGE,
        }
    if (
        scan.result.organization_rules_load_result is not None
        and scan.result.organization_rules_load_result.status
        == ORGANIZATION_RULES_STATUS_UNAVAILABLE
    ):
        return {
            "presets": list(_PRESETS),
            "scope_choices": [],
            "creation_available": False,
            "creation_message": _STORAGE_MESSAGE,
        }
    try:
        request.app.state.standards.ensure_creation_available()
    except StandardsApplicationError as error:
        if error.code == "active_plan_conflict":
            message = _ACTIVE_PLAN_MESSAGE
        elif error.code == "standards_storage_unavailable":
            message = _STORAGE_MESSAGE
        else:
            raise
        return {
            "presets": list(_PRESETS),
            "scope_choices": [],
            "creation_available": False,
            "creation_message": message,
        }
    service = request.app.state.standards
    choices = request.app.state.standards_creation.issue_scope_choices(
        session_id=session_id_for_session(request.session),
        root_identity=service.repository.configured_root_identity,
        scan=scan,
    )
    return {
        "presets": list(_PRESETS),
        "scope_choices": [choice.to_dict() for choice in choices],
        "creation_available": True,
        "creation_message": None,
    }


def _attention_message(code: str | None) -> str | None:
    if code == "duplicate_standard":
        return "This Standard is already saved."
    if code == "standard_conflict":
        return "This Standard conflicts with another active Standard."
    return None


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


def _authentication_error() -> JSONResponse:
    return _error_response(
        "request_authentication_failed",
        "The Standards request could not be authenticated.",
        403,
    )


def _invalid_request() -> JSONResponse:
    return _error_response(
        "invalid_request",
        "The Standards request is invalid.",
        400,
    )


def _json_response(payload: object, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return _json_response(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )
