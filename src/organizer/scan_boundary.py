from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from organizer.models import FileMetadata

DIFFERENT_VOLUME_SUMMARY = (
    "Items stored on another volume were left where they are."
)
UNAVAILABLE_ITEMS_SUMMARY = (
    "Some items could not be checked and were left where they are."
)


class ScanBoundaryKind(StrEnum):
    DIFFERENT_VOLUME = "different_volume"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ScanBoundary:
    kind: ScanBoundaryKind
    relative_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScanBoundaryKind):
            raise ValueError("scan boundary kind is invalid")
        path = self.relative_path
        if (
            not isinstance(path, Path)
            or path.is_absolute()
            or path.as_posix() in {"", "."}
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("scan boundary path is invalid")


@dataclass(frozen=True)
class DirectoryScanResult:
    metadata_items: tuple[FileMetadata, ...]
    boundaries: tuple[ScanBoundary, ...]


def safe_boundary_summaries(
    boundaries: tuple[ScanBoundary, ...],
) -> tuple[str, ...]:
    kinds = {boundary.kind for boundary in boundaries}
    summaries: list[str] = []
    if ScanBoundaryKind.DIFFERENT_VOLUME in kinds:
        summaries.append(DIFFERENT_VOLUME_SUMMARY)
    if ScanBoundaryKind.UNAVAILABLE in kinds:
        summaries.append(UNAVAILABLE_ITEMS_SUMMARY)
    return tuple(summaries)
