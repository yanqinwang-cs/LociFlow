from __future__ import annotations

from dataclasses import dataclass

from organizer.application.standards_read_models import StandardsReadProjection
from organizer.models import FileMetadata
from organizer.organization_rules import StandardRecord, StandardScope


PREVIEW_LIFETIME_SECONDS = 15 * 60


class StandardsApplicationError(ValueError):
    def __init__(
        self,
        code: str,
        *,
        conflicting_standard_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.conflicting_standard_ids = tuple(sorted(conflicting_standard_ids))


@dataclass(frozen=True)
class ResolvedStandardScope:
    configured_root_identity: str
    scope: StandardScope
    scan_generation: str | None = None


@dataclass(frozen=True)
class StandardPreview:
    preview_id: str
    preset_type: str
    scope: ResolvedStandardScope
    matcher_kind: str | None
    matcher_value: str | None
    expected_revision: int
    name: str
    description: str
    scope_label: str
    effect_summary: str
    matching_file_count: int | None
    attention_code: str | None
    conflicting_standard_ids: tuple[str, ...]
    fresh_scan_required: bool
    issued_at: str
    expires_at: str


@dataclass(frozen=True)
class StandardsMutationResult:
    standard: StandardRecord
    catalog_revision: int
    created: bool
    changed: bool
    fresh_scan_required: bool
    projection: StandardsReadProjection


@dataclass(frozen=True)
class StandardsScopeChoice:
    id: str
    label: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "kind": self.kind}


@dataclass(frozen=True)
class StandardsCreationContext:
    scope_choices: tuple[StandardsScopeChoice, ...]
    expires_at: str
    revision: int


@dataclass(frozen=True)
class ResolvedCreationScope:
    kind: str
    folder: FileMetadata | None
    label: str


@dataclass(frozen=True)
class PreparedStandardsPreviewToken:
    token: str
    session_id: str
    root_identity: str
    scan_generation: str
    expected_revision: int
    application_preview_id: str
