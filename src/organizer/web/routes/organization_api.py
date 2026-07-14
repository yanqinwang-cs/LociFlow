from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from organizer.application.organization_slice_service import (
    build_read_only_organization_slice,
)
from organizer.web.security import is_authenticated


def create_organization_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/organization/current", include_in_schema=False)
    async def current_organization(request: Request) -> Response:
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

        try:
            organization_slice = build_read_only_organization_slice(
                scan_result=scan.result,
                scan_generation=scan.job_id,
                completed_at=scan.completed_at,
            )
        except (TypeError, ValueError):
            return _error_response(
                "organization_unavailable",
                "The organization result is unavailable for this scan.",
                409,
            )
        response = JSONResponse(organization_slice.to_dict())
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    response = JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
