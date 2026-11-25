from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from organizer.application.duplicate_bin_movement_models import OP_TERMINAL_STATUSES
from organizer.application.duplicate_bin_restore_models import RESTORE_TERMINAL
from organizer.application.runtime_lifecycle import (
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.application.standards_models import (
    PREVIEW_LIFETIME_SECONDS,
    ResolvedStandardScope,
    StandardPreview,
    StandardsApplicationError,
    StandardsMutationResult,
)
from organizer.application.standards_read_models import StandardsReadProjection
from organizer.application.standards_read_service import build_standards_read_projection
from organizer.application.standards_repository import (
    StandardsRepository,
    StandardsRepositoryError,
    StandardsStorageSnapshot,
)
from organizer.application.view_models import ScanApplicationResult
from organizer.models import FileMetadata
from organizer.organization_rules import (
    DEFAULT_IGNORED_TERMS,
    STANDARD_KEEP_FOLDER_UNCHANGED,
    STANDARD_KEEP_MATCHING_FILES_TOGETHER,
    STANDARD_LEGACY_ALIAS,
    STANDARD_LEGACY_IGNORED_TERM,
    STANDARD_MATCHER_KINDS,
    WRITABLE_STANDARD_TYPES,
    StandardRecord,
    StandardsCatalog,
    StandardScope,
    conflicting_standard_records,
    equivalent_standard_record,
    normalize_anchor,
    normalize_matcher_value,
    normalize_standard_relative_path,
)
from organizer.scope import is_protected_context_path

_MIGRATION_ID_NAMESPACE = uuid.UUID("66378439-bc92-4ea5-a710-dbb8b7ad3103")
_COURSE_CODE = re.compile(r"^[A-Za-z]{2,4}\d{3,4}[A-Za-z]?$")
_PROJECT_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*$")


class StandardsService:
    """Own guided, revision-safe Standards mutations without transport concerns."""

    def __init__(
        self,
        repository: StandardsRepository,
        *,
        decision_store: object | None = None,
        execution_coordinator: object | None = None,
        operation_repository: object | None = None,
        duplicate_bin_repository: object | None = None,
        duplicate_bin_operation_repository: object | None = None,
        duplicate_bin_restore_repository: object | None = None,
        additional_plan_blocker: Callable[[], bool] | None = None,
        now: Callable[[], datetime] | None = None,
        runtime_lifecycle: RuntimeLifecycle | None = None,
    ) -> None:
        self.repository = repository
        self._decision_store = decision_store
        self._execution_coordinator = execution_coordinator
        self._operation_repository = operation_repository
        self._duplicate_bin_repository = duplicate_bin_repository
        self._duplicate_bin_operation_repository = duplicate_bin_operation_repository
        self._duplicate_bin_restore_repository = duplicate_bin_restore_repository
        self._additional_plan_blocker = additional_plan_blocker
        self._now = now or (lambda: datetime.now(UTC))
        self._runtime_lifecycle = runtime_lifecycle or RuntimeLifecycle()
        self._previews: dict[str, StandardPreview] = {}
        self._preview_owners: dict[str, str] = {}
        self._latest_preview_ids: dict[str, str] = {}
        self._saved_previews: dict[str, StandardsMutationResult] = {}

    def list_standards(self) -> StandardsReadProjection:
        snapshot = self._load()
        return build_standards_read_projection(snapshot.load_result)

    def ensure_creation_available(self) -> None:
        self._load()
        self._ensure_mutation_allowed()

    def resolve_root_scope(self, *, scan_generation: str | None = None) -> ResolvedStandardScope:
        return ResolvedStandardScope(
            configured_root_identity=self.repository.configured_root_identity,
            scope=StandardScope("root", None),
            scan_generation=scan_generation,
        )

    def resolve_folder_scope(
        self,
        folder: FileMetadata,
        *,
        scan_generation: str,
    ) -> ResolvedStandardScope:
        if not folder.is_dir:
            raise StandardsApplicationError("folder_scope_stale")
        try:
            relative = folder.path.resolve(strict=True).relative_to(self.repository.root)
        except (OSError, ValueError) as error:
            raise StandardsApplicationError("wrong_root_scope") from error
        normalized = normalize_standard_relative_path(relative.as_posix())
        self._validate_folder_scope(normalized)
        return ResolvedStandardScope(
            configured_root_identity=self.repository.configured_root_identity,
            scope=StandardScope("folder", normalized),
            scan_generation=scan_generation,
        )

    def preview(
        self,
        *,
        preset_type: str,
        scope: ResolvedStandardScope,
        expected_revision: int,
        matcher_kind: str | None = None,
        matcher_value: str | None = None,
        scan_result: ScanApplicationResult | None = None,
        owner_key: str | None = None,
    ) -> StandardPreview:
        self.validate_preview_request(
            preset_type=preset_type,
            matcher_kind=matcher_kind,
            matcher_value=matcher_value,
        )
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
            return self._preview_admitted(
                preset_type=preset_type,
                scope=scope,
                expected_revision=expected_revision,
                matcher_kind=matcher_kind,
                matcher_value=matcher_value,
                scan_result=scan_result,
                owner_key=owner_key,
            )

    def validate_preview_request(
        self,
        *,
        preset_type: str,
        matcher_kind: str | None = None,
        matcher_value: str | None = None,
    ) -> None:
        if preset_type not in WRITABLE_STANDARD_TYPES:
            raise StandardsApplicationError("invalid_preset")
        _proposal_parameters(preset_type, matcher_kind, matcher_value)

    def _preview_admitted(
        self,
        *,
        preset_type: str,
        scope: ResolvedStandardScope,
        expected_revision: int,
        matcher_kind: str | None = None,
        matcher_value: str | None = None,
        scan_result: ScanApplicationResult | None = None,
        owner_key: str | None = None,
    ) -> StandardPreview:
        preview = self.prepare_preview_admitted(
            preset_type=preset_type,
            scope=scope,
            expected_revision=expected_revision,
            matcher_kind=matcher_kind,
            matcher_value=matcher_value,
            scan_result=scan_result,
        )
        self.commit_preview_admitted(
            preview,
            owner_key=owner_key or self.repository.configured_root_identity,
        )
        return preview

    def prepare_preview_admitted(
        self,
        *,
        preset_type: str,
        scope: ResolvedStandardScope,
        expected_revision: int,
        matcher_kind: str | None = None,
        matcher_value: str | None = None,
        scan_result: ScanApplicationResult | None = None,
    ) -> StandardPreview:
        if preset_type not in WRITABLE_STANDARD_TYPES:
            raise StandardsApplicationError("invalid_preset")
        snapshot = self._load()
        if snapshot.revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
        self._validate_scope(scope, scan_result)
        parameters = _proposal_parameters(preset_type, matcher_kind, matcher_value)
        provisional = _candidate_record(
            preset_type,
            scope.scope,
            parameters,
            standard_id=str(uuid.uuid4()),
            timestamp=_timestamp(self._now()),
        )
        catalog = _catalog_for_mutation(snapshot, self.repository.configured_root_identity, self._now())
        duplicate = _equivalent_record(catalog.standards, provisional)
        conflicts = _conflicting_records(catalog.standards, provisional)
        issued = _aware(self._now())
        preview_id = str(uuid.uuid4())
        name, description, scope_label, effect = _preview_copy(provisional)
        preview = StandardPreview(
            preview_id=preview_id,
            preset_type=preset_type,
            scope=scope,
            matcher_kind=matcher_kind,
            matcher_value=parameters.get("matcher_value"),
            expected_revision=expected_revision,
            name=name,
            description=description,
            scope_label=scope_label,
            effect_summary=effect,
            matching_file_count=_matching_count(provisional, scan_result),
            attention_code=(
                "duplicate_standard" if duplicate is not None
                else "standard_conflict" if conflicts
                else None
            ),
            conflicting_standard_ids=tuple(
                sorted({item.id for item in conflicts} | ({duplicate.id} if duplicate else set()))
            ),
            fresh_scan_required=True,
            issued_at=issued.isoformat(),
            expires_at=(issued + timedelta(seconds=PREVIEW_LIFETIME_SECONDS)).isoformat(),
        )
        return preview

    def commit_preview_admitted(
        self,
        preview: StandardPreview,
        *,
        owner_key: str,
    ) -> str | None:
        previous = self._latest_preview_ids.get(owner_key)
        self._previews[preview.preview_id] = preview
        self._preview_owners[preview.preview_id] = owner_key
        self._latest_preview_ids[owner_key] = preview.preview_id
        return previous

    def latest_preview_id_admitted(self, owner_key: str) -> str | None:
        return self._latest_preview_ids.get(owner_key)

    def rollback_preview_admitted(
        self,
        preview_id: str,
        *,
        owner_key: str,
        previous_preview_id: str | None,
    ) -> None:
        if self._preview_owners.get(preview_id) != owner_key:
            return
        self._previews.pop(preview_id, None)
        self._preview_owners.pop(preview_id, None)
        self._saved_previews.pop(preview_id, None)
        if self._latest_preview_ids.get(owner_key) != preview_id:
            return
        if (
            previous_preview_id is not None
            and self._preview_owners.get(previous_preview_id) == owner_key
        ):
            self._latest_preview_ids[owner_key] = previous_preview_id
        else:
            self._latest_preview_ids.pop(owner_key, None)

    def save_preview(
        self,
        preview_id: str,
        *,
        expected_revision: int,
        owner_key: str | None = None,
    ) -> StandardsMutationResult:
        self.validate_save_request(
            preview_id,
            expected_revision=expected_revision,
            owner_key=owner_key,
        )
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
            return self._save_preview_admitted(
                preview_id,
                expected_revision=expected_revision,
                owner_key=owner_key,
            )

    def validate_save_request(
        self,
        preview_id: str,
        *,
        expected_revision: int,
        owner_key: str | None = None,
    ) -> None:
        owner = owner_key or self.repository.configured_root_identity
        if self._preview_owners.get(preview_id) != owner:
            raise StandardsApplicationError("preview_expired")
        if preview_id not in self._saved_previews:
            self._validated_preview(preview_id, expected_revision, owner)

    def _save_preview_admitted(
        self,
        preview_id: str,
        *,
        expected_revision: int,
        owner_key: str | None = None,
    ) -> StandardsMutationResult:
        owner = owner_key or self.repository.configured_root_identity
        if self._preview_owners.get(preview_id) != owner:
            raise StandardsApplicationError("preview_expired")
        previous = self._saved_previews.get(preview_id)
        if previous is not None:
            return previous
        preview = self._validated_preview(preview_id, expected_revision, owner)
        self._ensure_mutation_allowed()
        snapshot = self._load()
        if snapshot.revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
        catalog = _catalog_for_mutation(snapshot, self.repository.configured_root_identity, self._now())
        parameters = _proposal_parameters(
            preview.preset_type,
            preview.matcher_kind,
            preview.matcher_value,
        )
        candidate = _candidate_record(
            preview.preset_type,
            preview.scope.scope,
            parameters,
            standard_id=str(uuid.uuid4()),
            timestamp=_timestamp(self._now()),
        )
        duplicate = _equivalent_record(catalog.standards, candidate)
        if duplicate is not None:
            result = StandardsMutationResult(
                standard=duplicate,
                catalog_revision=snapshot.revision,
                created=False,
                changed=False,
                fresh_scan_required=False,
                projection=build_standards_read_projection(snapshot.load_result),
            )
            self._saved_previews[preview_id] = result
            return result
        conflicts = _conflicting_records(catalog.standards, candidate)
        if conflicts:
            raise StandardsApplicationError(
                "standard_conflict",
                conflicting_standard_ids=tuple(item.id for item in conflicts),
            )
        updated = replace(
            catalog,
            revision=expected_revision + 1,
            standards=tuple(sorted((*catalog.standards, candidate), key=lambda item: item.id)),
        )
        self._save(updated, expected_revision)
        self._invalidate_planning_state()
        result = StandardsMutationResult(
            standard=candidate,
            catalog_revision=updated.revision,
            created=True,
            changed=True,
            fresh_scan_required=True,
            projection=self.list_standards(),
        )
        self._saved_previews[preview_id] = result
        return result

    def set_active(
        self,
        standard_id: str,
        *,
        active: bool,
        expected_revision: int,
    ) -> StandardsMutationResult:
        self.validate_state_request(
            standard_id,
            active=active,
            expected_revision=expected_revision,
        )
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
            return self._set_active_admitted(
                standard_id,
                active=active,
                expected_revision=expected_revision,
            )

    def validate_state_request(
        self,
        standard_id: str,
        *,
        active: bool,
        expected_revision: int,
    ) -> None:
        if not isinstance(active, bool):
            raise StandardsApplicationError("unknown_standard")
        if standard_id.startswith("builtin-"):
            raise StandardsApplicationError("immutable_standard")
        snapshot = self._load()
        if snapshot.revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
        catalog = _catalog_for_mutation(
            snapshot,
            self.repository.configured_root_identity,
            self._now(),
        )
        current = next((item for item in catalog.standards if item.id == standard_id), None)
        if current is None or (current.archived_at is not None and active):
            raise StandardsApplicationError("unknown_standard")

    def _set_active_admitted(
        self,
        standard_id: str,
        *,
        active: bool,
        expected_revision: int,
    ) -> StandardsMutationResult:
        if standard_id.startswith("builtin-"):
            raise StandardsApplicationError("immutable_standard")
        snapshot = self._load()
        if snapshot.revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
        catalog = _catalog_for_mutation(snapshot, self.repository.configured_root_identity, self._now())
        current = next((item for item in catalog.standards if item.id == standard_id), None)
        if current is None:
            raise StandardsApplicationError("unknown_standard")
        if current.archived_at is not None and active:
            raise StandardsApplicationError("unknown_standard")
        if current.active == active:
            return StandardsMutationResult(
                standard=current,
                catalog_revision=expected_revision,
                created=False,
                changed=False,
                fresh_scan_required=False,
                projection=build_standards_read_projection(snapshot.load_result),
            )
        self._ensure_mutation_allowed()
        updated_record = replace(
            current,
            active=active,
            updated_at=_timestamp(self._now()),
            record_revision=current.record_revision + 1,
        )
        if active:
            conflicts = _conflicting_records(
                tuple(item for item in catalog.standards if item.id != current.id),
                updated_record,
            )
            if conflicts:
                raise StandardsApplicationError(
                    "standard_conflict",
                    conflicting_standard_ids=tuple(item.id for item in conflicts),
                )
        updated = replace(
            catalog,
            revision=expected_revision + 1,
            standards=tuple(
                updated_record if item.id == current.id else item
                for item in catalog.standards
            ),
        )
        self._save(updated, expected_revision)
        self._invalidate_planning_state()
        return StandardsMutationResult(
            standard=updated_record,
            catalog_revision=updated.revision,
            created=False,
            changed=True,
            fresh_scan_required=True,
            projection=self.list_standards(),
        )

    def _validated_preview(
        self,
        preview_id: str,
        expected_revision: int,
        owner_key: str,
    ) -> StandardPreview:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise StandardsApplicationError("preview_expired")
        if self._preview_owners.get(preview_id) != owner_key:
            raise StandardsApplicationError("preview_expired")
        if self._latest_preview_ids.get(owner_key) != preview_id:
            raise StandardsApplicationError("preview_superseded")
        if preview.expected_revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
        if _aware(self._now()) >= datetime.fromisoformat(preview.expires_at):
            raise StandardsApplicationError("preview_expired")
        self._validate_scope(preview.scope, None)
        return preview

    def _validate_scope(
        self,
        resolved: ResolvedStandardScope,
        scan_result: ScanApplicationResult | None,
    ) -> None:
        if resolved.configured_root_identity != self.repository.configured_root_identity:
            raise StandardsApplicationError("wrong_root_scope")
        if resolved.scope.kind == "folder":
            if resolved.scope.relative_path is None:
                raise StandardsApplicationError("folder_scope_stale")
            self._validate_folder_scope(resolved.scope.relative_path)
            if scan_result is not None and resolved.scan_generation is None:
                raise StandardsApplicationError("folder_scope_stale")

    def _validate_folder_scope(self, relative_path: str) -> None:
        normalized = normalize_standard_relative_path(relative_path)
        if is_protected_context_path(Path(normalized)):
            raise StandardsApplicationError("standard_conflict")
        path = self.repository.root / normalized
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.repository.root)
        except (OSError, ValueError) as error:
            raise StandardsApplicationError("folder_scope_stale") from error
        if path.is_symlink() or not resolved.is_dir():
            raise StandardsApplicationError("folder_scope_stale")

    def _ensure_mutation_allowed(self) -> None:
        root = self.repository.root
        if self._decision_store is not None and self._decision_store.has_approved_plan(root):
            raise StandardsApplicationError("active_plan_conflict")
        if self._execution_coordinator is not None and self._execution_coordinator.has_available_movement_authorization(self.repository.configured_root_identity):
            raise StandardsApplicationError("active_plan_conflict")
        if self._operation_repository is not None and self._operation_repository.blocking_record() is not None:
            raise StandardsApplicationError("active_plan_conflict")
        if self._duplicate_bin_repository is not None and self._duplicate_bin_repository.list_active_approved_batches():
            raise StandardsApplicationError("active_plan_conflict")
        if self._duplicate_bin_operation_repository is not None and any(
            record.status not in OP_TERMINAL_STATUSES
            for record in self._duplicate_bin_operation_repository.list_records()
        ):
            raise StandardsApplicationError("active_plan_conflict")
        if self._duplicate_bin_restore_repository is not None and any(
            record.status not in RESTORE_TERMINAL
            for record in self._duplicate_bin_restore_repository.list_operations()
        ):
            raise StandardsApplicationError("active_plan_conflict")
        if self._additional_plan_blocker is not None and self._additional_plan_blocker():
            raise StandardsApplicationError("active_plan_conflict")

    def _invalidate_planning_state(self) -> None:
        if self._decision_store is not None:
            self._decision_store.invalidate_for_root(self.repository.root)

    def _load(self) -> StandardsStorageSnapshot:
        try:
            return self.repository.load()
        except StandardsRepositoryError as error:
            raise StandardsApplicationError(error.code) from error

    def _save(self, catalog: StandardsCatalog, expected_revision: int) -> None:
        try:
            self.repository.save_catalog(catalog, expected_revision=expected_revision)
        except StandardsRepositoryError as error:
            raise StandardsApplicationError(error.code) from error


def _catalog_for_mutation(
    snapshot: StandardsStorageSnapshot,
    root_identity: str,
    now: datetime,
) -> StandardsCatalog:
    if snapshot.catalog is not None:
        return snapshot.catalog
    return _migrate_v1(snapshot, root_identity, _timestamp(now))


def _migrate_v1(
    snapshot: StandardsStorageSnapshot,
    root_identity: str,
    timestamp: str,
) -> StandardsCatalog:
    records: list[StandardRecord] = []
    if snapshot.is_v1:
        data = snapshot.load_result.raw_data or {}
        for value in sorted(_string_values(data.get("locked_anchors", []))):
            kind = "course_code" if _COURSE_CODE.fullmatch(value) else "project_code"
            records.append(
                _candidate_record(
                    STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                    StandardScope("root", None),
                    {"matcher_kind": kind, "matcher_value": normalize_matcher_value(value)},
                    standard_id=_migration_id("legacy_locked_anchor", normalize_anchor(value)),
                    timestamp=timestamp,
                )
            )
        for value in sorted(_string_values(data.get("ignored_terms", []))):
            if normalize_anchor(value) in DEFAULT_IGNORED_TERMS:
                continue
            records.append(
                _candidate_record(
                    STANDARD_LEGACY_IGNORED_TERM,
                    StandardScope("root", None),
                    {"term": normalize_matcher_value(value)},
                    standard_id=_migration_id("legacy_ignored_term", normalize_anchor(value)),
                    timestamp=timestamp,
                )
            )
        aliases = data.get("anchor_aliases", {})
        if isinstance(aliases, dict):
            for alias, target in sorted(aliases.items()):
                if not isinstance(alias, str) or not isinstance(target, str):
                    continue
                records.append(
                    _candidate_record(
                        STANDARD_LEGACY_ALIAS,
                        StandardScope("root", None),
                        {
                            "alias": normalize_matcher_value(alias),
                            "target": normalize_matcher_value(target),
                        },
                        standard_id=_migration_id(
                            "legacy_alias",
                            f"{normalize_anchor(alias)}\0{normalize_anchor(target)}",
                        ),
                        timestamp=timestamp,
                    )
                )
    return StandardsCatalog(
        configured_root_identity=root_identity,
        revision=0,
        standards=tuple(sorted(records, key=lambda item: item.id)),
    )


def _candidate_record(
    standard_type: str,
    scope: StandardScope,
    parameters: dict[str, str],
    *,
    standard_id: str,
    timestamp: str,
) -> StandardRecord:
    return StandardRecord(
        id=standard_id,
        type=standard_type,
        active=True,
        scope=scope,
        parameters=tuple(sorted(parameters.items())),
        created_at=timestamp,
        updated_at=timestamp,
        record_revision=1,
        archived_at=None,
    )


def _proposal_parameters(
    preset_type: str,
    matcher_kind: str | None,
    matcher_value: str | None,
) -> dict[str, str]:
    if preset_type == STANDARD_KEEP_FOLDER_UNCHANGED:
        if matcher_kind is not None or matcher_value is not None:
            raise StandardsApplicationError("invalid_matcher")
        return {}
    if matcher_kind not in STANDARD_MATCHER_KINDS:
        raise StandardsApplicationError("invalid_matcher")
    try:
        value = normalize_matcher_value(matcher_value)
    except ValueError as error:
        raise StandardsApplicationError("invalid_matcher") from error
    if matcher_kind == "course_code" and _COURSE_CODE.fullmatch(value) is None:
        raise StandardsApplicationError("invalid_matcher")
    if matcher_kind == "project_code" and _PROJECT_CODE.fullmatch(value) is None:
        raise StandardsApplicationError("invalid_matcher")
    return {"matcher_kind": matcher_kind, "matcher_value": value}


def _equivalent_record(
    records: tuple[StandardRecord, ...],
    candidate: StandardRecord,
) -> StandardRecord | None:
    return equivalent_standard_record(records, candidate)


def _conflicting_records(
    records: tuple[StandardRecord, ...],
    candidate: StandardRecord,
) -> tuple[StandardRecord, ...]:
    return conflicting_standard_records(records, candidate)


def _preview_copy(record: StandardRecord) -> tuple[str, str, str, str]:
    scope_label = (
        "Selected folder"
        if record.scope.kind == "root"
        else Path(record.scope.relative_path or ".").name
    )
    if record.type == STANDARD_KEEP_FOLDER_UNCHANGED:
        return (
            f"Keep {scope_label} unchanged",
            "Files in this folder stay where they are.",
            scope_label,
            "Files in this folder will stay out of organization suggestions.",
        )
    value = record.parameter("matcher_value") or "matching"
    return (
        f"Keep matching {value} files together",
        f"Files matching {value} will be treated as one group.",
        scope_label,
        "Matching files will stay grouped when organization suggestions are prepared.",
    )


def _matching_count(
    record: StandardRecord,
    scan_result: ScanApplicationResult | None,
) -> int | None:
    if scan_result is None:
        return None
    files = tuple(item for item in scan_result.metadata_items if not item.is_dir)
    if record.scope.kind == "folder":
        scope = Path(record.scope.relative_path or ".")
        files = tuple(
            item for item in files
            if item.relative_path == scope or scope in item.relative_path.parents
        )
    if record.type == STANDARD_KEEP_FOLDER_UNCHANGED:
        return len(files)
    value = normalize_anchor(record.parameter("matcher_value") or "")
    return sum(value in normalize_anchor(item.relative_path.stem) for item in files)


def _migration_id(preset_type: str, identity: str) -> str:
    return str(uuid.uuid5(_MIGRATION_ID_NAMESPACE, f"{preset_type}\0{normalize_anchor(identity)}"))


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _timestamp(value: datetime) -> str:
    return _aware(value).isoformat()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Standards clock must be timezone-aware")
    return value
