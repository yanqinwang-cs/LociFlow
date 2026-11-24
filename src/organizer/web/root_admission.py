from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Final

@dataclass(frozen=True)
class RootAdmissionError(ValueError):
    code: str
    safe_message: str

    def __str__(self) -> str:
        return self.safe_message


@dataclass(frozen=True)
class AdmittedRoot:
    path: Path


_ERRORS: Final[dict[str, str]] = {
    "root_invalid": "The selected folder is not valid.",
    "root_missing": "The selected folder does not exist.",
    "root_symlink": "A symbolic link cannot be selected as the root folder.",
    "root_not_directory": "The selected item is not a folder.",
    "root_filesystem": "The filesystem root cannot be selected.",
    "root_app_bundle": "An application bundle cannot be selected as the root folder.",
    "root_inaccessible": "The selected folder cannot be accessed.",
}


def admit_root(raw_root: str | os.PathLike[str]) -> AdmittedRoot:
    """Validate and resolve one process-immutable organization root."""

    try:
        expanded_root = Path(os.path.expanduser(os.fspath(raw_root)))
        lexical_root = Path(os.path.abspath(expanded_root))
    except (TypeError, ValueError, OSError):
        raise _error("root_invalid") from None

    try:
        if not os.path.lexists(lexical_root):
            raise _error("root_missing")
        if lexical_root.is_symlink():
            raise _error("root_symlink")
        if not lexical_root.is_dir():
            raise _error("root_not_directory")
        if lexical_root.parent == lexical_root:
            raise _error("root_filesystem")
        if lexical_root.name.casefold().endswith(".app"):
            raise _error("root_app_bundle")
        with os.scandir(lexical_root):
            pass
    except RootAdmissionError:
        raise
    except OSError:
        raise _error("root_inaccessible") from None

    try:
        resolved_root = lexical_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise _error("root_not_directory")
        if resolved_root.parent == resolved_root:
            raise _error("root_filesystem")
        if resolved_root.name.casefold().endswith(".app"):
            raise _error("root_app_bundle")
    except RootAdmissionError:
        raise
    except (OSError, ValueError):
        raise _error("root_inaccessible") from None

    return AdmittedRoot(path=resolved_root)


def _error(code: str) -> RootAdmissionError:
    return RootAdmissionError(code=code, safe_message=_ERRORS[code])
