from __future__ import annotations

from datetime import datetime
import uuid

from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
    approved_plan_identity,
)
from organizer.application.execution_service import (
    CONFIRM_MOVE_APPROVED_FILES,
    OrganizationExecutionError,
    issue_movement_authorization,
    move_approved_organization,
)
from organizer.application.organization_decision_models import (
    STATUS_READY_TO_MOVE,
    ApprovedOrganizationPlan,
)
from organizer.application.organization_movement_models import (
    OrganizationExecutionState,
    OrganizationMovementPreview,
)
from organizer.application.organization_operation_models import (
    AUTHORIZATION_AVAILABLE,
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
) -> OrganizationMovementPreview:
    """Issue a one-use authorization from server-owned approved state."""

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
    if not coordinator.try_acquire():
        raise OrganizationMovementError("movement_or_undo_in_progress")
    coordinator.release()
    authorization = issue_movement_authorization(
        approved_plan,
        coordinator,
        browser_session_id=browser_session_id,
        configured_root_identity=operation_repository.configured_root_identity,
    )
    return OrganizationMovementPreview(
        files_to_move=len(approved_plan.reviewed_move_items),
        duplicate_copies_deferred=(
            approved_plan.summary.duplicate_copies_for_future_bin
        ),
        confirmation=CONFIRM_MOVE_APPROVED_FILES,
        confirmation_token=authorization.authorization_id,
    )


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
) -> OrganizationExecutionState:
    """Revalidate server state and delegate movement to the durable D1 owner."""

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
    try:
        replay = operation_repository.find_for_authorization(confirmation_token)
        existing_plan = operation_repository.find_for_plan(
            approved_plan.scan_generation,
            approved_plan.decision_revision,
        )
    except ValueError as error:
        raise OrganizationMovementError("movement_recovery_required") from error
    if replay is not None:
        return _record_state(replay)
    if existing_plan is not None:
        raise OrganizationMovementError("movement_already_started")
    if authorization.status != AUTHORIZATION_AVAILABLE:
        raise OrganizationMovementError("movement_already_started")
    try:
        outcome = move_approved_organization(
            approved_plan=approved_plan,
            current_scan_generation=current_scan_generation,
            root=operation_repository.root,
            authorization=authorization,
            coordinator=coordinator,
            operation_repository=operation_repository,
            confirmation=confirmation,
        )
    except OrganizationExecutionError as error:
        if error.code == "movement_already_started":
            current = coordinator.authorization_snapshot(confirmation_token)
            code = (
                "movement_or_undo_in_progress"
                if current is not None and current.status == AUTHORIZATION_AVAILABLE
                else "movement_already_started"
            )
            raise OrganizationMovementError(code) from error
        mapping = {
            "confirmation_required": "invalid_confirmation",
            "approved_plan_not_found": "approved_plan_not_found",
            "approved_plan_stale": "approved_plan_stale",
        }
        raise OrganizationMovementError(mapping.get(error.code, error.code)) from error
    return _outcome_state(outcome)


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
        files_moved=sum(move.result_state == MOVE_MOVED for move in record.expected_moves),
        files_failed=sum(move.result_state == MOVE_FAILED for move in record.expected_moves),
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


def _record_ordering_key(record: OrganizationOperationRecord) -> tuple[datetime, str]:
    value = record.completed_at or record.started_at or record.created_at
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("organization operation timestamp lacks a timezone")
    return parsed, record.operation_id
