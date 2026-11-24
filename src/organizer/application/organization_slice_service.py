from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from organizer.application.file_kinds import consumer_file_kind
from organizer.application.organization_slice_models import (
    ChoiceNeeded,
    ChoiceOption,
    ConsumerFileSnapshot,
    ConsumerStandard,
    KeptByStandard,
    MovementTreeEntry,
    OrganizationSlice,
    OrganizationSliceSummary,
    RecommendedChange,
)
from organizer.application.standards_read_models import (
    OrganizationUnavailableError,
    StandardsReadProjection,
)
from organizer.application.standards_read_service import read_item_by_id
from organizer.application.view_models import ScanApplicationResult
from organizer.models import FileMetadata, MovePlanItem
from organizer.organization_review import build_organization_review
from organizer.scope import (
    ActionableEligibility,
    evaluate_actionable_source_path,
    is_actionable_destination_path,
)


@dataclass(frozen=True)
class _SafeMove:
    source: str
    destination: str
    destination_folder: str
    risk_level: str
    reason: str
    confidence: int
    overwrite_risk: bool


@dataclass(frozen=True)
class InternalMoveCandidate:
    public_id: str
    file_id: str
    source_metadata: FileMetadata
    move_item: MovePlanItem
    risk_level: str
    protection: ActionableEligibility


@dataclass(frozen=True)
class InternalDestinationChoice:
    public_id: str
    file_id: str
    source_metadata: FileMetadata
    options: tuple[tuple[str, MovePlanItem | None], ...]
    protection: ActionableEligibility


@dataclass(frozen=True)
class InternalDuplicateChoice:
    public_id: str
    sha256: str
    size_bytes: int
    files: tuple[FileMetadata, ...]
    copy_ids: tuple[str, ...]
    option_ids: tuple[str, ...]


@dataclass(frozen=True)
class InternalOrganizationProjection:
    public_slice: OrganizationSlice
    scan_generation: str
    root: Path
    recommendations_by_id: Mapping[str, InternalMoveCandidate]
    destination_choices_by_id: Mapping[str, InternalDestinationChoice]
    duplicate_choices_by_id: Mapping[str, InternalDuplicateChoice]
    protected_item_ids: frozenset[str]
    source_metadata_by_item_id: Mapping[str, FileMetadata]
    metadata_items: tuple[FileMetadata, ...]
    standards: StandardsReadProjection


def build_read_only_organization_slice(
    *,
    scan_result: ScanApplicationResult,
    scan_generation: str,
    completed_at: datetime,
) -> OrganizationSlice:
    """Project one completed deterministic scan without rescanning or restating files."""
    return build_internal_organization_projection(
        scan_result=scan_result,
        scan_generation=scan_generation,
        completed_at=completed_at,
    ).public_slice


def build_internal_organization_projection(
    *,
    scan_result: ScanApplicationResult,
    scan_generation: str,
    completed_at: datetime,
) -> InternalOrganizationProjection:
    """Build the public slice and its server-only candidate map in one pass."""
    if not scan_generation:
        raise ValueError("scan generation is required")
    if completed_at.tzinfo is None:
        raise ValueError("completed scan timestamp must include a timezone")
    standards = scan_result.standards
    if standards is None:
        raise OrganizationUnavailableError(
            "standards_unavailable",
            "Standards could not be loaded. Fix them before organizing.",
        )
    if not standards.organization_available:
        raise OrganizationUnavailableError(
            standards.error_code or "standards_unavailable",
            standards.attention_message
            or "Standards could not be loaded. Fix them before organizing.",
        )
    metadata_items = scan_result.metadata_items
    if not metadata_items:
        raise ValueError("completed scan metadata snapshot is unavailable")

    metadata_by_path = {
        _metadata_relative_path(metadata): metadata
        for metadata in metadata_items
        if not metadata.is_dir
    }
    directory_paths = {
        _metadata_relative_path(metadata)
        for metadata in metadata_items
        if metadata.is_dir
    }
    all_paths = {
        _metadata_relative_path(metadata)
        for metadata in metadata_items
    }
    evaluations = {
        relative_path: evaluate_actionable_source_path(
            metadata.relative_path,
            metadata_items,
        )
        for relative_path, metadata in metadata_by_path.items()
    }
    snapshots = {
        relative_path: _consumer_snapshot(
            metadata,
            scan_generation=scan_generation,
        )
        for relative_path, metadata in metadata_by_path.items()
    }

    duplicate_choices, duplicate_paths, internal_duplicates = _duplicate_choices(
        scan_result.report,
        evaluations,
        snapshots,
        metadata_by_path,
        standard_kept_paths=_standard_kept_paths(standards),
    )
    safe_moves = _safe_organization_moves(
        scan_result.report,
        metadata_items=metadata_items,
        metadata_by_path=metadata_by_path,
        directory_paths=directory_paths,
        all_paths=all_paths,
        evaluations=evaluations,
        duplicate_paths=duplicate_paths,
        standard_kept_paths=_standard_kept_paths(standards),
    )
    (
        recommended,
        destination_choices,
        internal_recommendations,
        internal_destination_choices,
    ) = _classify_organization_moves(
        safe_moves,
        snapshots,
        metadata_by_path=metadata_by_path,
        evaluations=evaluations,
        root=scan_result.root,
    )
    visible_paths = {
        change.item.relative_path for change in recommended
    }
    visible_paths.update(
        choice.item.relative_path
        for choice in destination_choices
        if choice.item is not None
    )
    visible_paths.update(
        copy.relative_path
        for choice in duplicate_choices
        for copy in choice.copies
    )
    kept = _kept_by_standards(
        evaluations,
        snapshots,
        standards,
        visible_paths=frozenset(visible_paths),
    )
    choices = tuple((*destination_choices, *duplicate_choices))
    movement_tree = _movement_tree(recommended)
    summary = OrganizationSliceSummary(
        recommended_changes=len(recommended),
        choices_needed=len(choices),
        kept_by_standards=len(kept),
        files_to_move=sum(change.action == "move" for change in recommended),
        names_to_change=0,
        folders_to_create=0,
        duplicate_copies=sum(
            max((choice.identical_copy_count or 0) - 1, 0)
            for choice in duplicate_choices
        ),
        movement_tree=movement_tree,
    )
    public_slice = OrganizationSlice(
        scope_display_name=scan_result.root.name or "Selected folder",
        scan_generation=scan_generation,
        last_checked_at=completed_at.isoformat(),
        recommended_changes=recommended,
        choices_needed=choices,
        kept_by_standards=kept,
        summary=summary,
    )
    return InternalOrganizationProjection(
        public_slice=public_slice,
        scan_generation=scan_generation,
        root=scan_result.root,
        recommendations_by_id=MappingProxyType(internal_recommendations),
        destination_choices_by_id=MappingProxyType(internal_destination_choices),
        duplicate_choices_by_id=MappingProxyType(internal_duplicates),
        protected_item_ids=frozenset(kept_item.item.id for kept_item in kept),
        source_metadata_by_item_id=MappingProxyType(
            {
                snapshots[path].id: metadata
                for path, metadata in metadata_by_path.items()
            }
        ),
        metadata_items=tuple(metadata_items),
        standards=standards,
    )


def _consumer_snapshot(
    metadata: FileMetadata,
    *,
    scan_generation: str,
) -> ConsumerFileSnapshot:
    relative_path = _metadata_relative_path(metadata)
    extension = metadata.extension.casefold()
    current_folder = _parent_path(relative_path)
    return ConsumerFileSnapshot(
        id=f"{scan_generation}:{relative_path}",
        name=metadata.name,
        entity_type="folder" if metadata.is_dir else "file",
        kind=consumer_file_kind(extension, is_dir=metadata.is_dir),
        extension=extension,
        size_bytes=metadata.size_bytes,
        modified_at=datetime.fromtimestamp(
            metadata.modified_time,
            tz=timezone.utc,
        ).isoformat(),
        relative_path=relative_path,
        current_folder=current_folder,
    )


def _kept_by_standards(
    evaluations: dict[str, ActionableEligibility],
    snapshots: dict[str, ConsumerFileSnapshot],
    standards: StandardsReadProjection,
    *,
    visible_paths: frozenset[str],
) -> tuple[KeptByStandard, ...]:
    kept: list[KeptByStandard] = []
    kept_paths: set[str] = set()
    for relative_path in sorted(snapshots):
        evaluation = evaluations[relative_path]
        standard = read_item_by_id(standards, evaluation.standard_id)
        if (
            evaluation.allowed
            or evaluation.reason_code is None
            or standard is None
        ):
            continue
        kept.append(
            KeptByStandard(
                item=snapshots[relative_path],
                standard=ConsumerStandard(
                    id=standard.id,
                    name=standard.name,
                ),
                reason_code=evaluation.reason_code,
            )
        )
        kept_paths.add(relative_path)

    for standard in standards.items:
        if standard.preset_type not in {
            "legacy_ignored_term",
            "keep_folder_unchanged",
        } or not standard.active:
            continue
        for relative_path in standard.matched_relative_paths:
            if (
                relative_path not in snapshots
                or relative_path in kept_paths
                or relative_path in visible_paths
            ):
                continue
            kept.append(
                KeptByStandard(
                    item=snapshots[relative_path],
                    standard=ConsumerStandard(id=standard.id, name=standard.name),
                    reason_code="saved_standard_kept",
                )
            )
            kept_paths.add(relative_path)
    return tuple(sorted(kept, key=lambda item: item.item.relative_path))


def _duplicate_choices(
    report: dict[str, Any],
    evaluations: dict[str, ActionableEligibility],
    snapshots: dict[str, ConsumerFileSnapshot],
    metadata_by_path: dict[str, FileMetadata],
    *,
    standard_kept_paths: frozenset[str] = frozenset(),
) -> tuple[
    tuple[ChoiceNeeded, ...],
    frozenset[str],
    dict[str, InternalDuplicateChoice],
]:
    raw_groups = report.get("duplicates")
    if not isinstance(raw_groups, list):
        raise ValueError("scan report duplicates must be a list")
    copy_groups: list[
        tuple[tuple[ConsumerFileSnapshot, ...], str, int, tuple[FileMetadata, ...]]
    ] = []
    duplicate_paths: set[str] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise ValueError("scan report duplicate group must be an object")
        files = raw_group.get("files")
        if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
            raise ValueError("scan report duplicate files must be strings")
        eligible_paths = sorted(
            {
                path
                for raw_path in files
                if (path := _safe_relative_path(raw_path)) in snapshots
                and evaluations[path].allowed
                and path not in standard_kept_paths
            }
        )
        if len(eligible_paths) < 2:
            continue
        sha256 = raw_group.get("sha256")
        size_bytes = raw_group.get("size_bytes")
        if not isinstance(sha256, str) or not sha256:
            raise ValueError("scan report duplicate hash must be a string")
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError("scan report duplicate size must be a non-negative integer")
        copies = tuple(snapshots[path] for path in eligible_paths)
        files_tuple = tuple(metadata_by_path[path] for path in eligible_paths)
        copy_groups.append((copies, sha256, size_bytes, files_tuple))
        duplicate_paths.update(eligible_paths)

    copy_groups.sort(
        key=lambda row: tuple(copy.relative_path for copy in row[0])
    )
    choices = tuple(
        ChoiceNeeded(
            id=f"D{index}",
            choice_type="exact_duplicate",
            reason_code="exact_duplicate_choice",
            identical_copy_count=len(copies),
            copies=copies,
            options=(
                ChoiceOption(id="keep-all", action="keep_all_copies"),
                ChoiceOption(
                    id="choose-for-bin",
                    action="choose_copies_for_duplicate_bin",
                ),
            ),
        )
        for index, (copies, _sha256, _size_bytes, _files) in enumerate(
            copy_groups,
            start=1,
        )
    )
    internal = {
        f"D{index}": InternalDuplicateChoice(
            public_id=f"D{index}",
            sha256=sha256,
            size_bytes=size_bytes,
            files=files,
            copy_ids=tuple(copy.id for copy in copies),
            option_ids=("keep-all", "choose-for-bin"),
        )
        for index, (copies, sha256, size_bytes, files) in enumerate(
            copy_groups,
            start=1,
        )
    }
    return choices, frozenset(duplicate_paths), internal


def _safe_organization_moves(
    report: dict[str, Any],
    *,
    metadata_items: tuple[FileMetadata, ...],
    metadata_by_path: dict[str, FileMetadata],
    directory_paths: set[str],
    all_paths: set[str],
    evaluations: dict[str, ActionableEligibility],
    duplicate_paths: frozenset[str],
    standard_kept_paths: frozenset[str] = frozenset(),
) -> tuple[_SafeMove, ...]:
    organization_review = build_organization_review(report)
    raw_items = organization_review.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("organization review items must be a list")
    moves: list[_SafeMove] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("organization review item must be an object")
        source = _safe_relative_path(raw_item.get("source"))
        destination = _safe_relative_path(raw_item.get("destination"))
        if source is None or destination is None or source == destination:
            continue
        if source not in metadata_by_path or source in duplicate_paths:
            continue
        if source in standard_kept_paths:
            continue
        if not evaluations[source].allowed:
            continue
        if raw_item.get("overwrite_risk") is not False:
            continue
        if destination in all_paths:
            continue
        destination_folder = _parent_path(destination)
        if destination_folder not in directory_paths:
            continue
        if not is_actionable_destination_path(Path(destination), metadata_items):
            continue
        risk_level = raw_item.get("risk_level")
        if risk_level not in {"low", "medium", "high"}:
            continue
        reason = raw_item.get("reason")
        confidence = raw_item.get("confidence")
        if not isinstance(reason, str) or not reason:
            continue
        if type(confidence) is not int or not 0 <= confidence <= 100:
            continue
        moves.append(
            _SafeMove(
                source=source,
                destination=destination,
                destination_folder=destination_folder,
                risk_level=risk_level,
                reason=reason,
                confidence=confidence,
                overwrite_risk=False,
            )
        )
    return tuple(sorted(set(moves), key=lambda move: (move.source, move.destination)))


def _standard_kept_paths(standards: StandardsReadProjection) -> frozenset[str]:
    return frozenset(
        path
        for standard in standards.items
        if standard.active and standard.preset_type == "keep_folder_unchanged"
        for path in standard.matched_relative_paths
    )


def _classify_organization_moves(
    safe_moves: tuple[_SafeMove, ...],
    snapshots: dict[str, ConsumerFileSnapshot],
    *,
    metadata_by_path: dict[str, FileMetadata],
    evaluations: dict[str, ActionableEligibility],
    root: Path,
) -> tuple[
    tuple[RecommendedChange, ...],
    tuple[ChoiceNeeded, ...],
    dict[str, InternalMoveCandidate],
    dict[str, InternalDestinationChoice],
]:
    by_source: dict[str, list[_SafeMove]] = defaultdict(list)
    for move in safe_moves:
        by_source[move.source].append(move)

    recommended_rows: list[tuple[ConsumerFileSnapshot, _SafeMove]] = []
    choice_rows: list[tuple[ConsumerFileSnapshot, tuple[_SafeMove, ...], str]] = []
    for source in sorted(by_source):
        moves = tuple(sorted(by_source[source], key=lambda item: item.destination))
        if len(moves) == 1 and moves[0].risk_level == "low":
            recommended_rows.append((snapshots[source], moves[0]))
            continue
        reason_code = (
            "multiple_safe_destinations"
            if len(moves) > 1
            else "review_risk_requires_choice"
        )
        choice_rows.append((snapshots[source], moves, reason_code))

    recommended = tuple(
        RecommendedChange(
            id=f"O{index}",
            action="move",
            item=snapshot,
            destination_folder=move.destination_folder,
            reason_code="matched_existing_group",
        )
        for index, (snapshot, move) in enumerate(recommended_rows, start=1)
    )
    choices = tuple(
        ChoiceNeeded(
            id=f"C{index}",
            choice_type="choose_destination",
            item=snapshot,
            reason_code=reason_code,
            options=tuple(
                [
                    ChoiceOption(
                        id=f"destination-{option_index}",
                        action="move",
                        destination_folder=move.destination_folder,
                    )
                    for option_index, move in enumerate(moves, start=1)
                ]
                + [ChoiceOption(id="keep", action="keep")]
            ),
        )
        for index, (snapshot, moves, reason_code) in enumerate(choice_rows, start=1)
    )
    internal_recommendations = {
        f"O{index}": InternalMoveCandidate(
            public_id=f"O{index}",
            file_id=snapshot.id,
            source_metadata=metadata_by_path[move.source],
            move_item=_move_plan_item(move, metadata_by_path[move.source], root),
            risk_level=move.risk_level,
            protection=evaluations[move.source],
        )
        for index, (snapshot, move) in enumerate(recommended_rows, start=1)
    }
    internal_choices = {
        f"C{index}": InternalDestinationChoice(
            public_id=f"C{index}",
            file_id=snapshot.id,
            source_metadata=metadata_by_path[moves[0].source],
            options=tuple(
                [
                    (
                        f"destination-{option_index}",
                        _move_plan_item(
                            move,
                            metadata_by_path[move.source],
                            root,
                        ),
                    )
                    for option_index, move in enumerate(moves, start=1)
                ]
                + [("keep", None)]
            ),
            protection=evaluations[moves[0].source],
        )
        for index, (snapshot, moves, _reason_code) in enumerate(
            choice_rows,
            start=1,
        )
    }
    return recommended, choices, internal_recommendations, internal_choices


def _move_plan_item(
    move: _SafeMove,
    source_metadata: FileMetadata,
    root: Path,
) -> MovePlanItem:
    return MovePlanItem(
        source=source_metadata.path,
        destination=root / move.destination,
        reason=move.reason,
        confidence=move.confidence,
        operation="dry-run move",
        overwrite_risk=move.overwrite_risk,
    )


def _movement_tree(
    recommended: tuple[RecommendedChange, ...],
) -> tuple[MovementTreeEntry, ...]:
    files_by_folder: dict[str, list[str]] = defaultdict(list)
    for change in recommended:
        files_by_folder[change.destination_folder].append(change.item.relative_path)
    return tuple(
        MovementTreeEntry(folder=folder, files=tuple(sorted(files)))
        for folder, files in sorted(files_by_folder.items())
    )


def _metadata_relative_path(metadata: FileMetadata) -> str:
    relative_path = metadata.relative_path.as_posix()
    safe = _safe_relative_path(relative_path)
    if safe is None:
        raise ValueError("scan metadata contains an unsafe relative path")
    return safe


def _safe_relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        if value == ".":
            return "."
        return None
    return path.as_posix()


def _parent_path(relative_path: str) -> str:
    parent = PurePosixPath(relative_path).parent.as_posix()
    return "." if parent in {"", "."} else parent
