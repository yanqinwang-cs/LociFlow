from __future__ import annotations

import os
import stat
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from organizer.application.duplicate_bin_authorization_repository import (
    DuplicateBinAuthorizationError,
    DuplicateBinAuthorizationRepository,
    approved_batch_digest,
)
from organizer.application.duplicate_bin_models import (
    BATCH_EXECUTING,
    BATCH_INTERRUPTED,
    BATCH_MOVED,
    BATCH_MOVEMENT_FAILED,
    BATCH_PARTIALLY_MOVED,
    BATCH_VERIFICATION_FAILED,
    ITEM_ATTENTION,
    ITEM_AVAILABLE,
    ITEM_MOVING,
    ITEM_UNATTEMPTED,
    ApprovedDuplicateBatch,
    SelectedDuplicateBinItem,
    canonical_digest,
)
from organizer.application.duplicate_bin_movement_models import (
    AUTH_AVAILABLE,
    MOVE_ATTEMPTING,
    MOVE_ATTENTION,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    MOVE_VERIFIED,
    OP_INTERRUPTED,
    OP_MOVED,
    OP_MOVED_UNVERIFIED,
    OP_MOVEMENT_FAILED,
    OP_MOVING,
    OP_PARTIAL,
    OP_PREPARED,
    OP_TERMINAL_STATUSES,
    OP_VERIFICATION_FAILED,
    DuplicateBinExpectedMove,
    DuplicateBinMovementAuthorization,
    DuplicateBinMovementPreview,
    DuplicateBinOperationProjection,
    DuplicateBinOperationRecord,
    DuplicateBinVerification,
)
from organizer.application.duplicate_bin_operation_repository import (
    DuplicateBinOperationRepository,
)
from organizer.application.duplicate_bin_repository import (
    DuplicateBinRepository,
    DuplicateBinRepositoryError,
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
    validate_duplicate_bin_item_destination,
)

CONFIRM_MOVE_COPIES_TO_DUPLICATE_BIN = "MOVE COPIES TO DUPLICATE BIN"


class DuplicateBinMovementError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def preview_duplicate_bin_movement(
    *,
    batch_id: str,
    browser_session_id: str,
    batch_repository: DuplicateBinRepository,
    authorization_repository: DuplicateBinAuthorizationRepository,
    operation_repository: DuplicateBinOperationRepository,
    coordinator: OrganizationExecutionCoordinator,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    issued_at: str | None = None,
) -> DuplicateBinMovementPreview:
    _server_uuid(batch_id, "duplicate_batch_not_found")
    with coordinator.runtime_lifecycle.begin_operation(
        RuntimeOperationKind.STATE_MUTATION
    ):
        return _preview_duplicate_bin_movement(
            batch_id=batch_id,
            browser_session_id=browser_session_id,
            batch_repository=batch_repository,
            authorization_repository=authorization_repository,
            operation_repository=operation_repository,
            coordinator=coordinator,
            filesystem_operation_admission=filesystem_operation_admission,
            issued_at=issued_at,
        )


def _preview_duplicate_bin_movement(
    *,
    batch_id: str,
    browser_session_id: str,
    batch_repository: DuplicateBinRepository,
    authorization_repository: DuplicateBinAuthorizationRepository,
    operation_repository: DuplicateBinOperationRepository,
    coordinator: OrganizationExecutionCoordinator,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    issued_at: str | None = None,
) -> DuplicateBinMovementPreview:
    if not coordinator.try_acquire():
        raise DuplicateBinMovementError("movement_or_undo_in_progress")
    try:
        try:
            batch = batch_repository.load(batch_id)
        except DuplicateBinRepositoryError as error:
            raise DuplicateBinMovementError("duplicate_batch_not_found") from error
        if not batch.can_receive_authorization:
            raise DuplicateBinMovementError("duplicate_batch_not_approved")
        if operation_repository.find_for_batch(batch_id) is not None:
            raise DuplicateBinMovementError("duplicate_bin_movement_already_started")
        admission = filesystem_operation_admission or FilesystemOperationAdmission(
            FilesystemOperationInspector(batch_repository.root)
        )
        if admission.root != batch_repository.root:
            raise DuplicateBinMovementError("duplicate_batch_stale")
        admission.inspect_and_require_clear()
        authorization = authorization_repository.issue(
            batch,
            browser_session_id=browser_session_id,
            issued_at=issued_at,
        )
    except DuplicateBinAuthorizationError as error:
        raise DuplicateBinMovementError(error.code) from error
    finally:
        coordinator.release()
    return DuplicateBinMovementPreview(
        copies_to_move=len(batch.items),
        confirmation=CONFIRM_MOVE_COPIES_TO_DUPLICATE_BIN,
        confirmation_token=authorization.authorization_id,
        expires_at=authorization.expires_at,
    )


def execute_duplicate_bin_movement(
    *,
    browser_session_id: str,
    confirmation_token: str,
    confirmation: str,
    batch_repository: DuplicateBinRepository,
    authorization_repository: DuplicateBinAuthorizationRepository,
    operation_repository: DuplicateBinOperationRepository,
    coordinator: OrganizationExecutionCoordinator,
    filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    now: str | None = None,
) -> DuplicateBinOperationProjection:
    _server_uuid(confirmation_token, "invalid_confirmation_token")
    if confirmation != CONFIRM_MOVE_COPIES_TO_DUPLICATE_BIN:
        raise DuplicateBinMovementError("invalid_confirmation")
    authorization = _authorization_for_execution(
        confirmation_token,
        browser_session_id,
        batch_repository,
        authorization_repository,
    )
    if not coordinator.try_acquire(RuntimeOperationKind.DUPLICATE_BIN_MOVEMENT):
        raise DuplicateBinMovementError("movement_or_undo_in_progress")
    try:
        authorization = _authorization_for_execution(
            confirmation_token,
            browser_session_id,
            batch_repository,
            authorization_repository,
        )
        batch = None
        if authorization.status == AUTH_AVAILABLE:
            current_time = _timestamp(now or datetime.now(UTC).isoformat())
            if current_time >= _timestamp(authorization.expires_at):
                raise DuplicateBinMovementError("confirmation_expired")
            try:
                batch = batch_repository.load(authorization.batch_id)
            except DuplicateBinRepositoryError as error:
                raise DuplicateBinMovementError("duplicate_batch_not_found") from error
            _validate_authorization(authorization, batch)

        admission = filesystem_operation_admission or FilesystemOperationAdmission(
            FilesystemOperationInspector(batch_repository.root)
        )
        if admission.root != batch_repository.root:
            raise DuplicateBinMovementError("duplicate_batch_stale")
        admission.inspect_and_require_clear()

        existing = operation_repository.find_for_authorization(confirmation_token)
        if existing is not None:
            return _project(existing)
        if authorization.status != AUTH_AVAILABLE:
            raise DuplicateBinMovementError("invalid_confirmation_token")
        assert batch is not None

        plan, expected_moves, preflight_digest = _preflight(
            batch, batch_repository.root
        )
        created_at = current_time.isoformat()
        operation = DuplicateBinOperationRecord(
            operation_id=str(uuid.uuid4()),
            authorization_id=authorization.authorization_id,
            batch_id=batch.batch_id,
            configured_root_identity=batch_repository.configured_root_identity,
            approved_batch_digest=authorization.approved_batch_digest,
            batch_version=batch.batch_version,
            preflight_digest=preflight_digest,
            selected_bin_item_ids=tuple(item.bin_item_id for item in expected_moves),
            expected_moves=expected_moves,
            status=OP_PREPARED,
            created_at=created_at,
            started_at=None,
            completed_at=None,
            verification=DuplicateBinVerification(),
            executor_log_path=None,
            failure_code=None,
            recovery_state=None,
        )
        try:
            operation_repository.create(operation)
            authorization_repository.consume(
                authorization.authorization_id,
                operation.operation_id,
            )
            executing_batch = batch_repository.save_execution_state(
                batch.batch_id,
                expected_version=batch.batch_version,
                status=BATCH_EXECUTING,
                items=tuple(replace(item, state=ITEM_MOVING) for item in batch.items),
                updated_at=created_at,
            )
        except (
            DuplicateBinAuthorizationError,
            DuplicateBinRepositoryError,
            ValueError,
        ) as error:
            interrupted = replace(
                operation,
                status=OP_INTERRUPTED,
                completed_at=_utc_timestamp(),
                failure_code="durable_start_failed",
                recovery_state="recovery_required",
            )
            try:
                operation_repository.save(interrupted)
            except Exception:
                pass
            raise DuplicateBinMovementError(
                "duplicate_bin_recovery_required"
            ) from error

        recorder = _DuplicateBinRecorder(operation, operation_repository)
        try:
            apply_move_plan(
                plan,
                batch_repository.root,
                log_dir_name="duplicate_bin/operation_logs",
                recorder=recorder,
            )
        except MovementRecordingError as error:
            interrupted = _interrupted_operation(
                operation_repository.load(operation.operation_id),
                failure_code="durable_recording_failed",
                executor_log_path=(
                    _relative(error.operation_log.log_path, batch_repository.root)
                    if error.operation_log is not None
                    else None
                ),
            )
            operation_repository.save(interrupted)
            _finish_batch(batch_repository, executing_batch, interrupted)
            return _project(interrupted)
        except Exception:
            interrupted = _interrupted_operation(
                operation_repository.load(operation.operation_id),
                failure_code="executor_interrupted",
            )
            operation_repository.save(interrupted)
            _finish_batch(batch_repository, executing_batch, interrupted)
            return _project(interrupted)

        moved = operation_repository.load(operation.operation_id)
        finished = _verify(moved, batch_repository.root)
        operation_repository.save(finished)
        _finish_batch(batch_repository, executing_batch, finished)
        return _project(finished)
    finally:
        coordinator.release()


def _authorization_for_execution(
    confirmation_token: str,
    browser_session_id: str,
    batch_repository: DuplicateBinRepository,
    authorization_repository: DuplicateBinAuthorizationRepository,
) -> DuplicateBinMovementAuthorization:
    try:
        authorization = authorization_repository.load(confirmation_token)
    except DuplicateBinAuthorizationError as error:
        raise DuplicateBinMovementError(error.code) from error
    if (
        authorization.browser_session_id != browser_session_id
        or authorization.configured_root_identity
        != batch_repository.configured_root_identity
    ):
        raise DuplicateBinMovementError("invalid_confirmation_token")
    return authorization


def _validate_authorization(authorization, batch: ApprovedDuplicateBatch) -> None:
    if (
        not batch.can_receive_authorization
        or authorization.batch_id != batch.batch_id
        or authorization.approved_batch_digest != approved_batch_digest(batch)
        or authorization.expected_batch_version != batch.batch_version
        or authorization.expected_batch_state != batch.status
        or authorization.selected_bin_item_ids
        != tuple(sorted(item.bin_item_id for item in batch.items))
    ):
        raise DuplicateBinMovementError("duplicate_batch_stale")


def _preflight(
    batch: ApprovedDuplicateBatch,
    root: Path,
) -> tuple[list[MovePlanItem], tuple[DuplicateBinExpectedMove, ...], str]:
    selected_items = {item.selected_member_id: item for item in batch.items}
    if set(selected_items) != {
        member_id for group in batch.groups for member_id in group.selected_member_ids
    }:
        raise DuplicateBinMovementError("duplicate_batch_stale")
    live_members: list[dict[str, object]] = []
    expected: list[DuplicateBinExpectedMove] = []
    plan: list[MovePlanItem] = []
    for group in batch.groups:
        if not group.retained_member_ids or set(group.selected_member_ids) == {
            member.member_id for member in group.members
        }:
            raise DuplicateBinMovementError("retained_copy_missing")
        for member in group.members:
            path = root / member.relative_path
            is_selected = member.member_id in group.selected_member_ids
            selected = selected_items.get(member.member_id)
            if not os.path.lexists(path):
                raise DuplicateBinMovementError(
                    "selected_copy_changed" if is_selected else "retained_copy_missing"
                )
            live = _stable_hash(path, root)
            if (
                live["size_bytes"] != member.size_bytes
                or live["modified_time"] != member.modified_time
                or live["size_bytes"] != group.approved_size_bytes
                or live["sha256"] != group.approved_sha256
            ):
                raise DuplicateBinMovementError(
                    "selected_copy_changed" if is_selected else "retained_copy_changed"
                )
            approval_fingerprint = canonical_digest(
                {
                    "schema": "loci-duplicate-approval-metadata-v1",
                    "size_bytes": live["size_bytes"],
                    "modified_time": live["modified_time"],
                }
            )
            if (
                selected is not None
                and approval_fingerprint != selected.approval_metadata_fingerprint
            ):
                raise DuplicateBinMovementError("selected_copy_changed")
            live["metadata_fingerprint"] = canonical_digest(
                {
                    "schema": "loci-duplicate-live-metadata-v1",
                    "size_bytes": live["size_bytes"],
                    "modified_time_ns": live["modified_time_ns"],
                    "device": live["device"],
                    "inode": live["inode"],
                }
            )
            live_members.append(
                {
                    "group_id": group.group_id,
                    "member_id": member.member_id,
                    "relative_path": member.relative_path,
                    **live,
                }
            )
            if selected is None:
                continue
            destination = root / selected.future_relative_path
            try:
                validate_duplicate_bin_item_destination(destination, root)
            except (OSError, ValueError) as error:
                raise DuplicateBinMovementError(
                    "unsafe_duplicate_bin_destination"
                ) from error
            expected.append(
                DuplicateBinExpectedMove(
                    bin_item_id=selected.bin_item_id,
                    member_id=member.member_id,
                    source=member.relative_path,
                    destination=selected.future_relative_path,
                    expected_sha256=group.approved_sha256,
                    source_size_bytes=int(live["size_bytes"]),
                    source_modified_time_ns=int(live["modified_time_ns"]),
                )
            )
            plan.append(
                MovePlanItem(
                    source=path,
                    destination=destination,
                    reason="Move approved duplicate copy to Duplicate Bin",
                    confidence=100,
                    operation="move",
                    overwrite_risk=False,
                )
            )
    rows = sorted(zip(expected, plan), key=lambda pair: pair[0].bin_item_id)
    expected = [pair[0] for pair in rows]
    plan = [pair[1] for pair in rows]
    digest = canonical_digest(
        {
            "schema": "loci-duplicate-bin-preflight-v1",
            "batch_id": batch.batch_id,
            "approved_batch_digest": approved_batch_digest(batch),
            "selected_bin_item_ids": [item.bin_item_id for item in expected],
            "members": sorted(live_members, key=lambda item: str(item["member_id"])),
            "destinations": [item.destination for item in expected],
        }
    )
    return plan, tuple(expected), digest


def _stable_hash(path: Path, root: Path) -> dict[str, object]:
    if path.is_symlink():
        raise DuplicateBinMovementError("duplicate_member_unsafe")
    try:
        resolved = path.resolve(strict=True)
        validate_under_root(resolved, root)
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("member is not regular")
        sha256 = hash_file(path)
        after = os.lstat(path)
    except FileNotFoundError as error:
        raise DuplicateBinMovementError("duplicate_member_unsafe") from error
    except (OSError, ValueError) as error:
        raise DuplicateBinMovementError("duplicate_member_unsafe") from error
    stable_fields = ("st_mode", "st_size", "st_mtime_ns", "st_dev", "st_ino")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise DuplicateBinMovementError("duplicate_member_changed_during_hash")
    return {
        "size_bytes": before.st_size,
        "modified_time": before.st_mtime,
        "modified_time_ns": before.st_mtime_ns,
        "device": before.st_dev,
        "inode": before.st_ino,
        "sha256": sha256,
    }


class _DuplicateBinRecorder:
    def __init__(self, record, repository) -> None:
        self.record = record
        self.repository = repository

    def before_batch(self, plan_items: tuple[MovePlanItem, ...]) -> None:
        if len(plan_items) != len(self.record.expected_moves):
            raise ValueError("duplicate bin move batch changed")
        self.record = replace(self.record, status=OP_MOVING)
        self.repository.save(self.record)

    def before_move(self, index: int, item: MovePlanItem) -> None:
        attempted_at = _utc_timestamp()
        self._item(index, MOVE_ATTEMPTING, attempted_at=attempted_at)

    def after_success(self, index: int, item: MovePlanItem, result: MoveResult) -> None:
        self._item(index, MOVE_MOVED, completed_at=_utc_timestamp())

    def after_failure(self, index: int, item: MovePlanItem, result: MoveResult) -> None:
        self._item(
            index,
            MOVE_FAILED,
            "movement_failed",
            completed_at=_utc_timestamp(),
        )

    def after_batch(self, operation_log: OperationLog) -> None:
        completed_at = _utc_timestamp()
        moves = tuple(
            replace(
                item,
                result_state=MOVE_UNATTEMPTED,
                completed_at=completed_at,
            )
            if item.result_state == MOVE_PENDING
            else item
            for item in self.record.expected_moves
        )
        moved = sum(item.result_state == MOVE_MOVED for item in moves)
        status = (
            OP_MOVED_UNVERIFIED
            if moved == len(moves)
            else OP_PARTIAL
            if moved
            else OP_MOVEMENT_FAILED
        )
        self.record = replace(
            self.record,
            status=status,
            completed_at=None if status == OP_MOVED_UNVERIFIED else completed_at,
            expected_moves=moves,
            executor_log_path=_relative(operation_log.log_path, self.repository.root),
            failure_code=None
            if status == OP_MOVED_UNVERIFIED
            else "movement_incomplete",
        )
        self.repository.save(self.record)

    def _item(
        self,
        index: int,
        state: str,
        failure: str | None = None,
        *,
        attempted_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        moves = list(self.record.expected_moves)
        moves[index] = replace(
            moves[index],
            result_state=state,
            attempted_at=attempted_at or moves[index].attempted_at,
            completed_at=completed_at,
            failure_code=failure,
        )
        self.record = replace(
            self.record,
            started_at=self.record.started_at or attempted_at,
            expected_moves=tuple(moves),
        )
        self.repository.save(self.record)


def _verify(
    record: DuplicateBinOperationRecord, root: Path
) -> DuplicateBinOperationRecord:
    moves = list(record.expected_moves)
    mismatches: list[str] = []
    verified = 0
    for index, item in enumerate(moves):
        if item.result_state != MOVE_MOVED:
            continue
        source = root / item.source
        destination = root / item.destination
        code: str | None = None
        if os.path.lexists(source):
            code = "source_still_present"
        else:
            try:
                _validate_existing_destination(
                    destination,
                    root,
                    item.bin_item_id,
                    Path(item.source).name,
                )
                live = _stable_hash(destination, root)
                if live["size_bytes"] != item.source_size_bytes:
                    code = "destination_size_changed"
                elif live["sha256"] != item.expected_sha256:
                    code = "destination_hash_changed"
            except (OSError, DuplicateBinMovementError, ValueError):
                code = "destination_missing_or_unsafe"
        if code is None:
            moves[index] = replace(item, result_state=MOVE_VERIFIED)
            verified += 1
        else:
            moves[index] = replace(item, result_state=MOVE_ATTENTION, failure_code=code)
            mismatches.append(f"{code}:{item.bin_item_id}")
    failed = sum(item.result_state == MOVE_FAILED for item in moves)
    unattempted = sum(item.result_state == MOVE_UNATTEMPTED for item in moves)
    if verified == len(moves):
        status = OP_MOVED
    elif verified and (failed or unattempted):
        status = OP_PARTIAL
    elif any(item.result_state == MOVE_ATTENTION for item in moves):
        status = OP_VERIFICATION_FAILED
    else:
        status = OP_MOVEMENT_FAILED
    return replace(
        record,
        status=status,
        completed_at=_utc_timestamp(),
        expected_moves=tuple(moves),
        verification=DuplicateBinVerification(
            status="passed" if status == OP_MOVED else "failed",
            verified_count=verified,
            mismatch_codes=tuple(sorted(mismatches)),
        ),
        failure_code=None if status == OP_MOVED else "duplicate_bin_move_incomplete",
    )


def _validate_existing_destination(
    destination: Path,
    root: Path,
    bin_item_id: str,
    basename: str,
) -> None:
    expected = root / DUPLICATE_BIN_ITEMS_RELATIVE_ROOT / bin_item_id / basename
    if destination != expected:
        raise ValueError("duplicate bin destination layout changed")
    relative = destination.relative_to(root)
    current = root
    for index, component in enumerate(relative.parts):
        current = current / component
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("duplicate bin destination contains a symlink")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("duplicate bin destination parent is not a directory")
    if not stat.S_ISREG(os.lstat(destination).st_mode):
        raise ValueError("duplicate bin destination is not a regular file")
    validate_under_root(destination.resolve(strict=True), root)


def _finish_batch(repository, batch, operation) -> None:
    state_by_id = {
        item.bin_item_id: item.result_state for item in operation.expected_moves
    }
    items: list[SelectedDuplicateBinItem] = []
    for item in batch.items:
        state = state_by_id[item.bin_item_id]
        item_state = (
            ITEM_AVAILABLE
            if state == MOVE_VERIFIED
            else ITEM_UNATTEMPTED
            if state in {MOVE_PENDING, MOVE_UNATTEMPTED}
            else ITEM_ATTENTION
        )
        items.append(replace(item, state=item_state))
    batch_status = {
        OP_MOVED: BATCH_MOVED,
        OP_PARTIAL: BATCH_PARTIALLY_MOVED,
        OP_MOVEMENT_FAILED: BATCH_MOVEMENT_FAILED,
        OP_VERIFICATION_FAILED: BATCH_VERIFICATION_FAILED,
        OP_INTERRUPTED: BATCH_INTERRUPTED,
    }.get(operation.status, BATCH_INTERRUPTED)
    repository.save_execution_state(
        batch.batch_id,
        expected_version=batch.batch_version,
        status=batch_status,
        items=tuple(items),
        updated_at=operation.completed_at or _utc_timestamp(),
    )


def _interrupted_operation(
    record: DuplicateBinOperationRecord,
    *,
    failure_code: str,
    executor_log_path: str | None = None,
) -> DuplicateBinOperationRecord:
    completed_at = _utc_timestamp()
    moves: list[DuplicateBinExpectedMove] = []
    for item in record.expected_moves:
        if item.result_state == MOVE_PENDING:
            item = replace(
                item,
                result_state=MOVE_UNATTEMPTED,
                completed_at=completed_at,
                failure_code="not_attempted_after_interruption",
                recovery_state="recovery_required",
            )
        elif item.result_state in {MOVE_ATTEMPTING, MOVE_MOVED}:
            item = replace(
                item,
                result_state=MOVE_ATTENTION,
                completed_at=completed_at,
                failure_code=failure_code,
                recovery_state="recovery_required",
            )
        moves.append(item)
    return replace(
        record,
        status=OP_INTERRUPTED,
        completed_at=completed_at,
        expected_moves=tuple(moves),
        executor_log_path=executor_log_path or record.executor_log_path,
        failure_code=failure_code,
        recovery_state="recovery_required",
    )


def _project(record: DuplicateBinOperationRecord) -> DuplicateBinOperationProjection:
    moved = sum(
        item.result_state in {MOVE_MOVED, MOVE_VERIFIED}
        for item in record.expected_moves
    )
    failed = sum(
        item.result_state in {MOVE_FAILED, MOVE_ATTENTION}
        for item in record.expected_moves
    )
    unattempted = sum(
        item.result_state in {MOVE_PENDING, MOVE_UNATTEMPTED}
        for item in record.expected_moves
    )
    return DuplicateBinOperationProjection(
        id=record.operation_id,
        status=record.status,
        copies_expected=len(record.expected_moves),
        copies_moved=moved,
        copies_failed=failed,
        copies_unattempted=unattempted,
        verification_status=record.verification.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
        recovery_required=record.status not in OP_TERMINAL_STATUSES,
    )


def _relative(path: Path, root: Path) -> str:
    return (
        validate_under_root(path.resolve(strict=False), root)
        .relative_to(root)
        .as_posix()
    )


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DuplicateBinMovementError("invalid_authorization_time")
    return parsed


def _server_uuid(value: str, code: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise DuplicateBinMovementError(code) from error
    if str(parsed) != value:
        raise DuplicateBinMovementError(code)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()
