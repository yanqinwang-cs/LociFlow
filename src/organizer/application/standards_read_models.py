from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StandardsReadItem:
    id: str
    name: str
    description: str
    active: bool
    mutable: bool
    scope_label: str
    effect_summary: str
    preset_type: str
    attention_message: str | None = None
    matched_relative_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "active": self.active,
            "mutable": self.mutable,
            "scope_label": self.scope_label,
            "effect_summary": self.effect_summary,
            "preset_type": self.preset_type,
            "attention_message": self.attention_message,
        }


@dataclass(frozen=True)
class StandardsReadProjection:
    status: str
    items: tuple[StandardsReadItem, ...]
    revision: int = 0
    error_code: str | None = None
    attention_message: str | None = None

    @property
    def organization_available(self) -> bool:
        return self.status == "available"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "revision": self.revision,
            "items": [item.to_dict() for item in self.items],
            "attention_message": self.attention_message,
        }


class OrganizationUnavailableError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.safe_message = message
