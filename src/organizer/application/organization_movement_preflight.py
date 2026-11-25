from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from organizer.application.organization_decision_models import (
    ApprovedOrganizationPlan,
)
from organizer.application.organization_operation_models import (
    OrganizationExpectedMove,
)
from organizer.application.volume_admission import (
    OrganizationVolumeAdmission,
)
from organizer.models import MovePlanItem, ReviewedPlanItem
from organizer.review_session import (
    CATEGORY_ORGANIZATION,
    DECISION_APPROVED,
    find_approved_move_conflicts,
)
from organizer.safety import validate_under_root
from organizer.scanner import scan_directory
from organizer.scope import (
    evaluate_actionable_source_path,
    is_actionable_destination_path,
)

_ERROR_CODES = {
    "approved_plan_stale",
    "source_changed",
    "protected_item_selected",
    "unsafe_destination",
    "overwrite_detected",
}


class OrganizationMovementPreflightError(ValueError):
    def __init__(self, code: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("movement preflight error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OrganizationMovementPreflight:
    plan_items: tuple[MovePlanItem, ...]
    expected_moves: tuple[OrganizationExpectedMove, ...]


def preflight_organization_movement(
    approved_plan: ApprovedOrganizationPlan,
    root: Path,
    *,
    volume_admission: OrganizationVolumeAdmission | None = None,
) -> OrganizationMovementPreflight:
    """Validate one complete approved batch without hashing or mutation."""

    resolved_root = Path(root).resolve(strict=True)
    volume = volume_admission or OrganizationVolumeAdmission(resolved_root)
    if volume.root != resolved_root:
        raise OrganizationMovementPreflightError("approved_plan_stale")
    volume.inspect_and_require_allowed(approved_plan)

    reviewed = approved_plan.reviewed_move_items
    fingerprints = approved_plan.source_fingerprints
    if len(reviewed) != len(fingerprints):
        raise OrganizationMovementPreflightError("approved_plan_stale")
    if (
        len({item.id for item in reviewed}) != len(reviewed)
        or len({item.item_id for item in fingerprints}) != len(fingerprints)
    ):
        raise OrganizationMovementPreflightError("approved_plan_stale")
    try:
        if find_approved_move_conflicts(list(reviewed), resolved_root):
            raise OrganizationMovementPreflightError("approved_plan_stale")
        frozen_sources = {
            validate_under_root(
                item.plan_item.source.resolve(strict=False),
                resolved_root,
            )
            for item in reviewed
        }
        frozen_destinations = {
            validate_under_root(
                item.plan_item.destination.resolve(strict=False),
                resolved_root,
            )
            for item in reviewed
        }
    except OrganizationMovementPreflightError:
        raise
    except (OSError, ValueError):
        raise OrganizationMovementPreflightError("approved_plan_stale") from None
    if (
        len(frozen_sources) != len(reviewed)
        or len(frozen_destinations) != len(reviewed)
        or frozen_sources & frozen_destinations
    ):
        raise OrganizationMovementPreflightError("approved_plan_stale")

    metadata = tuple(scan_directory(resolved_root))
    plan_items: list[MovePlanItem] = []
    expected_moves: list[OrganizationExpectedMove] = []
    source_paths: set[Path] = set()
    destination_paths: set[Path] = set()

    for reviewed_item, fingerprint in zip(reviewed, fingerprints):
        _validate_reviewed_item(reviewed_item)
        plan_item = reviewed_item.plan_item
        source = plan_item.source
        destination = plan_item.destination
        if source.is_symlink() or not source.exists() or not source.is_file():
            raise OrganizationMovementPreflightError("source_changed")
        try:
            resolved_source = validate_under_root(
                source.resolve(),
                resolved_root,
            )
            source_relative = resolved_source.relative_to(resolved_root)
            fingerprint_path = _safe_relative_path(fingerprint.relative_path)
            expected_source = (resolved_root / fingerprint_path).resolve(
                strict=False
            )
            if resolved_source != expected_source:
                raise ValueError("approved source changed")
            stat = source.stat()
        except (OSError, ValueError):
            raise OrganizationMovementPreflightError("source_changed") from None
        if (
            stat.st_size != fingerprint.size_bytes
            or stat.st_mtime != fingerprint.modified_time
        ):
            raise OrganizationMovementPreflightError("source_changed")
        if not evaluate_actionable_source_path(
            source_relative,
            metadata,
        ).allowed:
            raise OrganizationMovementPreflightError("protected_item_selected")

        try:
            resolved_destination = validate_under_root(
                destination.resolve(strict=False),
                resolved_root,
            )
            destination_relative = resolved_destination.relative_to(
                resolved_root
            )
            existing_parent = _nearest_existing_parent(destination.parent)
            validate_under_root(existing_parent.resolve(), resolved_root)
            if not existing_parent.is_dir():
                raise ValueError("destination parent is not a directory")
            if not is_actionable_destination_path(
                destination_relative,
                metadata,
            ):
                raise ValueError("destination is protected")
        except (OSError, ValueError):
            raise OrganizationMovementPreflightError(
                "unsafe_destination"
            ) from None
        if os.path.lexists(destination):
            raise OrganizationMovementPreflightError("overwrite_detected")

        source_paths.add(resolved_source.resolve(strict=False))
        destination_paths.add(resolved_destination.resolve(strict=False))
        plan_items.append(plan_item)
        expected_moves.append(
            OrganizationExpectedMove(
                change_id=reviewed_item.id,
                source=source_relative.as_posix(),
                destination=destination_relative.as_posix(),
                source_size_bytes=fingerprint.size_bytes,
                source_modified_time=fingerprint.modified_time,
                reason=plan_item.reason,
                confidence=plan_item.confidence,
                operation=plan_item.operation,
                overwrite_risk=plan_item.overwrite_risk,
            )
        )

    if source_paths != frozen_sources or destination_paths != frozen_destinations:
        raise OrganizationMovementPreflightError("approved_plan_stale")
    return OrganizationMovementPreflight(
        plan_items=tuple(plan_items),
        expected_moves=tuple(expected_moves),
    )


def _validate_reviewed_item(item: ReviewedPlanItem) -> None:
    if item.category != CATEGORY_ORGANIZATION or item.decision != DECISION_APPROVED:
        raise OrganizationMovementPreflightError("approved_plan_stale")


def _safe_relative_path(path: Path) -> Path:
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe relative path")
    return path


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current
