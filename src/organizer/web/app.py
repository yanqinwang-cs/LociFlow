from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from organizer.web.config import WebAppConfig
from organizer.web.routes.home import create_home_router
from organizer.web.routes.activity_api import create_activity_api_router
from organizer.web.routes.consumer import create_consumer_router
from organizer.web.routes.loci_flow import create_loci_flow_router
from organizer.web.routes.organization_api import create_organization_api_router
from organizer.web.routes.recovery_api import create_recovery_api_router
from organizer.web.routes.review import create_review_router
from organizer.web.routes.standards_api import create_standards_api_router
from organizer.web.review_explorer import ReviewExplorerStore
from organizer.web.organization_decision_store import OrganizationDecisionStore
from organizer.web.scan_jobs import ScanJobController
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.current_recovery_admission_status import (
    CurrentRecoveryAdmissionStatus,
)
from organizer.application.identity_verification_publication import (
    OrganizationIdentityVerificationPublicationService,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationInspector,
)
from organizer.application.recovery_evidence import RecoveryEvidenceInspector
from organizer.application.recovery_check_context import (
    RecoveryCheckContextStore,
)
from organizer.application.recovery_check_service import (
    RecoveryCheckEligibilityInspector,
    RecoveryCheckService,
)
from organizer.application.recovery_projection_service import RecoveryProjectionService
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.duplicate_bin_authorization_repository import (
    DuplicateBinAuthorizationRepository,
)
from organizer.application.duplicate_bin_operation_repository import (
    DuplicateBinOperationRepository,
)
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
)
from organizer.web.routes.duplicate_bin_api import create_duplicate_bin_api_router
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_identity_execution import (
    OrganizationIdentityExecutionService,
)
from organizer.application.organization_undo_identity_authorization import (
    V2UndoAuthorizationStore,
)
from organizer.application.organization_undo_identity_orchestration import (
    OrganizationIdentityUndoOrchestrationService,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.undo_identity_verification_publication import (
    OrganizationUndoIdentityVerificationPublicationService,
)
from organizer.application.runtime_lifecycle import RuntimeLifecycle
from organizer.application.standards_creation_application_service import (
    StandardsCreationApplicationService,
)
from organizer.application.standards_repository import StandardsRepository
from organizer.application.standards_service import StandardsService
from organizer.web.standards_creation_store import StandardsCreationStore
from organizer.web.security import (
    LaunchTokenGate,
    SecurityHeadersMiddleware,
    apply_security_headers,
)

_PACKAGE_DIRECTORY = Path(__file__).resolve().parent
_TEMPLATE_DIRECTORY = _PACKAGE_DIRECTORY / "templates"
_STATIC_DIRECTORY = _PACKAGE_DIRECTORY / "static"


def create_app(config: WebAppConfig) -> FastAPI:
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIRECTORY))
    middleware = [
        Middleware(SecurityHeadersMiddleware),
        Middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(config.allowed_hosts),
            www_redirect=False,
        ),
        Middleware(
            SessionMiddleware,
            secret_key=config.session_secret,
            session_cookie="bootai_session",
            max_age=None,
            path="/",
            same_site="strict",
            https_only=False,
        ),
    ]
    app = FastAPI(
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        middleware=middleware,
    )
    app.state.web_config = config
    app.state.templates = templates
    app.state.launch_token_gate = LaunchTokenGate(config.launch_token)
    app.state.runtime_lifecycle = RuntimeLifecycle()
    app.state.scan_jobs = ScanJobController(
        config.root,
        runtime_lifecycle=app.state.runtime_lifecycle,
    )
    app.state.review_explorer = ReviewExplorerStore(
        config.root,
        runtime_lifecycle=app.state.runtime_lifecycle,
    )
    app.state.duplicate_bin_batches = DuplicateBinRepository(config.root)
    app.state.duplicate_bin_authorizations = DuplicateBinAuthorizationRepository(
        config.root
    )
    app.state.duplicate_bin_operations = DuplicateBinOperationRepository(config.root)
    app.state.duplicate_bin_restores = DuplicateBinRestoreRepository(config.root)
    app.state.organization_execution = OrganizationExecutionCoordinator(
        app.state.runtime_lifecycle
    )
    app.state.organization_operations = OrganizationOperationRepository(config.root)
    app.state.organization_undos = OrganizationUndoRepository(config.root)
    app.state.filesystem_operation_inspector = FilesystemOperationInspector(
        config.root,
        organization_operations=app.state.organization_operations,
        organization_undos=app.state.organization_undos,
        duplicate_bin_batches=app.state.duplicate_bin_batches,
        duplicate_bin_authorizations=app.state.duplicate_bin_authorizations,
        duplicate_bin_operations=app.state.duplicate_bin_operations,
        duplicate_bin_restores=app.state.duplicate_bin_restores,
    )
    app.state.current_recovery_admission_status = (
        CurrentRecoveryAdmissionStatus(
            app.state.filesystem_operation_inspector
        )
    )
    app.state.recovery_admission_snapshot = (
        app.state.current_recovery_admission_status.snapshot()
    )
    app.state.recovery_evidence_inspector = RecoveryEvidenceInspector(
        app.state.filesystem_operation_inspector
    )
    app.state.filesystem_operation_admission = FilesystemOperationAdmission(
        app.state.filesystem_operation_inspector
    )
    app.state.organization_identity_execution = (
        OrganizationIdentityExecutionService(
            app.state.organization_operations,
            consume_authorization=app.state.organization_execution.consume,
            refresh_recovery_status=(
                app.state.current_recovery_admission_status.refresh
            ),
        )
    )
    app.state.organization_identity_undo_authorizations = (
        V2UndoAuthorizationStore(
            app.state.organization_operations.configured_root_identity
        )
    )
    app.state.organization_identity_undo = (
        OrganizationIdentityUndoOrchestrationService(
            app.state.organization_operations,
            app.state.organization_undos,
            app.state.organization_identity_undo_authorizations,
            refresh_recovery_status=(
                app.state.current_recovery_admission_status.refresh
            ),
        )
    )
    app.state.recovery_check_contexts = RecoveryCheckContextStore(
        app.state.organization_operations.configured_root_identity
    )
    app.state.recovery_check_eligibility = RecoveryCheckEligibilityInspector(
        app.state.recovery_evidence_inspector,
        app.state.organization_operations,
        app.state.organization_undos,
    )
    app.state.recovery_projection_service = RecoveryProjectionService(
        app.state.recovery_evidence_inspector,
        app.state.recovery_check_eligibility,
    )
    app.state.recovery_check_service = RecoveryCheckService(
        app.state.runtime_lifecycle,
        app.state.organization_execution,
        app.state.recovery_check_eligibility,
        app.state.recovery_check_contexts,
        OrganizationIdentityVerificationPublicationService(
            app.state.organization_operations
        ),
        OrganizationUndoIdentityVerificationPublicationService(
            app.state.organization_operations,
            app.state.organization_undos,
        ),
        app.state.current_recovery_admission_status,
    )
    app.state.organization_decisions = OrganizationDecisionStore(
        app.state.duplicate_bin_batches,
        runtime_lifecycle=app.state.runtime_lifecycle,
        execution_coordinator=app.state.organization_execution,
        filesystem_operation_admission=app.state.filesystem_operation_admission,
    )
    app.state.standards = StandardsService(
        StandardsRepository(config.root),
        decision_store=app.state.organization_decisions,
        execution_coordinator=app.state.organization_execution,
        operation_repository=app.state.organization_operations,
        duplicate_bin_repository=app.state.duplicate_bin_batches,
        duplicate_bin_operation_repository=app.state.duplicate_bin_operations,
        duplicate_bin_restore_repository=app.state.duplicate_bin_restores,
        runtime_lifecycle=app.state.runtime_lifecycle,
    )
    app.state.standards_creation = StandardsCreationStore(config.root)
    app.state.standards_creation_service = StandardsCreationApplicationService(
        app.state.standards,
        app.state.standards_creation,
        app.state.runtime_lifecycle,
    )
    app.include_router(create_home_router(templates))
    app.include_router(create_consumer_router(templates))
    app.include_router(create_loci_flow_router(templates))
    app.include_router(create_organization_api_router())
    app.include_router(create_duplicate_bin_api_router())
    app.include_router(create_activity_api_router())
    app.include_router(create_standards_api_router())
    app.include_router(create_recovery_api_router())
    app.include_router(create_review_router(templates))
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIRECTORY), check_dir=True),
        name="static",
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        request: Request,
        error: StarletteHTTPException,
    ) -> Response:
        if error.status_code == 404:
            heading = "Page unavailable"
            message = "This bootAI page is not available."
        else:
            heading = "Request unavailable"
            message = "This bootAI request is not available."
        response = templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "page_title": heading,
                "heading": heading,
                "message": message,
            },
            status_code=error.status_code,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> Response:
        del error
        response = templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "page_title": "Session unavailable",
                "heading": "Session unavailable",
                "message": (
                    "This bootAI session is not available. "
                    "Launch bootAI again from the local application."
                ),
            },
            status_code=500,
        )
        response.headers["Cache-Control"] = "no-store"
        return apply_security_headers(response)

    return app


def app_runtime_lifecycle(app: FastAPI) -> RuntimeLifecycle:
    """Return only the authoritative lifecycle owned by a constructed app."""

    lifecycle = getattr(app.state, "runtime_lifecycle", None)
    if not isinstance(lifecycle, RuntimeLifecycle):
        raise RuntimeError("the application runtime lifecycle is unavailable")
    return lifecycle
