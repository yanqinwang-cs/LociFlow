from __future__ import annotations

from dataclasses import dataclass

from organizer.application.content_identity import (
    OrdinaryFileIdentityCompatibility,
    RegularFileContentIdentity,
    identity_compatibility,
)


ORGANIZATION_UNDO_SCHEMA_VERSION = 1
ORGANIZATION_UNDO_SCHEMA_VERSION_V2 = 2
ORGANIZATION_UNDO_RECORD_TYPE = "loci_flow_organization_undo"

UNDO_AUTHORIZATION_AVAILABLE = "available"
UNDO_AUTHORIZATION_CONSUMED = "consumed"

UNDO_PREPARED = "prepared"
UNDO_UNDOING = "undoing"
UNDO_RESTORED_UNVERIFIED = "restored_unverified"
UNDO_RESTORED = "restored"
UNDO_PARTIAL = "partial_undo"
UNDO_FAILED = "undo_failed"
UNDO_VERIFICATION_FAILED = "undo_verification_failed"
UNDO_INTERRUPTED = "interrupted"

UNDO_ITEM_PENDING = "pending"
UNDO_ITEM_ATTEMPTING = "attempting"
UNDO_ITEM_RESTORED = "restored"
UNDO_ITEM_FAILED = "failed"
UNDO_ITEM_UNATTEMPTED = "unattempted"

UNDO_VERIFICATION_NOT_RUN = "not_run"
UNDO_VERIFICATION_PASSED = "passed"
UNDO_VERIFICATION_FAILED_STATE = "failed"


@dataclass(frozen=True)
class UndoAuthorization:
    authorization_id: str
    browser_session_id: str
    configured_root_identity: str
    operation_id: str
    issued_at: str
    consumed_at: str | None
    status: str


@dataclass(frozen=True)
class OrganizationUndoItem:
    change_id: str
    restore_target: str
    moved_from: str
    baseline_size_bytes: int
    baseline_modified_time: float
    result_state: str = UNDO_ITEM_PENDING
    failure_code: str | None = None
    recovery_state: str | None = None
    content_identity: RegularFileContentIdentity | None = None


@dataclass(frozen=True)
class UndoVerificationResult:
    status: str = UNDO_VERIFICATION_NOT_RUN
    verified_count: int = 0
    mismatch_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrganizationUndoRecord:
    undo_id: str
    operation_id: str
    authorization_id: str
    configured_root_identity: str
    status: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    expected_restores: tuple[OrganizationUndoItem, ...]
    verification: UndoVerificationResult
    legacy_undo_log: str | None
    failure_code: str | None
    schema_version: int = ORGANIZATION_UNDO_SCHEMA_VERSION
    record_type: str = ORGANIZATION_UNDO_RECORD_TYPE

    @property
    def identity_compatibility(self) -> OrdinaryFileIdentityCompatibility:
        return identity_compatibility(self.schema_version)


@dataclass(frozen=True)
class OrganizationUndoOutcome:
    undo_id: str
    operation_id: str
    status: str
    started_at: str | None
    completed_at: str | None
    files_expected: int
    files_restored: int
    files_failed: int
    files_unattempted: int
    verification_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "undo_id": self.undo_id,
            "operation_id": self.operation_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "files_expected": self.files_expected,
            "files_restored": self.files_restored,
            "files_failed": self.files_failed,
            "files_unattempted": self.files_unattempted,
            "verification_status": self.verification_status,
        }


@dataclass(frozen=True)
class UndoPreview:
    operation_id: str
    files_to_restore: int
    confirmation_required: str
    confirmation_token: str

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "files_to_restore": self.files_to_restore,
            "confirmation": self.confirmation_required,
            "confirmation_token": self.confirmation_token,
        }


@dataclass(frozen=True)
class OrganizationUndoRecoveryInspection:
    undo_id: str
    status: str
    likely_restored: int
    likely_not_restored: int
    recovery_required: int
