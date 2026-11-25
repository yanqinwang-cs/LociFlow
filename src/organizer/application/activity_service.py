from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from organizer.application.activity_models import (
    ActivityDetail,
    ActivityEntry,
    ActivityList,
    UndoEligibility,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_ATTENTION as DUPLICATE_MOVE_ATTENTION,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_FAILED as DUPLICATE_MOVE_FAILED,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_MOVED as DUPLICATE_MOVE_MOVED,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_PENDING as DUPLICATE_MOVE_PENDING,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_UNATTEMPTED as DUPLICATE_MOVE_UNATTEMPTED,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_VERIFIED as DUPLICATE_MOVE_VERIFIED,
)
from organizer.application.duplicate_bin_movement_models import (
    OP_MOVED as DUPLICATE_OP_MOVED,
)
from organizer.application.duplicate_bin_movement_models import (
    OP_PARTIAL as DUPLICATE_OP_PARTIAL,
)
from organizer.application.duplicate_bin_movement_models import (
    DuplicateBinOperationRecord,
)
from organizer.application.duplicate_bin_operation_repository import (
    DuplicateBinOperationRepository,
)
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.duplicate_bin_restore_models import (
    ITEM_ATTENTION as DUPLICATE_RESTORE_ATTENTION,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_FAILED as DUPLICATE_RESTORE_FAILED,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_PENDING as DUPLICATE_RESTORE_PENDING,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_RESTORED as DUPLICATE_RESTORE_RESTORED,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_UNATTEMPTED as DUPLICATE_RESTORE_UNATTEMPTED,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_VERIFIED as DUPLICATE_RESTORE_VERIFIED,
)
from organizer.application.duplicate_bin_restore_models import (
    RESTORE_PARTIAL as DUPLICATE_RESTORE_PARTIAL,
)
from organizer.application.duplicate_bin_restore_models import (
    RESTORE_RESTORED as DUPLICATE_RESTORE_COMPLETE,
)
from organizer.application.duplicate_bin_restore_models import (
    DuplicateBinRestoreOperation,
)
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
)
from organizer.application.duplicate_bin_restore_service import (
    restorable_item_ids_for_movement,
)
from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    ORGANIZATION_OPERATION_SCHEMA_VERSION,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVEMENT_FAILED,
    STATUS_MOVING,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_PREPARED,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    VERIFICATION_PASSED,
    OrganizationOperationRecord,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_identity_eligibility import (
    IdentityUndoEligibilityReason,
    IdentityUndoEligibilityState,
    OrganizationUndoIdentityEligibilityInspector,
)
from organizer.application.organization_undo_models import (
    UNDO_FAILED,
    UNDO_INTERRUPTED,
    UNDO_ITEM_FAILED,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_ITEM_UNATTEMPTED,
    UNDO_PARTIAL,
    UNDO_RESTORED,
    UNDO_VERIFICATION_FAILED,
    OrganizationUndoRecord,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.safety import validate_under_root


def list_activity_entries(
    *,
    operation_repository: OrganizationOperationRepository,
    undo_repository: OrganizationUndoRepository,
    duplicate_bin_operation_repository: DuplicateBinOperationRepository | None = None,
    duplicate_bin_batch_repository: DuplicateBinRepository | None = None,
    duplicate_bin_restore_repository: DuplicateBinRestoreRepository | None = None,
) -> ActivityList:
    _validate_repositories(operation_repository, undo_repository)
    operations = operation_repository.list_records()
    undos = undo_repository.list_records()
    eligibility = calculate_undo_eligibility(
        operation_repository=operation_repository,
        undo_repository=undo_repository,
    )
    undoable_id = eligibility.operation_id if eligibility.undoable else None
    entries = [
        _movement_entry(record, undoable=record.operation_id == undoable_id)
        for record in operations
    ]
    entries.extend(_restore_entry(record) for record in undos)
    if duplicate_bin_operation_repository is not None:
        if duplicate_bin_operation_repository.root != operation_repository.root:
            raise ValueError(
                "Duplicate Bin Activity repository belongs to another root"
            )
        for record in duplicate_bin_operation_repository.list_records():
            if record.started_at is None:
                continue
            undoable = False
            if (
                duplicate_bin_batch_repository is not None
                and duplicate_bin_restore_repository is not None
            ):
                undoable = bool(
                    restorable_item_ids_for_movement(
                        record.operation_id,
                        duplicate_bin_batch_repository,
                        duplicate_bin_operation_repository,
                        duplicate_bin_restore_repository,
                    )
                )
            entries.append(_duplicate_bin_entry(record, undoable=undoable))
    if duplicate_bin_restore_repository is not None:
        if duplicate_bin_restore_repository.root != operation_repository.root:
            raise ValueError("Duplicate Bin restore repository belongs to another root")
        entries.extend(
            _duplicate_bin_restore_entry(record)
            for record in duplicate_bin_restore_repository.list_operations()
            if record.started_at is not None
        )
    entries.sort(key=_entry_sort_key, reverse=True)
    return ActivityList(tuple(entries))


def calculate_undo_eligibility(
    *,
    operation_repository: OrganizationOperationRepository,
    undo_repository: OrganizationUndoRepository,
    requested_operation_id: str | None = None,
) -> UndoEligibility:
    _validate_repositories(operation_repository, undo_repository)
    operations = tuple(
        sorted(operation_repository.list_records(), key=_operation_created_key)
    )
    meaningful = tuple(record for record in operations if _is_meaningful(record))
    if not meaningful:
        return _ineligible(requested_operation_id or "", "operation_not_undoable")
    candidate = meaningful[-1]
    if requested_operation_id is not None and requested_operation_id != candidate.operation_id:
        return _ineligible(requested_operation_id, "operation_superseded")
    if candidate.schema_version == ORGANIZATION_OPERATION_SCHEMA_VERSION_V2:
        identity = OrganizationUndoIdentityEligibilityInspector(
            operation_repository,
            undo_repository,
        ).inspect(candidate.operation_id)
        if identity.state is IdentityUndoEligibilityState.ELIGIBLE:
            return UndoEligibility(
                operation_id=candidate.operation_id,
                undoable=True,
                error_code=None,
                files_to_restore=len(identity.captures),
                legacy_log_path=None,
            )
        return _ineligible(
            candidate.operation_id,
            _identity_undo_error(identity.reason, undo_repository, candidate),
        )
    if candidate.schema_version != ORGANIZATION_OPERATION_SCHEMA_VERSION:
        return _ineligible(candidate.operation_id, "operation_not_undoable")
    if not _is_complete_verified(candidate):
        return _ineligible(candidate.operation_id, "operation_not_undoable")
    linked = undo_repository.linked_records(candidate.operation_id)
    if linked:
        completed = any(record.status == UNDO_RESTORED for record in linked)
        return _ineligible(
            candidate.operation_id,
            "undo_already_completed" if completed else "undo_already_started",
        )
    try:
        _validate_live_files(candidate, operation_repository.root)
        log_path = validate_legacy_operation_log(candidate, operation_repository.root)
    except ActivityEligibilityError as error:
        return _ineligible(candidate.operation_id, error.code)
    return UndoEligibility(
        operation_id=candidate.operation_id,
        undoable=True,
        error_code=None,
        files_to_restore=len(candidate.expected_moves),
        legacy_log_path=str(log_path),
    )


def _identity_undo_error(
    reason: IdentityUndoEligibilityReason | None,
    undo_repository: OrganizationUndoRepository,
    candidate: OrganizationOperationRecord,
) -> str:
    if reason is IdentityUndoEligibilityReason.PARENT_SUPERSEDED:
        return "operation_superseded"
    if reason is IdentityUndoEligibilityReason.EXISTING_UNDO:
        try:
            completed = any(
                record.status == UNDO_RESTORED
                for record in undo_repository.linked_records(
                    candidate.operation_id
                )
            )
        except ValueError:
            completed = False
        return "undo_already_completed" if completed else "undo_already_started"
    mapping = {
        IdentityUndoEligibilityReason.RESTORE_TARGET_OCCUPIED: (
            "undo_destination_occupied"
        ),
        IdentityUndoEligibilityReason.SOURCE_MISSING: "undo_source_missing",
        IdentityUndoEligibilityReason.CONTENT_CHANGED: "undo_source_changed",
        IdentityUndoEligibilityReason.SOURCE_UNSAFE: "unsafe_undo_path",
        IdentityUndoEligibilityReason.UNSAFE_RESTORE_PATH: "unsafe_undo_path",
    }
    return mapping.get(reason, "operation_not_undoable")


class ActivityEligibilityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_legacy_operation_log(
    record: OrganizationOperationRecord,
    root: Path,
) -> Path:
    relative = record.legacy_operation_log
    if not relative:
        raise ActivityEligibilityError("operation_not_undoable")
    log_path = root / relative
    if log_path.is_symlink():
        raise ActivityEligibilityError("unsafe_undo_path")
    try:
        resolved = log_path.resolve(strict=True)
        validate_under_root(resolved, root)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise ActivityEligibilityError("unsafe_undo_path") from error
    if not resolved.is_file():
        raise ActivityEligibilityError("unsafe_undo_path")
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ActivityEligibilityError("unsafe_undo_path") from error
    if not isinstance(data, dict) or set(data) != {"operations"}:
        raise ActivityEligibilityError("unsafe_undo_path")
    operations = data["operations"]
    if not isinstance(operations, list) or len(operations) != len(record.expected_moves):
        raise ActivityEligibilityError("operation_not_undoable")
    for operation, expected in zip(operations, record.expected_moves):
        if not isinstance(operation, dict) or set(operation) != {
            "source", "destination", "success", "message"
        }:
            raise ActivityEligibilityError("unsafe_undo_path")
        source = operation["source"]
        destination = operation["destination"]
        success = operation["success"]
        message = operation["message"]
        if (
            not isinstance(source, str)
            or not isinstance(destination, str)
            or not isinstance(success, bool)
            or not isinstance(message, str)
        ):
            raise ActivityEligibilityError("unsafe_undo_path")
        if not success:
            raise ActivityEligibilityError("operation_not_undoable")
        try:
            resolved_source = Path(source).resolve(strict=False)
            resolved_destination = Path(destination).resolve(strict=False)
            validate_under_root(resolved_source, root)
            validate_under_root(resolved_destination, root)
        except (OSError, ValueError) as error:
            raise ActivityEligibilityError("unsafe_undo_path") from error
        if (
            resolved_source != (root / expected.source).resolve(strict=False)
            or resolved_destination
            != (root / expected.destination).resolve(strict=False)
        ):
            raise ActivityEligibilityError("operation_not_undoable")
    return resolved


def _validate_live_files(record: OrganizationOperationRecord, root: Path) -> None:
    for move in record.expected_moves:
        original = root / move.source
        moved = root / move.destination
        if os.path.lexists(original):
            raise ActivityEligibilityError("undo_destination_occupied")
        if moved.is_symlink():
            raise ActivityEligibilityError("unsafe_undo_path")
        try:
            resolved_moved = moved.resolve(strict=True)
            validate_under_root(resolved_moved, root)
        except FileNotFoundError as error:
            raise ActivityEligibilityError("undo_source_missing") from error
        except (OSError, ValueError) as error:
            raise ActivityEligibilityError("unsafe_undo_path") from error
        if not resolved_moved.is_file():
            raise ActivityEligibilityError("unsafe_undo_path")
        stat = resolved_moved.stat()
        if (
            stat.st_size != move.source_size_bytes
            or stat.st_mtime != move.source_modified_time
        ):
            raise ActivityEligibilityError("undo_source_changed")
        try:
            validate_under_root(original.resolve(strict=False), root)
            parent = _nearest_existing_parent(original.parent)
            validate_under_root(parent.resolve(strict=True), root)
        except (OSError, ValueError) as error:
            raise ActivityEligibilityError("unsafe_undo_path") from error
        if not parent.is_dir():
            raise ActivityEligibilityError("unsafe_undo_path")


def _is_meaningful(record: OrganizationOperationRecord) -> bool:
    if record.status == STATUS_MOVEMENT_FAILED:
        return any(
            move.result_state not in {MOVE_FAILED, MOVE_UNATTEMPTED}
            for move in record.expected_moves
        )
    return (
        record.status
        in {
            STATUS_PREPARED,
            STATUS_MOVING,
            STATUS_MOVED_UNVERIFIED,
            STATUS_VERIFIED,
            STATUS_PARTIAL_MOVEMENT,
            STATUS_VERIFICATION_FAILED,
            STATUS_INTERRUPTED,
        }
        or any(move.result_state in {MOVE_MOVED, MOVE_ATTEMPTING} for move in record.expected_moves)
    )


def _is_complete_verified(record: OrganizationOperationRecord) -> bool:
    return (
        record.schema_version == ORGANIZATION_OPERATION_SCHEMA_VERSION
        and record.status == STATUS_VERIFIED
        and record.verification.status == VERIFICATION_PASSED
        and bool(record.expected_moves)
        and all(move.result_state == MOVE_MOVED for move in record.expected_moves)
        and record.verification.verified_count == len(record.expected_moves)
        and record.legacy_operation_log is not None
    )


def _movement_entry(record: OrganizationOperationRecord, *, undoable: bool) -> ActivityEntry:
    moved = sum(move.result_state == MOVE_MOVED for move in record.expected_moves)
    failed = sum(move.result_state == MOVE_FAILED for move in record.expected_moves)
    unattempted = sum(
        move.result_state in {MOVE_PENDING, MOVE_UNATTEMPTED}
        for move in record.expected_moves
    )
    return ActivityEntry(
        id=record.operation_id,
        type="organization",
        title=_movement_title(record.status, moved),
        status=record.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
        files_expected=len(record.expected_moves),
        files_moved=moved,
        files_restored=0,
        files_failed=failed,
        files_unattempted=unattempted,
        verification_status=record.verification.status,
        undoable=undoable,
        related_operation_id=None,
        details=tuple(
            ActivityDetail(
                source=move.source,
                destination=move.destination,
                restored_to=None,
                moved_from=None,
                status=move.result_state,
            )
            for move in record.expected_moves
        ),
        ordering_at=record.completed_at or record.started_at or record.created_at,
    )


def _restore_entry(record: OrganizationUndoRecord) -> ActivityEntry:
    restored = sum(item.result_state == UNDO_ITEM_RESTORED for item in record.expected_restores)
    failed = sum(item.result_state == UNDO_ITEM_FAILED for item in record.expected_restores)
    unattempted = sum(
        item.result_state in {UNDO_ITEM_PENDING, UNDO_ITEM_UNATTEMPTED}
        for item in record.expected_restores
    )
    return ActivityEntry(
        id=record.undo_id,
        type="restore",
        title=_restore_title(record.status, restored),
        status=record.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
        files_expected=len(record.expected_restores),
        files_moved=0,
        files_restored=restored,
        files_failed=failed,
        files_unattempted=unattempted,
        verification_status=record.verification.status,
        undoable=False,
        related_operation_id=record.operation_id,
        details=tuple(
            ActivityDetail(
                source=None,
                destination=None,
                restored_to=item.restore_target,
                moved_from=item.moved_from,
                status=item.result_state,
            )
            for item in record.expected_restores
        ),
        ordering_at=record.completed_at or record.started_at or record.created_at,
    )


def _duplicate_bin_entry(
    record: DuplicateBinOperationRecord, *, undoable: bool
) -> ActivityEntry:
    moved = sum(
        item.result_state in {DUPLICATE_MOVE_MOVED, DUPLICATE_MOVE_VERIFIED}
        for item in record.expected_moves
    )
    failed = sum(
        item.result_state in {DUPLICATE_MOVE_FAILED, DUPLICATE_MOVE_ATTENTION}
        for item in record.expected_moves
    )
    unattempted = sum(
        item.result_state in {DUPLICATE_MOVE_PENDING, DUPLICATE_MOVE_UNATTEMPTED}
        for item in record.expected_moves
    )
    if record.status == DUPLICATE_OP_MOVED:
        title = f"Moved {moved} {'copy' if moved == 1 else 'copies'} to Duplicate Bin"
    elif record.status == DUPLICATE_OP_PARTIAL:
        title = "Duplicate Bin move partly completed"
    else:
        title = "Duplicate Bin move needs attention"
    return ActivityEntry(
        id=record.operation_id,
        type="duplicate_bin_move",
        title=title,
        status=record.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
        files_expected=len(record.expected_moves),
        files_moved=moved,
        files_restored=0,
        files_failed=failed,
        files_unattempted=unattempted,
        verification_status=record.verification.status,
        undoable=undoable,
        related_operation_id=None,
        details=tuple(
            ActivityDetail(
                source=(
                    str(Path(item.source).parent)
                    if Path(item.source).parent != Path(".")
                    else "Selected folder"
                ),
                destination="Duplicate Bin",
                restored_to=None,
                moved_from=None,
                status=item.result_state,
            )
            for item in record.expected_moves
            if item.result_state != DUPLICATE_MOVE_PENDING
            or record.status == DUPLICATE_OP_MOVED
        ),
        ordering_at=record.completed_at or record.started_at or record.created_at,
    )


def _duplicate_bin_restore_entry(
    record: DuplicateBinRestoreOperation,
) -> ActivityEntry:
    restored = sum(
        item.state in {DUPLICATE_RESTORE_RESTORED, DUPLICATE_RESTORE_VERIFIED}
        for item in record.items
    )
    failed = sum(
        item.state in {DUPLICATE_RESTORE_FAILED, DUPLICATE_RESTORE_ATTENTION}
        for item in record.items
    )
    unattempted = sum(
        item.state in {DUPLICATE_RESTORE_PENDING, DUPLICATE_RESTORE_UNATTEMPTED}
        for item in record.items
    )
    if record.status == DUPLICATE_RESTORE_COMPLETE:
        title = f"Restored {restored} {'copy' if restored == 1 else 'copies'}"
    elif record.status == DUPLICATE_RESTORE_PARTIAL:
        title = "Duplicate Bin restore partly completed"
    else:
        title = "Duplicate Bin restore needs attention"
    return ActivityEntry(
        id=record.restore_operation_id,
        type="duplicate_bin_restore",
        title=title,
        status=record.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
        files_expected=len(record.items),
        files_moved=0,
        files_restored=restored,
        files_failed=failed,
        files_unattempted=unattempted,
        verification_status=record.verification.status,
        undoable=False,
        related_operation_id=None,
        details=tuple(
            ActivityDetail(
                source=None,
                destination=None,
                restored_to=(
                    str(Path(item.previous_destination).parent)
                    if Path(item.previous_destination).parent != Path(".")
                    else "Selected folder"
                ),
                moved_from="Duplicate Bin",
                status=item.state,
            )
            for item in record.items
            if item.state != DUPLICATE_RESTORE_PENDING
        ),
        ordering_at=record.completed_at or record.started_at or record.created_at,
    )


def _movement_title(status: str, moved: int) -> str:
    if status == STATUS_VERIFIED:
        return f"Organized {moved} {'file' if moved == 1 else 'files'}"
    if status == STATUS_PARTIAL_MOVEMENT:
        return "Move partly completed"
    if status == STATUS_MOVEMENT_FAILED:
        return "Move failed"
    if status == STATUS_VERIFICATION_FAILED:
        return "Move could not be confirmed"
    return "Move needs attention"


def _restore_title(status: str, restored: int) -> str:
    if status == UNDO_RESTORED:
        return f"Restored {restored} {'file' if restored == 1 else 'files'}"
    if status == UNDO_PARTIAL:
        return "Restore partly completed"
    if status == UNDO_FAILED:
        return "Restore failed"
    if status == UNDO_VERIFICATION_FAILED:
        return "Restore could not be confirmed"
    if status == UNDO_INTERRUPTED:
        return "Restore needs attention"
    # A durable nonterminal record may be observed after process interruption.
    # D2A never resumes it automatically, so Activity must not imply live work.
    return "Restore needs attention"


def _entry_sort_key(entry: ActivityEntry) -> tuple[datetime, str]:
    return (_parse_timestamp(entry.ordering_at), entry.id)


def _operation_created_key(record: OrganizationOperationRecord) -> tuple[datetime, str]:
    return (_parse_timestamp(record.created_at), record.operation_id)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("activity timestamp must include a timezone")
    return parsed


def _ineligible(operation_id: str, code: str) -> UndoEligibility:
    return UndoEligibility(operation_id, False, code, 0, None)


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _validate_repositories(
    operation_repository: OrganizationOperationRepository,
    undo_repository: OrganizationUndoRepository,
) -> None:
    if operation_repository.root != undo_repository.root:
        raise ValueError("Activity repositories belong to different roots")
    if operation_repository.configured_root_identity != undo_repository.configured_root_identity:
        raise ValueError("Activity repository root identities do not match")
