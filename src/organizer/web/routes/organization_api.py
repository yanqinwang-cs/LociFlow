from __future__ import annotations

import json

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from organizer.application.organization_decision_service import (
    OrganizationDecisionError,
    organization_review_to_dict,
)
from organizer.application.organization_slice_service import (
    build_internal_organization_projection,
)
from organizer.web.forms import FormDataError, read_urlencoded_form
from organizer.web.scan_jobs import ScanJobSnapshot
from organizer.web.security import (
    WebSecurityError,
    is_authenticated,
    session_id_for_session,
    validate_csrf_token,
    validate_same_origin,
)


_DECISION_FIELDS = {
    "csrf_token",
    "scan_generation",
    "expected_revision",
    "kind",
    "change_id",
    "decision",
    "choice_id",
    "option_id",
    "copy_ids",
    "expected_change_ids",
}
_MAX_ID_COUNT = 500
_MAX_ID_LENGTH = 2048

_ERRORS = {
    "scan_generation_mismatch": (409, "The organization review belongs to an earlier scan."),
    "decision_revision_mismatch": (409, "Organization choices were updated elsewhere."),
    "review_session_not_found": (409, "The organization review session is unavailable."),
    "unknown_change_id": (404, "That organization change is unavailable."),
    "unknown_choice_id": (404, "That organization choice is unavailable."),
    "invalid_option": (400, "That organization option is invalid."),
    "recommendation_set_changed": (409, "The recommendation set changed."),
    "incomplete_decisions": (409, "Every organization choice must be completed first."),
    "protected_item_selected": (409, "A protected item cannot be selected."),
    "unsafe_destination": (409, "A destination is no longer available."),
    "overwrite_detected": (409, "A destination file now exists."),
    "source_changed": (409, "A file changed after it was checked."),
    "duplicate_group_changed": (409, "Duplicate copies changed after they were checked."),
    "approval_already_completed": (409, "This organization plan is already approved."),
}


def create_organization_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/organization/current", include_in_schema=False)
    async def current_organization(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        scan_or_response = _completed_scan(request)
        if isinstance(scan_or_response, Response):
            return scan_or_response
        try:
            projection = _projection(scan_or_response)
            current = request.app.state.organization_decisions.current(
                session_id_for_session(request.session),
                scan_or_response,
                projection,
            )
        except (OSError, TypeError, ValueError):
            return _error_response(
                "organization_unavailable",
                "The organization result is unavailable for this scan.",
                409,
            )
        return _current_response(current.projection, current.session)

    @router.post("/api/organization/decisions", include_in_schema=False)
    async def update_decision(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        scan_or_response = _completed_scan(request)
        if isinstance(scan_or_response, Response):
            return scan_or_response
        try:
            form = await _validated_mutation_form(request, _DECISION_FIELDS)
            revision = _revision(form.get("expected_revision"))
            generation = _required(form.get("scan_generation"))
            projection = _projection(scan_or_response)
            store = request.app.state.organization_decisions
            browser_session_id = session_id_for_session(request.session)
            store.validate_mutation(
                browser_session_id,
                scan_or_response,
                projection,
                scan_generation=generation,
                expected_revision=revision,
            )
            kind = _required(form.get("kind"))
            if kind == "recommendation":
                _require_exact_fields(
                    form,
                    {
                        "csrf_token",
                        "scan_generation",
                        "expected_revision",
                        "kind",
                        "change_id",
                        "decision",
                    },
                )
                current = store.update(
                    browser_session_id,
                    scan_or_response,
                    projection,
                    scan_generation=generation,
                    expected_revision=revision,
                    kind=kind,
                    change_id=_required(form.get("change_id")),
                    decision=_required(form.get("decision")),
                )
            elif kind == "choice":
                option_id = _required(form.get("option_id"))
                expected = {
                    "csrf_token",
                    "scan_generation",
                    "expected_revision",
                    "kind",
                    "choice_id",
                    "option_id",
                }
                copy_ids: tuple[str, ...] = ()
                if option_id == "choose-for-bin":
                    expected.add("copy_ids")
                    copy_ids = _id_array(form.get("copy_ids"))
                _require_exact_fields(form, expected)
                current = store.update(
                    browser_session_id,
                    scan_or_response,
                    projection,
                    scan_generation=generation,
                    expected_revision=revision,
                    kind=kind,
                    choice_id=_required(form.get("choice_id")),
                    option_id=option_id,
                    copy_ids=copy_ids,
                )
            elif kind == "bulk_recommendations":
                _require_exact_fields(
                    form,
                    {
                        "csrf_token",
                        "scan_generation",
                        "expected_revision",
                        "kind",
                        "decision",
                        "expected_change_ids",
                    },
                )
                if form.get("decision") != "accept":
                    raise OrganizationDecisionError("invalid_option")
                current = store.accept_all_recommended(
                    browser_session_id,
                    scan_or_response,
                    projection,
                    scan_generation=generation,
                    expected_revision=revision,
                    expected_change_ids=_id_array(form.get("expected_change_ids")),
                )
            else:
                raise OrganizationDecisionError("invalid_option")
        except OrganizationDecisionError as error:
            return _decision_error(error)
        except WebSecurityError:
            return _error_response(
                "request_authentication_failed",
                "The organization request could not be authenticated.",
                403,
            )
        except (FormDataError, json.JSONDecodeError, TypeError, ValueError):
            return _decision_error(OrganizationDecisionError("invalid_option"))
        return _current_response(current.projection, current.session)

    @router.post("/api/organization/approval", include_in_schema=False)
    async def approve_organization(request: Request) -> Response:
        common = _validate_common_request(request)
        if common is not None:
            return common
        scan_or_response = _completed_scan(request)
        if isinstance(scan_or_response, Response):
            return scan_or_response
        try:
            form = await _validated_mutation_form(
                request,
                {"csrf_token", "scan_generation", "expected_revision"},
            )
            _require_exact_fields(
                form,
                {"csrf_token", "scan_generation", "expected_revision"},
            )
            projection = _projection(scan_or_response)
            current = request.app.state.organization_decisions.approve(
                session_id_for_session(request.session),
                scan_or_response,
                projection,
                scan_generation=_required(form.get("scan_generation")),
                expected_revision=_revision(form.get("expected_revision")),
            )
        except OrganizationDecisionError as error:
            return _decision_error(error)
        except WebSecurityError:
            return _error_response(
                "request_authentication_failed",
                "The organization request could not be authenticated.",
                403,
            )
        except (FormDataError, TypeError, ValueError):
            return _decision_error(OrganizationDecisionError("invalid_option"))
        return _current_response(current.projection, current.session)

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


def _completed_scan(request: Request) -> ScanJobSnapshot | Response:
    scan = request.app.state.scan_jobs.snapshot()
    if scan.status == "scanning":
        return _error_response(
            "scan_in_progress",
            "The current scan is still in progress.",
            409,
        )
    if scan.status == "failed":
        return _error_response(
            "scan_failed",
            "The current scan did not complete successfully.",
            409,
        )
    if (
        scan.status != "completed"
        or scan.result is None
        or scan.job_id is None
        or scan.completed_at is None
    ):
        return _error_response(
            "scan_not_ready",
            "A completed scan is required.",
            409,
        )
    return scan


def _projection(scan: ScanJobSnapshot):
    assert scan.result is not None
    assert scan.job_id is not None
    assert scan.completed_at is not None
    return build_internal_organization_projection(
        scan_result=scan.result,
        scan_generation=scan.job_id,
        completed_at=scan.completed_at,
    )


async def _validated_mutation_form(
    request: Request,
    allowed_fields: set[str],
) -> dict[str, str]:
    form = await read_urlencoded_form(request, allowed_fields=allowed_fields)
    validate_same_origin(request)
    validate_csrf_token(request.session, form.get("csrf_token"))
    return form


def _require_exact_fields(form: dict[str, str], expected: set[str]) -> None:
    if set(form) != expected:
        raise OrganizationDecisionError("invalid_option")


def _required(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise OrganizationDecisionError("invalid_option")
    return value


def _revision(value: str | None) -> int:
    text = _required(value)
    if not text.isdigit():
        raise OrganizationDecisionError("invalid_option")
    return int(text)


def _id_array(value: str | None) -> tuple[str, ...]:
    data = json.loads(_required(value))
    if (
        not isinstance(data, list)
        or len(data) > _MAX_ID_COUNT
        or not all(
            isinstance(item, str) and 0 < len(item) <= _MAX_ID_LENGTH
            for item in data
        )
    ):
        raise OrganizationDecisionError("invalid_option")
    return tuple(data)


def _current_response(projection, session) -> JSONResponse:
    payload = projection.public_slice.to_dict()
    payload["review"] = organization_review_to_dict(session, projection)
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def _decision_error(error: OrganizationDecisionError) -> JSONResponse:
    status, message = _ERRORS.get(
        error.code,
        (400, "The organization request is invalid."),
    )
    return _error_response(error.code, message, status)


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    response = JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
