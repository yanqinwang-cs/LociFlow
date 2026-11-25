import os
import stat
from collections.abc import Callable
from pathlib import Path

from organizer.models import FileMetadata
from organizer.safety import validate_under_root
from organizer.scan_boundary import (
    DirectoryScanResult,
    ScanBoundary,
    ScanBoundaryKind,
)
from organizer.scope import is_duplicate_bin_path


def scan_directory(root: Path, max_depth: int | None = None) -> list[FileMetadata]:
    return list(
        scan_directory_with_boundaries(root, max_depth=max_depth).metadata_items
    )


def scan_directory_with_boundaries(
    root: Path,
    max_depth: int | None = None,
    *,
    directory_device: Callable[[Path], int] | None = None,
) -> DirectoryScanResult:
    resolved_root = root.resolve()

    if not resolved_root.exists():
        raise ValueError(f"{resolved_root} does not exist")
    if not resolved_root.is_dir():
        raise ValueError(f"{resolved_root} is not a directory")
    if max_depth is not None and max_depth < 0:
        raise ValueError("max_depth must be non-negative")

    validate_under_root(resolved_root, resolved_root)
    device_reader = directory_device or _directory_device
    try:
        root_device = device_reader(resolved_root)
    except OSError:
        raise ValueError("selected folder is not a readable directory") from None

    results: list[FileMetadata] = []
    boundaries: list[ScanBoundary] = []
    _scan_path(
        resolved_root,
        resolved_root,
        results,
        boundaries,
        max_depth,
        root_device,
        device_reader,
    )
    return DirectoryScanResult(
        metadata_items=tuple(
            sorted(results, key=lambda metadata: metadata.relative_path.as_posix())
        ),
        boundaries=tuple(
            sorted(
                boundaries,
                key=lambda boundary: (
                    boundary.relative_path.as_posix(),
                    boundary.kind.value,
                ),
            )
        ),
    )


def _scan_path(
    path: Path,
    root: Path,
    results: list[FileMetadata],
    boundaries: list[ScanBoundary],
    max_depth: int | None,
    root_device: int,
    directory_device: Callable[[Path], int],
) -> None:
    try:
        lexical_relative_path = path.relative_to(root)
    except ValueError:
        return
    if is_duplicate_bin_path(lexical_relative_path):
        return

    try:
        resolved_path = validate_under_root(path, root)
    except ValueError:
        return
    except OSError:
        if lexical_relative_path.parts:
            boundaries.append(
                ScanBoundary(
                    ScanBoundaryKind.UNAVAILABLE,
                    lexical_relative_path,
                )
            )
        return

    relative_path = resolved_path.relative_to(root)
    if is_duplicate_bin_path(relative_path):
        return

    try:
        stat_result = resolved_path.stat()
    except OSError:
        if lexical_relative_path.parts:
            boundaries.append(
                ScanBoundary(
                    ScanBoundaryKind.UNAVAILABLE,
                    lexical_relative_path,
                )
            )
        return

    is_dir = stat.S_ISDIR(stat_result.st_mode)
    if is_dir and lexical_relative_path.parts:
        try:
            current_device = directory_device(path)
        except OSError:
            boundaries.append(
                ScanBoundary(
                    ScanBoundaryKind.UNAVAILABLE,
                    lexical_relative_path,
                )
            )
            return
        if current_device != root_device:
            boundaries.append(
                ScanBoundary(
                    ScanBoundaryKind.DIFFERENT_VOLUME,
                    lexical_relative_path,
                )
            )
            return

    results.append(
        FileMetadata(
            path=resolved_path,
            relative_path=relative_path,
            name=resolved_path.name,
            extension=resolved_path.suffix,
            size_bytes=stat_result.st_size,
            modified_time=stat_result.st_mtime,
            is_dir=is_dir,
        )
    )

    if not is_dir:
        return

    depth = len(relative_path.parts)
    if max_depth is not None and depth >= max_depth:
        return

    try:
        children = _directory_entries(resolved_path)
    except OSError:
        if lexical_relative_path.parts:
            results.pop()
            boundaries.append(
                ScanBoundary(
                    ScanBoundaryKind.UNAVAILABLE,
                    lexical_relative_path,
                )
            )
            return
        raise ValueError("selected folder is not a readable directory") from None

    for child in children:
        if child.is_symlink():
            if not child.exists():
                continue
            try:
                validate_under_root(child, root)
            except ValueError:
                continue
            continue
        _scan_path(
            child,
            root,
            results,
            boundaries,
            max_depth,
            root_device,
            directory_device,
        )


def _directory_device(path: Path) -> int:
    value = os.lstat(path)
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise OSError("directory device is unavailable")
    return value.st_dev


def _directory_entries(path: Path) -> tuple[Path, ...]:
    return tuple(sorted(path.iterdir(), key=lambda item: item.name))
