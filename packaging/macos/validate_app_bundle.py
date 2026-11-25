from __future__ import annotations

import json
import os
import platform
import plistlib
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from release_metadata import (
    APP_NAME,
    BUILD_NUMBER,
    BUNDLE_IDENTIFIER,
    ICON_FILE_NAME,
    MINIMUM_MACOS_VERSION,
    SEMANTIC_VERSION,
    SUPPORTED_ARCHITECTURE,
)

_EXPECTED_ARCHITECTURES: Final = ("arm64",)
_FORBIDDEN_PARTS: Final = frozenset(
    {
        ".env",
        ".git",
        "ai_review",
        "prototype",
        "research",
        "tests",
    }
)
_REQUIRED_RESOURCES: Final = (
    "organizer/web/templates/loci_flow.html",
    "organizer/web/static/css/bootai.css",
    "organizer/web/static/js/bootai.js",
    "organizer/web/static/loci_flow/app.js",
    "organizer/web/static/loci_flow/assets/loci-flow-mark.svg",
    "organizer/web/static/vendor/bootstrap.min.css",
    "organizer/web/static/THIRD_PARTY_NOTICES.txt",
)
_RUNTIME_IDENTITY = "organizer/macos_launcher/release_identity.json"
_SAFE_FAILURE = "The Loci Flow application bundle is unavailable."


class BundleValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(_SAFE_FAILURE)
        self.code = code
        self.safe_message = _SAFE_FAILURE


@dataclass(frozen=True)
class MachORecord:
    relative_path: str
    architectures: tuple[str, ...]


@dataclass(frozen=True)
class BundleValidationReport:
    application_name: str
    bundle_identifier: str
    version: str
    build_number: int
    minimum_macos: str
    architecture_claim: str
    macho_count: int
    architecture_sets: tuple[str, ...]
    required_resources: str
    smoke_test: str

    def as_safe_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BundleValidationResult:
    report: BundleValidationReport
    macho_files: tuple[MachORecord, ...]


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def validate_app_bundle(
    app_path: Path,
    *,
    command_runner: CommandRunner = subprocess.run,
    host_architecture: str | None = None,
    run_smoke: bool = True,
) -> BundleValidationResult:
    bundle = _validate_bundle_path(app_path)
    if (host_architecture or platform.machine()) != "arm64":
        raise BundleValidationError("wrong_host_architecture")
    if SUPPORTED_ARCHITECTURE != "arm64":
        raise BundleValidationError("wrong_release_architecture")

    contents = bundle / "Contents"
    frameworks = contents / "Frameworks"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    plist_path = contents / "Info.plist"
    _require_directory(contents, "missing_contents")
    _require_directory(frameworks, "missing_frameworks")
    _require_directory(macos, "missing_macos")
    _require_directory(resources, "missing_resources")
    _require_regular_file(plist_path, "invalid_info_plist")

    metadata = _read_info_plist(plist_path)
    executable = _validate_metadata_and_executable(metadata, macos)
    _validate_symlinks_and_forbidden_content(bundle)
    _validate_resources(resources, metadata)

    native_files = _discover_macho_files(
        bundle,
        command_runner=command_runner,
    )
    if not native_files:
        raise BundleValidationError("native_closure_missing")
    executable_relative = executable.relative_to(bundle).as_posix()
    if not any(record.relative_path == executable_relative for record in native_files):
        raise BundleValidationError("primary_executable_not_native")
    if any(record.architectures != _EXPECTED_ARCHITECTURES for record in native_files):
        raise BundleValidationError("architecture_mismatch")

    smoke_result = "not_run"
    if run_smoke:
        completed = _run_command(
            command_runner,
            [str(executable), "--packaging-smoke-test"],
        )
        if completed.returncode != 0:
            raise BundleValidationError("packaging_smoke_failed")
        smoke_result = "passed"

    architecture_sets = tuple(
        sorted({"+".join(record.architectures) for record in native_files})
    )
    return BundleValidationResult(
        report=BundleValidationReport(
            application_name=APP_NAME,
            bundle_identifier=BUNDLE_IDENTIFIER,
            version=SEMANTIC_VERSION,
            build_number=BUILD_NUMBER,
            minimum_macos=MINIMUM_MACOS_VERSION,
            architecture_claim=SUPPORTED_ARCHITECTURE,
            macho_count=len(native_files),
            architecture_sets=architecture_sets,
            required_resources="passed",
            smoke_test=smoke_result,
        ),
        macho_files=native_files,
    )


def format_validation_report(result: BundleValidationResult) -> str:
    return json.dumps(
        result.report.as_safe_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_bundle_path(app_path: Path) -> Path:
    candidate = Path(app_path)
    try:
        if candidate.suffix != ".app" or candidate.is_symlink():
            raise BundleValidationError("invalid_bundle")
        if not candidate.is_dir():
            raise BundleValidationError("missing_bundle")
        return candidate.resolve(strict=True)
    except BundleValidationError:
        raise
    except (OSError, ValueError) as error:
        raise BundleValidationError("invalid_bundle") from error


def _read_info_plist(plist_path: Path) -> dict[str, object]:
    try:
        with plist_path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as error:
        raise BundleValidationError("invalid_info_plist") from error
    if not isinstance(payload, dict):
        raise BundleValidationError("invalid_info_plist")
    return payload


def _validate_metadata_and_executable(
    metadata: dict[str, object],
    macos: Path,
) -> Path:
    expected = {
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleShortVersionString": SEMANTIC_VERSION,
        "CFBundleVersion": str(BUILD_NUMBER),
        "LSMinimumSystemVersion": MINIMUM_MACOS_VERSION,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": ICON_FILE_NAME,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise BundleValidationError("metadata_mismatch")
    try:
        entries = tuple(sorted(macos.iterdir(), key=lambda item: item.name))
    except OSError as error:
        raise BundleValidationError("invalid_executable_directory") from error
    if len(entries) != 1 or entries[0].name != APP_NAME:
        raise BundleValidationError("unexpected_launch_executable")
    executable = entries[0]
    _require_regular_file(executable, "invalid_primary_executable")
    try:
        mode = executable.stat().st_mode
    except OSError as error:
        raise BundleValidationError("invalid_primary_executable") from error
    if mode & 0o111 == 0:
        raise BundleValidationError("invalid_primary_executable")
    return executable


def _validate_symlinks_and_forbidden_content(bundle: Path) -> None:
    for current, directories, filenames in os.walk(
        bundle,
        topdown=True,
        followlinks=False,
    ):
        directories.sort()
        filenames.sort()
        current_path = Path(current)
        for name in (*directories, *filenames):
            item = current_path / name
            relative = item.relative_to(bundle)
            if _is_forbidden_relative(relative):
                raise BundleValidationError("forbidden_content")
            if not item.is_symlink():
                continue
            try:
                item.resolve(strict=True).relative_to(bundle)
            except (OSError, ValueError) as error:
                raise BundleValidationError("unsafe_bundle_symlink") from error


def _is_forbidden_relative(relative: Path) -> bool:
    folded = tuple(part.casefold() for part in relative.parts)
    filename = folded[-1]
    return (
        any(part in _FORBIDDEN_PARTS for part in folded)
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


def _validate_resources(
    resources: Path,
    metadata: dict[str, object],
) -> None:
    icon = resources / ICON_FILE_NAME
    _require_regular_file(icon, "icon_missing")
    if metadata.get("CFBundleIconFile") != icon.name:
        raise BundleValidationError("icon_mismatch")
    for relative in _REQUIRED_RESOURCES:
        _require_nonempty_file(resources / relative, "resource_missing")

    identity_path = resources / _RUNTIME_IDENTITY
    _require_nonempty_file(identity_path, "release_identity_missing")
    try:
        identity = json.loads(identity_path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BundleValidationError("release_identity_invalid") from error
    if identity != {
        "application_version": SEMANTIC_VERSION,
        "build_number": BUILD_NUMBER,
    }:
        raise BundleValidationError("release_identity_mismatch")


def _discover_macho_files(
    bundle: Path,
    *,
    command_runner: CommandRunner,
) -> tuple[MachORecord, ...]:
    records: list[MachORecord] = []
    for file_path in _regular_bundle_files(bundle):
        relative = file_path.relative_to(bundle).as_posix()
        try:
            with file_path.open("rb") as stream:
                stream.read(1)
        except OSError as error:
            raise BundleValidationError("unreadable_bundle_file") from error
        classified = _run_command(
            command_runner,
            ["file", "-b", str(file_path)],
        )
        if classified.returncode != 0 or not classified.stdout.strip():
            raise BundleValidationError("file_classification_failed")
        is_macho = "Mach-O" in classified.stdout
        looks_native = (
            file_path.suffix in {".dylib", ".so"}
            or relative == f"Contents/MacOS/{APP_NAME}"
        )
        if looks_native and not is_macho:
            raise BundleValidationError("indeterminate_native_file")
        if not is_macho:
            continue
        architecture = _run_command(
            command_runner,
            ["lipo", "-archs", str(file_path)],
        )
        slices = tuple(architecture.stdout.strip().split())
        if architecture.returncode != 0 or not slices:
            raise BundleValidationError("architecture_inspection_failed")
        if any(slice_name not in {"arm64", "x86_64"} for slice_name in slices):
            raise BundleValidationError("architecture_inspection_failed")
        records.append(MachORecord(relative, slices))
    return tuple(sorted(records, key=lambda record: record.relative_path))


def _regular_bundle_files(bundle: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for current, directories, filenames in os.walk(
        bundle,
        topdown=True,
        followlinks=False,
    ):
        directories.sort()
        filenames.sort()
        current_path = Path(current)
        for filename in filenames:
            candidate = current_path / filename
            if candidate.is_symlink():
                continue
            try:
                details = candidate.stat()
            except OSError as error:
                raise BundleValidationError("unreadable_bundle_file") from error
            if stat.S_ISREG(details.st_mode):
                files.append(candidate)
            else:
                raise BundleValidationError("nonregular_bundle_file")
    return tuple(sorted(files, key=lambda item: item.relative_to(bundle).as_posix()))


def _run_command(
    command_runner: CommandRunner,
    command: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    try:
        return command_runner(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BundleValidationError("inspection_tool_failed") from error


def _require_directory(path: Path, code: str) -> None:
    try:
        if path.is_symlink() or not path.is_dir():
            raise BundleValidationError(code)
    except OSError as error:
        raise BundleValidationError(code) from error


def _require_regular_file(path: Path, code: str) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise BundleValidationError(code) from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise BundleValidationError(code)


def _require_nonempty_file(path: Path, code: str) -> None:
    _require_regular_file(path, code)
    try:
        if path.stat().st_size < 1:
            raise BundleValidationError(code)
    except OSError as error:
        raise BundleValidationError(code) from error


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print(_SAFE_FAILURE, file=sys.stderr)
        return 2
    try:
        result = validate_app_bundle(Path(arguments[0]))
    except BundleValidationError as error:
        print(error.safe_message, file=sys.stderr)
        return 1
    print(format_validation_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
