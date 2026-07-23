from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import uuid

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.organization_operation_models import (
    MOVE_MOVED,
    ORGANIZATION_OPERATION_RECORD_TYPE,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_VERIFIED,
    VERIFICATION_PASSED,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_undo_models import (
    ORGANIZATION_UNDO_SCHEMA_VERSION_V2,
    UNDO_ITEM_PENDING,
    UNDO_PREPARED,
    OrganizationUndoItem,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.file_identity import RegularFileStatFingerprint


_ROOT_IDENTITY = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, repr=False)
class OrganizationUndoIdentityCapture:
    change_id: str
    observed_content_identity: RegularFileContentIdentity
    stat_fingerprint: RegularFileStatFingerprint

    def __repr__(self) -> str:
        return "OrganizationUndoIdentityCapture(<identity redacted>)"


@dataclass(frozen=True, repr=False)
class OrganizationUndoExecutorEvidence:
    change_id: str
    content_identity: RegularFileContentIdentity
    stat_fingerprint: RegularFileStatFingerprint

    def __repr__(self) -> str:
        return "OrganizationUndoExecutorEvidence(<identity redacted>)"


@dataclass(frozen=True, repr=False)
class V2OrganizationUndoIntent:
    record: OrganizationUndoRecord
    executor_evidence: tuple[OrganizationUndoExecutorEvidence, ...]

    def __repr__(self) -> str:
        return f"V2OrganizationUndoIntent(items={len(self.executor_evidence)})"


def build_v2_organization_undo_intent(
    *,
    parent: OrganizationOperationRecord,
    undo_id: str,
    authorization_id: str,
    configured_root_identity: str,
    created_at: str,
    captures: tuple[OrganizationUndoIdentityCapture, ...],
) -> V2OrganizationUndoIntent:
    """Purely construct complete v2 reverse intent from authoritative evidence."""

    validate_v2_organization_undo_parent(parent, configured_root_identity)
    _canonical_uuid(undo_id, "Undo ID")
    _canonical_uuid(authorization_id, "authorization ID")
    _nonempty(created_at, "creation timestamp")
    if not isinstance(captures, tuple) or any(
        not isinstance(item, OrganizationUndoIdentityCapture) for item in captures
    ):
        raise ValueError("Undo identity captures are invalid")
    capture_by_id: dict[str, OrganizationUndoIdentityCapture] = {}
    for capture in captures:
        if capture.change_id in capture_by_id:
            raise ValueError("Undo identity captures contain a duplicate")
        if not isinstance(capture.observed_content_identity, RegularFileContentIdentity):
            raise ValueError("Undo identity capture is invalid")
        if not isinstance(capture.stat_fingerprint, RegularFileStatFingerprint):
            raise ValueError("Undo identity capture is invalid")
        capture_by_id[capture.change_id] = capture

    parent_ids = tuple(item.change_id for item in parent.expected_moves)
    if set(capture_by_id) != set(parent_ids) or len(captures) != len(parent_ids):
        raise ValueError("Undo identity captures do not exactly cover the parent")

    restores: list[OrganizationUndoItem] = []
    executor_evidence: list[OrganizationUndoExecutorEvidence] = []
    for move in parent.expected_moves:
        capture = capture_by_id[move.change_id]
        if capture.observed_content_identity != move.content_identity:
            raise ValueError("Undo source content does not match the parent identity")
        restores.append(
            OrganizationUndoItem(
                change_id=move.change_id,
                restore_target=move.source,
                moved_from=move.destination,
                baseline_size_bytes=move.source_size_bytes,
                baseline_modified_time=move.source_modified_time,
                result_state=UNDO_ITEM_PENDING,
                content_identity=move.content_identity,
            )
        )
        executor_evidence.append(
            OrganizationUndoExecutorEvidence(
                change_id=move.change_id,
                content_identity=capture.observed_content_identity,
                stat_fingerprint=capture.stat_fingerprint,
            )
        )

    return V2OrganizationUndoIntent(
        record=OrganizationUndoRecord(
            undo_id=undo_id,
            operation_id=parent.operation_id,
            authorization_id=authorization_id,
            configured_root_identity=configured_root_identity,
            status=UNDO_PREPARED,
            created_at=created_at,
            started_at=None,
            completed_at=None,
            expected_restores=tuple(restores),
            verification=UndoVerificationResult(),
            legacy_undo_log=None,
            failure_code=None,
            schema_version=ORGANIZATION_UNDO_SCHEMA_VERSION_V2,
        ),
        executor_evidence=tuple(executor_evidence),
    )


def validate_v2_organization_undo_parent(
    parent: OrganizationOperationRecord,
    configured_root_identity: str,
) -> None:
    if (
        not isinstance(parent, OrganizationOperationRecord)
        or parent.record_type != ORGANIZATION_OPERATION_RECORD_TYPE
        or parent.schema_version != ORGANIZATION_OPERATION_SCHEMA_VERSION_V2
        or parent.status != STATUS_VERIFIED
        or not isinstance(parent.verification, OrganizationOperationVerification)
        or parent.verification.status != VERIFICATION_PASSED
        or not isinstance(parent.expected_moves, tuple)
        or not parent.expected_moves
        or parent.verification.verified_count != len(parent.expected_moves)
        or parent.verification.mismatch_codes
        or parent.failure_code is not None
        or not isinstance(parent.started_at, str)
        or not parent.started_at
        or not isinstance(parent.completed_at, str)
        or not parent.completed_at
        or parent.configured_root_identity != configured_root_identity
        or not isinstance(configured_root_identity, str)
        or _ROOT_IDENTITY.fullmatch(configured_root_identity) is None
        or not _canonical_uuid_value(parent.operation_id)
        or not _canonical_uuid_value(parent.authorization_id)
    ):
        raise ValueError("parent operation is not eligible for v2 Undo")

    change_ids: set[str] = set()
    sources: set[str] = set()
    destinations: set[str] = set()
    all_paths: list[Path] = []
    for move in parent.expected_moves:
        if not _valid_parent_item(move):
            raise ValueError("parent operation item is not eligible for v2 Undo")
        source = _canonical_relative_path(move.source)
        destination = _canonical_relative_path(move.destination)
        if (
            move.change_id in change_ids
            or source in sources
            or destination in destinations
        ):
            raise ValueError("parent operation paths or change IDs are not unique")
        change_ids.add(move.change_id)
        sources.add(source)
        destinations.add(destination)
        all_paths.extend((Path(source), Path(destination)))
    if sources & destinations or _has_ancestry_overlap(tuple(all_paths)):
        raise ValueError("parent operation paths overlap")


def _valid_parent_item(item: object) -> bool:
    if not isinstance(item, OrganizationExpectedMove):
        return False
    return (
        _canonical_change_id(item.change_id)
        and item.result_state == MOVE_MOVED
        and item.failure_code is None
        and item.recovery_state is None
        and isinstance(item.content_identity, RegularFileContentIdentity)
        and isinstance(item.reason, str)
        and bool(item.reason)
        and isinstance(item.confidence, int)
        and not isinstance(item.confidence, bool)
        and item.confidence >= 0
        and item.operation == "move"
        and isinstance(item.overwrite_risk, bool)
        and isinstance(item.source_size_bytes, int)
        and not isinstance(item.source_size_bytes, bool)
        and item.source_size_bytes >= 0
        and isinstance(item.source_modified_time, (int, float))
        and not isinstance(item.source_modified_time, bool)
        and math.isfinite(item.source_modified_time)
    )


def _canonical_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("parent operation path is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("parent operation path is invalid")
    canonical = path.as_posix()
    if canonical != value:
        raise ValueError("parent operation path is not canonical")
    return canonical


def _has_ancestry_overlap(paths: tuple[Path, ...]) -> bool:
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path in other.parents or other in path.parents:
                return True
    return False


def _canonical_change_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _canonical_uuid(value: object, label: str) -> str:
    if not _canonical_uuid_value(value):
        raise ValueError(f"{label} is invalid")
    return value


def _canonical_uuid_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
    return value
