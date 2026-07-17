from __future__ import annotations

from dataclasses import dataclass


RESTORE_SCHEMA_VERSION = 1
AUTH_RECORD_TYPE = "loci_duplicate_bin_restore_authorization"
OP_RECORD_TYPE = "loci_duplicate_bin_restore_operation"

AUTH_AVAILABLE = "available"
AUTH_SUPERSEDED = "superseded"
AUTH_CONSUMED = "consumed"
AUTH_STATUSES = frozenset({AUTH_AVAILABLE, AUTH_SUPERSEDED, AUTH_CONSUMED})

RESTORE_PREPARED = "prepared"
RESTORE_RESTORING = "restoring"
RESTORE_RESTORED_UNVERIFIED = "restored_unverified"
RESTORE_RESTORED = "restored"
RESTORE_PARTIAL = "partially_restored"
RESTORE_FAILED = "restore_failed"
RESTORE_VERIFICATION_FAILED = "verification_failed"
RESTORE_INTERRUPTED = "interrupted"
RESTORE_STATUSES = frozenset(
    {
        RESTORE_PREPARED,
        RESTORE_RESTORING,
        RESTORE_RESTORED_UNVERIFIED,
        RESTORE_RESTORED,
        RESTORE_PARTIAL,
        RESTORE_FAILED,
        RESTORE_VERIFICATION_FAILED,
        RESTORE_INTERRUPTED,
    }
)
RESTORE_TERMINAL = frozenset(
    {RESTORE_RESTORED, RESTORE_PARTIAL, RESTORE_FAILED, RESTORE_VERIFICATION_FAILED}
)

ITEM_PENDING = "pending"
ITEM_ATTEMPTING = "attempting"
ITEM_RESTORED = "restored"
ITEM_FAILED = "failed"
ITEM_UNATTEMPTED = "unattempted"
ITEM_VERIFIED = "verified"
ITEM_ATTENTION = "attention"
RESTORE_ITEM_STATES = frozenset(
    {
        ITEM_PENDING,
        ITEM_ATTEMPTING,
        ITEM_RESTORED,
        ITEM_FAILED,
        ITEM_UNATTEMPTED,
        ITEM_VERIFIED,
        ITEM_ATTENTION,
    }
)


@dataclass(frozen=True)
class RestoreAuthorizationItem:
    bin_item_id: str
    movement_operation_id: str
    batch_id: str
    batch_version: int
    item_state: str


@dataclass(frozen=True)
class DuplicateBinRestoreAuthorization:
    authorization_id: str
    browser_session_id: str
    configured_root_identity: str
    selected_bin_item_ids: tuple[str, ...]
    parent_movement_operation_ids: tuple[str, ...]
    items: tuple[RestoreAuthorizationItem, ...]
    issued_at: str
    expires_at: str
    status: str
    superseded_by: str | None
    restore_operation_id: str | None
    schema_version: int = RESTORE_SCHEMA_VERSION
    record_type: str = AUTH_RECORD_TYPE


@dataclass(frozen=True)
class DuplicateBinRestorePreview:
    copies_to_restore: int
    confirmation: str
    confirmation_token: str
    expires_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "copies_to_restore": self.copies_to_restore,
            "confirmation": self.confirmation,
            "confirmation_token": self.confirmation_token,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class DuplicateBinRestoreItem:
    bin_item_id: str
    movement_operation_id: str
    batch_id: str
    protected_source: str
    previous_destination: str
    expected_sha256: str
    expected_size_bytes: int
    source_modified_time_ns: int
    validated_parent_directories: tuple[str, ...]
    state: str = ITEM_PENDING
    attempted_at: str | None = None
    completed_at: str | None = None
    failure_code: str | None = None
    recovery_state: str | None = None


@dataclass(frozen=True)
class DuplicateBinRestoreVerification:
    status: str = "not_run"
    verified_count: int = 0
    mismatch_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DuplicateBinRestoreOperation:
    restore_operation_id: str
    authorization_id: str
    configured_root_identity: str
    selected_bin_item_ids: tuple[str, ...]
    parent_movement_operation_ids: tuple[str, ...]
    items: tuple[DuplicateBinRestoreItem, ...]
    preflight_digest: str
    status: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    verification: DuplicateBinRestoreVerification
    executor_log_path: str | None
    failure_code: str | None
    recovery_state: str | None
    schema_version: int = RESTORE_SCHEMA_VERSION
    record_type: str = OP_RECORD_TYPE


@dataclass(frozen=True)
class DuplicateBinRestoreProjection:
    id: str
    status: str
    copies_expected: int
    copies_restored: int
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
            "copies_restored": self.copies_restored,
            "copies_failed": self.copies_failed,
            "copies_unattempted": self.copies_unattempted,
            "verification_status": self.verification_status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "recovery_required": self.recovery_required,
        }


@dataclass(frozen=True)
class SafeDuplicateBinItem:
    id: str
    name: str
    previous_folder: str
    moved_at: str
    size: int
    kind: str
    restore_available: bool
    attention_message: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "previous_folder": self.previous_folder,
            "moved_at": self.moved_at,
            "size": self.size,
            "kind": self.kind,
            "restore_available": self.restore_available,
            "attention_message": self.attention_message,
        }
