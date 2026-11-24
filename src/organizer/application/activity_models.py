from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActivityDetail:
    source: str | None
    destination: str | None
    restored_to: str | None
    moved_from: str | None
    status: str

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"status": self.status}
        if self.source is not None:
            data["source"] = self.source
        if self.destination is not None:
            data["destination"] = self.destination
        if self.restored_to is not None:
            data["restored_to"] = self.restored_to
        if self.moved_from is not None:
            data["moved_from"] = self.moved_from
        return data


@dataclass(frozen=True)
class ActivityEntry:
    id: str
    type: str
    title: str
    status: str
    started_at: str | None
    completed_at: str | None
    files_expected: int
    files_moved: int
    files_restored: int
    files_failed: int
    files_unattempted: int
    verification_status: str
    undoable: bool
    related_operation_id: str | None
    details: tuple[ActivityDetail, ...]
    ordering_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "files_expected": self.files_expected,
            "files_moved": self.files_moved,
            "files_restored": self.files_restored,
            "files_failed": self.files_failed,
            "files_unattempted": self.files_unattempted,
            "verification_status": self.verification_status,
            "undoable": self.undoable,
            "related_operation_id": self.related_operation_id,
            "details": [item.to_dict() for item in self.details],
        }


@dataclass(frozen=True)
class ActivityList:
    activity: tuple[ActivityEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {"activity": [entry.to_dict() for entry in self.activity]}


@dataclass(frozen=True)
class UndoEligibility:
    operation_id: str
    undoable: bool
    error_code: str | None
    files_to_restore: int
    legacy_log_path: str | None = None
