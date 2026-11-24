from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from threading import Lock
from typing import Final, Protocol

from organizer.macos_launcher.application_paths import MacOSApplicationPaths


ACTIVE_LOG_NAME: Final = "launcher.log"
ROTATED_LOG_NAMES: Final = ("launcher.log.1", "launcher.log.2")
LOCK_NAME: Final = "launcher.log.lock"
MAX_ACTIVE_BYTES: Final = 256 * 1024
RUNTIME_IDENTITY_NAME: Final = "release_identity.json"

ALLOWED_PHASES: Final = frozenset(
    {
        "startup",
        "selecting_root",
        "constructing_application",
        "binding_server",
        "waiting_for_ready",
        "opening_browser",
        "running",
        "shutting_down",
    }
)
ALLOWED_CODES: Final = frozenset(
    {
        "native_dependency_unavailable",
        "global_lease_already_held",
        "global_lease_unavailable",
        "root_lease_already_held",
        "root_lease_unavailable",
        "packaged_resource_missing",
        "application_construction_failed",
        "socket_bind_failed",
        "server_start_failed",
        "readiness_failed",
        "browser_launch_failed",
        "coordinated_shutdown_failed",
        "launcher_started",
        "root_selection_started",
        "root_selected",
        "shutdown_started",
        "shutdown_completed",
        "unknown_launcher_failure",
    }
)
ALLOWED_EXCEPTION_CLASSES: Final = frozenset(
    {
        "ApplicationPathError",
        "Exception",
        "ModuleNotFoundError",
        "NativeAdapterError",
        "OSError",
        "ProcessLeaseError",
        "RuntimeError",
        "TimeoutError",
        "ValueError",
        "WebServerError",
    }
)
_RELEASE_VERSION: Final = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?"
)


class DiagnosticLike(Protocol):
    phase: str
    code: str
    exception_class: str | None


@dataclass(frozen=True)
class PackagedReleaseIdentity:
    application_version: str
    build_number: int


UNKNOWN_RELEASE_IDENTITY: Final = PackagedReleaseIdentity("unknown", 0)


@dataclass(frozen=True)
class PackagedDiagnosticEvent:
    timestamp_utc: str
    application_version: str
    build_number: int
    phase: str
    code: str
    exception_class: str | None = None


@dataclass(frozen=True)
class PackagedDiagnosticInput:
    phase: str
    code: str
    exception_class: str | None = None


def runtime_identity_path() -> Path:
    return Path(__file__).resolve().with_name(RUNTIME_IDENTITY_NAME)


def read_runtime_identity(
    path: Path | None = None,
) -> PackagedReleaseIdentity | None:
    candidate = runtime_identity_path() if path is None else path
    try:
        raw = candidate.read_bytes()
        if len(raw) > 256:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {
            "application_version",
            "build_number",
        }:
            return None
        version = payload["application_version"]
        build = payload["build_number"]
        if (
            not isinstance(version, str)
            or _RELEASE_VERSION.fullmatch(version) is None
            or isinstance(build, bool)
            or not isinstance(build, int)
            or build < 1
        ):
            return None
        return PackagedReleaseIdentity(version, build)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None


def create_packaged_diagnostic_sink(
    paths: MacOSApplicationPaths,
    *,
    identity: PackagedReleaseIdentity | None = None,
    now: Callable[[], datetime] | None = None,
) -> PackagedDiagnosticSink:
    return PackagedDiagnosticSink(
        paths,
        identity=identity or read_runtime_identity() or UNKNOWN_RELEASE_IDENTITY,
        now=now,
    )


class PackagedDiagnosticSink:
    def __init__(
        self,
        paths: MacOSApplicationPaths,
        *,
        identity: PackagedReleaseIdentity | None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._paths = paths
        self._identity = _bounded_identity(identity)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()

    def __call__(self, diagnostic: DiagnosticLike) -> None:
        try:
            event = self._event(diagnostic)
            if event is None:
                return
            encoded = (
                json.dumps(
                    _event_payload(event),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii")
            if len(encoded) > 512:
                return
            with self._lock:
                self._append(encoded)
        except Exception:
            pass

    def _event(
        self,
        diagnostic: DiagnosticLike,
    ) -> PackagedDiagnosticEvent | None:
        identity = self._identity
        if identity is None:
            return None
        phase, code = _bounded_category(diagnostic.phase, diagnostic.code)
        exception_class = diagnostic.exception_class
        if exception_class is not None:
            exception_class = (
                exception_class
                if exception_class in ALLOWED_EXCEPTION_CLASSES
                else "Exception"
            )
        timestamp = self._now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        return PackagedDiagnosticEvent(
            timestamp_utc=timestamp.astimezone(timezone.utc).isoformat(),
            application_version=identity.application_version,
            build_number=identity.build_number,
            phase=phase,
            code=code,
            exception_class=exception_class,
        )

    def _append(self, encoded: bytes) -> None:
        self._paths.create_logs_directory()
        directory = _open_log_directory(self._paths.logs_directory)
        try:
            lock_descriptor = _open_safe_file(directory, LOCK_NAME)
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                _rotate_if_needed(directory, len(encoded))
                descriptor = _open_safe_file(directory, ACTIVE_LOG_NAME)
                try:
                    _write_all(descriptor, encoded)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(lock_descriptor)
        finally:
            os.close(directory)


def _event_payload(event: PackagedDiagnosticEvent) -> dict[str, object]:
    payload = asdict(event)
    if event.exception_class is None:
        del payload["exception_class"]
    return payload


def _bounded_identity(
    identity: PackagedReleaseIdentity | None,
) -> PackagedReleaseIdentity | None:
    if identity is None:
        return None
    if identity == UNKNOWN_RELEASE_IDENTITY:
        return identity
    if (
        _RELEASE_VERSION.fullmatch(identity.application_version) is None
        or isinstance(identity.build_number, bool)
        or not isinstance(identity.build_number, int)
        or identity.build_number < 1
    ):
        return UNKNOWN_RELEASE_IDENTITY
    return identity


def _bounded_category(phase: str, code: str) -> tuple[str, str]:
    direct = _DIRECT_CATEGORIES.get((phase, code))
    if direct is not None:
        return direct
    code_mapping = _CODE_CATEGORIES.get(code)
    if code_mapping is not None:
        mapped_phase, mapped_code = code_mapping
        return (
            mapped_phase if mapped_phase in ALLOWED_PHASES else "startup",
            mapped_code,
        )
    if phase in {"native_shutdown", "process_lease_cleanup"}:
        return "shutting_down", "coordinated_shutdown_failed"
    return (
        phase if phase in ALLOWED_PHASES else "startup",
        code if code in ALLOWED_CODES else "unknown_launcher_failure",
    )


_DIRECT_CATEGORIES: Final = {
    ("application_lease", "application_already_open"): (
        "startup",
        "global_lease_already_held",
    ),
    ("application_lease", "lease_unavailable"): (
        "startup",
        "global_lease_unavailable",
    ),
    ("root_lease", "root_already_open"): (
        "selecting_root",
        "root_lease_already_held",
    ),
    ("root_lease", "lease_unavailable"): (
        "selecting_root",
        "root_lease_unavailable",
    ),
}

_CODE_CATEGORIES: Final = {
    "native_dependency_unavailable": (
        "startup",
        "native_dependency_unavailable",
    ),
    "packaged_resource_missing": ("startup", "packaged_resource_missing"),
    "app_creation_failed": (
        "constructing_application",
        "application_construction_failed",
    ),
    "bind_failed": ("binding_server", "socket_bind_failed"),
    "server_failed": ("running", "server_start_failed"),
    "readiness_failed": ("waiting_for_ready", "readiness_failed"),
    "browser_open_failed": ("opening_browser", "browser_launch_failed"),
    "native_app_initialized": ("startup", "launcher_started"),
    "chooser_presenting": ("selecting_root", "root_selection_started"),
    "selection_committed": ("selecting_root", "root_selected"),
    "quit_action_received": ("shutting_down", "shutdown_started"),
    "delayed_shutdown_started": ("shutting_down", "shutdown_started"),
    "appkit_termination_reply": ("shutting_down", "shutdown_completed"),
}


def _open_log_directory(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise OSError("unsafe diagnostics directory")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_safe_file(directory: int, name: str) -> int:
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise OSError("unsafe diagnostics file")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _rotate_if_needed(directory: int, incoming_bytes: int) -> None:
    try:
        active = os.stat(ACTIVE_LOG_NAME, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    _validate_existing_file(active)
    if active.st_size + incoming_bytes <= MAX_ACTIVE_BYTES:
        return

    for name in (*ROTATED_LOG_NAMES, ACTIVE_LOG_NAME, LOCK_NAME):
        try:
            details = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _validate_existing_file(details)

    try:
        os.unlink(ROTATED_LOG_NAMES[-1], dir_fd=directory)
    except FileNotFoundError:
        pass
    if _exists(directory, ROTATED_LOG_NAMES[0]):
        os.replace(
            ROTATED_LOG_NAMES[0],
            ROTATED_LOG_NAMES[1],
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
    os.replace(
        ACTIVE_LOG_NAME,
        ROTATED_LOG_NAMES[0],
        src_dir_fd=directory,
        dst_dir_fd=directory,
    )


def _exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written < 1:
            raise OSError("diagnostics write did not progress")
        remaining = remaining[written:]


def _validate_existing_file(details: os.stat_result) -> None:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise OSError("unsafe diagnostics file")
