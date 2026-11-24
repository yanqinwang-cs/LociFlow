from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import stat
from typing import Iterable

from organizer.file_identity import (
    REGULAR_FILE_TYPE,
    RegularFileStatFingerprint,
    is_canonical_sha256,
)
from organizer.models import MovePlanItem
from organizer.safety import validate_under_root


_SAFE_MESSAGES = {
    "invalid_strict_plan": "The file could not be moved safely. Check the folder and try again.",
    "missing_identity": "The file could not be moved safely. Check the folder and try again.",
    "malformed_identity": "The file could not be moved safely. Check the folder and try again.",
    "source_identity_changed": (
        "The file changed before it could be moved. Check the folder and try again."
    ),
    "source_unstable": (
        "The file changed before it could be moved. Check the folder and try again."
    ),
    "unsafe_source": "The file could not be moved safely. Check the folder and try again.",
    "unsupported_item": "The file could not be moved safely. Check the folder and try again.",
    "destination_collision": (
        "A file is already in the destination. Check the folder and try again."
    ),
    "cross_volume_unsupported": (
        "The file could not be moved safely. Check the folder and try again."
    ),
    "destination_volume_indeterminate": (
        "The file could not be moved safely. Check the folder and try again."
    ),
    "destination_publication_failed": (
        "The file could not be moved safely. Check the folder and try again."
    ),
    "destination_verification_failed": (
        "The moved file could not be verified. Check the folder before trying again."
    ),
    "source_path_changed": (
        "The file changed while it was being moved. Check the folder before trying again."
    ),
    "source_removal_failed": (
        "The file could not be removed from its old folder. Check the folder before trying again."
    ),
    "filesystem_changed_recovery_required": (
        "The move needs attention before more file work can start."
    ),
}


class StrictExecutorError(Exception):
    def __init__(
        self,
        code: str,
        *,
        filesystem_changed: bool = False,
        recovery_required: bool = False,
    ) -> None:
        if code not in _SAFE_MESSAGES:
            raise ValueError("strict executor error code is invalid")
        if recovery_required and not filesystem_changed:
            raise ValueError("recovery requires a filesystem change")
        self.code = code
        self.safe_message = _SAFE_MESSAGES[code]
        self.filesystem_changed = filesystem_changed
        self.recovery_required = recovery_required
        super().__init__(self.safe_message)


class StrictExecutorInterrupted(KeyboardInterrupt):
    code = "filesystem_changed_recovery_required"
    safe_message = _SAFE_MESSAGES[code]
    filesystem_changed = True
    recovery_required = True

    def __str__(self) -> str:
        return self.safe_message


class VolumePlacement(str, Enum):
    SAME_VOLUME = "same_volume"
    CROSS_VOLUME = "cross_volume"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, repr=False)
class IdentityMovePlanItem:
    plan_item: MovePlanItem
    change_id: str
    expected_digest: str
    captured_stat: RegularFileStatFingerprint

    def __post_init__(self) -> None:
        if not isinstance(self.plan_item, MovePlanItem):
            raise StrictExecutorError("invalid_strict_plan")
        if not isinstance(self.change_id, str) or not self.change_id:
            raise StrictExecutorError("invalid_strict_plan")
        if self.expected_digest is None or self.captured_stat is None:
            raise StrictExecutorError("missing_identity")
        if not is_canonical_sha256(self.expected_digest):
            raise StrictExecutorError("malformed_identity")
        if not isinstance(self.captured_stat, RegularFileStatFingerprint):
            raise StrictExecutorError("malformed_identity")
        if self.captured_stat.file_type != REGULAR_FILE_TYPE:
            raise StrictExecutorError("unsupported_item")

    def __repr__(self) -> str:
        return "IdentityMovePlanItem(<strict identity redacted>)"


@dataclass(frozen=True, repr=False)
class ClassifiedIdentityMovePlanItem:
    item: IdentityMovePlanItem
    placement: VolumePlacement

    def __repr__(self) -> str:
        return f"ClassifiedIdentityMovePlanItem(placement={self.placement.value!r})"


@dataclass(frozen=True, repr=False)
class ValidatedIdentityMovePlan:
    items: tuple[ClassifiedIdentityMovePlanItem, ...]

    def __repr__(self) -> str:
        return f"ValidatedIdentityMovePlan(items={len(self.items)})"


def validate_identity_move_plan(
    items: tuple[IdentityMovePlanItem, ...] | Iterable[IdentityMovePlanItem],
    root: Path,
) -> ValidatedIdentityMovePlan:
    if not isinstance(items, tuple) or not items:
        raise StrictExecutorError("invalid_strict_plan")
    if any(not isinstance(item, IdentityMovePlanItem) for item in items):
        raise StrictExecutorError("invalid_strict_plan")
    if len({item.change_id for item in items}) != len(items):
        raise StrictExecutorError("invalid_strict_plan")

    resolved_root = _resolve_root(root)
    structural_sources = tuple(
        _absolute_normalized(item.plan_item.source) for item in items
    )
    structural_destinations = tuple(
        _absolute_normalized(item.plan_item.destination) for item in items
    )
    if (
        len(set(structural_sources)) != len(items)
        or len(set(structural_destinations)) != len(items)
        or set(structural_sources) & set(structural_destinations)
        or _has_path_ancestry_overlap(
            structural_sources + structural_destinations
        )
    ):
        raise StrictExecutorError("invalid_strict_plan")

    classified: list[ClassifiedIdentityMovePlanItem] = []
    sources: set[Path] = set()
    destinations: set[Path] = set()
    for item in items:
        plan_item = item.plan_item
        source, source_stat = _validate_source(
            plan_item.source,
            resolved_root,
            item.captured_stat,
        )
        destination, ancestor = _validate_destination(
            plan_item.destination,
            resolved_root,
        )
        if source == destination:
            raise StrictExecutorError("invalid_strict_plan")
        if source in sources or destination in destinations:
            raise StrictExecutorError("invalid_strict_plan")
        sources.add(source)
        destinations.add(destination)
        placement = _classify_volume(source_stat.st_dev, ancestor)
        classified.append(ClassifiedIdentityMovePlanItem(item, placement))

    if sources & destinations:
        raise StrictExecutorError("invalid_strict_plan")
    return ValidatedIdentityMovePlan(tuple(classified))


def validate_same_volume_identity_move_plan(
    items: tuple[IdentityMovePlanItem, ...] | Iterable[IdentityMovePlanItem],
    root: Path,
) -> ValidatedIdentityMovePlan:
    plan = validate_identity_move_plan(items, root)
    if any(
        item.placement is VolumePlacement.INDETERMINATE for item in plan.items
    ):
        raise StrictExecutorError("destination_volume_indeterminate")
    if any(item.placement is VolumePlacement.CROSS_VOLUME for item in plan.items):
        raise StrictExecutorError("cross_volume_unsupported")
    return plan


def _resolve_root(root: Path) -> Path:
    try:
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError
    except (OSError, TypeError, ValueError):
        raise StrictExecutorError("invalid_strict_plan") from None
    return resolved


def _absolute_normalized(path: Path) -> Path:
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError
        return Path(os.path.abspath(candidate))
    except (OSError, TypeError, ValueError):
        raise StrictExecutorError("invalid_strict_plan")


def _validate_source(
    source: Path,
    root: Path,
    expected: RegularFileStatFingerprint,
) -> tuple[Path, os.stat_result]:
    try:
        path = Path(source)
    except TypeError:
        raise StrictExecutorError("unsafe_source") from None
    if not path.is_absolute():
        raise StrictExecutorError("unsafe_source")
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        raise StrictExecutorError("source_identity_changed") from None
    except OSError:
        raise StrictExecutorError("unsafe_source") from None
    if stat.S_ISLNK(current.st_mode):
        raise StrictExecutorError("unsafe_source")
    if not stat.S_ISREG(current.st_mode):
        raise StrictExecutorError("unsupported_item")
    try:
        resolved = path.resolve(strict=True)
        validate_under_root(resolved, root)
        validate_under_root(path.parent.resolve(strict=True), root)
    except (OSError, ValueError):
        raise StrictExecutorError("unsafe_source") from None
    try:
        observed = RegularFileStatFingerprint(
            file_type=REGULAR_FILE_TYPE,
            device=current.st_dev,
            inode=current.st_ino,
            size_bytes=current.st_size,
            modified_time_ns=current.st_mtime_ns,
            changed_time_ns=getattr(current, "st_ctime_ns", None),
        )
    except ValueError:
        raise StrictExecutorError("source_unstable") from None
    if observed != expected:
        raise StrictExecutorError("source_identity_changed")
    return resolved, current


def _validate_destination(destination: Path, root: Path) -> tuple[Path, Path]:
    try:
        path = Path(destination)
    except TypeError:
        raise StrictExecutorError("invalid_strict_plan") from None
    if not path.is_absolute():
        raise StrictExecutorError("invalid_strict_plan")
    if os.path.lexists(path):
        raise StrictExecutorError("destination_collision")
    ancestor = _nearest_existing_ancestor(path.parent)
    try:
        if ancestor.is_symlink() or not ancestor.is_dir():
            raise ValueError
        resolved_ancestor = ancestor.resolve(strict=True)
        validate_under_root(resolved_ancestor, root)
        resolved = path.resolve(strict=False)
        validate_under_root(resolved, root)
        _reject_existing_parent_symlinks(ancestor, root)
    except (OSError, ValueError):
        raise StrictExecutorError("invalid_strict_plan") from None
    return resolved, resolved_ancestor


def _nearest_existing_ancestor(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _reject_existing_parent_symlinks(ancestor: Path, root: Path) -> None:
    current = ancestor
    while True:
        if os.path.lexists(current) and current.is_symlink():
            raise ValueError("destination parent is a symlink")
        resolved_current = current.resolve(strict=True)
        validate_under_root(resolved_current, root)
        if resolved_current == root:
            return
        parent = current.parent
        if parent == current:
            raise ValueError("destination parent is outside root")
        current = parent


def _classify_volume(source_device: int, destination_ancestor: Path) -> VolumePlacement:
    try:
        destination_device = _volume_device(destination_ancestor)
    except OSError:
        return VolumePlacement.INDETERMINATE
    return (
        VolumePlacement.SAME_VOLUME
        if source_device == destination_device
        else VolumePlacement.CROSS_VOLUME
    )


def _volume_device(path: Path) -> int:
    return path.stat().st_dev


def _has_path_ancestry_overlap(paths: tuple[Path, ...]) -> bool:
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path in other.parents or other in path.parents:
                return True
    return False
