from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import stat
from typing import Callable

from organizer.application.organization_decision_models import (
    ApprovedOrganizationPlan,
)
from organizer.safety import validate_under_root


SAFE_CROSS_VOLUME_MESSAGE = (
    "Some approved files are on different storage volumes. Nothing was moved."
)
SAFE_INDETERMINATE_VOLUME_MESSAGE = (
    "The storage location for some approved files could not be checked. "
    "Nothing was moved."
)
SAFE_UNSAFE_VOLUME_PATH_MESSAGE = (
    "Some approved files are no longer in a safe location. Nothing was moved."
)

_SAFE_MESSAGES = {
    "cross_volume_plan": SAFE_CROSS_VOLUME_MESSAGE,
    "indeterminate_volume": SAFE_INDETERMINATE_VOLUME_MESSAGE,
    "unsafe_volume_path": SAFE_UNSAFE_VOLUME_PATH_MESSAGE,
}


class VolumeAdmissionState(str, Enum):
    ALLOWED = "allowed"
    CROSS_VOLUME = "cross_volume"
    INDETERMINATE = "indeterminate"
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class VolumeAdmissionResult:
    state: VolumeAdmissionState

    @property
    def allowed(self) -> bool:
        return self.state is VolumeAdmissionState.ALLOWED


@dataclass(frozen=True, repr=False)
class OrganizationVolumePathPair:
    source_relative_path: str
    destination_relative_path: str

    def __repr__(self) -> str:
        return "OrganizationVolumePathPair(<paths redacted>)"


class VolumeAdmissionError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in _SAFE_MESSAGES:
            raise ValueError("volume admission error code is invalid")
        self.code = code
        self.safe_message = _SAFE_MESSAGES[code]
        super().__init__(self.safe_message)


class OrganizationVolumeAdmission:
    """Freshly require every approved ordinary move to remain on its root device."""

    def __init__(
        self,
        root: Path,
        *,
        lstat_reader: Callable[[Path], os.stat_result] = os.lstat,
        stat_reader: Callable[[Path], os.stat_result] = os.stat,
        lexists: Callable[[Path], bool] = os.path.lexists,
    ) -> None:
        try:
            candidate = Path(root)
            if not candidate.is_absolute():
                raise ValueError
            resolved_root = Path(os.path.abspath(candidate))
        except (OSError, TypeError, ValueError):
            raise ValueError("volume admission root is invalid") from None
        self.root = resolved_root
        self._lstat = lstat_reader
        self._stat = stat_reader
        self._lexists = lexists

    def inspect(self, approved_plan: ApprovedOrganizationPlan) -> VolumeAdmissionResult:
        if not isinstance(approved_plan, ApprovedOrganizationPlan):
            return VolumeAdmissionResult(VolumeAdmissionState.UNSAFE)
        try:
            pairs = tuple(
                self._relative_pair(
                    item.plan_item.source,
                    item.plan_item.destination,
                )
                for item in approved_plan.reviewed_move_items
            )
        except (OSError, TypeError, ValueError):
            return VolumeAdmissionResult(VolumeAdmissionState.UNSAFE)
        return self.inspect_path_pairs(pairs)

    def inspect_path_pairs(
        self,
        path_pairs: tuple[OrganizationVolumePathPair, ...],
    ) -> VolumeAdmissionResult:
        """Classify one complete server-owned relative-path batch read-only."""

        if not isinstance(path_pairs, tuple) or any(
            not isinstance(pair, OrganizationVolumePathPair) for pair in path_pairs
        ):
            return VolumeAdmissionResult(VolumeAdmissionState.UNSAFE)
        try:
            root_stat = self._lstat(self.root)
        except OSError:
            return VolumeAdmissionResult(VolumeAdmissionState.INDETERMINATE)
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return VolumeAdmissionResult(VolumeAdmissionState.UNSAFE)

        try:
            states = tuple(
                self._inspect_item(
                    self._path_under_root(pair.source_relative_path),
                    self._path_under_root(pair.destination_relative_path),
                    root_stat.st_dev,
                )
                for pair in path_pairs
            )
        except (OSError, TypeError, ValueError):
            return VolumeAdmissionResult(VolumeAdmissionState.UNSAFE)
        if any(state is VolumeAdmissionState.UNSAFE for state in states):
            return VolumeAdmissionResult(VolumeAdmissionState.UNSAFE)
        if any(state is VolumeAdmissionState.INDETERMINATE for state in states):
            return VolumeAdmissionResult(VolumeAdmissionState.INDETERMINATE)
        if any(state is VolumeAdmissionState.CROSS_VOLUME for state in states):
            return VolumeAdmissionResult(VolumeAdmissionState.CROSS_VOLUME)
        return VolumeAdmissionResult(VolumeAdmissionState.ALLOWED)

    def require_same_volume_path_pairs(
        self,
        path_pairs: tuple[OrganizationVolumePathPair, ...],
    ) -> VolumeAdmissionResult:
        result = self.inspect_path_pairs(path_pairs)
        self._require_allowed(result)
        return result

    def inspect_and_require_allowed(
        self,
        approved_plan: ApprovedOrganizationPlan,
    ) -> VolumeAdmissionResult:
        result = self.inspect(approved_plan)
        self._require_allowed(result)
        return result

    def _require_allowed(self, result: VolumeAdmissionResult) -> None:
        if result.state is VolumeAdmissionState.CROSS_VOLUME:
            raise VolumeAdmissionError("cross_volume_plan")
        if result.state is VolumeAdmissionState.INDETERMINATE:
            raise VolumeAdmissionError("indeterminate_volume")
        if result.state is VolumeAdmissionState.UNSAFE:
            raise VolumeAdmissionError("unsafe_volume_path")

    def _relative_pair(
        self,
        source: Path,
        destination: Path,
    ) -> OrganizationVolumePathPair:
        source_path = self._absolute_path(source)
        destination_path = self._absolute_path(destination)
        normalized_source = source_path.parent.resolve(strict=False) / source_path.name
        normalized_destination = (
            destination_path.parent.resolve(strict=False) / destination_path.name
        )
        return OrganizationVolumePathPair(
            source_relative_path=normalized_source.relative_to(self.root).as_posix(),
            destination_relative_path=normalized_destination.relative_to(
                self.root
            ).as_posix(),
        )

    def _path_under_root(self, value: str) -> Path:
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("relative path is invalid")
        path = Path(value)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != value
        ):
            raise ValueError("relative path is invalid")
        return self.root / path

    def _inspect_item(
        self,
        source: Path,
        destination: Path,
        root_device: int,
    ) -> VolumeAdmissionState:
        try:
            source_path = self._absolute_path(source)
            destination_path = self._absolute_path(destination)
        except (OSError, TypeError, ValueError):
            return VolumeAdmissionState.UNSAFE
        try:
            source_stat = self._lstat(source_path)
        except OSError:
            return VolumeAdmissionState.INDETERMINATE
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            return VolumeAdmissionState.UNSAFE
        try:
            resolved_source = source_path.resolve(strict=True)
            validate_under_root(resolved_source, self.root)
            self._require_safe_existing_ancestry(source_path.parent)
            resolved_destination = destination_path.resolve(strict=False)
            validate_under_root(resolved_destination, self.root)
            destination_ancestor = self._nearest_existing_ancestor(
                destination_path.parent
            )
            self._require_safe_existing_ancestry(destination_ancestor)
        except (OSError, TypeError, ValueError):
            return VolumeAdmissionState.UNSAFE
        try:
            destination_stat = self._stat(destination_ancestor)
        except OSError:
            return VolumeAdmissionState.INDETERMINATE
        if (
            source_stat.st_dev != root_device
            or destination_stat.st_dev != root_device
        ):
            return VolumeAdmissionState.CROSS_VOLUME
        return VolumeAdmissionState.ALLOWED

    def _absolute_path(self, value: Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("path is not absolute")
        return Path(os.path.abspath(path))

    def _nearest_existing_ancestor(self, path: Path) -> Path:
        current = path
        while not self._lexists(current):
            parent = current.parent
            if parent == current:
                raise ValueError("no existing destination ancestor")
            current = parent
        return current

    def _require_safe_existing_ancestry(self, path: Path) -> None:
        current = path
        while True:
            if not self._lexists(current):
                raise ValueError("ancestry is unavailable")
            current_stat = self._lstat(current)
            if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(
                current_stat.st_mode
            ):
                raise ValueError("ancestry is unsafe")
            resolved = current.resolve(strict=True)
            validate_under_root(resolved, self.root)
            if resolved == self.root:
                return
            parent = current.parent
            if parent == current:
                raise ValueError("ancestry escapes root")
            current = parent
