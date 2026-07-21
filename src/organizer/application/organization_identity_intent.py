from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Iterable, Mapping
import uuid

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.execution_coordinator import approved_plan_identity
from organizer.application.file_identity_capture import (
    CapturedRegularFileIdentity,
    RegularFileStatFingerprint,
)
from organizer.application.organization_decision_models import (
    ApprovedOrganizationPlan,
    STATUS_READY_TO_MOVE,
)
from organizer.application.organization_operation_models import (
    AUTHORIZATION_AVAILABLE,
    MOVE_PENDING,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_PREPARED,
    MovementAuthorization,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)


_ROOT_IDENTITY = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class OrganizationMoveIdentityEvidence:
    change_id: str
    stat_fingerprint: RegularFileStatFingerprint


@dataclass(frozen=True)
class V2OrganizationIdentityIntent:
    record: OrganizationOperationRecord
    executor_evidence: tuple[OrganizationMoveIdentityEvidence, ...]


def build_v2_organization_intent(
    *,
    operation_id: str,
    created_at: str,
    authorization: MovementAuthorization,
    approved_plan: ApprovedOrganizationPlan,
    configured_root_identity: str,
    expected_moves: tuple[OrganizationExpectedMove, ...],
    captures: (
        Mapping[str, CapturedRegularFileIdentity]
        | Iterable[tuple[str, CapturedRegularFileIdentity]]
    ),
) -> V2OrganizationIdentityIntent:
    """Build, but never persist, a complete v2 prepared movement intent.

    Capture is deliberately separate: identity starts at explicit confirmation,
    not at scan or approval, and does not change authorization/plan digests.
    """

    _canonical_uuid(operation_id, "operation ID")
    _canonical_uuid(authorization.authorization_id, "authorization ID")
    _nonempty(created_at, "created timestamp")
    if (
        not isinstance(configured_root_identity, str)
        or _ROOT_IDENTITY.fullmatch(configured_root_identity) is None
    ):
        raise ValueError("configured root identity is invalid")
    if approved_plan.status != STATUS_READY_TO_MOVE:
        raise ValueError("approved plan is not ready to move")
    if (
        authorization.status != AUTHORIZATION_AVAILABLE
        or authorization.consumed_at is not None
        or authorization.scan_generation != approved_plan.scan_generation
        or authorization.decision_revision != approved_plan.decision_revision
        or authorization.approved_at != approved_plan.approved_at
        or authorization.approved_plan_identity != approved_plan_identity(approved_plan)
        or authorization.configured_root_identity != configured_root_identity
    ):
        raise ValueError("movement authorization does not match approved plan")

    approved_ids = tuple(item.id for item in approved_plan.reviewed_move_items)
    move_ids = tuple(item.change_id for item in expected_moves)
    if (
        not approved_ids
        or move_ids != approved_ids
        or len(set(move_ids)) != len(move_ids)
    ):
        raise ValueError("expected moves do not match approved plan order")
    for reviewed, move in zip(approved_plan.reviewed_move_items, expected_moves):
        if reviewed.category != "organization" or reviewed.decision != "approved":
            raise ValueError("approved plan contains an unsupported item")
        if (
            move.content_identity is not None
            or move.result_state != MOVE_PENDING
            or move.failure_code is not None
            or move.recovery_state is not None
        ):
            raise ValueError("expected move is not a fresh regular-file intent")

    capture_by_id = _capture_mapping(captures)
    if set(capture_by_id) != set(move_ids):
        raise ValueError("identity captures do not exactly cover expected moves")

    versioned_moves: list[OrganizationExpectedMove] = []
    executor_evidence: list[OrganizationMoveIdentityEvidence] = []
    for move in expected_moves:
        capture = capture_by_id[move.change_id]
        if not isinstance(capture, CapturedRegularFileIdentity):
            raise ValueError("identity capture is invalid")
        if (
            not isinstance(capture.content_identity, RegularFileContentIdentity)
            or not isinstance(capture.stat_fingerprint, RegularFileStatFingerprint)
        ):
            raise ValueError("identity capture is invalid")
        if capture.stat_fingerprint.file_type != "regular_file":
            raise ValueError("identity capture item type is unsupported")
        if (
            capture.stat_fingerprint.size_bytes != move.source_size_bytes
            or capture.stat_fingerprint.modified_time_ns / 1_000_000_000
            != move.source_modified_time
        ):
            raise ValueError("identity capture does not match preflight evidence")
        versioned_moves.append(
            replace(move, content_identity=capture.content_identity)
        )
        executor_evidence.append(
            OrganizationMoveIdentityEvidence(
                change_id=move.change_id,
                stat_fingerprint=capture.stat_fingerprint,
            )
        )

    record = OrganizationOperationRecord(
        operation_id=operation_id,
        authorization_id=authorization.authorization_id,
        status=STATUS_PREPARED,
        scan_generation=approved_plan.scan_generation,
        decision_revision=approved_plan.decision_revision,
        approved_at=approved_plan.approved_at,
        created_at=created_at,
        started_at=None,
        completed_at=None,
        configured_root_identity=configured_root_identity,
        expected_moves=tuple(versioned_moves),
        verification=OrganizationOperationVerification(),
        legacy_operation_log=None,
        failure_code=None,
        duplicate_actions_deferred=len(approved_plan.duplicate_decisions),
        schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    )
    return V2OrganizationIdentityIntent(
        record=record,
        executor_evidence=tuple(executor_evidence),
    )


def _capture_mapping(
    captures: (
        Mapping[str, CapturedRegularFileIdentity]
        | Iterable[tuple[str, CapturedRegularFileIdentity]]
    ),
) -> dict[str, CapturedRegularFileIdentity]:
    entries = captures.items() if isinstance(captures, Mapping) else captures
    result: dict[str, CapturedRegularFileIdentity] = {}
    try:
        for change_id, capture in entries:
            if not isinstance(change_id, str) or not change_id or change_id in result:
                raise ValueError("identity capture change IDs are invalid")
            result[change_id] = capture
    except (TypeError, ValueError):
        raise ValueError("identity capture mapping is invalid") from None
    return result


def _canonical_uuid(value: str, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{label} is invalid") from None
    if str(parsed) != value:
        raise ValueError(f"{label} is invalid")


def _nonempty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
