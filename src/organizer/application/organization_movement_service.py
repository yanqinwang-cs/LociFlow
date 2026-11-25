from __future__ import annotations

import uuid
from datetime import datetime

from organizer.application.current_recovery_admission_status import (
    CurrentRecoveryAdmissionStatus,
)
from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
    approved_plan_identity,
)
from organizer.application.execution_service import (
    CONFIRM_MOVE_APPROVED_FILES,
    _issue_movement_authorization,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationInspector,
)
from organizer.application.organization_decision_models import (
    STATUS_READY_TO_MOVE,
    ApprovedOrganizationPlan,
)
from organizer.application.organization_identity_execution import (
    IdentityOrganizationExecutionResult,
    OrganizationIdentityExecutionError,
    OrganizationIdentityExecutionService,
)
from organizer.application.organization_movement_models import (
    OrganizationExecutionState,
    OrganizationMovementPreview,
)
from organizer.application.organization_operation_models import (
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    STATUS_INTERRUPTED,
    OrganizationOperationOutcome,
    OrganizationOperationRecord,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.runtime_lifecycle import RuntimeOperationKind
from organizer.application.volume_admission import (
    OrganizationVolumeAdmission,
    VolumeAdmissionError,
)


class OrganizationMovementError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def project_organization_execution(
    *,
    approved_plan: ApprovedOrganizationPlan | None,
    operation_repository: OrganizationOperationRepository,
) -> OrganizationExecutionState:
    """Project durable execution state without exposing operation internals."""

    try:
        records = operation_repository.list_records()
        blocking = operation_repository.blocking_record()
    except ValueError:
        return _empty_state("recovery_required")
    if approved_plan is None or approved_plan.status != STATUS_READY_TO_MOVE:
        if blocking is not None:
            return _record_state(
                blocking,
                status=(
                    "recovery_required"
                    if blocking.status == STATUS_INTERRUPTED
                    else blocking.status
                ),
            )
        if records:
            return _record_state(max(records, key=_record_ordering_key))
        return _empty_state("not_ready")
    existing = next(
        (
            record
            for record in records
            if record.scan_generation == approved_plan.scan_generation
            and record.decision_revision == approved_plan.decision_revision
        ),
        None,
    )
    if existing is not None:
        return _record_state(existing)
    if blocking is not None:
        return _record_state(
            blocking,
            status=(
                "recovery_required"
                if blocking.status == STATUS_INTERRUPTED
                else blocking.status
            ),
        )
    files_to_move = len(approved_plan.reviewed_move_items)
    if files_to_move == 0:
        return _empty_state("no_supported_moves")
    return OrganizationExecutionState(
        status="ready",
        operation_id=None,
        files_expected=files_to_move,
        files_moved=0,
        files_failed=0,
        files_unattempted=files_to_move,
        verification_status=None,
    )


def preview_organization_movement(
    *,
    approved_plan: ApprovedOrganizationPlan,
    browser_session_id: str,
    current_scan_generation: str,
    current_decision_revision: int,
    coordinator: OrganizationExecutionCoordinator,
    operation_repository: OrganizationOperationRepository,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    volume_admission: OrganizationVolumeAdmission | None = None,
) -> OrganizationMovementPreview:
    """Issue a one-use authorization from server-owned approved state."""

    with coordinator.runtime_lifecycle.begin_operation(
        RuntimeOperationKind.STATE_MUTATION
    ):
        if not coordinator.try_acquire():
            raise OrganizationMovementError("movement_or_undo_in_progress")
        try:
            _validate_current_approval(
                approved_plan,
                current_scan_generation,
                current_decision_revision,
            )
            if not approved_plan.reviewed_move_items:
                raise OrganizationMovementError("no_supported_moves")
            try:
                existing = operation_repository.find_for_plan(
                    approved_plan.scan_generation,
                    approved_plan.decision_revision,
                )
                blocking = operation_repository.blocking_record()
            except ValueError as error:
                raise OrganizationMovementError("movement_recovery_required") from error
            if existing is not None:
                raise OrganizationMovementError("movement_already_started")
            if blocking is not None:
                raise OrganizationMovementError(
                    "movement_recovery_required"
                    if blocking.status == STATUS_INTERRUPTED
                    else "movement_or_undo_in_progress"
                )
            admission = filesystem_operation_admission or FilesystemOperationAdmission(
                FilesystemOperationInspector(operation_repository.root)
            )
            if admission.root != operation_repository.root:
                raise OrganizationMovementError("approved_plan_stale")
            admission.inspect_and_require_clear()
            volume = volume_admission or OrganizationVolumeAdmission(
                operation_repository.root
            )
            if volume.root != operation_repository.root:
                raise OrganizationMovementError("approved_plan_stale")
            try:
                volume.inspect_and_require_allowed(approved_plan)
            except VolumeAdmissionError as error:
                raise OrganizationMovementError(error.code) from error
            authorization = _issue_movement_authorization(
                approved_plan,
                coordinator,
                browser_session_id=browser_session_id,
                configured_root_identity=(
                    operation_repository.configured_root_identity
                ),
            )
            return OrganizationMovementPreview(
                files_to_move=len(approved_plan.reviewed_move_items),
                duplicate_copies_deferred=(
                    approved_plan.summary.duplicate_copies_for_future_bin
                ),
                confirmation=CONFIRM_MOVE_APPROVED_FILES,
                confirmation_token=authorization.authorization_id,
            )
        finally:
            coordinator.release()


def execute_organization_movement(
    *,
    approved_plan: ApprovedOrganizationPlan,
    browser_session_id: str,
    current_scan_generation: str,
    current_decision_revision: int,
    confirmation_token: str,
    confirmation: str,
    coordinator: OrganizationExecutionCoordinator,
    operation_repository: OrganizationOperationRepository,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    volume_admission: OrganizationVolumeAdmission | None = None,
    identity_execution_service: (OrganizationIdentityExecutionService | None) = None,
    recovery_status: CurrentRecoveryAdmissionStatus | None = None,
) -> OrganizationExecutionState:
    """Revalidate server state and execute the identity-safe v2 movement path."""

    _validate_authorization_id(confirmation_token)
    _validate_current_approval(
        approved_plan,
        current_scan_generation,
        current_decision_revision,
    )
    if not approved_plan.reviewed_move_items:
        raise OrganizationMovementError("no_supported_moves")
    authorization = coordinator.authorization_snapshot(confirmation_token)
    if authorization is None:
        raise OrganizationMovementError("invalid_confirmation_token")
    if confirmation != CONFIRM_MOVE_APPROVED_FILES:
        raise OrganizationMovementError("invalid_confirmation")
    if (
        authorization.browser_session_id != browser_session_id
        or authorization.configured_root_identity
        != operation_repository.configured_root_identity
        or authorization.scan_generation != current_scan_generation
        or authorization.decision_revision != current_decision_revision
        or authorization.approved_plan_identity != approved_plan_identity(approved_plan)
    ):
        raise OrganizationMovementError("invalid_confirmation_token")
    if not coordinator.try_acquire(RuntimeOperationKind.ORGANIZATION_MOVEMENT):
        raise OrganizationMovementError("movement_or_undo_in_progress")
    try:
        try:
            existing = operation_repository.find_for_plan(
                approved_plan.scan_generation,
                approved_plan.decision_revision,
            )
            blocking = operation_repository.blocking_record()
        except ValueError as error:
            raise OrganizationMovementError("movement_recovery_required") from error
        if existing is not None:
            if existing.authorization_id != authorization.authorization_id:
                raise OrganizationMovementError("movement_already_started")
            if existing.status in {
                "verified",
                "partial_movement",
                "movement_failed",
                "verification_failed",
            }:
                return _record_state(existing)
            raise OrganizationMovementError(
                "movement_recovery_required"
                if existing.status == STATUS_INTERRUPTED
                else "movement_already_started"
            )
        if blocking is not None:
            raise OrganizationMovementError(
                "movement_recovery_required"
                if blocking.status == STATUS_INTERRUPTED
                else "movement_or_undo_in_progress"
            )
        admission = filesystem_operation_admission or FilesystemOperationAdmission(
            FilesystemOperationInspector(operation_repository.root)
        )
        if admission.root != operation_repository.root:
            raise OrganizationMovementError("approved_plan_stale")
        admission.inspect_and_require_clear()
        service = identity_execution_service or OrganizationIdentityExecutionService(
            operation_repository,
            consume_authorization=coordinator.consume,
            volume_admission=volume_admission,
            refresh_recovery_status=(
                recovery_status.refresh if recovery_status is not None else None
            ),
        )
        try:
            result = service.execute(
                approved_plan=approved_plan,
                authorization=authorization,
            )
        except OrganizationIdentityExecutionError as error:
            coordinator.invalidate(authorization.authorization_id)
            mapping = {
                "identity_capture_failed": "source_changed",
                "strict_plan_invalid": "approved_plan_stale",
                "invalid_intent": "approved_plan_stale",
                "durable_intent_failed": "operation_record_failed",
            }
            raise OrganizationMovementError(
                mapping.get(error.code, error.code)
            ) from error
        return _identity_result_state(result)
    finally:
        if recovery_status is not None:
            try:
                recovery_status.refresh()
            except Exception:
                pass
        coordinator.release()


def _validate_current_approval(
    approved_plan: ApprovedOrganizationPlan,
    current_scan_generation: str,
    current_decision_revision: int,
) -> None:
    if approved_plan.status != STATUS_READY_TO_MOVE:
        raise OrganizationMovementError("approved_plan_not_found")
    if approved_plan.scan_generation != current_scan_generation:
        raise OrganizationMovementError("scan_generation_mismatch")
    if approved_plan.decision_revision != current_decision_revision:
        raise OrganizationMovementError("decision_revision_mismatch")


def _validate_authorization_id(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise OrganizationMovementError("invalid_confirmation_token") from error
    if str(parsed) != value:
        raise OrganizationMovementError("invalid_confirmation_token")


def _empty_state(status: str) -> OrganizationExecutionState:
    return OrganizationExecutionState(status, None, 0, 0, 0, 0, None)


def _record_state(
    record: OrganizationOperationRecord,
    *,
    status: str | None = None,
) -> OrganizationExecutionState:
    return OrganizationExecutionState(
        status=status or record.status,
        operation_id=record.operation_id,
        files_expected=len(record.expected_moves),
        files_moved=sum(
            move.result_state == MOVE_MOVED for move in record.expected_moves
        ),
        files_failed=sum(
            move.result_state == MOVE_FAILED for move in record.expected_moves
        ),
        files_unattempted=sum(
            move.result_state in {MOVE_PENDING, MOVE_UNATTEMPTED}
            for move in record.expected_moves
        ),
        verification_status=record.verification.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


def _outcome_state(outcome: OrganizationOperationOutcome) -> OrganizationExecutionState:
    return OrganizationExecutionState(
        status=outcome.status,
        operation_id=outcome.operation_id,
        files_expected=outcome.files_expected,
        files_moved=outcome.files_moved,
        files_failed=outcome.files_failed,
        files_unattempted=outcome.files_unattempted,
        verification_status=outcome.verification_status,
        started_at=outcome.started_at,
        completed_at=outcome.completed_at,
    )


def _identity_result_state(
    result: IdentityOrganizationExecutionResult,
) -> OrganizationExecutionState:
    return OrganizationExecutionState(
        status=result.state.value,
        operation_id=result.operation_id,
        files_expected=result.files_expected,
        files_moved=result.files_moved,
        files_failed=result.files_failed,
        files_unattempted=result.files_unattempted,
        verification_status=result.verification_status,
        started_at=result.started_at,
        completed_at=result.completed_at,
    )


def _record_ordering_key(record: OrganizationOperationRecord) -> tuple[datetime, str]:
    value = record.completed_at or record.started_at or record.created_at
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("organization operation timestamp lacks a timezone")
    return parsed, record.operation_id
