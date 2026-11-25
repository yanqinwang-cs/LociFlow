from __future__ import annotations

import os
import stat
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from organizer.application.duplicate_bin_models import (
    BATCH_COMPLETED,
    ApprovedDuplicateBatch,
    SelectedDuplicateBinItem,
    canonical_digest,
)
from organizer.application.duplicate_bin_models import (
    ITEM_ATTENTION as BIN_ITEM_ATTENTION,
)
from organizer.application.duplicate_bin_models import (
    ITEM_AVAILABLE as BIN_ITEM_AVAILABLE,
)
from organizer.application.duplicate_bin_models import (
    ITEM_RESTORED as BIN_ITEM_RESTORED,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_VERIFIED,
    DuplicateBinExpectedMove,
    DuplicateBinOperationRecord,
)
from organizer.application.duplicate_bin_operation_repository import (
    DuplicateBinOperationRepository,
)
from organizer.application.duplicate_bin_repository import (
    DuplicateBinRepository,
    DuplicateBinRepositoryError,
)
from organizer.application.duplicate_bin_restore_models import (
    AUTH_AVAILABLE,
    ITEM_ATTEMPTING,
    ITEM_ATTENTION,
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_RESTORED,
    ITEM_UNATTEMPTED,
    ITEM_VERIFIED,
    RESTORE_FAILED,
    RESTORE_INTERRUPTED,
    RESTORE_PARTIAL,
    RESTORE_PREPARED,
    RESTORE_RESTORED,
    RESTORE_RESTORED_UNVERIFIED,
    RESTORE_RESTORING,
    RESTORE_TERMINAL,
    RESTORE_VERIFICATION_FAILED,
    DuplicateBinRestoreAuthorization,
    DuplicateBinRestoreItem,
    DuplicateBinRestoreOperation,
    DuplicateBinRestorePreview,
    DuplicateBinRestoreProjection,
    DuplicateBinRestoreVerification,
    RestoreAuthorizationItem,
    SafeDuplicateBinItem,
)
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
    DuplicateBinRestoreRepositoryError,
)
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationInspector,
)
from organizer.application.runtime_lifecycle import RuntimeOperationKind
from organizer.duplicates import hash_file
from organizer.executor import MovementRecordingError, apply_move_plan
from organizer.models import MovePlanItem, MoveResult, OperationLog
from organizer.safety import validate_under_root
from organizer.scope import (
    DUPLICATE_BIN_ITEMS_RELATIVE_ROOT,
    is_protected_context_path,
)

CONFIRM_RESTORE_COPIES = "RESTORE COPIES"


class DuplicateBinRestoreError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _ResolvedItem:
    batch: ApprovedDuplicateBatch
    item: SelectedDuplicateBinItem
    movement: DuplicateBinOperationRecord
    move: DuplicateBinExpectedMove


def preview_duplicate_bin_restore(
    *,
    item_ids: tuple[str, ...],
    browser_session_id: str,
    batch_repository: DuplicateBinRepository,
    movement_repository: DuplicateBinOperationRepository,
    restore_repository: DuplicateBinRestoreRepository,
    coordinator: OrganizationExecutionCoordinator,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    issued_at: str | None = None,
) -> DuplicateBinRestorePreview:
    _selected_ids(item_ids)
    with coordinator.runtime_lifecycle.begin_operation(
        RuntimeOperationKind.STATE_MUTATION
    ):
        if not coordinator.try_acquire():
            raise DuplicateBinRestoreError("movement_or_undo_in_progress")
        try:
            return _preview_duplicate_bin_restore_locked(
                item_ids=item_ids,
                browser_session_id=browser_session_id,
                batch_repository=batch_repository,
                movement_repository=movement_repository,
                restore_repository=restore_repository,
                filesystem_operation_admission=filesystem_operation_admission,
                issued_at=issued_at,
            )
        finally:
            coordinator.release()


def _preview_duplicate_bin_restore_locked(
    *,
    item_ids: tuple[str, ...],
    browser_session_id: str,
    batch_repository: DuplicateBinRepository,
    movement_repository: DuplicateBinOperationRepository,
    restore_repository: DuplicateBinRestoreRepository,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    issued_at: str | None = None,
) -> DuplicateBinRestorePreview:
    selected = _selected_ids(item_ids)
    resolved = tuple(
        _resolve_restorable(
            item_id,
            batch_repository,
            movement_repository,
            restore_repository,
        )
        for item_id in selected
    )
    authorization_items = tuple(
        sorted(
            (
                RestoreAuthorizationItem(
                    bin_item_id=item.item.bin_item_id,
                    movement_operation_id=item.movement.operation_id,
                    batch_id=item.batch.batch_id,
                    batch_version=item.batch.batch_version,
                    item_state=item.item.state,
                )
                for item in resolved
            ),
            key=lambda item: item.bin_item_id,
        )
    )
    admission = filesystem_operation_admission or FilesystemOperationAdmission(
        FilesystemOperationInspector(batch_repository.root)
    )
    if admission.root != batch_repository.root:
        raise DuplicateBinRestoreError("restore_authorization_stale")
    admission.inspect_and_require_clear()
    try:
        authorization = restore_repository.issue_authorization(
            authorization_items,
            browser_session_id=browser_session_id,
            issued_at=issued_at,
        )
    except DuplicateBinRestoreRepositoryError as error:
        raise DuplicateBinRestoreError(error.code) from error
    return DuplicateBinRestorePreview(
        copies_to_restore=len(selected),
        confirmation=CONFIRM_RESTORE_COPIES,
        confirmation_token=authorization.authorization_id,
        expires_at=authorization.expires_at,
    )


def preview_duplicate_bin_activity_undo(
    *,
    movement_operation_id: str,
    browser_session_id: str,
    batch_repository: DuplicateBinRepository,
    movement_repository: DuplicateBinOperationRepository,
    restore_repository: DuplicateBinRestoreRepository,
    coordinator: OrganizationExecutionCoordinator,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    issued_at: str | None = None,
) -> DuplicateBinRestorePreview:
    with coordinator.runtime_lifecycle.begin_operation(
        RuntimeOperationKind.STATE_MUTATION
    ):
        if not coordinator.try_acquire():
            raise DuplicateBinRestoreError("movement_or_undo_in_progress")
        try:
            try:
                movement = movement_repository.load(movement_operation_id)
            except ValueError as error:
                raise DuplicateBinRestoreError("operation_not_found") from error
            item_ids = tuple(
                sorted(
                    move.bin_item_id
                    for move in movement.expected_moves
                    if _is_restorable(
                        move.bin_item_id,
                        batch_repository,
                        movement_repository,
                        restore_repository,
                    )
                )
            )
            if not item_ids:
                raise DuplicateBinRestoreError("no_restorable_items")
            return _preview_duplicate_bin_restore_locked(
                item_ids=item_ids,
                browser_session_id=browser_session_id,
                batch_repository=batch_repository,
                movement_repository=movement_repository,
                restore_repository=restore_repository,
                filesystem_operation_admission=filesystem_operation_admission,
                issued_at=issued_at,
            )
        finally:
            coordinator.release()


def execute_duplicate_bin_restore(
    *,
    browser_session_id: str,
    confirmation_token: str,
    confirmation: str,
    batch_repository: DuplicateBinRepository,
    movement_repository: DuplicateBinOperationRepository,
    restore_repository: DuplicateBinRestoreRepository,
    coordinator: OrganizationExecutionCoordinator,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    now: str | None = None,
    runtime_operation_kind: RuntimeOperationKind = (
        RuntimeOperationKind.DUPLICATE_BIN_RESTORE
    ),
) -> DuplicateBinRestoreProjection:
    _uuid(confirmation_token, "invalid_confirmation_token")
    if confirmation != CONFIRM_RESTORE_COPIES:
        raise DuplicateBinRestoreError("invalid_restore_confirmation")
    authorization = _authorization(
        confirmation_token,
        browser_session_id,
        restore_repository,
        batch_repository,
    )
    if not coordinator.try_acquire(runtime_operation_kind):
        raise DuplicateBinRestoreError("movement_or_undo_in_progress")
    try:
        authorization = _authorization(
            confirmation_token,
            browser_session_id,
            restore_repository,
            batch_repository,
        )
        resolved = None
        current = None
        if authorization.status == AUTH_AVAILABLE:
            current = _time(now or _utc())
            if current >= _time(authorization.expires_at):
                raise DuplicateBinRestoreError("confirmation_expired")
            resolved = tuple(
                _resolve_restorable(
                    item.bin_item_id,
                    batch_repository,
                    movement_repository,
                    restore_repository,
                )
                for item in authorization.items
            )
            _validate_authorization_items(authorization, resolved)

        admission = filesystem_operation_admission or FilesystemOperationAdmission(
            FilesystemOperationInspector(batch_repository.root)
        )
        if admission.root != batch_repository.root:
            raise DuplicateBinRestoreError("restore_authorization_stale")
        admission.inspect_and_require_clear()

        existing = restore_repository.find_for_authorization(confirmation_token)
        if existing is not None:
            return _reconcile_existing(existing, batch_repository)
        if authorization.status != AUTH_AVAILABLE:
            raise DuplicateBinRestoreError("invalid_confirmation_token")
        assert current is not None
        assert resolved is not None
        plan, restore_items, digest = _preflight(resolved, batch_repository.root)
        created_at = current.isoformat()
        operation = DuplicateBinRestoreOperation(
            restore_operation_id=str(uuid.uuid4()),
            authorization_id=authorization.authorization_id,
            configured_root_identity=batch_repository.configured_root_identity,
            selected_bin_item_ids=tuple(item.bin_item_id for item in restore_items),
            parent_movement_operation_ids=tuple(
                sorted({item.movement_operation_id for item in restore_items})
            ),
            items=restore_items,
            preflight_digest=digest,
            status=RESTORE_PREPARED,
            created_at=created_at,
            started_at=None,
            completed_at=None,
            verification=DuplicateBinRestoreVerification(),
            executor_log_path=None,
            failure_code=None,
            recovery_state=None,
        )
        try:
            restore_repository.create_operation(operation)
            restore_repository.consume_authorization(
                authorization.authorization_id,
                operation.restore_operation_id,
            )
        except (DuplicateBinRestoreRepositoryError, ValueError) as error:
            existing = restore_repository.find_for_authorization(
                authorization.authorization_id
            )
            if existing is not None:
                return _reconcile_existing(existing, batch_repository)
            raise DuplicateBinRestoreError("restore_recovery_required") from error

        recorder = _RestoreRecorder(operation, restore_repository)
        try:
            apply_move_plan(
                plan,
                batch_repository.root,
                log_dir_name="duplicate_bin/restores/operation_logs",
                recorder=recorder,
            )
        except MovementRecordingError as error:
            interrupted = _interrupted(
                restore_repository.load_operation(operation.restore_operation_id),
                "durable_recording_failed",
                _relative(error.operation_log.log_path, batch_repository.root)
                if error.operation_log is not None
                else None,
            )
            restore_repository.save_operation(interrupted)
            _update_batch_items(batch_repository, interrupted)
            return _project(interrupted)
        except Exception:
            interrupted = _interrupted(
                restore_repository.load_operation(operation.restore_operation_id),
                "executor_interrupted",
                None,
            )
            restore_repository.save_operation(interrupted)
            _update_batch_items(batch_repository, interrupted)
            return _project(interrupted)

        moved = restore_repository.load_operation(operation.restore_operation_id)
        finished = _verify(moved, batch_repository.root)
        restore_repository.save_operation(finished)
        _update_batch_items(batch_repository, finished)
        return _project(finished)
    finally:
        coordinator.release()


def authorization_matches_movement(
    authorization_id: str,
    movement_operation_id: str,
    restore_repository: DuplicateBinRestoreRepository,
) -> bool:
    try:
        authorization = restore_repository.load_authorization(authorization_id)
    except DuplicateBinRestoreRepositoryError:
        return False
    return authorization.parent_movement_operation_ids == (movement_operation_id,)


def restorable_item_ids_for_movement(
    movement_operation_id: str,
    batch_repository: DuplicateBinRepository,
    movement_repository: DuplicateBinOperationRepository,
    restore_repository: DuplicateBinRestoreRepository,
) -> tuple[str, ...]:
    try:
        movement = movement_repository.load(movement_operation_id)
    except ValueError:
        return ()
    return tuple(
        sorted(
            item.bin_item_id
            for item in movement.expected_moves
            if _is_restorable(
                item.bin_item_id,
                batch_repository,
                movement_repository,
                restore_repository,
            )
        )
    )


def list_duplicate_bin_items(
    batch_repository: DuplicateBinRepository,
    movement_repository: DuplicateBinOperationRepository,
    restore_repository: DuplicateBinRestoreRepository,
) -> tuple[SafeDuplicateBinItem, ...]:
    movement_by_batch = {
        operation.batch_id: operation
        for operation in movement_repository.list_records()
    }
    result: list[SafeDuplicateBinItem] = []
    for batch in batch_repository.list_batches():
        movement = movement_by_batch.get(batch.batch_id)
        if movement is None or movement.started_at is None:
            continue
        for item in batch.items:
            if item.state not in {BIN_ITEM_AVAILABLE, BIN_ITEM_ATTENTION}:
                continue
            physical = batch_repository.root / item.future_relative_path
            if not os.path.lexists(physical):
                continue
            available = _is_restorable(
                item.bin_item_id,
                batch_repository,
                movement_repository,
                restore_repository,
            )
            previous_parent = Path(item.previous_relative_path).parent
            result.append(
                SafeDuplicateBinItem(
                    id=item.bin_item_id,
                    name=item.basename,
                    previous_folder=(
                        previous_parent.as_posix()
                        if previous_parent != Path(".")
                        else "Selected folder"
                    ),
                    moved_at=movement.completed_at or movement.started_at,
                    size=item.size_bytes,
                    kind=item.kind,
                    restore_available=available,
                    attention_message=(
                        None if available else "This copy needs attention."
                    ),
                )
            )
    return tuple(sorted(result, key=lambda item: (item.moved_at, item.id)))


def _resolve_restorable(
    bin_item_id: str,
    batches: DuplicateBinRepository,
    movements: DuplicateBinOperationRepository,
    restores: DuplicateBinRestoreRepository,
) -> _ResolvedItem:
    _uuid(bin_item_id, "invalid_bin_item_id")
    found: tuple[ApprovedDuplicateBatch, SelectedDuplicateBinItem] | None = None
    for batch in batches.list_batches():
        item = next(
            (item for item in batch.items if item.bin_item_id == bin_item_id), None
        )
        if item is not None:
            found = (batch, item)
            break
    if found is None:
        raise DuplicateBinRestoreError("bin_item_not_found")
    batch, item = found
    movement = movements.find_for_batch(batch.batch_id)
    if movement is None:
        raise DuplicateBinRestoreError("bin_item_not_restorable")
    move = next(
        (move for move in movement.expected_moves if move.bin_item_id == bin_item_id),
        None,
    )
    if (
        move is None
        or move.result_state != MOVE_VERIFIED
        or item.state != BIN_ITEM_AVAILABLE
    ):
        raise DuplicateBinRestoreError("bin_item_not_restorable")
    if restores.find_for_item(bin_item_id) is not None:
        raise DuplicateBinRestoreError("bin_item_already_owned")
    source = batches.root / item.future_relative_path
    if not os.path.lexists(source):
        raise DuplicateBinRestoreError("bin_item_missing")
    return _ResolvedItem(batch, item, movement, move)


def _is_restorable(bin_item_id, batches, movements, restores) -> bool:
    try:
        _resolve_restorable(bin_item_id, batches, movements, restores)
        return True
    except (DuplicateBinRestoreError, DuplicateBinRepositoryError, ValueError):
        return False


def _preflight(
    resolved: tuple[_ResolvedItem, ...], root: Path
) -> tuple[list[MovePlanItem], tuple[DuplicateBinRestoreItem, ...], str]:
    ordered = tuple(
        sorted(
            resolved,
            key=lambda item: (
                item.movement.completed_at
                or item.movement.started_at
                or item.movement.created_at,
                item.item.bin_item_id,
            ),
        )
    )
    restore_items: list[DuplicateBinRestoreItem] = []
    plan: list[MovePlanItem] = []
    evidence: list[dict[str, object]] = []
    for resolved_item in ordered:
        item, move = resolved_item.item, resolved_item.move
        source = root / item.future_relative_path
        _validate_protected_source(source, root, item.bin_item_id, item.basename)
        live = _stable_hash(source, root, "bin_item_changed")
        if (
            live["size_bytes"] != move.source_size_bytes
            or live["sha256"] != move.expected_sha256
        ):
            raise DuplicateBinRestoreError("bin_item_changed")
        destination = root / item.previous_relative_path
        parents = _validate_previous_destination(destination, root)
        restore_items.append(
            DuplicateBinRestoreItem(
                bin_item_id=item.bin_item_id,
                movement_operation_id=resolved_item.movement.operation_id,
                batch_id=resolved_item.batch.batch_id,
                protected_source=item.future_relative_path,
                previous_destination=item.previous_relative_path,
                expected_sha256=move.expected_sha256,
                expected_size_bytes=move.source_size_bytes,
                source_modified_time_ns=int(live["modified_time_ns"]),
                validated_parent_directories=parents,
            )
        )
        plan.append(
            MovePlanItem(
                source=source,
                destination=destination,
                reason="Restore approved Duplicate Bin copy",
                confidence=100,
                operation="move",
                overwrite_risk=False,
            )
        )
        evidence.append(
            {
                "bin_item_id": item.bin_item_id,
                "movement_operation_id": resolved_item.movement.operation_id,
                "source": item.future_relative_path,
                "destination": item.previous_relative_path,
                **live,
                "validated_parent_directories": list(parents),
            }
        )
    digest = canonical_digest(
        {
            "schema": "loci-duplicate-bin-restore-preflight-v1",
            "selected_bin_item_ids": [item.bin_item_id for item in restore_items],
            "items": evidence,
        }
    )
    return plan, tuple(restore_items), digest


class _RestoreRecorder:
    def __init__(self, record, repository) -> None:
        self.record = record
        self.repository = repository

    def before_batch(self, plan_items: tuple[MovePlanItem, ...]) -> None:
        if len(plan_items) != len(self.record.items):
            raise ValueError("restore plan changed")
        self.record = replace(self.record, status=RESTORE_RESTORING)
        self.repository.save_operation(self.record)

    def before_move(self, index: int, item: MovePlanItem) -> None:
        stamp = _utc()
        self._item(index, ITEM_ATTEMPTING, attempted_at=stamp)

    def after_success(self, index: int, item: MovePlanItem, result: MoveResult) -> None:
        self._item(index, ITEM_RESTORED, completed_at=_utc())

    def after_failure(self, index: int, item: MovePlanItem, result: MoveResult) -> None:
        self._item(index, ITEM_FAILED, "restore_failed", completed_at=_utc())

    def after_batch(self, operation_log: OperationLog) -> None:
        stamp = _utc()
        items = tuple(
            replace(item, state=ITEM_UNATTEMPTED, completed_at=stamp)
            if item.state == ITEM_PENDING
            else item
            for item in self.record.items
        )
        restored = sum(item.state == ITEM_RESTORED for item in items)
        status = (
            RESTORE_RESTORED_UNVERIFIED
            if restored == len(items)
            else RESTORE_PARTIAL
            if restored
            else RESTORE_FAILED
        )
        self.record = replace(
            self.record,
            status=status,
            completed_at=None if status == RESTORE_RESTORED_UNVERIFIED else stamp,
            items=items,
            executor_log_path=_relative(operation_log.log_path, self.repository.root),
            failure_code=None
            if status == RESTORE_RESTORED_UNVERIFIED
            else "restore_incomplete",
        )
        self.repository.save_operation(self.record)

    def _item(
        self, index, state, failure=None, *, attempted_at=None, completed_at=None
    ):
        items = list(self.record.items)
        items[index] = replace(
            items[index],
            state=state,
            attempted_at=attempted_at or items[index].attempted_at,
            completed_at=completed_at,
            failure_code=failure,
        )
        self.record = replace(
            self.record,
            started_at=self.record.started_at or attempted_at,
            items=tuple(items),
        )
        self.repository.save_operation(self.record)


def _verify(
    record: DuplicateBinRestoreOperation, root: Path
) -> DuplicateBinRestoreOperation:
    items = list(record.items)
    mismatches: list[str] = []
    verified = 0
    for index, item in enumerate(items):
        if item.state != ITEM_RESTORED:
            continue
        source = root / item.protected_source
        destination = root / item.previous_destination
        code = None
        if os.path.lexists(source):
            code = "protected_source_still_present"
        else:
            try:
                _validate_restored_destination(destination, root)
                live = _stable_hash(destination, root, "restored_copy_changed")
                if live["size_bytes"] != item.expected_size_bytes:
                    code = "restored_size_changed"
                elif live["sha256"] != item.expected_sha256:
                    code = "restored_hash_changed"
            except (OSError, ValueError, DuplicateBinRestoreError):
                code = "restored_destination_missing_or_unsafe"
        if code is None:
            items[index] = replace(item, state=ITEM_VERIFIED)
            verified += 1
        else:
            items[index] = replace(item, state=ITEM_ATTENTION, failure_code=code)
            mismatches.append(f"{code}:{item.bin_item_id}")
    failed = sum(item.state == ITEM_FAILED for item in items)
    unattempted = sum(item.state == ITEM_UNATTEMPTED for item in items)
    if verified == len(items):
        status = RESTORE_RESTORED
    elif verified and (failed or unattempted):
        status = RESTORE_PARTIAL
    elif any(item.state == ITEM_ATTENTION for item in items):
        status = RESTORE_VERIFICATION_FAILED
    else:
        status = RESTORE_FAILED
    return replace(
        record,
        status=status,
        completed_at=_utc(),
        items=tuple(items),
        verification=DuplicateBinRestoreVerification(
            status="passed" if status == RESTORE_RESTORED else "failed",
            verified_count=verified,
            mismatch_codes=tuple(sorted(mismatches)),
        ),
        failure_code=None if status == RESTORE_RESTORED else "restore_incomplete",
    )


def _update_batch_items(repository: DuplicateBinRepository, operation) -> None:
    states = {item.bin_item_id: item.state for item in operation.items}
    for durable_batch_id in sorted({item.batch_id for item in operation.items}):
        batch = repository.load(durable_batch_id)
        updated = tuple(
            replace(
                item,
                state=(
                    BIN_ITEM_RESTORED
                    if states.get(item.bin_item_id) == ITEM_VERIFIED
                    else BIN_ITEM_ATTENTION
                    if item.bin_item_id in states
                    else item.state
                ),
            )
            for item in batch.items
        )
        status = (
            BATCH_COMPLETED
            if all(item.state == BIN_ITEM_RESTORED for item in updated)
            else batch.status
        )
        if updated == batch.items and status == batch.status:
            continue
        repository.save_execution_state(
            batch.batch_id,
            expected_version=batch.batch_version,
            status=status,
            items=updated,
            updated_at=operation.completed_at or _utc(),
        )


def _reconcile_existing(
    operation: DuplicateBinRestoreOperation,
    batch_repository: DuplicateBinRepository,
) -> DuplicateBinRestoreProjection:
    if operation.status in RESTORE_TERMINAL or operation.status == RESTORE_INTERRUPTED:
        _update_batch_items(batch_repository, operation)
    return _project(operation)


def _interrupted(record, failure_code, executor_log_path):
    stamp = _utc()
    items = []
    for item in record.items:
        if item.state == ITEM_PENDING:
            item = replace(
                item,
                state=ITEM_UNATTEMPTED,
                completed_at=stamp,
                recovery_state="recovery_required",
            )
        elif item.state in {ITEM_ATTEMPTING, ITEM_RESTORED}:
            item = replace(
                item,
                state=ITEM_ATTENTION,
                completed_at=stamp,
                failure_code=failure_code,
                recovery_state="recovery_required",
            )
        items.append(item)
    return replace(
        record,
        status=RESTORE_INTERRUPTED,
        items=tuple(items),
        completed_at=stamp,
        executor_log_path=executor_log_path or record.executor_log_path,
        failure_code=failure_code,
        recovery_state="recovery_required",
    )


def _authorization(
    token, session, restores, batches
) -> DuplicateBinRestoreAuthorization:
    try:
        authorization = restores.load_authorization(token)
    except DuplicateBinRestoreRepositoryError as error:
        raise DuplicateBinRestoreError(error.code) from error
    if (
        authorization.browser_session_id != session
        or authorization.configured_root_identity != batches.configured_root_identity
    ):
        raise DuplicateBinRestoreError("invalid_confirmation_token")
    return authorization


def _validate_authorization_items(authorization, resolved) -> None:
    actual = tuple(
        sorted(
            (
                item.item.bin_item_id,
                item.movement.operation_id,
                item.batch.batch_id,
                item.batch.batch_version,
                item.item.state,
            )
            for item in resolved
        )
    )
    expected = tuple(
        (
            item.bin_item_id,
            item.movement_operation_id,
            item.batch_id,
            item.batch_version,
            item.item_state,
        )
        for item in authorization.items
    )
    if actual != expected:
        raise DuplicateBinRestoreError("restore_authorization_stale")


def _selected_ids(item_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not item_ids:
        raise DuplicateBinRestoreError("no_restorable_items")
    selected = tuple(sorted(_uuid(item, "invalid_bin_item_id") for item in item_ids))
    if len(set(selected)) != len(selected):
        raise DuplicateBinRestoreError("duplicate_restore_item")
    return selected


def _validate_protected_source(
    path: Path, root: Path, item_id: str, basename: str
) -> None:
    expected = root / DUPLICATE_BIN_ITEMS_RELATIVE_ROOT / item_id / basename
    if path != expected:
        raise DuplicateBinRestoreError("unsafe_bin_item")
    _validate_existing_components(path, root, final_regular=True)


def _validate_previous_destination(destination: Path, root: Path) -> tuple[str, ...]:
    validate_under_root(destination.resolve(strict=False), root)
    relative = destination.relative_to(root)
    if is_protected_context_path(relative):
        raise DuplicateBinRestoreError("unsafe_restore_destination")
    if os.path.lexists(destination):
        raise DuplicateBinRestoreError("restore_destination_occupied")
    current = root
    missing: list[str] = []
    for part in relative.parent.parts:
        parent = current
        current = current / part
        if os.path.lexists(current):
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise DuplicateBinRestoreError("unsafe_restore_parent")
        else:
            _reject_equivalent_child(parent, part)
            missing.append(current.relative_to(root).as_posix())
    nearest = current
    while not nearest.exists():
        nearest = nearest.parent
    validate_under_root(nearest.resolve(strict=True), root)
    if nearest.is_symlink() or not nearest.is_dir():
        raise DuplicateBinRestoreError("unsafe_restore_parent")
    _reject_equivalent_child(destination.parent, destination.name)
    _path_lengths(destination, root)
    return tuple(missing)


def _validate_restored_destination(destination: Path, root: Path) -> None:
    relative = destination.relative_to(root)
    if is_protected_context_path(relative):
        raise DuplicateBinRestoreError("unsafe_restore_destination")
    _validate_existing_components(destination, root, final_regular=True)


def _validate_existing_components(
    path: Path, root: Path, *, final_regular: bool
) -> None:
    validate_under_root(path.resolve(strict=True), root)
    relative = path.relative_to(root)
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            raise DuplicateBinRestoreError("unsafe_restore_path")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise DuplicateBinRestoreError("unsafe_restore_path")
    if final_regular and not stat.S_ISREG(os.lstat(path).st_mode):
        raise DuplicateBinRestoreError("unsafe_restore_path")


def _stable_hash(path: Path, root: Path, code: str) -> dict[str, object]:
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise ValueError("not a regular file")
        validate_under_root(path.resolve(strict=True), root)
        digest = hash_file(path)
        after = os.lstat(path)
    except FileNotFoundError as error:
        raise DuplicateBinRestoreError("bin_item_missing") from error
    except (OSError, ValueError) as error:
        raise DuplicateBinRestoreError(code) from error
    fields = ("st_mode", "st_size", "st_mtime_ns", "st_dev", "st_ino")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise DuplicateBinRestoreError("restore_item_changed_during_hash")
    return {
        "size_bytes": before.st_size,
        "modified_time_ns": before.st_mtime_ns,
        "device": before.st_dev,
        "inode": before.st_ino,
        "sha256": digest,
    }


def _reject_equivalent_child(parent: Path, name: str) -> None:
    if not parent.exists() or not parent.is_dir():
        return
    normalized = unicodedata.normalize("NFC", name).casefold()
    for child in parent.iterdir():
        if unicodedata.normalize("NFC", child.name).casefold() == normalized:
            raise DuplicateBinRestoreError("restore_destination_occupied")


def _path_lengths(destination: Path, root: Path) -> None:
    try:
        name_max = os.pathconf(root, "PC_NAME_MAX")
        path_max = os.pathconf(root, "PC_PATH_MAX")
    except (OSError, ValueError):
        return
    if name_max >= 0 and len(os.fsencode(destination.name)) > name_max:
        raise DuplicateBinRestoreError("unsafe_restore_destination")
    if path_max >= 0 and len(os.fsencode(destination)) > path_max:
        raise DuplicateBinRestoreError("unsafe_restore_destination")


def _project(record: DuplicateBinRestoreOperation) -> DuplicateBinRestoreProjection:
    restored = sum(
        item.state in {ITEM_RESTORED, ITEM_VERIFIED} for item in record.items
    )
    failed = sum(item.state in {ITEM_FAILED, ITEM_ATTENTION} for item in record.items)
    unattempted = sum(
        item.state in {ITEM_PENDING, ITEM_UNATTEMPTED} for item in record.items
    )
    return DuplicateBinRestoreProjection(
        id=record.restore_operation_id,
        status=record.status,
        copies_expected=len(record.items),
        copies_restored=restored,
        copies_failed=failed,
        copies_unattempted=unattempted,
        verification_status=record.verification.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
        recovery_required=record.status not in RESTORE_TERMINAL,
    )


def _relative(path: Path, root: Path) -> str:
    return (
        validate_under_root(path.resolve(strict=False), root)
        .relative_to(root)
        .as_posix()
    )


def _uuid(value: str, code: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise DuplicateBinRestoreError(code) from error
    if str(parsed) != value:
        raise DuplicateBinRestoreError(code)
    return value


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DuplicateBinRestoreError("invalid_authorization_time")
    return parsed


def _utc() -> str:
    return datetime.now(UTC).isoformat()
