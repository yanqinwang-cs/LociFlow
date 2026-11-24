from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConsumerFileSnapshot:
    id: str
    name: str
    entity_type: str
    kind: str
    extension: str
    size_bytes: int
    modified_at: str
    relative_path: str
    current_folder: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type,
            "kind": self.kind,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "relative_path": self.relative_path,
            "current_folder": self.current_folder,
        }


@dataclass(frozen=True)
class RecommendedChange:
    id: str
    action: str
    item: ConsumerFileSnapshot
    destination_folder: str
    reason_code: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "action": self.action,
            "item": self.item.to_dict(),
            "destination_folder": self.destination_folder,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ChoiceOption:
    id: str
    action: str
    destination_folder: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"id": self.id, "action": self.action}
        if self.destination_folder is not None:
            data["destination_folder"] = self.destination_folder
        return data


@dataclass(frozen=True)
class ChoiceNeeded:
    id: str
    choice_type: str
    reason_code: str
    options: tuple[ChoiceOption, ...]
    item: ConsumerFileSnapshot | None = None
    copies: tuple[ConsumerFileSnapshot, ...] = ()
    identical_copy_count: int | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "choice_type": self.choice_type,
            "reason_code": self.reason_code,
            "options": [option.to_dict() for option in self.options],
        }
        if self.item is not None:
            data["item"] = self.item.to_dict()
        if self.copies:
            data["copies"] = [copy.to_dict() for copy in self.copies]
        if self.identical_copy_count is not None:
            data["identical_copy_count"] = self.identical_copy_count
        return data


@dataclass(frozen=True)
class ConsumerStandard:
    id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class KeptByStandard:
    item: ConsumerFileSnapshot
    standard: ConsumerStandard
    reason_code: str

    def to_dict(self) -> dict[str, object]:
        return {
            "item": self.item.to_dict(),
            "standard": self.standard.to_dict(),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class MovementTreeEntry:
    folder: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"folder": self.folder, "files": list(self.files)}


@dataclass(frozen=True)
class OrganizationSliceSummary:
    recommended_changes: int
    choices_needed: int
    kept_by_standards: int
    files_to_move: int
    names_to_change: int
    folders_to_create: int
    duplicate_copies: int
    movement_tree: tuple[MovementTreeEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "recommended_changes": self.recommended_changes,
            "choices_needed": self.choices_needed,
            "kept_by_standards": self.kept_by_standards,
            "files_to_move": self.files_to_move,
            "names_to_change": self.names_to_change,
            "folders_to_create": self.folders_to_create,
            "duplicate_copies": self.duplicate_copies,
            "movement_tree": [entry.to_dict() for entry in self.movement_tree],
        }


@dataclass(frozen=True)
class OrganizationSlice:
    scope_display_name: str
    scan_generation: str
    last_checked_at: str
    recommended_changes: tuple[RecommendedChange, ...]
    choices_needed: tuple[ChoiceNeeded, ...]
    kept_by_standards: tuple[KeptByStandard, ...]
    summary: OrganizationSliceSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": {
                "display_name": self.scope_display_name,
                "root_label": ".",
            },
            "freshness": {
                "scan_generation": self.scan_generation,
                "last_checked_at": self.last_checked_at,
                "last_organized_at": None,
            },
            "planning": {
                "strategy_id": "current_deterministic",
                "organization_style": None,
            },
            "recommended_changes": [
                change.to_dict() for change in self.recommended_changes
            ],
            "choices_needed": [choice.to_dict() for choice in self.choices_needed],
            "kept_by_standards": [
                kept.to_dict() for kept in self.kept_by_standards
            ],
            "summary": self.summary.to_dict(),
        }
