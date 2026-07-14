from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import uuid

from organizer.application.activity_service import calculate_undo_eligibility
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.organization_operation_models import OrganizationOperationRecord
from organizer.application.organization_operation_repository import OrganizationOperationRepository
from organizer.application.organization_undo_models import (
    UNDO_AUTHORIZATION_AVAILABLE,
    UNDO_FAILED,
    UNDO_INTERRUPTED,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_FAILED,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_ITEM_UNATTEMPTED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
    UNDO_VERIFICATION_FAILED,
    UNDO_VERIFICATION_FAILED_STATE,
    UNDO_VERIFICATION_PASSED,
    OrganizationUndoItem,
    OrganizationUndoOutcome,
    OrganizationUndoRecord,
    OrganizationUndoRecoveryInspection,
    UndoPreview,
    UndoVerificationResult,
)
from organizer.application.organization_undo_repository import OrganizationUndoRepository
from organizer.executor import UndoRecordingError, undo_operation_log
from organizer.models import MoveResult, OperationLog
from organizer.organization_verify import (
    OrganizationUndoVerificationItem,
    verify_organization_undo,
)
from organizer.safety import validate_under_root


CONFIRM_UNDO_FILE_MOVE = "UNDO FILE MOVE"


class OrganizationUndoError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def preview_organization_undo(
    *,
    operation_id: str,
    browser_session_id: str,
    coordinator: OrganizationExecutionCoordinator,
    operation_repository: OrganizationOperationRepository,
    undo_repository: OrganizationUndoRepository,
) -> UndoPreview:
    _load_operation(operation_id, operation_repository)
    eligibility = calculate_undo_eligibility(
        operation_repository=operation_repository,
        undo_repository=undo_repository,
        requested_operation_id=operation_id,
    )
    if not eligibility.undoable:
        raise OrganizationUndoError(eligibility.error_code or "operation_not_undoable")
    authorization = coordinator.issue_undo_authorization(
        browser_session_id=browser_session_id,
        configured_root_identity=operation_repository.configured_root_identity,
        operation_id=operation_id,
    )
    return UndoPreview(
        operation_id=operation_id,
        files_to_restore=eligibility.files_to_restore,
        confirmation_required=CONFIRM_UNDO_FILE_MOVE,
        confirmation_token=authorization.authorization_id,
    )


def undo_organization_operation(
    *,
    operation_id: str,
    browser_session_id: str,
    confirmation_token: str,
    confirmation: str,
    coordinator: OrganizationExecutionCoordinator,
    operation_repository: OrganizationOperationRepository,
    undo_repository: OrganizationUndoRepository,
) -> OrganizationUndoOutcome:
    if not coordinator.try_acquire():
        raise OrganizationUndoError("movement_or_undo_in_progress")
    try:
        record = _load_operation(operation_id, operation_repository)
        authorization = coordinator.undo_authorization_snapshot(confirmation_token)
        if authorization is None:
            raise OrganizationUndoError("operation_not_undoable")
        if authorization.status != UNDO_AUTHORIZATION_AVAILABLE:
            raise OrganizationUndoError("undo_already_started")
        if (
            authorization.browser_session_id != browser_session_id
            or authorization.configured_root_identity
            != operation_repository.configured_root_identity
            or authorization.operation_id != operation_id
        ):
            raise OrganizationUndoError("operation_not_undoable")
        if confirmation != CONFIRM_UNDO_FILE_MOVE:
            raise OrganizationUndoError("operation_not_undoable")

        eligibility = calculate_undo_eligibility(
            operation_repository=operation_repository,
            undo_repository=undo_repository,
            requested_operation_id=operation_id,
        )
        if not eligibility.undoable or eligibility.legacy_log_path is None:
            raise OrganizationUndoError(
                eligibility.error_code or "operation_not_undoable"
            )

        now = _utc_timestamp()
        undo_record = OrganizationUndoRecord(
            undo_id=str(uuid.uuid4()),
            operation_id=operation_id,
            authorization_id=authorization.authorization_id,
            configured_root_identity=undo_repository.configured_root_identity,
            status=UNDO_PREPARED,
            created_at=now,
            started_at=None,
            completed_at=None,
            expected_restores=tuple(
                OrganizationUndoItem(
                    change_id=move.change_id,
                    restore_target=move.source,
                    moved_from=move.destination,
                    baseline_size_bytes=move.source_size_bytes,
                    baseline_modified_time=move.source_modified_time,
                )
                for move in record.expected_moves
            ),
            verification=UndoVerificationResult(),
            legacy_undo_log=None,
            failure_code=None,
        )
        try:
            undo_repository.create(undo_record)
        except Exception as error:
            raise OrganizationUndoError("undo_failed") from error
        if not coordinator.consume_undo_authorization(authorization.authorization_id):
            interrupted = replace(
                undo_record,
                status=UNDO_INTERRUPTED,
                completed_at=_utc_timestamp(),
                failure_code="undo_already_started",
            )
            undo_repository.save(interrupted)
            raise OrganizationUndoError("undo_already_started")

        recorder = _DurableUndoRecorder(
            undo_record,
            operation_repository.root,
            undo_repository,
        )
        try:
            undo_operation_log(
                Path(eligibility.legacy_log_path),
                operation_repository.root,
                recorder=recorder,
            )
        except UndoRecordingError as error:
            interrupted = _persist_interrupted(
                undo_repository,
                undo_record.undo_id,
                error.operation_log,
            )
            return _safe_outcome(interrupted)
        except Exception:
            interrupted = _persist_interrupted(
                undo_repository,
                undo_record.undo_id,
                None,
            )
            return _safe_outcome(interrupted)

        completed = undo_repository.load(undo_record.undo_id)
        if completed.status != UNDO_RESTORED_UNVERIFIED:
            return _safe_outcome(completed)
        verified = _verify_and_finish(
            completed,
            record,
            operation_repository.root,
            undo_repository,
        )
        return _safe_outcome(verified)
    finally:
        coordinator.release()


def inspect_incomplete_organization_undo(
    *,
    undo_id: str,
    undo_repository: OrganizationUndoRepository,
) -> OrganizationUndoRecoveryInspection:
    try:
        record = undo_repository.load(undo_id)
    except ValueError as error:
        raise OrganizationUndoError("operation_not_found") from error
    likely_restored = 0
    likely_not_restored = 0
    recovery_required = 0
    root = undo_repository.root
    for item in record.expected_restores:
        if item.result_state == UNDO_ITEM_ATTEMPTING:
            target_present = os.path.lexists(root / item.restore_target)
            moved_present = os.path.lexists(root / item.moved_from)
            if target_present and not moved_present:
                likely_restored += 1
            elif not target_present and moved_present:
                likely_not_restored += 1
            else:
                recovery_required += 1
        elif item.result_state == UNDO_ITEM_PENDING:
            likely_not_restored += 1
    return OrganizationUndoRecoveryInspection(
        undo_id=record.undo_id,
        status=record.status,
        likely_restored=likely_restored,
        likely_not_restored=likely_not_restored,
        recovery_required=recovery_required,
    )


class _DurableUndoRecorder:
    def __init__(
        self,
        record: OrganizationUndoRecord,
        root: Path,
        repository: OrganizationUndoRepository,
    ) -> None:
        self.record = record
        self.root = root
        self.repository = repository

    def before_batch(self, items: tuple[MoveResult, ...]) -> None:
        if len(items) != len(self.record.expected_restores):
            raise ValueError("authorized undo batch changed")
        for item, expected in zip(items, self.record.expected_restores):
            if (
                _relative(item.source, self.root) != expected.restore_target
                or _relative(item.destination, self.root) != expected.moved_from
            ):
                raise ValueError("authorized undo batch changed")
        self.record = replace(
            self.record,
            status=UNDO_UNDOING,
            started_at=_utc_timestamp(),
        )
        self.repository.save(self.record)

    def before_restore(self, index: int, item: MoveResult) -> None:
        self._update_item(index, UNDO_ITEM_ATTEMPTING)

    def after_success(
        self,
        index: int,
        item: MoveResult,
        result: MoveResult,
    ) -> None:
        self._update_item(index, UNDO_ITEM_RESTORED)

    def after_failure(
        self,
        index: int,
        item: MoveResult,
        result: MoveResult,
    ) -> None:
        code = (
            "undo_destination_occupied"
            if result.message == "original source path already exists"
            else "undo_failed"
        )
        self._update_item(index, UNDO_ITEM_FAILED, failure_code=code)

    def after_batch(self, operation_log: OperationLog) -> None:
        items = tuple(
            replace(item, result_state=UNDO_ITEM_UNATTEMPTED)
            if item.result_state == UNDO_ITEM_PENDING
            else item
            for item in self.record.expected_restores
        )
        restored = sum(item.result_state == UNDO_ITEM_RESTORED for item in items)
        if restored == len(items):
            status = UNDO_RESTORED_UNVERIFIED
            completed_at = None
            failure_code = None
        elif restored:
            status = UNDO_PARTIAL
            completed_at = _utc_timestamp()
            failure_code = UNDO_PARTIAL
        else:
            status = UNDO_FAILED
            completed_at = _utc_timestamp()
            failure_code = UNDO_FAILED
        self.record = replace(
            self.record,
            status=status,
            completed_at=completed_at,
            expected_restores=items,
            legacy_undo_log=_relative(operation_log.log_path, self.root),
            failure_code=failure_code,
        )
        self.repository.save(self.record)

    def _update_item(
        self,
        index: int,
        state: str,
        *,
        failure_code: str | None = None,
    ) -> None:
        items = list(self.record.expected_restores)
        items[index] = replace(
            items[index],
            result_state=state,
            failure_code=failure_code,
        )
        self.record = replace(self.record, expected_restores=tuple(items))
        self.repository.save(self.record)


def _verify_and_finish(
    undo_record: OrganizationUndoRecord,
    operation: OrganizationOperationRecord,
    root: Path,
    repository: OrganizationUndoRepository,
) -> OrganizationUndoRecord:
    expected_pairs = tuple(
        (move.source, move.destination) for move in operation.expected_moves
    )
    items = tuple(
        OrganizationUndoVerificationItem(
            change_id=item.change_id,
            restore_target=item.restore_target,
            moved_from=item.moved_from,
            baseline_size_bytes=item.baseline_size_bytes,
            baseline_modified_time=item.baseline_modified_time,
            result_state=item.result_state,
        )
        for item in undo_record.expected_restores
    )
    try:
        verification = verify_organization_undo(items, expected_pairs, root)
    except Exception:
        result = UndoVerificationResult(
            status=UNDO_VERIFICATION_FAILED_STATE,
            mismatch_codes=("verification_unavailable",),
        )
        status = UNDO_VERIFICATION_FAILED
    else:
        result = UndoVerificationResult(
            status=(
                UNDO_VERIFICATION_PASSED
                if verification.passed
                else UNDO_VERIFICATION_FAILED_STATE
            ),
            verified_count=verification.verified_count,
            mismatch_codes=verification.mismatch_codes,
        )
        status = UNDO_RESTORED if verification.passed else UNDO_VERIFICATION_FAILED
    completed = replace(
        undo_record,
        status=status,
        completed_at=_utc_timestamp(),
        verification=result,
        failure_code=None if status == UNDO_RESTORED else UNDO_VERIFICATION_FAILED,
    )
    repository.save(completed)
    return completed


def _persist_interrupted(
    repository: OrganizationUndoRepository,
    undo_id: str,
    operation_log: OperationLog | None,
) -> OrganizationUndoRecord:
    record = repository.load(undo_id)
    interrupted = replace(
        record,
        status=UNDO_INTERRUPTED,
        completed_at=_utc_timestamp(),
        legacy_undo_log=(
            _relative(operation_log.log_path, repository.root)
            if operation_log is not None
            else record.legacy_undo_log
        ),
        failure_code="undo_recovery_required",
    )
    repository.save(interrupted)
    return interrupted


def _safe_outcome(record: OrganizationUndoRecord) -> OrganizationUndoOutcome:
    return OrganizationUndoOutcome(
        undo_id=record.undo_id,
        operation_id=record.operation_id,
        status=record.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
        files_expected=len(record.expected_restores),
        files_restored=sum(
            item.result_state == UNDO_ITEM_RESTORED
            for item in record.expected_restores
        ),
        files_failed=sum(
            item.result_state == UNDO_ITEM_FAILED
            for item in record.expected_restores
        ),
        files_unattempted=sum(
            item.result_state in {UNDO_ITEM_PENDING, UNDO_ITEM_UNATTEMPTED}
            for item in record.expected_restores
        ),
        verification_status=record.verification.status,
    )


def _load_operation(
    operation_id: str,
    repository: OrganizationOperationRepository,
) -> OrganizationOperationRecord:
    try:
        parsed = uuid.UUID(operation_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise OrganizationUndoError("operation_not_found") from error
    if len(operation_id) != 36 or str(parsed) != operation_id:
        raise OrganizationUndoError("operation_not_found")
    try:
        return repository.load(operation_id)
    except ValueError as error:
        raise OrganizationUndoError("operation_not_found") from error


def _relative(path: Path, root: Path) -> str:
    return validate_under_root(path.resolve(strict=False), root).relative_to(root).as_posix()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
