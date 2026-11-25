from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_identity_authorization import (
    IDENTITY_V2_UNDO_MODE,
    V2UndoAuthorizationError,
    V2UndoAuthorizationStore,
    canonical_parent_record_digest,
    canonical_reverse_evidence_digest,
)
from organizer.application.organization_undo_identity_eligibility import (
    IdentityUndoEligibilityReason,
    IdentityUndoEligibilityState,
    OrganizationUndoIdentityEligibilityInspector,
)
from organizer.application.organization_undo_identity_execution import (
    IdentityUndoExecutionResult,
    IdentityUndoExecutionState,
    OrganizationIdentityUndoExecutionError,
    execute_v2_organization_undo_intent,
)
from organizer.application.organization_undo_identity_intent import (
    build_v2_organization_undo_intent,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.undo_identity_verification_publication import (
    IdentityUndoVerificationPublicationState,
    OrganizationUndoIdentityVerificationPublicationService,
)

UNDO_FILE_MOVE_CONFIRMATION = "UNDO FILE MOVE"


class IdentityUndoPreviewState(str, Enum):
    READY = "ready"
    INELIGIBLE = "ineligible"
    UNAVAILABLE = "unavailable"


class IdentityUndoConfirmationState(str, Enum):
    RESTORED = "restored"
    UNDO_VERIFICATION_FAILED = "undo_verification_failed"
    RESTORED_UNVERIFIED = "restored_unverified"
    PARTIAL_UNDO = "partial_undo"
    UNDO_FAILED = "undo_failed"
    INTERRUPTED = "interrupted"
    STALE = "stale"
    EXPIRED = "expired"


@dataclass(frozen=True, repr=False)
class IdentityUndoPreviewResult:
    state: IdentityUndoPreviewState
    item_count: int
    reason: IdentityUndoEligibilityReason | None = None
    confirmation_phrase: str | None = None
    authorization_id: str | None = None
    expires_at: str | None = None

    def __repr__(self) -> str:
        return (
            "IdentityUndoPreviewResult("
            f"state={self.state.value!r}, item_count={self.item_count})"
        )


@dataclass(frozen=True, repr=False)
class IdentityUndoConfirmationResult:
    state: IdentityUndoConfirmationState
    item_count: int
    files_restored: int
    undo_id: str | None = None

    def __repr__(self) -> str:
        return (
            "IdentityUndoConfirmationResult("
            f"state={self.state.value!r}, item_count={self.item_count}, "
            f"files_restored={self.files_restored})"
        )


class OrganizationIdentityUndoOrchestrationError(RuntimeError):
    _CODES = {
        "invalid_confirmation",
        "authorization_rejected",
        "durable_intent_failed",
        "recovery_required",
    }

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("identity Undo orchestration error code is invalid")
        self.code = code
        super().__init__(code)


class OrganizationIdentityUndoOrchestrationService:
    """Disconnected v2 Undo orchestration; caller owns lifecycle and coordinator."""

    def __init__(
        self,
        operation_repository: OrganizationOperationRepository,
        undo_repository: OrganizationUndoRepository,
        authorization_store: V2UndoAuthorizationStore,
        *,
        eligibility_inspector: (
            OrganizationUndoIdentityEligibilityInspector | None
        ) = None,
        verification_publisher: (
            OrganizationUndoIdentityVerificationPublicationService | None
        ) = None,
        now: Callable[[], datetime] | None = None,
        id_source: Callable[[], str] | None = None,
        executor: Callable[..., object] | None = None,
        refresh_recovery_status: Callable[[], object] | None = None,
    ) -> None:
        if (
            operation_repository.root != undo_repository.root
            or authorization_store.configured_root_identity
            != operation_repository.configured_root_identity
        ):
            raise ValueError(
                "identity Undo orchestration owners belong to different roots"
            )
        self.operation_repository = operation_repository
        self.undo_repository = undo_repository
        self.authorization_store = authorization_store
        self.eligibility_inspector = eligibility_inspector or (
            OrganizationUndoIdentityEligibilityInspector(
                operation_repository, undo_repository
            )
        )
        self.verification_publisher = verification_publisher or (
            OrganizationUndoIdentityVerificationPublicationService(
                operation_repository, undo_repository
            )
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._id_source = id_source or (lambda: str(uuid.uuid4()))
        self._executor = executor
        self._refresh_recovery_status = refresh_recovery_status

    def preview(
        self,
        *,
        parent_operation_id: str,
        browser_session_id: str,
    ) -> IdentityUndoPreviewResult:
        eligibility = self.eligibility_inspector.inspect(parent_operation_id)
        if eligibility.state is not IdentityUndoEligibilityState.ELIGIBLE:
            return IdentityUndoPreviewResult(
                state=(
                    IdentityUndoPreviewState.INELIGIBLE
                    if eligibility.state is IdentityUndoEligibilityState.INELIGIBLE
                    else IdentityUndoPreviewState.UNAVAILABLE
                ),
                item_count=0,
                reason=eligibility.reason,
            )
        if eligibility.parent is None:
            return IdentityUndoPreviewResult(
                IdentityUndoPreviewState.UNAVAILABLE, 0
            )
        authorization = self.authorization_store.issue(
            browser_session_id=browser_session_id,
            parent_operation_id=parent_operation_id,
            parent_record_digest=canonical_parent_record_digest(
                eligibility.parent
            ),
            reverse_evidence_digest=canonical_reverse_evidence_digest(
                eligibility
            ),
        )
        return IdentityUndoPreviewResult(
            state=IdentityUndoPreviewState.READY,
            item_count=len(eligibility.captures),
            reason=None,
            confirmation_phrase=UNDO_FILE_MOVE_CONFIRMATION,
            authorization_id=authorization.authorization_id,
            expires_at=authorization.expires_at,
        )

    def confirm(
        self,
        *,
        parent_operation_id: str,
        authorization_id: str,
        browser_session_id: str,
        confirmation: str,
    ) -> IdentityUndoConfirmationResult:
        if confirmation != UNDO_FILE_MOVE_CONFIRMATION:
            raise OrganizationIdentityUndoOrchestrationError(
                "invalid_confirmation"
            )
        try:
            authorization = self.authorization_store.require_available(
                authorization_id,
                browser_session_id=browser_session_id,
                parent_operation_id=parent_operation_id,
                mode=IDENTITY_V2_UNDO_MODE,
            )
        except V2UndoAuthorizationError as error:
            if error.code == "expired":
                return IdentityUndoConfirmationResult(
                    IdentityUndoConfirmationState.EXPIRED, 0, 0
                )
            raise OrganizationIdentityUndoOrchestrationError(
                "authorization_rejected"
            ) from error

        eligibility = self.eligibility_inspector.inspect(parent_operation_id)
        if (
            eligibility.state is not IdentityUndoEligibilityState.ELIGIBLE
            or eligibility.parent is None
            or canonical_parent_record_digest(eligibility.parent)
            != authorization.parent_record_digest
            or canonical_reverse_evidence_digest(eligibility)
            != authorization.reverse_evidence_digest
        ):
            self.authorization_store.invalidate(authorization_id)
            return IdentityUndoConfirmationResult(
                IdentityUndoConfirmationState.STALE, 0, 0
            )

        created_at = self._timestamp()
        try:
            intent = build_v2_organization_undo_intent(
                parent=eligibility.parent,
                undo_id=self._id_source(),
                authorization_id=authorization.authorization_id,
                configured_root_identity=(
                    self.operation_repository.configured_root_identity
                ),
                created_at=created_at,
                captures=eligibility.captures,
            )
        except (TypeError, ValueError) as error:
            self.authorization_store.invalidate(authorization_id)
            raise OrganizationIdentityUndoOrchestrationError(
                "authorization_rejected"
            ) from error

        consumption_failed = False

        def consume_after_create() -> bool:
            nonlocal consumption_failed
            self._refresh_status()
            try:
                consumed = self.authorization_store.consume(authorization_id)
            except Exception:
                consumed = False
            if not consumed:
                consumption_failed = True
                self.authorization_store.invalidate(authorization_id)
            return consumed

        execution_kwargs = {}
        if self._executor is not None:
            execution_kwargs["executor"] = self._executor
        try:
            execution = execute_v2_organization_undo_intent(
                intent=intent,
                operation_repository=self.operation_repository,
                undo_repository=self.undo_repository,
                clock=lambda: self._timestamp(),
                post_create=consume_after_create,
                **execution_kwargs,
            )
        except OrganizationIdentityUndoExecutionError as error:
            if error.code == "durable_intent_failed":
                raise OrganizationIdentityUndoOrchestrationError(
                    "durable_intent_failed"
                ) from error
            raise OrganizationIdentityUndoOrchestrationError(
                "recovery_required"
            ) from error
        finally:
            self._refresh_status()
        if consumption_failed:
            return self._execution_result(execution)
        if execution.state is IdentityUndoExecutionState.RESTORED_UNVERIFIED:
            try:
                publication = self.verification_publisher.publish(
                    execution.undo_id
                )
            finally:
                self._refresh_status()
            if (
                publication.state
                is IdentityUndoVerificationPublicationState.PUBLISHED_RESTORED
            ):
                state = IdentityUndoConfirmationState.RESTORED
            elif (
                publication.state
                is IdentityUndoVerificationPublicationState
                .PUBLISHED_VERIFICATION_FAILED
            ):
                state = IdentityUndoConfirmationState.UNDO_VERIFICATION_FAILED
            else:
                state = IdentityUndoConfirmationState.RESTORED_UNVERIFIED
            return IdentityUndoConfirmationResult(
                state,
                execution.files_expected,
                execution.files_restored,
                execution.undo_id,
            )
        return self._execution_result(execution)

    def _refresh_status(self) -> None:
        if self._refresh_recovery_status is None:
            return
        try:
            self._refresh_recovery_status()
        except Exception:
            pass

    def _execution_result(
        self,
        execution: IdentityUndoExecutionResult,
    ) -> IdentityUndoConfirmationResult:
        mapping = {
            IdentityUndoExecutionState.PARTIAL: (
                IdentityUndoConfirmationState.PARTIAL_UNDO
            ),
            IdentityUndoExecutionState.FAILED: (
                IdentityUndoConfirmationState.UNDO_FAILED
            ),
            IdentityUndoExecutionState.INTERRUPTED: (
                IdentityUndoConfirmationState.INTERRUPTED
            ),
            IdentityUndoExecutionState.RESTORED_UNVERIFIED: (
                IdentityUndoConfirmationState.RESTORED_UNVERIFIED
            ),
        }
        return IdentityUndoConfirmationResult(
            mapping[execution.state],
            execution.files_expected,
            execution.files_restored,
            execution.undo_id,
        )

    def _timestamp(self) -> str:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("identity Undo orchestration clock is invalid")
        return value.astimezone(UTC).isoformat()
