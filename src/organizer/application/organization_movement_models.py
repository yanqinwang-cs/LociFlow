from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrganizationMovementPreview:
    files_to_move: int
    duplicate_copies_deferred: int
    confirmation: str
    confirmation_token: str

    def to_dict(self) -> dict[str, object]:
        return {
            "files_to_move": self.files_to_move,
            "duplicate_copies_deferred": self.duplicate_copies_deferred,
            "confirmation": self.confirmation,
            "confirmation_token": self.confirmation_token,
        }


@dataclass(frozen=True)
class OrganizationExecutionState:
    status: str
    operation_id: str | None
    files_expected: int
    files_moved: int
    files_failed: int
    files_unattempted: int
    verification_status: str | None
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "operation_id": self.operation_id,
            "files_expected": self.files_expected,
            "files_moved": self.files_moved,
            "files_failed": self.files_failed,
            "files_unattempted": self.files_unattempted,
            "verification_status": self.verification_status,
        }

    def to_operation_dict(self) -> dict[str, object]:
        if self.operation_id is None:
            raise ValueError("movement result requires an operation ID")
        return {
            "id": self.operation_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "files_expected": self.files_expected,
            "files_moved": self.files_moved,
            "files_failed": self.files_failed,
            "files_unattempted": self.files_unattempted,
            "verification_status": self.verification_status,
        }
