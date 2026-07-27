from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Final


APP_NAME: Final = "Loci Flow"
BUNDLE_IDENTIFIER: Final = "com.yanqinwang.lociflow"
SEMANTIC_VERSION: Final = "0.1.0-beta.1"
BUILD_NUMBER: Final = 1
MINIMUM_MACOS_VERSION: Final = "13.0"
SUPPORTED_ARCHITECTURE: Final = "arm64"

_SEMANTIC_VERSION = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?"
)
_MACOS_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")


def validate_release_metadata() -> None:
    if not APP_NAME:
        raise ValueError("application name must not be empty")
    if not re.fullmatch(
        r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",
        BUNDLE_IDENTIFIER,
    ):
        raise ValueError("bundle identifier is invalid")
    if _SEMANTIC_VERSION.fullmatch(SEMANTIC_VERSION) is None:
        raise ValueError("semantic version is invalid")
    if isinstance(BUILD_NUMBER, bool) or not isinstance(BUILD_NUMBER, int):
        raise ValueError("bundle build number must be an integer")
    if BUILD_NUMBER < 1:
        raise ValueError("bundle build number must be positive")
    if _MACOS_VERSION.fullmatch(MINIMUM_MACOS_VERSION) is None:
        raise ValueError("minimum macOS version is invalid")
    if SUPPORTED_ARCHITECTURE not in {"arm64", "x86_64"}:
        raise ValueError("supported architecture is invalid")


def write_runtime_identity(path: Path) -> None:
    validate_release_metadata()
    payload = {
        "application_version": SEMANTIC_VERSION,
        "build_number": BUILD_NUMBER,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )


validate_release_metadata()
