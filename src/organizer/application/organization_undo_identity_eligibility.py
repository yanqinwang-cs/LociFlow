from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import os
from pathlib import Path
from typing import Callable

from organizer.application.file_identity_capture import (
    CapturedRegularFileIdentity,
    FileIdentityCaptureError,
    capture_regular_file_identity,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationBlockedError,
    FilesystemOperationInspector,
)
from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_UNATTEMPTED,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVEMENT_FAILED,
    STATUS_MOVING,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_PREPARED,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    OrganizationOperationRecord,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_identity_intent import (
    OrganizationUndoIdentityCapture,
    validate_v2_organization_undo_parent,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.volume_admission import (
    OrganizationVolumeAdmission,
    OrganizationVolumePathPair,
    VolumeAdmissionState,
)


class IdentityUndoEligibilityState(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNAVAILABLE = "unavailable"


class IdentityUndoEligibilityReason(str, Enum):
    PARENT_UNAVAILABLE = "parent_unavailable"
    PARENT_NOT_V2 = "parent_not_v2"
    PARENT_NOT_VERIFIED = "parent_not_verified"
    PARENT_INVALID = "parent_invalid"
    PARENT_SUPERSEDED = "parent_superseded"
    WRONG_ROOT = "wrong_root"
    EXISTING_UNDO = "existing_undo"
    CROSS_FAMILY_BLOCKED = "cross_family_blocked"
    SOURCE_MISSING = "source_missing"
    SOURCE_UNSAFE = "source_unsafe"
    CONTENT_CHANGED = "content_changed"
    RESTORE_TARGET_OCCUPIED = "restore_target_occupied"
    UNSAFE_RESTORE_PATH = "unsafe_restore_path"
    CROSS_VOLUME = "cross_volume"
    INSPECTION_UNAVAILABLE = "inspection_unavailable"


@dataclass(frozen=True, repr=False)
class IdentityUndoEligibility:
    state: IdentityUndoEligibilityState
    reason: IdentityUndoEligibilityReason | None
    parent: OrganizationOperationRecord | None = None
    captures: tuple[OrganizationUndoIdentityCapture, ...] = ()
    reverse_path_pairs: tuple[OrganizationVolumePathPair, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.state is IdentityUndoEligibilityState.ELIGIBLE

    def __repr__(self) -> str:
        return (
            "IdentityUndoEligibility("
            f"state={self.state.value!r}, reason="
            f"{None if self.reason is None else self.reason.value!r}, "
            f"items={len(self.captures)})"
        )


class OrganizationUndoIdentityEligibilityInspector:
    """Read-only, root-bound eligibility for a complete v2 ordinary Undo."""

    def __init__(
        self,
        operation_repository: OrganizationOperationRepository,
        undo_repository: OrganizationUndoRepository,
        *,
        filesystem_operation_admission: FilesystemOperationAdmission | None = None,
        volume_admission: OrganizationVolumeAdmission | None = None,
        capture: Callable[[Path, Path], CapturedRegularFileIdentity] = (
            capture_regular_file_identity
        ),
        lexists: Callable[[Path], bool] = os.path.lexists,
    ) -> None:
        root = operation_repository.root
        if undo_repository.root != root:
            raise ValueError("Undo eligibility repositories belong to different roots")
        filesystem_admission = filesystem_operation_admission or (
            FilesystemOperationAdmission(
                FilesystemOperationInspector(
                    root,
                    organization_operations=operation_repository,
                    organization_undos=undo_repository,
                )
            )
        )
        volume = volume_admission or OrganizationVolumeAdmission(root)
        if filesystem_admission.root != root or volume.root != root:
            raise ValueError("Undo eligibility admission belongs to another root")
        self.root = root
        self.operation_repository = operation_repository
        self.undo_repository = undo_repository
        self.filesystem_operation_admission = filesystem_admission
        self.volume_admission = volume
        self._capture = capture
        self._lexists = lexists

    def inspect(self, operation_id: str) -> IdentityUndoEligibility:
        try:
            parent = self.operation_repository.load(operation_id)
            operations = self.operation_repository.list_records()
        except (OSError, UnicodeError, ValueError):
            return _unavailable(IdentityUndoEligibilityReason.PARENT_UNAVAILABLE)

        if parent.schema_version != 2:
            return _ineligible(IdentityUndoEligibilityReason.PARENT_NOT_V2)
        if parent.status != STATUS_VERIFIED:
            return _ineligible(IdentityUndoEligibilityReason.PARENT_NOT_VERIFIED)
        if parent.configured_root_identity != self.operation_repository.configured_root_identity:
            return _ineligible(IdentityUndoEligibilityReason.WRONG_ROOT)
        try:
            validate_v2_organization_undo_parent(
                parent,
                self.operation_repository.configured_root_identity,
            )
        except ValueError:
            return _ineligible(IdentityUndoEligibilityReason.PARENT_INVALID)

        try:
            meaningful = tuple(
                sorted(
                    (item for item in operations if _is_meaningful(item)),
                    key=_operation_created_key,
                )
            )
        except (TypeError, ValueError):
            return _unavailable(IdentityUndoEligibilityReason.PARENT_UNAVAILABLE)
        if not meaningful or meaningful[-1].operation_id != parent.operation_id:
            return _ineligible(IdentityUndoEligibilityReason.PARENT_SUPERSEDED)

        try:
            if self.undo_repository.linked_records(parent.operation_id):
                return _ineligible(IdentityUndoEligibilityReason.EXISTING_UNDO)
            self.filesystem_operation_admission.inspect_and_require_clear()
        except FilesystemOperationBlockedError:
            return _ineligible(IdentityUndoEligibilityReason.CROSS_FAMILY_BLOCKED)
        except (OSError, UnicodeError, ValueError):
            return _unavailable(IdentityUndoEligibilityReason.INSPECTION_UNAVAILABLE)

        pairs = tuple(
            OrganizationVolumePathPair(
                source_relative_path=item.destination,
                destination_relative_path=item.source,
            )
            for item in parent.expected_moves
        )
        for pair in pairs:
            try:
                occupied = self._lexists(self.root / pair.destination_relative_path)
            except (OSError, TypeError, ValueError):
                return _unavailable(IdentityUndoEligibilityReason.INSPECTION_UNAVAILABLE)
            if occupied:
                return _ineligible(
                    IdentityUndoEligibilityReason.RESTORE_TARGET_OCCUPIED
                )

        captures: list[OrganizationUndoIdentityCapture] = []
        for item in parent.expected_moves:
            try:
                captured = self._capture(self.root / item.destination, self.root)
            except FileIdentityCaptureError as error:
                if error.code == "source_missing":
                    return _ineligible(IdentityUndoEligibilityReason.SOURCE_MISSING)
                if error.code in {
                    "source_unreadable",
                    "source_changed_during_hash",
                    "identity_capture_failed",
                }:
                    return _unavailable(
                        IdentityUndoEligibilityReason.INSPECTION_UNAVAILABLE
                    )
                return _ineligible(IdentityUndoEligibilityReason.SOURCE_UNSAFE)
            except (OSError, UnicodeError, ValueError):
                return _unavailable(
                    IdentityUndoEligibilityReason.INSPECTION_UNAVAILABLE
                )
            if not isinstance(captured, CapturedRegularFileIdentity):
                return _unavailable(
                    IdentityUndoEligibilityReason.INSPECTION_UNAVAILABLE
                )
            if captured.content_identity != item.content_identity:
                return _ineligible(IdentityUndoEligibilityReason.CONTENT_CHANGED)
            captures.append(
                OrganizationUndoIdentityCapture(
                    change_id=item.change_id,
                    observed_content_identity=captured.content_identity,
                    stat_fingerprint=captured.stat_fingerprint,
                )
            )

        try:
            volume = self.volume_admission.inspect_path_pairs(pairs)
        except (OSError, TypeError, ValueError):
            return _unavailable(IdentityUndoEligibilityReason.INSPECTION_UNAVAILABLE)
        if volume.state is VolumeAdmissionState.CROSS_VOLUME:
            return _ineligible(IdentityUndoEligibilityReason.CROSS_VOLUME)
        if volume.state is VolumeAdmissionState.UNSAFE:
            return _ineligible(IdentityUndoEligibilityReason.UNSAFE_RESTORE_PATH)
        if volume.state is VolumeAdmissionState.INDETERMINATE:
            return _unavailable(IdentityUndoEligibilityReason.INSPECTION_UNAVAILABLE)

        return IdentityUndoEligibility(
            state=IdentityUndoEligibilityState.ELIGIBLE,
            reason=None,
            parent=parent,
            captures=tuple(captures),
            reverse_path_pairs=pairs,
        )


def _ineligible(reason: IdentityUndoEligibilityReason) -> IdentityUndoEligibility:
    return IdentityUndoEligibility(IdentityUndoEligibilityState.INELIGIBLE, reason)


def _unavailable(reason: IdentityUndoEligibilityReason) -> IdentityUndoEligibility:
    return IdentityUndoEligibility(IdentityUndoEligibilityState.UNAVAILABLE, reason)


def _operation_created_key(record: OrganizationOperationRecord) -> tuple[datetime, str]:
    created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("operation timestamp is invalid")
    return created, record.operation_id


def _is_meaningful(record: OrganizationOperationRecord) -> bool:
    if record.status == STATUS_MOVEMENT_FAILED:
        return any(
            item.result_state not in {MOVE_FAILED, MOVE_UNATTEMPTED}
            for item in record.expected_moves
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
        or any(
            item.result_state in {MOVE_MOVED, MOVE_ATTEMPTING}
            for item in record.expected_moves
        )
    )
