from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from organizer.application.duplicate_bin_authorization_repository import (
    DuplicateBinAuthorizationRepository,
    approved_batch_digest,
)
from organizer.application.duplicate_bin_models import (
    BATCH_APPROVED,
    BATCH_CANCELLED,
    BATCH_SUPERSEDED,
    ITEM_RESTORED as BIN_ITEM_RESTORED,
)
from organizer.application.duplicate_bin_movement_models import (
    AUTH_AVAILABLE as MOVE_AUTH_AVAILABLE,
    AUTH_CONSUMED as MOVE_AUTH_CONSUMED,
    OP_TERMINAL_STATUSES,
)
from organizer.application.duplicate_bin_operation_repository import (
    DuplicateBinOperationRepository,
)
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.duplicate_bin_restore_models import (
    AUTH_AVAILABLE as RESTORE_AUTH_AVAILABLE,
    AUTH_CONSUMED as RESTORE_AUTH_CONSUMED,
    AUTH_SUPERSEDED as RESTORE_AUTH_SUPERSEDED,
    ITEM_VERIFIED as RESTORE_ITEM_VERIFIED,
    RESTORE_TERMINAL,
)
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
)
from organizer.application.organization_operation_models import (
    BLOCKING_OPERATION_STATUSES,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_VERIFIED,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_models import (
    UNDO_INTERRUPTED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)


SAFE_RECOVERY_MESSAGE = (
    "Loci Flow found an unfinished file operation. New file changes are paused."
)


class RecoveryAdmissionState(str, Enum):
    CLEAR = "clear"
    BLOCKED_UNFINISHED_OPERATION = "blocked_unfinished_operation"
    BLOCKED_MALFORMED_OPERATION_STATE = "blocked_malformed_operation_state"
    BLOCKED_INCONSISTENT_OPERATION_STATE = "blocked_inconsistent_operation_state"


@dataclass(frozen=True)
class RecoveryAdmissionSnapshot:
    state: RecoveryAdmissionState

    @property
    def is_blocked(self) -> bool:
        return self.state is not RecoveryAdmissionState.CLEAR


class RecoveryInspectionError(RuntimeError):
    """Safe future-facing inspection failure without durable record details."""

    def __init__(self, state: RecoveryAdmissionState) -> None:
        super().__init__(SAFE_RECOVERY_MESSAGE)
        self.state = state


class FilesystemOperationBlockedError(RuntimeError):
    """Public-safe rejection retaining only typed internal diagnostic state."""

    code = "filesystem_operation_blocked"

    def __init__(self, state: RecoveryAdmissionState) -> None:
        super().__init__(SAFE_RECOVERY_MESSAGE)
        self.state = state


class FilesystemOperationAdmission:
    """Require a fresh clear cross-family inspection for each filesystem action."""

    def __init__(self, inspector: FilesystemOperationInspector) -> None:
        self.inspector = inspector

    @property
    def root(self) -> Path:
        return self.inspector.root

    def inspect_and_require_clear(self) -> RecoveryAdmissionSnapshot:
        try:
            snapshot = self.inspector.inspect()
        except Exception as error:
            raise FilesystemOperationBlockedError(
                RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE
            ) from error
        if snapshot.state is not RecoveryAdmissionState.CLEAR:
            raise FilesystemOperationBlockedError(snapshot.state)
        return snapshot


class _InconsistentOperationState(ValueError):
    pass


_UNDO_UNFINISHED = frozenset(
    {
        UNDO_PREPARED,
        UNDO_UNDOING,
        UNDO_RESTORED_UNVERIFIED,
        UNDO_INTERRUPTED,
    }
)
_MOVEMENT_BLOCKING_TERMINAL = frozenset({STATUS_PARTIAL_MOVEMENT})
_UNDO_BLOCKING_TERMINAL = frozenset({UNDO_PARTIAL})


class FilesystemOperationInspector:
    """Inspect root-bound durable filesystem intent without mutating it."""

    def __init__(
        self,
        root: Path,
        *,
        organization_operations: OrganizationOperationRepository | None = None,
        organization_undos: OrganizationUndoRepository | None = None,
        duplicate_bin_batches: DuplicateBinRepository | None = None,
        duplicate_bin_authorizations: DuplicateBinAuthorizationRepository
        | None = None,
        duplicate_bin_operations: DuplicateBinOperationRepository | None = None,
        duplicate_bin_restores: DuplicateBinRestoreRepository | None = None,
    ) -> None:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("inspection root is not a directory")
        self.root = resolved_root
        self.organization_operations = organization_operations or (
            OrganizationOperationRepository(resolved_root)
        )
        self.organization_undos = organization_undos or OrganizationUndoRepository(
            resolved_root
        )
        self.duplicate_bin_batches = duplicate_bin_batches or DuplicateBinRepository(
            resolved_root
        )
        self.duplicate_bin_authorizations = duplicate_bin_authorizations or (
            DuplicateBinAuthorizationRepository(resolved_root)
        )
        self.duplicate_bin_operations = duplicate_bin_operations or (
            DuplicateBinOperationRepository(resolved_root)
        )
        self.duplicate_bin_restores = duplicate_bin_restores or (
            DuplicateBinRestoreRepository(resolved_root)
        )
        owners = (
            self.organization_operations,
            self.organization_undos,
            self.duplicate_bin_batches,
            self.duplicate_bin_authorizations,
            self.duplicate_bin_operations,
            self.duplicate_bin_restores,
        )
        if any(owner.root != resolved_root for owner in owners):
            raise ValueError("inspection repositories belong to another root")

    def inspect(self) -> RecoveryAdmissionSnapshot:
        """Return a fresh immutable snapshot; all durable reads are non-mutating."""

        try:
            organization = self.organization_operations.list_records()
            undos = self.organization_undos.list_records()
            catalog = self.duplicate_bin_batches.read_catalog()
            move_authorizations = self.duplicate_bin_authorizations.list_records()
            duplicate_moves = self.duplicate_bin_operations.list_records()
            restore_authorizations = (
                self.duplicate_bin_restores.list_authorizations()
            )
            restores = self.duplicate_bin_restores.list_operations()
        except (OSError, UnicodeError, ValueError):
            return RecoveryAdmissionSnapshot(
                RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE
            )

        has_blocking_state = (
            any(
                item.status
                in BLOCKING_OPERATION_STATUSES | _MOVEMENT_BLOCKING_TERMINAL
                for item in organization
            )
            or any(
                item.status in _UNDO_UNFINISHED | _UNDO_BLOCKING_TERMINAL
                for item in undos
            )
            or any(item.status not in OP_TERMINAL_STATUSES for item in duplicate_moves)
            or any(item.status not in RESTORE_TERMINAL for item in restores)
        )

        try:
            self._validate_ordinary(organization, undos)
            self._validate_duplicate_bin_movement(
                catalog.batches,
                move_authorizations,
                duplicate_moves,
            )
            self._validate_duplicate_bin_restore(
                catalog.batches,
                duplicate_moves,
                restore_authorizations,
                restores,
            )
        except _InconsistentOperationState:
            return RecoveryAdmissionSnapshot(
                RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                if has_blocking_state
                else RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE
            )
        if has_blocking_state:
            return RecoveryAdmissionSnapshot(
                RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
            )
        return RecoveryAdmissionSnapshot(RecoveryAdmissionState.CLEAR)

    def _validate_ordinary(self, operations, undos) -> None:
        if len({item.authorization_id for item in operations}) != len(operations):
            raise _InconsistentOperationState
        if len({item.authorization_id for item in undos}) != len(undos):
            raise _InconsistentOperationState
        if len({item.operation_id for item in undos}) != len(undos):
            raise _InconsistentOperationState
        operations_by_id = {item.operation_id: item for item in operations}
        for undo in undos:
            parent = operations_by_id.get(undo.operation_id)
            if parent is None or parent.status != STATUS_VERIFIED:
                raise _InconsistentOperationState
            expected = tuple(
                (
                    move.change_id,
                    move.source,
                    move.destination,
                    move.source_size_bytes,
                    move.source_modified_time,
                )
                for move in parent.expected_moves
            )
            actual = tuple(
                (
                    item.change_id,
                    item.restore_target,
                    item.moved_from,
                    item.baseline_size_bytes,
                    item.baseline_modified_time,
                )
                for item in undo.expected_restores
            )
            if actual != expected:
                raise _InconsistentOperationState

    def _validate_duplicate_bin_movement(
        self, batches, authorizations, operations
    ) -> None:
        batches_by_id = {item.batch_id: item for item in batches}
        authorizations_by_id = {
            item.authorization_id: item for item in authorizations
        }
        operations_by_id = {item.operation_id: item for item in operations}
        operations_by_batch = {item.batch_id: item for item in operations}

        for authorization in authorizations:
            batch = batches_by_id.get(authorization.batch_id)
            if (
                authorization.configured_root_identity
                != self.duplicate_bin_batches.configured_root_identity
                or batch is None
                or authorization.approved_batch_digest
                != approved_batch_digest(batch)
                or authorization.selected_bin_item_ids
                != tuple(sorted(item.bin_item_id for item in batch.items))
            ):
                raise _InconsistentOperationState
            if authorization.status == MOVE_AUTH_AVAILABLE and (
                batch.status != BATCH_APPROVED
                or authorization.authorization_id
                in {item.authorization_id for item in operations}
            ):
                raise _InconsistentOperationState
            if authorization.status == MOVE_AUTH_CONSUMED:
                operation = operations_by_id.get(authorization.operation_id)
                if (
                    operation is None
                    or operation.authorization_id != authorization.authorization_id
                ):
                    raise _InconsistentOperationState

        for operation in operations:
            batch = batches_by_id.get(operation.batch_id)
            authorization = authorizations_by_id.get(operation.authorization_id)
            if (
                batch is None
                or authorization is None
                or authorization.status != MOVE_AUTH_CONSUMED
                or authorization.operation_id != operation.operation_id
                or operation.configured_root_identity
                != self.duplicate_bin_batches.configured_root_identity
                or operation.approved_batch_digest != approved_batch_digest(batch)
                or operation.batch_version > batch.batch_version
                or operation.selected_bin_item_ids
                != tuple(sorted(item.bin_item_id for item in batch.items))
            ):
                raise _InconsistentOperationState
            items = {item.bin_item_id: item for item in batch.items}
            for move in operation.expected_moves:
                item = items.get(move.bin_item_id)
                if (
                    item is None
                    or item.selected_member_id != move.member_id
                    or item.previous_relative_path != move.source
                    or item.future_relative_path != move.destination
                    or item.approved_sha256 != move.expected_sha256
                    or item.size_bytes != move.source_size_bytes
                ):
                    raise _InconsistentOperationState

        for batch in batches:
            if batch.status not in {
                BATCH_APPROVED,
                BATCH_SUPERSEDED,
                BATCH_CANCELLED,
            } and batch.batch_id not in operations_by_batch:
                raise _InconsistentOperationState

    def _validate_duplicate_bin_restore(
        self, batches, movement_operations, authorizations, operations
    ) -> None:
        batches_by_id = {item.batch_id: item for item in batches}
        movements_by_id = {item.operation_id: item for item in movement_operations}
        authorizations_by_id = {
            item.authorization_id: item for item in authorizations
        }
        operations_by_id = {
            item.restore_operation_id: item for item in operations
        }
        for authorization in authorizations:
            if (
                authorization.configured_root_identity
                != self.duplicate_bin_restores.configured_root_identity
            ):
                raise _InconsistentOperationState
            if authorization.status == RESTORE_AUTH_AVAILABLE and (
                authorization.authorization_id
                in {item.authorization_id for item in operations}
            ):
                raise _InconsistentOperationState
            if authorization.status == RESTORE_AUTH_CONSUMED:
                operation = operations_by_id.get(authorization.restore_operation_id)
                if (
                    operation is None
                    or operation.authorization_id != authorization.authorization_id
                ):
                    raise _InconsistentOperationState
            if authorization.status == RESTORE_AUTH_SUPERSEDED:
                successor = authorizations_by_id.get(authorization.superseded_by)
                if successor is None or not set(
                    authorization.selected_bin_item_ids
                ).intersection(successor.selected_bin_item_ids):
                    raise _InconsistentOperationState
            self._validate_restore_references(
                authorization.items,
                batches_by_id,
                movements_by_id,
            )

        for operation in operations:
            authorization = authorizations_by_id.get(operation.authorization_id)
            if (
                authorization is None
                or authorization.status != RESTORE_AUTH_CONSUMED
                or authorization.restore_operation_id
                != operation.restore_operation_id
                or operation.configured_root_identity
                != self.duplicate_bin_restores.configured_root_identity
                or authorization.selected_bin_item_ids
                != operation.selected_bin_item_ids
                or authorization.parent_movement_operation_ids
                != operation.parent_movement_operation_ids
            ):
                raise _InconsistentOperationState
            self._validate_restore_references(
                operation.items,
                batches_by_id,
                movements_by_id,
            )

        catalog_restored = {
            item.bin_item_id
            for batch in batches
            for item in batch.items
            if item.state == BIN_ITEM_RESTORED
        }
        operation_restored = {
            item.bin_item_id
            for operation in operations
            for item in operation.items
            if item.state == RESTORE_ITEM_VERIFIED
        }
        if catalog_restored != operation_restored:
            raise _InconsistentOperationState

    @staticmethod
    def _validate_restore_references(items, batches_by_id, movements_by_id) -> None:
        for item in items:
            batch = batches_by_id.get(item.batch_id)
            movement = movements_by_id.get(item.movement_operation_id)
            if (
                batch is None
                or movement is None
                or movement.batch_id != item.batch_id
                or getattr(item, "batch_version", 1) > batch.batch_version
            ):
                raise _InconsistentOperationState
            batch_item = next(
                (
                    candidate
                    for candidate in batch.items
                    if candidate.bin_item_id == item.bin_item_id
                ),
                None,
            )
            move = next(
                (
                    candidate
                    for candidate in movement.expected_moves
                    if candidate.bin_item_id == item.bin_item_id
                ),
                None,
            )
            if batch_item is None or move is None:
                raise _InconsistentOperationState
            protected_source = getattr(item, "protected_source", None)
            previous_destination = getattr(item, "previous_destination", None)
            if protected_source is not None and (
                protected_source != batch_item.future_relative_path
                or previous_destination != batch_item.previous_relative_path
                or item.expected_sha256 != move.expected_sha256
                or item.expected_size_bytes != move.source_size_bytes
            ):
                raise _InconsistentOperationState
