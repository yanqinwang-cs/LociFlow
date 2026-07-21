from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from organizer.web.security import is_authenticated


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

    return router


def _json_response(payload: object, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return _json_response(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )
