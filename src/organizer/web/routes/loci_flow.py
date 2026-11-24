from __future__ import annotations

from fastapi import APIRouter
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from organizer.web.security import csrf_token_for_session, is_authenticated


_SESSION_UNAVAILABLE = (
    "This bootAI session is not available. "
    "Launch bootAI again from the local application."
)


def create_loci_flow_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/loci-flow", include_in_schema=False)
    async def loci_flow(request: Request) -> Response:
        if not is_authenticated(request.session):
            return _session_error(request, templates)
        if request.query_params:
            raise HTTPException(
                status_code=400,
                detail="Loci Flow request unavailable",
            )
        response = templates.TemplateResponse(
            request=request,
            name="loci_flow.html",
            context={
                "csrf_token": csrf_token_for_session(request.session),
                "recovery_attention": (
                    "true"
                    if request.app.state.current_recovery_admission_status
                    .snapshot()
                    .is_blocked
                    else "false"
                ),
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


def _session_error(
    request: Request,
    templates: Jinja2Templates,
) -> Response:
    response = templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "page_title": "Session unavailable",
            "heading": "Session unavailable",
            "message": _SESSION_UNAVAILABLE,
        },
        status_code=403,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
