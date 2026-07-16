from __future__ import annotations

from dataclasses import dataclass


DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION = 1
AUTHORIZATION_RECORD_TYPE = "loci_duplicate_bin_movement_authorization"
OPERATION_RECORD_TYPE = "loci_duplicate_bin_movement_operation"

AUTH_AVAILABLE = "available"
AUTH_SUPERSEDED = "superseded"
AUTH_CONSUMED = "consumed"
AUTH_STATUSES = frozenset({AUTH_AVAILABLE, AUTH_SUPERSEDED, AUTH_CONSUMED})

OP_PREPARED = "prepared"
OP_MOVING = "moving"
OP_MOVED_UNVERIFIED = "moved_unverified"
OP_MOVED = "moved"
OP_PARTIAL = "partially_moved"
OP_MOVEMENT_FAILED = "movement_failed"
OP_VERIFICATION_FAILED = "verification_failed"
OP_INTERRUPTED = "interrupted"
OP_STATUSES = frozenset(
    {
        OP_PREPARED,
        OP_MOVING,
        OP_MOVED_UNVERIFIED,
        OP_MOVED,
        OP_PARTIAL,
        OP_MOVEMENT_FAILED,
        OP_VERIFICATION_FAILED,
        OP_INTERRUPTED,
    }
)
OP_TERMINAL_STATUSES = frozenset(
    {OP_MOVED, OP_PARTIAL, OP_MOVEMENT_FAILED, OP_VERIFICATION_FAILED}
)

MOVE_PENDING = "pending"
MOVE_ATTEMPTING = "attempting"
MOVE_MOVED = "moved"
MOVE_FAILED = "failed"
MOVE_UNATTEMPTED = "unattempted"
MOVE_VERIFIED = "verified"
MOVE_ATTENTION = "attention"
MOVE_STATES = frozenset(
    {
        MOVE_PENDING,
        MOVE_ATTEMPTING,
        MOVE_MOVED,
        MOVE_FAILED,
        MOVE_UNATTEMPTED,
        MOVE_VERIFIED,
        MOVE_ATTENTION,
    }
)


@dataclass(frozen=True)
class DuplicateBinMovementAuthorization:
    authorization_id: str
    browser_session_id: str
    configured_root_identity: str
    batch_id: str
    approved_batch_digest: str
    selected_bin_item_ids: tuple[str, ...]
    expected_batch_version: int
    expected_batch_state: str
    issued_at: str
    expires_at: str
    status: str
    superseded_by: str | None
    operation_id: str | None
    schema_version: int = DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION
    record_type: str = AUTHORIZATION_RECORD_TYPE


@dataclass(frozen=True)
class DuplicateBinMovementPreview:
    copies_to_move: int
    confirmation: str
    confirmation_token: str
    expires_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "copies_to_move": self.copies_to_move,
            "confirmation": self.confirmation,
            "confirmation_token": self.confirmation_token,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class DuplicateBinExpectedMove:
    bin_item_id: str
    member_id: str
    source: str
    destination: str
    expected_sha256: str
    source_size_bytes: int
    source_modified_time_ns: int
    result_state: str = MOVE_PENDING
    attempted_at: str | None = None
    completed_at: str | None = None
    failure_code: str | None = None
    recovery_state: str | None = None


@dataclass(frozen=True)
class DuplicateBinVerification:
    status: str = "not_run"
    verified_count: int = 0
    mismatch_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DuplicateBinOperationRecord:
    operation_id: str
    authorization_id: str
    batch_id: str
    configured_root_identity: str
    approved_batch_digest: str
    batch_version: int
    preflight_digest: str
    selected_bin_item_ids: tuple[str, ...]
    expected_moves: tuple[DuplicateBinExpectedMove, ...]
    status: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    verification: DuplicateBinVerification
    executor_log_path: str | None
    failure_code: str | None
    recovery_state: str | None
    schema_version: int = DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION
    record_type: str = OPERATION_RECORD_TYPE


@dataclass(frozen=True)
class DuplicateBinOperationProjection:
    id: str
    status: str
    copies_expected: int
    copies_moved: int
    copies_failed: int
    copies_unattempted: int
    verification_status: str
    started_at: str | None
    completed_at: str | None
    recovery_required: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "copies_expected": self.copies_expected,
            "copies_moved": self.copies_moved,
            "copies_failed": self.copies_failed,
            "copies_unattempted": self.copies_unattempted,
            "verification_status": self.verification_status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "recovery_required": self.recovery_required,
        }
