from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import uuid

from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
    approved_plan_identity,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationInspector,
)
from organizer.application.runtime_lifecycle import RuntimeOperationKind
from organizer.application.volume_admission import (
    OrganizationVolumeAdmission,
    VolumeAdmissionError,
)
from organizer.application.organization_decision_models import (
    STATUS_READY_TO_MOVE,
    ApprovedOrganizationPlan,
)
from organizer.application.organization_operation_models import (
    AUTHORIZATION_AVAILABLE,
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    REPLAYABLE_TERMINAL_STATUSES,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVEMENT_FAILED,
    STATUS_MOVING,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_PREPARED,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_NOT_RUN,
    VERIFICATION_PASSED,
    MovementAuthorization,
    OrganizationExpectedMove,
    OrganizationOperationOutcome,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
    OrganizationRecoveryInspection,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.executor import MovementRecordingError, apply_move_plan
from organizer.models import MovePlanItem, MoveResult, OperationLog, ReviewedPlanItem
from organizer.organization_verify import (
    OrganizationOperationVerificationItem,
    verify_organization_operation,
)
from organizer.review_session import (
    CATEGORY_ORGANIZATION,
    DECISION_APPROVED,
    find_approved_move_conflicts,
)
from organizer.safety import validate_under_root
from organizer.scanner import scan_directory
from organizer.scope import (
    evaluate_actionable_source_path,
    is_actionable_destination_path,
)


CONFIRM_MOVE_APPROVED_FILES = "MOVE APPROVED FILES"


class OrganizationExecutionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def issue_movement_authorization(
    approved_plan: ApprovedOrganizationPlan,
    coordinator: OrganizationExecutionCoordinator,
    *,
    filesystem_operation_admission: FilesystemOperationAdmission,
    volume_admission: OrganizationVolumeAdmission | None = None,
    browser_session_id: str | None = None,
    configured_root_identity: str | None = None,
) -> MovementAuthorization:
    with coordinator.runtime_lifecycle.begin_operation(
        RuntimeOperationKind.STATE_MUTATION
    ):
        if not coordinator.try_acquire():
            raise OrganizationExecutionError("movement_already_started")
        try:
            if approved_plan.status != STATUS_READY_TO_MOVE:
                raise OrganizationExecutionError("approved_plan_not_found")
            filesystem_operation_admission.inspect_and_require_clear()
            volume = volume_admission or OrganizationVolumeAdmission(
                filesystem_operation_admission.root
            )
            if volume.root != filesystem_operation_admission.root:
                raise OrganizationExecutionError("approved_plan_stale")
            try:
                volume.inspect_and_require_allowed(approved_plan)
            except VolumeAdmissionError as error:
                raise OrganizationExecutionError(error.code) from error
            return _issue_movement_authorization(
                approved_plan,
                coordinator,
                browser_session_id=browser_session_id,
                configured_root_identity=configured_root_identity,
            )
        finally:
            coordinator.release()


def _issue_movement_authorization(
    approved_plan: ApprovedOrganizationPlan,
    coordinator: OrganizationExecutionCoordinator,
    *,
    browser_session_id: str | None = None,
    configured_root_identity: str | None = None,
) -> MovementAuthorization:
    if approved_plan.status != STATUS_READY_TO_MOVE:
        raise OrganizationExecutionError("approved_plan_not_found")
    return coordinator.issue_authorization(
        approved_plan,
        browser_session_id=browser_session_id,
        configured_root_identity=configured_root_identity,
    )


def move_approved_organization(
    *,
    approved_plan: ApprovedOrganizationPlan,
    current_scan_generation: str,
    root: Path,
    authorization: MovementAuthorization,
    coordinator: OrganizationExecutionCoordinator,
    operation_repository: OrganizationOperationRepository,
    confirmation: str,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    volume_admission: OrganizationVolumeAdmission | None = None,
) -> OrganizationOperationOutcome:
    if not coordinator.try_acquire(RuntimeOperationKind.ORGANIZATION_MOVEMENT):
        raise OrganizationExecutionError("movement_already_started")
    try:
        if confirmation != CONFIRM_MOVE_APPROVED_FILES:
            raise OrganizationExecutionError("confirmation_required")

        resolved_root = root.resolve()
        if operation_repository.root != resolved_root:
            raise OrganizationExecutionError("approved_plan_stale")
        _validate_approved_plan(
            approved_plan,
            current_scan_generation,
            authorization,
            coordinator,
            require_available=False,
        )

        admission = filesystem_operation_admission or FilesystemOperationAdmission(
            FilesystemOperationInspector(resolved_root)
        )
        if admission.root != resolved_root:
            raise OrganizationExecutionError("approved_plan_stale")
        admission.inspect_and_require_clear()

        try:
            existing = operation_repository.find_for_plan(
                approved_plan.scan_generation,
                approved_plan.decision_revision,
            )
            blocking = operation_repository.blocking_record()
        except ValueError as error:
            raise OrganizationExecutionError("movement_recovery_required") from error
        if existing is not None:
            if existing.authorization_id != authorization.authorization_id:
                raise OrganizationExecutionError("movement_already_started")
            if existing.status in REPLAYABLE_TERMINAL_STATUSES:
                return _safe_outcome(existing)
            if existing.status == STATUS_INTERRUPTED:
                raise OrganizationExecutionError("movement_recovery_required")
            raise OrganizationExecutionError("movement_already_started")

        if blocking is not None:
            code = (
                "movement_recovery_required"
                if blocking.status == STATUS_INTERRUPTED
                else "movement_already_started"
            )
            raise OrganizationExecutionError(code)

        _require_available_authorization(authorization, coordinator)

        volume = volume_admission or OrganizationVolumeAdmission(resolved_root)
        if volume.root != resolved_root:
            raise OrganizationExecutionError("approved_plan_stale")
        try:
            volume.inspect_and_require_allowed(approved_plan)
        except VolumeAdmissionError as error:
            coordinator.consume(authorization.authorization_id)
            raise OrganizationExecutionError(error.code) from error

        plan_items, expected_moves = _preflight(approved_plan, resolved_root)
        now = _utc_timestamp()
        record = OrganizationOperationRecord(
            operation_id=str(uuid.uuid4()),
            authorization_id=authorization.authorization_id,
            status=STATUS_PREPARED,
            scan_generation=approved_plan.scan_generation,
            decision_revision=approved_plan.decision_revision,
            approved_at=approved_plan.approved_at,
            created_at=now,
            started_at=None,
            completed_at=None,
            configured_root_identity=operation_repository.configured_root_identity,
            expected_moves=expected_moves,
            verification=OrganizationOperationVerification(),
            legacy_operation_log=None,
            failure_code=None,
            duplicate_actions_deferred=len(approved_plan.duplicate_decisions),
        )
        try:
            operation_repository.create(record)
        except Exception as error:
            raise OrganizationExecutionError("operation_record_failed") from error

        if not coordinator.consume(authorization.authorization_id):
            interrupted = replace(
                record,
                status=STATUS_INTERRUPTED,
                completed_at=_utc_timestamp(),
                failure_code="movement_already_started",
            )
            operation_repository.save(interrupted)
            raise OrganizationExecutionError("movement_already_started")

        recorder = _DurableMovementRecorder(
            record,
            resolved_root,
            operation_repository,
        )
        try:
            apply_move_plan(plan_items, resolved_root, recorder=recorder)
        except MovementRecordingError as error:
            interrupted = _persist_interrupted(
                operation_repository,
                record.operation_id,
                error.operation_log,
            )
            return _safe_outcome(interrupted)
        except ValueError:
            failed = _persist_executor_preflight_failure(
                operation_repository,
                record.operation_id,
            )
            return _safe_outcome(failed)
        except Exception:
            interrupted = _persist_interrupted(
                operation_repository,
                record.operation_id,
                None,
            )
            return _safe_outcome(interrupted)

        moved = operation_repository.load(record.operation_id)
        if moved.status == STATUS_MOVEMENT_FAILED:
            return _safe_outcome(moved)
        verified = _verify_and_finish(
            moved,
            record.expected_moves,
            resolved_root,
            operation_repository,
        )
        return _safe_outcome(verified)
    finally:
        coordinator.release()


def inspect_incomplete_organization_operation(
    *,
    operation_id: str,
    root: Path,
    operation_repository: OrganizationOperationRepository,
) -> OrganizationRecoveryInspection:
    resolved_root = root.resolve()
    if operation_repository.root != resolved_root:
        raise OrganizationExecutionError("approved_plan_stale")
    try:
        record = operation_repository.load(operation_id)
    except ValueError as error:
        raise OrganizationExecutionError("operation_not_found") from error
    if record.status not in {
        STATUS_PREPARED,
        STATUS_MOVING,
        STATUS_MOVED_UNVERIFIED,
        STATUS_INTERRUPTED,
    }:
        return OrganizationRecoveryInspection(
            operation_id=record.operation_id,
            status=record.status,
            likely_moved=0,
            likely_not_moved=0,
            recovery_required=0,
        )

    likely_moved = 0
    likely_not_moved = 0
    recovery_required = 0
    moves: list[OrganizationExpectedMove] = []
    for move in record.expected_moves:
        recovery_state = move.recovery_state
        if move.result_state == MOVE_ATTEMPTING:
            source_present = os.path.lexists(resolved_root / move.source)
            destination_present = os.path.lexists(resolved_root / move.destination)
            if not source_present and destination_present:
                recovery_state = "likely_moved_needs_verification"
                likely_moved += 1
            elif source_present and not destination_present:
                recovery_state = "likely_not_moved"
                likely_not_moved += 1
            else:
                recovery_state = "recovery_required"
                recovery_required += 1
        elif move.result_state == MOVE_PENDING:
            recovery_state = "likely_not_moved"
            likely_not_moved += 1
        moves.append(replace(move, recovery_state=recovery_state))

    interrupted = replace(
        record,
        status=STATUS_INTERRUPTED,
        completed_at=record.completed_at or _utc_timestamp(),
        expected_moves=tuple(moves),
        failure_code="movement_recovery_required",
    )
    operation_repository.save(interrupted)
    return OrganizationRecoveryInspection(
        operation_id=record.operation_id,
        status=interrupted.status,
        likely_moved=likely_moved,
        likely_not_moved=likely_not_moved,
        recovery_required=recovery_required,
    )


class _DurableMovementRecorder:
    def __init__(
        self,
        record: OrganizationOperationRecord,
        root: Path,
        repository: OrganizationOperationRepository,
    ) -> None:
        self.record = record
        self.root = root
        self.repository = repository

    def before_batch(self, plan_items: tuple[MovePlanItem, ...]) -> None:
        if len(plan_items) != len(self.record.expected_moves):
            raise ValueError("approved move batch changed")
        for plan_item, expected in zip(plan_items, self.record.expected_moves):
            if (
                _relative(plan_item.source, self.root) != expected.source
                or _relative(plan_item.destination, self.root) != expected.destination
            ):
                raise ValueError("approved move batch changed")
        self.record = replace(
            self.record,
            status=STATUS_MOVING,
            started_at=_utc_timestamp(),
        )
        self.repository.save(self.record)

    def before_move(self, index: int, item: MovePlanItem) -> None:
        self._update_move(index, MOVE_ATTEMPTING)

    def after_success(
        self,
        index: int,
        item: MovePlanItem,
        result: MoveResult,
    ) -> None:
        self._update_move(index, MOVE_MOVED)

    def after_failure(
        self,
        index: int,
        item: MovePlanItem,
        result: MoveResult,
    ) -> None:
        self._update_move(index, MOVE_FAILED, failure_code="movement_failed")

    def after_batch(self, operation_log: OperationLog) -> None:
        moves = tuple(
            replace(move, result_state=MOVE_UNATTEMPTED)
            if move.result_state == MOVE_PENDING
            else move
            for move in self.record.expected_moves
        )
        moved = sum(move.result_state == MOVE_MOVED for move in moves)
        failed = sum(move.result_state == MOVE_FAILED for move in moves)
        unattempted = sum(move.result_state == MOVE_UNATTEMPTED for move in moves)
        if moved == len(moves):
            status = STATUS_MOVED_UNVERIFIED
            completed_at = None
        elif moved:
            status = STATUS_PARTIAL_MOVEMENT
            completed_at = _utc_timestamp()
        else:
            status = STATUS_MOVEMENT_FAILED
            completed_at = _utc_timestamp()
        self.record = replace(
            self.record,
            status=status,
            completed_at=completed_at,
            expected_moves=moves,
            legacy_operation_log=_relative(operation_log.log_path, self.root),
            failure_code=(
                None
                if status == STATUS_MOVED_UNVERIFIED
                else "partial_movement"
                if moved and (failed or unattempted)
                else "movement_failed"
            ),
        )
        self.repository.save(self.record)

    def _update_move(
        self,
        index: int,
        state: str,
        *,
        failure_code: str | None = None,
    ) -> None:
        moves = list(self.record.expected_moves)
        moves[index] = replace(
            moves[index],
            result_state=state,
            failure_code=failure_code,
        )
        updated = replace(self.record, expected_moves=tuple(moves))
        self.repository.save(updated)
        self.record = updated


def _validate_approved_plan(
    approved_plan: ApprovedOrganizationPlan,
    current_scan_generation: str,
    authorization: MovementAuthorization,
    coordinator: OrganizationExecutionCoordinator,
    *,
    require_available: bool = True,
) -> None:
    if approved_plan.status != STATUS_READY_TO_MOVE:
        raise OrganizationExecutionError("approved_plan_not_found")
    if approved_plan.scan_generation != current_scan_generation:
        raise OrganizationExecutionError("approved_plan_stale")
    current_authorization = coordinator.authorization_snapshot(
        authorization.authorization_id
    )
    if current_authorization is None:
        raise OrganizationExecutionError("approved_plan_not_found")
    if (
        current_authorization.authorization_id != authorization.authorization_id
        or current_authorization.scan_generation != authorization.scan_generation
        or current_authorization.decision_revision != authorization.decision_revision
        or current_authorization.approved_at != authorization.approved_at
        or current_authorization.approved_plan_identity
        != authorization.approved_plan_identity
        or current_authorization.browser_session_id
        != authorization.browser_session_id
        or current_authorization.configured_root_identity
        != authorization.configured_root_identity
        or current_authorization.issued_at != authorization.issued_at
    ):
        raise OrganizationExecutionError("approved_plan_stale")
    if (
        authorization.scan_generation != approved_plan.scan_generation
        or authorization.decision_revision != approved_plan.decision_revision
        or authorization.approved_at != approved_plan.approved_at
        or authorization.approved_plan_identity != approved_plan_identity(approved_plan)
    ):
        raise OrganizationExecutionError("approved_plan_stale")
    if require_available:
        _require_available_authorization(authorization, coordinator)


def _require_available_authorization(
    authorization: MovementAuthorization,
    coordinator: OrganizationExecutionCoordinator,
) -> None:
    current = coordinator.authorization_snapshot(authorization.authorization_id)
    if (
        current is None
        or current != authorization
        or current.status != AUTHORIZATION_AVAILABLE
    ):
        raise OrganizationExecutionError("movement_already_started")


def _preflight(
    approved_plan: ApprovedOrganizationPlan,
    root: Path,
) -> tuple[list[MovePlanItem], tuple[OrganizationExpectedMove, ...]]:
    reviewed = approved_plan.reviewed_move_items
    fingerprints = approved_plan.source_fingerprints
    if len(reviewed) != len(fingerprints):
        raise OrganizationExecutionError("approved_plan_stale")
    if (
        len({item.id for item in reviewed}) != len(reviewed)
        or len({item.item_id for item in fingerprints}) != len(fingerprints)
    ):
        raise OrganizationExecutionError("approved_plan_stale")
    try:
        if find_approved_move_conflicts(list(reviewed), root):
            raise OrganizationExecutionError("approved_plan_stale")
        frozen_sources = {
            validate_under_root(item.plan_item.source.resolve(strict=False), root)
            for item in reviewed
        }
        frozen_destinations = {
            validate_under_root(item.plan_item.destination.resolve(strict=False), root)
            for item in reviewed
        }
    except OrganizationExecutionError:
        raise
    except (OSError, ValueError):
        raise OrganizationExecutionError("approved_plan_stale") from None
    if len(frozen_sources) != len(reviewed) or len(frozen_destinations) != len(reviewed):
        raise OrganizationExecutionError("approved_plan_stale")
    if frozen_sources & frozen_destinations:
        raise OrganizationExecutionError("approved_plan_stale")

    metadata = tuple(scan_directory(root))
    plan_items: list[MovePlanItem] = []
    expected_moves: list[OrganizationExpectedMove] = []
    source_paths: set[Path] = set()
    destination_paths: set[Path] = set()

    for reviewed_item, fingerprint in zip(reviewed, fingerprints):
        _validate_reviewed_item(reviewed_item)
        plan_item = reviewed_item.plan_item
        source = plan_item.source
        destination = plan_item.destination
        if source.is_symlink() or not source.exists() or not source.is_file():
            raise OrganizationExecutionError("source_changed")
        try:
            resolved_source = validate_under_root(source.resolve(), root)
            source_relative = resolved_source.relative_to(root)
            fingerprint_path = _safe_relative_path(fingerprint.relative_path)
            expected_source = (root / fingerprint_path).resolve(strict=False)
            if resolved_source != expected_source:
                raise ValueError("approved source changed")
            stat = source.stat()
        except (OSError, ValueError):
            raise OrganizationExecutionError("source_changed") from None
        if (
            stat.st_size != fingerprint.size_bytes
            or stat.st_mtime != fingerprint.modified_time
        ):
            raise OrganizationExecutionError("source_changed")
        if not evaluate_actionable_source_path(source_relative, metadata).allowed:
            raise OrganizationExecutionError("protected_item_selected")

        try:
            resolved_destination = validate_under_root(
                destination.resolve(strict=False),
                root,
            )
            destination_relative = resolved_destination.relative_to(root)
            existing_parent = _nearest_existing_parent(destination.parent)
            validate_under_root(existing_parent.resolve(), root)
            if not existing_parent.is_dir():
                raise ValueError("destination parent is not a directory")
            if not is_actionable_destination_path(destination_relative, metadata):
                raise ValueError("destination is protected")
        except (OSError, ValueError):
            raise OrganizationExecutionError("unsafe_destination") from None
        if os.path.lexists(destination):
            raise OrganizationExecutionError("overwrite_detected")

        normalized_source = resolved_source.resolve(strict=False)
        normalized_destination = resolved_destination.resolve(strict=False)
        source_paths.add(normalized_source)
        destination_paths.add(normalized_destination)
        plan_items.append(plan_item)
        expected_moves.append(
            OrganizationExpectedMove(
                change_id=reviewed_item.id,
                source=source_relative.as_posix(),
                destination=destination_relative.as_posix(),
                source_size_bytes=fingerprint.size_bytes,
                source_modified_time=fingerprint.modified_time,
                reason=plan_item.reason,
                confidence=plan_item.confidence,
                operation=plan_item.operation,
                overwrite_risk=plan_item.overwrite_risk,
            )
        )

    if source_paths != frozen_sources or destination_paths != frozen_destinations:
        raise OrganizationExecutionError("approved_plan_stale")
    return plan_items, tuple(expected_moves)


def _validate_reviewed_item(item: ReviewedPlanItem) -> None:
    if item.category != CATEGORY_ORGANIZATION or item.decision != DECISION_APPROVED:
        raise OrganizationExecutionError("approved_plan_stale")


def _verify_and_finish(
    record: OrganizationOperationRecord,
    approved_moves: tuple[OrganizationExpectedMove, ...],
    root: Path,
    repository: OrganizationOperationRepository,
) -> OrganizationOperationRecord:
    if not _frozen_moves_match(approved_moves, record.expected_moves):
        final_status = (
            STATUS_PARTIAL_MOVEMENT
            if record.status == STATUS_PARTIAL_MOVEMENT
            else STATUS_VERIFICATION_FAILED
        )
        invalid = replace(
            record,
            status=final_status,
            completed_at=record.completed_at or _utc_timestamp(),
            verification=OrganizationOperationVerification(
                status=VERIFICATION_FAILED,
                mismatch_codes=("durable_move_mismatch",),
            ),
            failure_code=(
                record.failure_code
                if final_status == STATUS_PARTIAL_MOVEMENT
                else "verification_failed"
            ),
        )
        repository.save(invalid)
        return invalid
    items = tuple(
        OrganizationOperationVerificationItem(
            change_id=move.change_id,
            source=move.source,
            destination=move.destination,
            source_size_bytes=move.source_size_bytes,
            source_modified_time=move.source_modified_time,
            result_state=move.result_state,
        )
        for move in record.expected_moves
    )
    try:
        verification = verify_organization_operation(items, root)
    except Exception:
        operation_verification = OrganizationOperationVerification(
            status=VERIFICATION_FAILED,
            mismatch_codes=("verification_unavailable",),
        )
        final_status = (
            STATUS_PARTIAL_MOVEMENT
            if record.status == STATUS_PARTIAL_MOVEMENT
            else STATUS_VERIFICATION_FAILED
        )
    else:
        operation_verification = OrganizationOperationVerification(
            status=(VERIFICATION_PASSED if verification.passed else VERIFICATION_FAILED),
            verified_count=verification.verified_count,
            mismatch_codes=verification.mismatch_codes,
        )
        if record.status == STATUS_PARTIAL_MOVEMENT:
            final_status = STATUS_PARTIAL_MOVEMENT
        else:
            final_status = (
                STATUS_VERIFIED if verification.passed else STATUS_VERIFICATION_FAILED
            )
    completed = replace(
        record,
        status=final_status,
        completed_at=record.completed_at or _utc_timestamp(),
        verification=operation_verification,
        failure_code=(
            record.failure_code
            if final_status == STATUS_PARTIAL_MOVEMENT
            else None
            if final_status == STATUS_VERIFIED
            else "verification_failed"
        ),
    )
    repository.save(completed)
    return completed


def _persist_executor_preflight_failure(
    repository: OrganizationOperationRepository,
    operation_id: str,
) -> OrganizationOperationRecord:
    record = repository.load(operation_id)
    moves = tuple(
        replace(move, result_state=MOVE_UNATTEMPTED)
        if move.result_state == MOVE_PENDING
        else move
        for move in record.expected_moves
    )
    failed = replace(
        record,
        status=STATUS_MOVEMENT_FAILED,
        completed_at=_utc_timestamp(),
        expected_moves=moves,
        failure_code="movement_failed",
    )
    repository.save(failed)
    return failed


def _persist_interrupted(
    repository: OrganizationOperationRepository,
    operation_id: str,
    operation_log: OperationLog | None,
) -> OrganizationOperationRecord:
    record = repository.load(operation_id)
    interrupted = replace(
        record,
        status=STATUS_INTERRUPTED,
        completed_at=_utc_timestamp(),
        legacy_operation_log=(
            _relative(operation_log.log_path, repository.root)
            if operation_log is not None
            else record.legacy_operation_log
        ),
        failure_code="movement_recovery_required",
    )
    repository.save(interrupted)
    return interrupted


def _safe_outcome(record: OrganizationOperationRecord) -> OrganizationOperationOutcome:
    return OrganizationOperationOutcome(
        operation_id=record.operation_id,
        status=record.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
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
    )


def _frozen_moves_match(
    approved: tuple[OrganizationExpectedMove, ...],
    recorded: tuple[OrganizationExpectedMove, ...],
) -> bool:
    if len(approved) != len(recorded):
        return False
    return all(
        (
            left.change_id,
            left.source,
            left.destination,
            left.source_size_bytes,
            left.source_modified_time,
            left.reason,
            left.confidence,
            left.operation,
            left.overwrite_risk,
        )
        == (
            right.change_id,
            right.source,
            right.destination,
            right.source_size_bytes,
            right.source_modified_time,
            right.reason,
            right.confidence,
            right.operation,
            right.overwrite_risk,
        )
        for left, right in zip(approved, recorded)
    )


def _safe_relative_path(path: Path) -> Path:
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe relative path")
    return path


def _relative(path: Path, root: Path) -> str:
    return validate_under_root(path.resolve(strict=False), root).relative_to(root).as_posix()


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
