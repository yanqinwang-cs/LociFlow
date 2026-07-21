from __future__ import annotations

from dataclasses import dataclass

from organizer.application.content_identity import (
    OrdinaryFileIdentityCompatibility,
    RegularFileContentIdentity,
    identity_compatibility,
)


ORGANIZATION_OPERATION_SCHEMA_VERSION = 1
ORGANIZATION_OPERATION_SCHEMA_VERSION_V2 = 2
ORGANIZATION_OPERATION_RECORD_TYPE = "loci_flow_organization_operation"

AUTHORIZATION_AVAILABLE = "available"
AUTHORIZATION_CONSUMED = "consumed"
AUTHORIZATION_INVALIDATED = "invalidated"

MOVE_PENDING = "pending"
MOVE_ATTEMPTING = "attempting"
MOVE_MOVED = "moved"
MOVE_FAILED = "failed"
MOVE_UNATTEMPTED = "unattempted"

STATUS_PREPARED = "prepared"
STATUS_MOVING = "moving"
STATUS_MOVED_UNVERIFIED = "moved_unverified"
STATUS_VERIFIED = "verified"
STATUS_PARTIAL_MOVEMENT = "partial_movement"
STATUS_MOVEMENT_FAILED = "movement_failed"
STATUS_VERIFICATION_FAILED = "verification_failed"
STATUS_INTERRUPTED = "interrupted"

VERIFICATION_NOT_RUN = "not_run"
VERIFICATION_PASSED = "passed"
VERIFICATION_FAILED = "failed"

BLOCKING_OPERATION_STATUSES = {
    STATUS_PREPARED,
    STATUS_MOVING,
    STATUS_MOVED_UNVERIFIED,
    STATUS_INTERRUPTED,
}
REPLAYABLE_TERMINAL_STATUSES = {
    STATUS_VERIFIED,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_MOVEMENT_FAILED,
    STATUS_VERIFICATION_FAILED,
}


@dataclass(frozen=True)
class MovementAuthorization:
    authorization_id: str
    scan_generation: str
    decision_revision: int
    approved_at: str
    approved_plan_identity: str
    issued_at: str
    consumed_at: str | None
    status: str
    browser_session_id: str | None = None
    configured_root_identity: str | None = None


@dataclass(frozen=True)
class OrganizationExpectedMove:
    change_id: str
    source: str
    destination: str
    source_size_bytes: int
    source_modified_time: float
    reason: str
    confidence: int
    operation: str
    overwrite_risk: bool
    result_state: str = MOVE_PENDING
    failure_code: str | None = None
    recovery_state: str | None = None
    content_identity: RegularFileContentIdentity | None = None


@dataclass(frozen=True)
class OrganizationOperationVerification:
    status: str = VERIFICATION_NOT_RUN
    verified_count: int = 0
    mismatch_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrganizationOperationRecord:
    operation_id: str
    authorization_id: str
    status: str
    scan_generation: str
    decision_revision: int
    approved_at: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    configured_root_identity: str
    expected_moves: tuple[OrganizationExpectedMove, ...]
    verification: OrganizationOperationVerification
    legacy_operation_log: str | None
    failure_code: str | None
    duplicate_actions_deferred: int
    schema_version: int = ORGANIZATION_OPERATION_SCHEMA_VERSION
    record_type: str = ORGANIZATION_OPERATION_RECORD_TYPE

    @property
    def identity_compatibility(self) -> OrdinaryFileIdentityCompatibility:
        return identity_compatibility(self.schema_version)


@dataclass(frozen=True)
class OrganizationOperationOutcome:
    operation_id: str
    status: str
    started_at: str | None
    completed_at: str | None
    files_expected: int
    files_moved: int
    files_failed: int
    files_unattempted: int
    verification_status: str


@dataclass(frozen=True)
class OrganizationRecoveryInspection:
    operation_id: str
    status: str
    likely_moved: int
    likely_not_moved: int
    recovery_required: int
