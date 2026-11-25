from __future__ import annotations

import errno
import hashlib
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.duplicate_bin_models import canonical_json_bytes
from organizer.application.duplicate_bin_movement_models import OP_TERMINAL_STATUSES
from organizer.application.duplicate_bin_restore_models import RESTORE_TERMINAL
from organizer.application.file_identity_capture import (
    CapturedRegularFileIdentity,
    FileIdentityCaptureError,
    capture_regular_file_identity,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
    _InconsistentOperationState,
)
from organizer.application.organization_identity_verification import (
    IdentityVerificationEligibilityError,
    require_identity_organization_verification_eligible,
)
from organizer.application.organization_operation_models import (
    BLOCKING_OPERATION_STATUSES,
    MOVE_MOVED,
    MOVE_PENDING,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVING,
    STATUS_PARTIAL_MOVEMENT,
    VERIFICATION_NOT_RUN,
    OrganizationOperationRecord,
)
from organizer.application.organization_undo_identity_verification import (
    IdentityUndoVerificationEligibilityError,
    require_identity_organization_undo_verification_eligible,
)
from organizer.application.organization_undo_models import (
    ORGANIZATION_UNDO_SCHEMA_VERSION_V2,
    UNDO_INTERRUPTED,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
    UNDO_VERIFICATION_NOT_RUN,
    OrganizationUndoRecord,
)
from organizer.application.volume_admission import (
    OrganizationVolumeAdmission,
    OrganizationVolumePathPair,
    VolumeAdmissionState,
)
from organizer.file_identity import RegularFileStatFingerprint


class RecoveryContinueEligibilityState(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNAVAILABLE = "unavailable"


class RecoveryContinueFamily(StrEnum):
    ORGANIZATION_MOVEMENT = "organization_movement"
    ORGANIZATION_UNDO = "organization_undo"


@dataclass(frozen=True, repr=False)
class PendingContinueCaptureEvidence:
    change_id: str
    content_identity: RegularFileContentIdentity
    stat_fingerprint: RegularFileStatFingerprint

    def __repr__(self) -> str:
        return "PendingContinueCaptureEvidence(<identity redacted>)"


@dataclass(frozen=True, repr=False)
class RecoveryContinueEligibilityResult:
    state: RecoveryContinueEligibilityState
    family: RecoveryContinueFamily | None = None
    operation_id: str | None = None
    parent_operation_id: str | None = None
    completed_change_ids: tuple[str, ...] = ()
    pending_change_ids: tuple[str, ...] = ()
    durable_record_digest: str | None = None
    completed_subset_digest: str | None = None
    pending_subset_digest: str | None = None
    filesystem_observation_digest: str | None = None
    pending_capture_evidence: tuple[PendingContinueCaptureEvidence, ...] = ()

    def __post_init__(self) -> None:
        if self.state is RecoveryContinueEligibilityState.ELIGIBLE:
            if (
                self.family is None
                or self.operation_id is None
                or not self.completed_change_ids
                or not self.pending_change_ids
                or len(self.pending_change_ids) != len(self.pending_capture_evidence)
                or any(
                    value is None
                    for value in (
                        self.durable_record_digest,
                        self.completed_subset_digest,
                        self.pending_subset_digest,
                        self.filesystem_observation_digest,
                    )
                )
            ):
                raise ValueError("Continue eligibility binding is incomplete")
        elif any(
            (
                self.family,
                self.operation_id,
                self.parent_operation_id,
                self.completed_change_ids,
                self.pending_change_ids,
                self.durable_record_digest,
                self.completed_subset_digest,
                self.pending_subset_digest,
                self.filesystem_observation_digest,
                self.pending_capture_evidence,
            )
        ):
            raise ValueError("ineligible Continue result contains binding detail")

    def __repr__(self) -> str:
        return (
            "RecoveryContinueEligibilityResult("
            f"state={self.state.value!r}, "
            f"completed={len(self.completed_change_ids)}, "
            f"pending={len(self.pending_change_ids)})"
        )


class _Ineligible(Exception):
    pass


class _Unavailable(Exception):
    pass


_UNDO_BLOCKING = {
    UNDO_PREPARED,
    UNDO_UNDOING,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_INTERRUPTED,
    UNDO_PARTIAL,
}


class RecoveryContinueEligibilityInspector:
    """Prove one exact schema-v2 Continue candidate without changing state."""

    def __init__(
        self,
        inspector: FilesystemOperationInspector,
        *,
        capture=capture_regular_file_identity,
        volume_admission: OrganizationVolumeAdmission | None = None,
    ) -> None:
        if not isinstance(inspector, FilesystemOperationInspector):
            raise ValueError("Continue inspector owner is invalid")
        self.inspector = inspector
        self.root = inspector.root
        self.configured_root_identity = (
            inspector.organization_operations.configured_root_identity
        )
        self._capture = capture
        self._volume = volume_admission or OrganizationVolumeAdmission(self.root)
        if self._volume.root != self.root:
            raise ValueError("Continue volume owner belongs to another root")

    def inspect(self) -> RecoveryContinueEligibilityResult:
        try:
            candidate, state = self._candidate()
            if candidate is None:
                raise _Ineligible
            if isinstance(candidate, OrganizationOperationRecord):
                return self._movement(candidate)
            return self._undo(candidate, state)
        except _Ineligible:
            return RecoveryContinueEligibilityResult(
                RecoveryContinueEligibilityState.INELIGIBLE
            )
        except _Unavailable:
            return RecoveryContinueEligibilityResult(
                RecoveryContinueEligibilityState.UNAVAILABLE
            )
        except (OSError, UnicodeError, ValueError):
            return RecoveryContinueEligibilityResult(
                RecoveryContinueEligibilityState.UNAVAILABLE
            )
        except Exception:
            return RecoveryContinueEligibilityResult(
                RecoveryContinueEligibilityState.UNAVAILABLE
            )

    def _candidate(self) -> tuple[object | None, dict[str, object]]:
        try:
            organization = self.inspector.organization_operations.list_records()
            undos = self.inspector.organization_undos.list_records()
            catalog = self.inspector.duplicate_bin_batches.read_catalog()
            move_authorizations = (
                self.inspector.duplicate_bin_authorizations.list_records()
            )
            duplicate_moves = self.inspector.duplicate_bin_operations.list_records()
            restore_authorizations = (
                self.inspector.duplicate_bin_restores.list_authorizations()
            )
            restores = self.inspector.duplicate_bin_restores.list_operations()
            self.inspector._validate_ordinary(organization, undos)
            self.inspector._validate_duplicate_bin_movement(
                catalog.batches,
                move_authorizations,
                duplicate_moves,
            )
            self.inspector._validate_duplicate_bin_restore(
                catalog.batches,
                duplicate_moves,
                restore_authorizations,
                restores,
            )
        except _InconsistentOperationState:
            raise _Ineligible from None
        except (OSError, UnicodeError, ValueError):
            raise _Unavailable from None

        unresolved = tuple(
            [
                item
                for item in organization
                if item.status in BLOCKING_OPERATION_STATUSES
            ]
            + [item for item in organization if item.status == STATUS_PARTIAL_MOVEMENT]
            + [item for item in undos if item.status in _UNDO_BLOCKING]
            + [
                item
                for item in duplicate_moves
                if item.status not in OP_TERMINAL_STATUSES
            ]
            + [item for item in restores if item.status not in RESTORE_TERMINAL]
        )
        state = {
            "organization": organization,
            "undos": undos,
        }
        return (unresolved[0] if len(unresolved) == 1 else None), state

    def _movement(
        self,
        record: OrganizationOperationRecord,
    ) -> RecoveryContinueEligibilityResult:
        self._require_movement_structure(record)
        completed = tuple(
            item for item in record.expected_moves if item.result_state == MOVE_MOVED
        )
        pending = tuple(
            item for item in record.expected_moves if item.result_state == MOVE_PENDING
        )
        completed_observations = tuple(
            self._completed_observation(
                item.change_id,
                item.source,
                item.destination,
                item.content_identity,
            )
            for item in completed
        )
        pending_observations, captures = self._pending_observations(
            tuple(
                (
                    item.change_id,
                    item.source,
                    item.destination,
                    item.content_identity,
                )
                for item in pending
            )
        )
        return self._eligible_result(
            family=RecoveryContinueFamily.ORGANIZATION_MOVEMENT,
            operation_id=record.operation_id,
            parent_operation_id=None,
            record_binding={"movement": asdict(record)},
            completed_items=completed,
            pending_items=pending,
            completed_observations=completed_observations,
            pending_observations=pending_observations,
            captures=captures,
            mapping=lambda item: {
                "change_id": item.change_id,
                "source": item.source,
                "destination": item.destination,
                "content_identity": asdict(item.content_identity),
            },
        )

    def _undo(
        self,
        record: OrganizationUndoRecord,
        state: dict[str, object],
    ) -> RecoveryContinueEligibilityResult:
        parent = next(
            (
                item
                for item in state["organization"]
                if item.operation_id == record.operation_id
            ),
            None,
        )
        if parent is None:
            raise _Ineligible
        self._require_undo_structure(record, parent)
        completed = tuple(
            item
            for item in record.expected_restores
            if item.result_state == UNDO_ITEM_RESTORED
        )
        pending = tuple(
            item
            for item in record.expected_restores
            if item.result_state == UNDO_ITEM_PENDING
        )
        completed_observations = tuple(
            self._completed_observation(
                item.change_id,
                item.moved_from,
                item.restore_target,
                item.content_identity,
            )
            for item in completed
        )
        pending_observations, captures = self._pending_observations(
            tuple(
                (
                    item.change_id,
                    item.moved_from,
                    item.restore_target,
                    item.content_identity,
                )
                for item in pending
            )
        )
        return self._eligible_result(
            family=RecoveryContinueFamily.ORGANIZATION_UNDO,
            operation_id=record.undo_id,
            parent_operation_id=parent.operation_id,
            record_binding={"undo": asdict(record), "parent": asdict(parent)},
            completed_items=completed,
            pending_items=pending,
            completed_observations=completed_observations,
            pending_observations=pending_observations,
            captures=captures,
            mapping=lambda item: {
                "change_id": item.change_id,
                "source": item.moved_from,
                "destination": item.restore_target,
                "content_identity": asdict(item.content_identity),
            },
        )

    def _require_movement_structure(
        self,
        record: OrganizationOperationRecord,
    ) -> None:
        states = tuple(item.result_state for item in record.expected_moves)
        if (
            record.schema_version != ORGANIZATION_OPERATION_SCHEMA_VERSION_V2
            or record.status not in {STATUS_MOVING, STATUS_INTERRUPTED}
            or record.verification.status != VERIFICATION_NOT_RUN
            or record.verification.verified_count != 0
            or record.verification.mismatch_codes
            or not record.expected_moves
            or MOVE_MOVED not in states
            or MOVE_PENDING not in states
            or any(state not in {MOVE_MOVED, MOVE_PENDING} for state in states)
            or not isinstance(record.started_at, str)
            or not _valid_timestamp(record.started_at)
            or not _valid_timestamp(record.created_at)
            or not _valid_timestamp(record.approved_at)
            or (
                record.status == STATUS_MOVING
                and (
                    record.completed_at is not None
                    or record.failure_code is not None
                    or record.legacy_operation_log is not None
                )
            )
            or (
                record.status == STATUS_INTERRUPTED
                and (
                    not _valid_timestamp(record.completed_at)
                    or not isinstance(record.failure_code, str)
                    or not record.failure_code
                )
            )
            or any(
                item.failure_code is not None or item.recovery_state is not None
                for item in record.expected_moves
            )
        ):
            raise _Ineligible
        candidate = replace(
            record,
            status=STATUS_MOVED_UNVERIFIED,
            completed_at=None,
            failure_code=None,
            expected_moves=tuple(
                replace(item, result_state=MOVE_MOVED) for item in record.expected_moves
            ),
        )
        try:
            require_identity_organization_verification_eligible(
                candidate,
                self.root,
            )
        except IdentityVerificationEligibilityError:
            raise _Ineligible from None

    def _require_undo_structure(
        self,
        record: OrganizationUndoRecord,
        parent: OrganizationOperationRecord,
    ) -> None:
        states = tuple(item.result_state for item in record.expected_restores)
        if (
            record.schema_version != ORGANIZATION_UNDO_SCHEMA_VERSION_V2
            or record.status not in {UNDO_UNDOING, UNDO_INTERRUPTED}
            or record.verification.status != UNDO_VERIFICATION_NOT_RUN
            or record.verification.verified_count != 0
            or record.verification.mismatch_codes
            or not record.expected_restores
            or UNDO_ITEM_RESTORED not in states
            or UNDO_ITEM_PENDING not in states
            or any(
                state not in {UNDO_ITEM_RESTORED, UNDO_ITEM_PENDING} for state in states
            )
            or not isinstance(record.started_at, str)
            or not _valid_timestamp(record.started_at)
            or not _valid_timestamp(record.created_at)
            or not _valid_timestamp(parent.approved_at)
            or not _valid_timestamp(parent.created_at)
            or not _valid_timestamp(parent.started_at)
            or not _valid_timestamp(parent.completed_at)
            or (
                record.status == UNDO_UNDOING
                and (
                    record.completed_at is not None
                    or record.failure_code is not None
                    or record.legacy_undo_log is not None
                )
            )
            or (
                record.status == UNDO_INTERRUPTED
                and (
                    not _valid_timestamp(record.completed_at)
                    or not isinstance(record.failure_code, str)
                    or not record.failure_code
                )
            )
            or any(
                item.failure_code is not None or item.recovery_state is not None
                for item in record.expected_restores
            )
        ):
            raise _Ineligible
        candidate = replace(
            record,
            status=UNDO_RESTORED_UNVERIFIED,
            completed_at=None,
            failure_code=None,
            expected_restores=tuple(
                replace(item, result_state=UNDO_ITEM_RESTORED)
                for item in record.expected_restores
            ),
        )
        try:
            require_identity_organization_undo_verification_eligible(
                candidate,
                parent,
                self.root,
            )
        except IdentityUndoVerificationEligibilityError:
            raise _Ineligible from None

    def _completed_observation(
        self,
        change_id: str,
        source: str,
        destination: str,
        identity: RegularFileContentIdentity,
    ) -> dict[str, object]:
        self._require_absent(source)
        captured = self._capture_identity(destination)
        if captured.content_identity != identity:
            raise _Ineligible
        return {
            "change_id": change_id,
            "source_absent": True,
            "destination_capture": _capture_dict(captured),
        }

    def _pending_observations(
        self,
        items: tuple[
            tuple[str, str, str, RegularFileContentIdentity],
            ...,
        ],
    ) -> tuple[
        tuple[dict[str, object], ...],
        tuple[PendingContinueCaptureEvidence, ...],
    ]:
        captures: list[PendingContinueCaptureEvidence] = []
        observations: list[dict[str, object]] = []
        pairs = tuple(
            OrganizationVolumePathPair(source, destination)
            for _, source, destination, _ in items
        )
        for change_id, source, destination, identity in items:
            captured = self._capture_identity(source)
            if captured.content_identity != identity:
                raise _Ineligible
            self._require_absent(destination)
            evidence = PendingContinueCaptureEvidence(
                change_id,
                captured.content_identity,
                captured.stat_fingerprint,
            )
            captures.append(evidence)
            observations.append(
                {
                    "change_id": change_id,
                    "source_capture": _capture_dict(captured),
                    "destination_absent": True,
                    "same_volume": True,
                }
            )
        volume = self._volume.inspect_path_pairs(pairs)
        if volume.state is VolumeAdmissionState.INDETERMINATE:
            raise _Unavailable
        if volume.state is not VolumeAdmissionState.ALLOWED:
            raise _Ineligible
        return tuple(observations), tuple(captures)

    def _capture_identity(self, relative: str) -> CapturedRegularFileIdentity:
        try:
            return self._capture(self.root / relative, self.root)
        except FileIdentityCaptureError as error:
            if error.code in {
                "source_unreadable",
                "source_changed_during_hash",
                "identity_capture_failed",
            }:
                raise _Unavailable from None
            raise _Ineligible from None
        except (OSError, UnicodeError):
            raise _Unavailable from None

    def _require_absent(self, relative: str) -> None:
        parts = tuple(Path(relative).parts)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.root,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
            )
            for component in parts[:-1]:
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    return
                except OSError as error:
                    if error.errno in {errno.ENOTDIR, errno.ELOOP}:
                        raise _Ineligible from None
                    raise _Unavailable from None
                os.close(descriptor)
                descriptor = child
            try:
                os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError:
                raise _Unavailable from None
            raise _Ineligible
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _eligible_result(
        self,
        *,
        family: RecoveryContinueFamily,
        operation_id: str,
        parent_operation_id: str | None,
        record_binding: dict[str, object],
        completed_items: tuple[object, ...],
        pending_items: tuple[object, ...],
        completed_observations: tuple[dict[str, object], ...],
        pending_observations: tuple[dict[str, object], ...],
        captures: tuple[PendingContinueCaptureEvidence, ...],
        mapping,
    ) -> RecoveryContinueEligibilityResult:
        completed_binding = tuple(mapping(item) for item in completed_items)
        pending_binding = tuple(mapping(item) for item in pending_items)
        complete_digest = _digest(
            {
                "schema": "loci-flow-recovery-continue-record-v1",
                "root_identity": self.configured_root_identity,
                "family": family.value,
                "record": record_binding,
            }
        )
        completed_digest = _digest(
            {
                "schema": "loci-flow-recovery-continue-completed-v1",
                "items": completed_binding,
                "observations": completed_observations,
            }
        )
        pending_digest = _digest(
            {
                "schema": "loci-flow-recovery-continue-pending-v1",
                "items": pending_binding,
                "observations": pending_observations,
            }
        )
        observation_digest = _digest(
            {
                "schema": "loci-flow-recovery-continue-observation-v1",
                "completed": completed_observations,
                "pending": pending_observations,
            }
        )
        return RecoveryContinueEligibilityResult(
            state=RecoveryContinueEligibilityState.ELIGIBLE,
            family=family,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            completed_change_ids=tuple(item.change_id for item in completed_items),
            pending_change_ids=tuple(item.change_id for item in pending_items),
            durable_record_digest=complete_digest,
            completed_subset_digest=completed_digest,
            pending_subset_digest=pending_digest,
            filesystem_observation_digest=observation_digest,
            pending_capture_evidence=captures,
        )


def _capture_dict(value: CapturedRegularFileIdentity) -> dict[str, object]:
    return {
        "content_identity": asdict(value.content_identity),
        "stat_fingerprint": asdict(value.stat_fingerprint),
    }


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None
