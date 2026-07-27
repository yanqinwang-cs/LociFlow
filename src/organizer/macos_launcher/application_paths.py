from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Final


APPLICATION_DIRECTORY_NAME: Final = "Loci Flow"

_ERROR_MESSAGES: Final[dict[str, str]] = {
    "unavailable": "Loci Flow application storage is unavailable.",
    "unsafe_path": "Loci Flow application storage is unsafe.",
}


class ApplicationPathError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(_ERROR_MESSAGES[code])
        self.code = code
        self.safe_message = _ERROR_MESSAGES[code]


@dataclass(frozen=True)
class MacOSApplicationPaths:
    """Immutable, application-owned macOS paths with explicit creation."""

    home: Path
    application_support: Path
    locks: Path
    root_locks: Path
    caches: Path
    logs: Path

    @classmethod
    def for_home(cls, home: Path | None = None) -> MacOSApplicationPaths:
        candidate = Path.home() if home is None else Path(home)
        if not candidate.is_absolute():
            raise ValueError("application home must be absolute")
        normalized_home = candidate.resolve(strict=False)
        application_support = (
            normalized_home
            / "Library"
            / "Application Support"
            / APPLICATION_DIRECTORY_NAME
        )
        locks = application_support / "locks"
        return cls(
            home=normalized_home,
            application_support=application_support,
            locks=locks,
            root_locks=locks / "roots",
            caches=(
                normalized_home
                / "Library"
                / "Caches"
                / APPLICATION_DIRECTORY_NAME
            ),
            logs=(
                normalized_home
                / "Library"
                / "Logs"
                / APPLICATION_DIRECTORY_NAME
            ),
        )

    @property
    def application_instance_lock(self) -> Path:
        return self.locks / "application-instance.lock"

    def root_lock_path(self, root_key: str) -> Path:
        if (
            len(root_key) != 64
            or not root_key.isascii()
            or any(character not in "0123456789abcdef" for character in root_key)
        ):
            raise ValueError("root lock key must be a lowercase SHA-256 digest")
        return self.root_locks / f"{root_key}.lock"

    def create_lock_directories(self) -> None:
        self._create_owned_directories(
            self.application_support,
            self.locks,
            self.root_locks,
        )

    def create(self) -> None:
        self._create_owned_directories(
            self.application_support,
            self.locks,
            self.root_locks,
            self.caches,
            self.logs,
        )

    def _create_owned_directories(self, *directories: Path) -> None:
        for directory in directories:
            _ensure_parent_chain(self.home, directory.parent)
            _ensure_owned_directory(directory)


def _ensure_parent_chain(home: Path, parent: Path) -> None:
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        home_details = home.lstat()
        if (
            not stat.S_ISDIR(home_details.st_mode)
            or stat.S_ISLNK(home_details.st_mode)
            or home_details.st_uid != os.geteuid()
        ):
            raise ApplicationPathError("unsafe_path")
        relative = parent.relative_to(home)
        current = home
        for part in relative.parts:
            current = current / part
            current.mkdir(mode=0o700, exist_ok=True)
            details = current.lstat()
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise ApplicationPathError("unsafe_path")
            if details.st_uid != os.geteuid():
                raise ApplicationPathError("unsafe_path")
    except ApplicationPathError:
        raise
    except (OSError, ValueError) as error:
        raise ApplicationPathError("unavailable") from error


def _ensure_owned_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        details = directory.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise ApplicationPathError("unsafe_path")
        if details.st_uid != os.geteuid():
            raise ApplicationPathError("unsafe_path")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ApplicationPathError("unsafe_path")
    except ApplicationPathError:
        raise
    except OSError as error:
        raise ApplicationPathError("unavailable") from error
