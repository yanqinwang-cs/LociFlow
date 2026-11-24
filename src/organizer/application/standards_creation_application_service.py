from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from organizer.application.runtime_lifecycle import (
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.application.standards_models import (
    PreparedStandardsPreviewToken,
    ResolvedCreationScope,
    StandardPreview,
    StandardsApplicationError,
    StandardsCreationContext,
)
from organizer.application.standards_service import StandardsService
from organizer.application.view_models import ScanApplicationResult
from organizer.models import FileMetadata


class StandardsCreationRegistry(Protocol):
    def issue_creation_context(
        self,
        *,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
        metadata_items: tuple[FileMetadata, ...],
    ) -> StandardsCreationContext: ...

    def validate_scope_reference(
        self,
        *,
        scope_id: str,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
    ) -> None: ...

    def resolve_scope(
        self,
        *,
        scope_id: str,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
        metadata_items: tuple[FileMetadata, ...],
    ) -> ResolvedCreationScope: ...

    def prepare_preview_token(
        self,
        *,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
        application_preview_id: str,
    ) -> PreparedStandardsPreviewToken: ...

    def commit_preview_token(self, prepared: PreparedStandardsPreviewToken) -> None: ...

    def rollback_preview_token(self, prepared: PreparedStandardsPreviewToken) -> None: ...


@dataclass(frozen=True)
class StandardsCreationPreviewResult:
    preview_token: str
    expires_at: str
    preview: StandardPreview
    scope_label: str


class StandardsCreationApplicationService:
    """Atomically owns browser creation context and Standards preview issuance."""

    def __init__(
        self,
        standards: StandardsService,
        registry: StandardsCreationRegistry,
        runtime_lifecycle: RuntimeLifecycle,
    ) -> None:
        self._standards = standards
        self._registry = registry
        self._runtime_lifecycle = runtime_lifecycle
        self._mutation_lock = Lock()

    def issue_creation_context(
        self,
        *,
        session_id: str,
        scan_generation: str,
        scan_result: ScanApplicationResult,
        expected_revision: int,
    ) -> StandardsCreationContext:
        with (
            self._runtime_lifecycle.begin_operation(RuntimeOperationKind.STATE_MUTATION),
            self._mutation_lock,
        ):
            self._validate_catalog_admitted(expected_revision)
            return self._registry.issue_creation_context(
                session_id=session_id,
                root_identity=self._standards.repository.configured_root_identity,
                scan_generation=scan_generation,
                expected_revision=expected_revision,
                metadata_items=scan_result.metadata_items,
            )

    def preview_standard(
        self,
        *,
        session_id: str,
        scan_generation: str,
        scan_result: ScanApplicationResult,
        expected_revision: int,
        scope_id: str,
        preset_type: str,
        matcher_kind: str | None = None,
        matcher_value: str | None = None,
    ) -> StandardsCreationPreviewResult:
        root_identity = self._standards.repository.configured_root_identity
        self._standards.validate_preview_request(
            preset_type=preset_type,
            matcher_kind=matcher_kind,
            matcher_value=matcher_value,
        )
        self._registry.validate_scope_reference(
            scope_id=scope_id,
            session_id=session_id,
            root_identity=root_identity,
            scan_generation=scan_generation,
            expected_revision=expected_revision,
        )
        with (
            self._runtime_lifecycle.begin_operation(RuntimeOperationKind.STATE_MUTATION),
            self._mutation_lock,
        ):
            self._validate_catalog_admitted(expected_revision)
            resolved = self._registry.resolve_scope(
                scope_id=scope_id,
                session_id=session_id,
                root_identity=root_identity,
                scan_generation=scan_generation,
                expected_revision=expected_revision,
                metadata_items=scan_result.metadata_items,
            )
            if resolved.kind == "root":
                scope = self._standards.resolve_root_scope(
                    scan_generation=scan_generation
                )
            elif resolved.kind == "folder" and resolved.folder is not None:
                scope = self._standards.resolve_folder_scope(
                    resolved.folder,
                    scan_generation=scan_generation,
                )
            else:
                raise StandardsApplicationError("folder_scope_stale")
            preview = self._standards.prepare_preview_admitted(
                preset_type=preset_type,
                scope=scope,
                expected_revision=expected_revision,
                matcher_kind=matcher_kind,
                matcher_value=matcher_value,
                scan_result=scan_result,
            )
            prepared_token = self._registry.prepare_preview_token(
                session_id=session_id,
                root_identity=root_identity,
                scan_generation=scan_generation,
                expected_revision=expected_revision,
                application_preview_id=preview.preview_id,
            )
            result = StandardsCreationPreviewResult(
                preview_token=prepared_token.token,
                expires_at=preview.expires_at,
                preview=preview,
                scope_label=resolved.label,
            )
            previous_preview_id = self._standards.latest_preview_id_admitted(session_id)
            try:
                self._standards.commit_preview_admitted(
                    preview,
                    owner_key=session_id,
                )
                self._registry.commit_preview_token(prepared_token)
            except Exception:
                self._registry.rollback_preview_token(prepared_token)
                self._standards.rollback_preview_admitted(
                    preview.preview_id,
                    owner_key=session_id,
                    previous_preview_id=previous_preview_id,
                )
                raise
            return result

    def _validate_catalog_admitted(self, expected_revision: int) -> None:
        self._standards.ensure_creation_available()
        if self._standards.list_standards().revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
