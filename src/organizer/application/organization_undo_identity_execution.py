from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
from typing import Callable
import uuid

from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_identity_intent import (
    OrganizationUndoExecutorEvidence,
    V2OrganizationUndoIntent,
    validate_v2_organization_undo_parent,
)
from organizer.application.organization_undo_models import (
    ORGANIZATION_UNDO_RECORD_TYPE,
    ORGANIZATION_UNDO_SCHEMA_VERSION_V2,
    UNDO_FAILED,
    UNDO_INTERRUPTED,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_FAILED,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_ITEM_UNATTEMPTED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
    UNDO_VERIFICATION_NOT_RUN,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.executor import (
    MovementRecordingError,
    apply_same_volume_identity_move_plan,
)
from organizer.executor_identity import (
    IdentityMovePlanItem,
    StrictExecutorError,
    StrictExecutorInterrupted,
)
from organizer.file_identity import RegularFileStatFingerprint
from organizer.models import MovePlanItem, MoveResult, OperationLog
from organizer.safety import validate_under_root


class IdentityUndoExecutionState(str, Enum):
    RESTORED_UNVERIFIED = UNDO_RESTORED_UNVERIFIED
    PARTIAL = UNDO_PARTIAL
    FAILED = UNDO_FAILED
    INTERRUPTED = UNDO_INTERRUPTED


@dataclass(frozen=True, repr=False)
class IdentityUndoExecutionResult:
    undo_id: str
    state: IdentityUndoExecutionState
    files_expected: int
    files_restored: int
    files_failed: int
    files_unattempted: int

    def __repr__(self) -> str:
        return (
            "IdentityUndoExecutionResult("
            f"state={self.state.value!r}, files_expected={self.files_expected}, "
            f"files_restored={self.files_restored}, files_failed={self.files_failed}, "
            f"files_unattempted={self.files_unattempted})"
        )


class OrganizationIdentityUndoExecutionError(RuntimeError):
    _CODES = {
        "invalid_intent",
        "existing_undo",
        "durable_intent_failed",
        "execution_failed",
        "recovery_required",
    }

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("identity Undo execution error code is invalid")
        self.code = code
        super().__init__(code)


def execute_v2_organization_undo_intent(
    *,
    intent: V2OrganizationUndoIntent,
    operation_repository: OrganizationOperationRepository,
    undo_repository: OrganizationUndoRepository,
    clock: Callable[[], str] | None = None,
    executor: Callable[..., OperationLog] = apply_same_volume_identity_move_plan,
) -> IdentityUndoExecutionResult:
    """Persist complete v2 Undo intent, then execute its strict reverse batch."""

    now = clock or _utc_timestamp
    root = operation_repository.root
    if undo_repository.root != root:
        raise OrganizationIdentityUndoExecutionError("invalid_intent")
    try:
        parent = operation_repository.load(intent.record.operation_id)
        _validate_execution_intent(
            intent,
            parent,
            operation_repository.configured_root_identity,
        )
        if undo_repository.linked_records(parent.operation_id):
            raise OrganizationIdentityUndoExecutionError("existing_undo")
    except OrganizationIdentityUndoExecutionError:
        raise
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as error:
        raise OrganizationIdentityUndoExecutionError("invalid_intent") from error

    try:
        undo_repository.create(intent.record)
    except Exception as error:
        raise OrganizationIdentityUndoExecutionError("durable_intent_failed") from error

    try:
        plan = _strict_reverse_plan(intent, parent, root)
    except StrictExecutorError:
        failed = _persist_preflight_failure(intent.record, undo_repository, now)
        return _result(failed)

    recorder = _V2UndoMovementRecorder(
        intent.record,
        root,
        undo_repository,
        clock=now,
    )
    try:
        executor(plan, root, recorder=recorder)
    except StrictExecutorInterrupted:
        _try_persist_interrupted(
            undo_repository,
            intent.record.undo_id,
            now,
            operation_log=None,
            recovery_required=True,
        )
        raise
    except StrictExecutorError as error:
        if error.recovery_required:
            interrupted = _persist_interrupted_or_raise(
                undo_repository,
                intent.record.undo_id,
                now,
                operation_log=None,
                recovery_required=True,
                primary=error,
            )
            return _result(interrupted)
        failed = _persist_preflight_failure(intent.record, undo_repository, now)
        return _result(failed)
    except MovementRecordingError as error:
        interrupted = _persist_interrupted_or_raise(
            undo_repository,
            intent.record.undo_id,
            now,
            operation_log=error.operation_log,
            recovery_required=error.filesystem_changed,
            primary=error,
        )
        return _result(interrupted)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        interrupted = _persist_interrupted_or_raise(
            undo_repository,
            intent.record.undo_id,
            now,
            operation_log=None,
            recovery_required=False,
            primary=error,
        )
        return _result(interrupted)

    try:
        completed = undo_repository.load(intent.record.undo_id)
    except Exception as error:
        raise OrganizationIdentityUndoExecutionError("execution_failed") from error
    if completed.status not in {
        UNDO_RESTORED_UNVERIFIED,
        UNDO_PARTIAL,
        UNDO_FAILED,
    }:
        raise OrganizationIdentityUndoExecutionError("execution_failed")
    return _result(completed)


def _validate_execution_intent(
    intent: V2OrganizationUndoIntent,
    parent,
    configured_root_identity: str,
) -> None:
    if not isinstance(intent, V2OrganizationUndoIntent):
        raise ValueError("v2 Undo intent is invalid")
    validate_v2_organization_undo_parent(parent, configured_root_identity)
    record = intent.record
    if (
        not isinstance(record, OrganizationUndoRecord)
        or record.record_type != ORGANIZATION_UNDO_RECORD_TYPE
        or record.schema_version != ORGANIZATION_UNDO_SCHEMA_VERSION_V2
        or record.status != UNDO_PREPARED
        or record.operation_id != parent.operation_id
        or record.configured_root_identity != configured_root_identity
        or not _canonical_uuid(record.undo_id)
        or not _canonical_uuid(record.authorization_id)
        or not isinstance(record.created_at, str)
        or not record.created_at
        or record.started_at is not None
        or record.completed_at is not None
        or record.legacy_undo_log is not None
        or record.failure_code is not None
        or not isinstance(record.verification, UndoVerificationResult)
        or record.verification.status != UNDO_VERIFICATION_NOT_RUN
        or record.verification.verified_count != 0
        or record.verification.mismatch_codes
        or not isinstance(record.expected_restores, tuple)
        or len(record.expected_restores) != len(parent.expected_moves)
        or not isinstance(intent.executor_evidence, tuple)
        or len(intent.executor_evidence) != len(parent.expected_moves)
    ):
        raise ValueError("v2 Undo intent is invalid")

    for move, restore, evidence in zip(
        parent.expected_moves,
        record.expected_restores,
        intent.executor_evidence,
    ):
        if (
            restore.change_id != move.change_id
            or restore.restore_target != move.source
            or restore.moved_from != move.destination
            or restore.baseline_size_bytes != move.source_size_bytes
            or restore.baseline_modified_time != move.source_modified_time
            or restore.result_state != UNDO_ITEM_PENDING
            or restore.failure_code is not None
            or restore.recovery_state is not None
            or restore.content_identity != move.content_identity
            or not isinstance(evidence, OrganizationUndoExecutorEvidence)
            or evidence.change_id != move.change_id
            or evidence.content_identity != move.content_identity
            or not isinstance(evidence.stat_fingerprint, RegularFileStatFingerprint)
        ):
            raise ValueError("v2 Undo item or evidence is invalid")


def _strict_reverse_plan(
    intent: V2OrganizationUndoIntent,
    parent,
    root: Path,
) -> tuple[IdentityMovePlanItem, ...]:
    return tuple(
        IdentityMovePlanItem(
            plan_item=MovePlanItem(
                source=root / restore.moved_from,
                destination=root / restore.restore_target,
                reason="restore organized file",
                confidence=move.confidence,
                operation="move",
                overwrite_risk=False,
            ),
            change_id=restore.change_id,
            expected_digest=restore.content_identity.digest,
            captured_stat=evidence.stat_fingerprint,
        )
        for move, restore, evidence in zip(
            parent.expected_moves,
            intent.record.expected_restores,
            intent.executor_evidence,
        )
    )


class _V2UndoMovementRecorder:
    def __init__(
        self,
        record: OrganizationUndoRecord,
        root: Path,
        repository: OrganizationUndoRepository,
        *,
        clock: Callable[[], str],
    ) -> None:
        self.record = record
        self.root = root
        self.repository = repository
        self._clock = clock

    def before_batch(self, plan_items: tuple[MovePlanItem, ...]) -> None:
        if self.record.status != UNDO_PREPARED:
            raise ValueError("v2 Undo recorder state is invalid")
        self._require_exact_batch(plan_items)
        candidate = replace(
            self.record,
            status=UNDO_UNDOING,
            started_at=self._clock(),
        )
        self.repository.save(candidate)
        self.record = candidate

    def before_move(self, index: int, item: MovePlanItem) -> None:
        expected = self._expected_item(index, item)
        if self.record.status != UNDO_UNDOING or expected.result_state != UNDO_ITEM_PENDING:
            raise ValueError("v2 Undo recorder item state is invalid")
        self._save_item(index, UNDO_ITEM_ATTEMPTING)

    def after_success(
        self,
        index: int,
        item: MovePlanItem,
        result: MoveResult,
    ) -> None:
        expected = self._expected_item(index, item)
        self._require_result(item, result, success=True)
        if expected.result_state != UNDO_ITEM_ATTEMPTING:
            raise ValueError("v2 Undo recorder item state is invalid")
        self._save_item(index, UNDO_ITEM_RESTORED)

    def after_failure(
        self,
        index: int,
        item: MovePlanItem,
        result: MoveResult,
    ) -> None:
        expected = self._expected_item(index, item)
        self._require_result(item, result, success=False)
        if expected.result_state != UNDO_ITEM_ATTEMPTING:
            raise ValueError("v2 Undo recorder item state is invalid")
        failure_code = (
            "undo_destination_occupied"
            if result.message
            == "A file is already in the destination. Check the folder and try again."
            else "undo_failed"
        )
        self._save_item(index, UNDO_ITEM_FAILED, failure_code=failure_code)

    def after_batch(self, operation_log: OperationLog) -> None:
        if self.record.status != UNDO_UNDOING:
            raise ValueError("v2 Undo recorder state is invalid")
        items = tuple(
            replace(item, result_state=UNDO_ITEM_UNATTEMPTED)
            if item.result_state == UNDO_ITEM_PENDING
            else item
            for item in self.record.expected_restores
        )
        restored = sum(item.result_state == UNDO_ITEM_RESTORED for item in items)
        failed = sum(item.result_state == UNDO_ITEM_FAILED for item in items)
        if restored == len(items):
            status = UNDO_RESTORED_UNVERIFIED
            completed_at = None
            failure_code = None
        elif restored and failed:
            status = UNDO_PARTIAL
            completed_at = self._clock()
            failure_code = UNDO_PARTIAL
        elif failed:
            status = UNDO_FAILED
            completed_at = self._clock()
            failure_code = UNDO_FAILED
        else:
            raise ValueError("v2 Undo recorder result is invalid")
        candidate = replace(
            self.record,
            status=status,
            completed_at=completed_at,
            expected_restores=items,
            legacy_undo_log=_relative_operation_log(operation_log, self.root),
            failure_code=failure_code,
        )
        self.repository.save(candidate)
        self.record = candidate

    def _require_exact_batch(self, plan_items: tuple[MovePlanItem, ...]) -> None:
        if not isinstance(plan_items, tuple) or len(plan_items) != len(
            self.record.expected_restores
        ):
            raise ValueError("v2 Undo recorder batch is invalid")
        for index, item in enumerate(plan_items):
            self._expected_item(index, item)

    def _expected_item(self, index: int, item: MovePlanItem):
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(self.record.expected_restores)
            or not isinstance(item, MovePlanItem)
        ):
            raise ValueError("v2 Undo recorder item is invalid")
        expected = self.record.expected_restores[index]
        if (
            Path(os.path.abspath(item.source))
            != self.root / expected.moved_from
            or Path(os.path.abspath(item.destination))
            != self.root / expected.restore_target
        ):
            raise ValueError("v2 Undo recorder item is invalid")
        return expected

    def _require_result(
        self,
        item: MovePlanItem,
        result: MoveResult,
        *,
        success: bool,
    ) -> None:
        if (
            not isinstance(result, MoveResult)
            or result.success is not success
            or Path(os.path.abspath(result.source))
            != Path(os.path.abspath(item.source))
            or Path(os.path.abspath(result.destination))
            != Path(os.path.abspath(item.destination))
        ):
            raise ValueError("v2 Undo recorder result is invalid")

    def _save_item(
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
            recovery_state=None,
        )
        candidate = replace(self.record, expected_restores=tuple(items))
        self.repository.save(candidate)
        self.record = candidate


def _persist_preflight_failure(
    record: OrganizationUndoRecord,
    repository: OrganizationUndoRepository,
    clock: Callable[[], str],
) -> OrganizationUndoRecord:
    try:
        current = repository.load(record.undo_id)
        failed = replace(
            current,
            status=UNDO_FAILED,
            completed_at=clock(),
            expected_restores=tuple(
                replace(item, result_state=UNDO_ITEM_UNATTEMPTED)
                if item.result_state == UNDO_ITEM_PENDING
                else item
                for item in current.expected_restores
            ),
            failure_code=UNDO_FAILED,
        )
        repository.save(failed)
        return failed
    except Exception as error:
        raise OrganizationIdentityUndoExecutionError("execution_failed") from error


def _persist_interrupted_or_raise(
    repository: OrganizationUndoRepository,
    undo_id: str,
    clock: Callable[[], str],
    *,
    operation_log: OperationLog | None,
    recovery_required: bool,
    primary: BaseException,
) -> OrganizationUndoRecord:
    try:
        return _persist_interrupted(
            repository,
            undo_id,
            clock,
            operation_log=operation_log,
            recovery_required=recovery_required,
        )
    except Exception:
        code = "recovery_required" if recovery_required else "execution_failed"
        raise OrganizationIdentityUndoExecutionError(code) from primary


def _try_persist_interrupted(
    repository: OrganizationUndoRepository,
    undo_id: str,
    clock: Callable[[], str],
    *,
    operation_log: OperationLog | None,
    recovery_required: bool,
) -> None:
    try:
        _persist_interrupted(
            repository,
            undo_id,
            clock,
            operation_log=operation_log,
            recovery_required=recovery_required,
        )
    except Exception:
        pass


def _persist_interrupted(
    repository: OrganizationUndoRepository,
    undo_id: str,
    clock: Callable[[], str],
    *,
    operation_log: OperationLog | None,
    recovery_required: bool,
) -> OrganizationUndoRecord:
    current = repository.load(undo_id)
    relative_log = (
        _relative_operation_log(operation_log, repository.root)
        if operation_log is not None
        else current.legacy_undo_log
    )
    interrupted = replace(
        current,
        status=UNDO_INTERRUPTED,
        completed_at=clock(),
        legacy_undo_log=relative_log,
        failure_code=(
            "undo_recovery_required" if recovery_required else "undo_recording_failed"
        ),
    )
    repository.save(interrupted)
    return interrupted


def _relative_operation_log(operation_log: OperationLog, root: Path) -> str:
    if not isinstance(operation_log, OperationLog):
        raise ValueError("operation log is invalid")
    path = Path(operation_log.log_path)
    if path.is_symlink():
        raise ValueError("operation log is unsafe")
    resolved = path.resolve(strict=True)
    validate_under_root(resolved, root)
    if not resolved.is_file():
        raise ValueError("operation log is unsafe")
    return resolved.relative_to(root).as_posix()


def _result(record: OrganizationUndoRecord) -> IdentityUndoExecutionResult:
    try:
        state = IdentityUndoExecutionState(record.status)
    except ValueError as error:
        raise OrganizationIdentityUndoExecutionError("execution_failed") from error
    return IdentityUndoExecutionResult(
        undo_id=record.undo_id,
        state=state,
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
    )


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
