from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from threading import Lock
from typing import Any, Final
import weakref

from organizer.macos_launcher.application_paths import (
    ApplicationPathError,
    MacOSApplicationPaths,
)

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised through injected tests
    _fcntl = None  # type: ignore[assignment]


class ProcessLeaseKind(StrEnum):
    APPLICATION_INSTANCE = "application_instance"
    ROOT = "root"


_ERROR_MESSAGES: Final[dict[str, str]] = {
    "already_held": "Loci Flow is already using this resource.",
    "unavailable": "Loci Flow process ownership is unavailable.",
    "unsafe_lock_path": "Loci Flow process ownership storage is unsafe.",
    "unsupported_platform": "Loci Flow process ownership is unsupported.",
}
_MAX_METADATA_BYTES: Final = 512


class ProcessLeaseError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(_ERROR_MESSAGES[code])
        self.code = code
        self.safe_message = _ERROR_MESSAGES[code]


class ProcessLease:
    """One descriptor-backed advisory lease owned until explicit release."""

    def __init__(
        self,
        descriptor: int,
        kind: ProcessLeaseKind,
        lock_path: Path,
        local_key: str,
    ) -> None:
        self._descriptor: int | None = descriptor
        self._kind = kind
        self._lock_path = lock_path
        self._local_key = local_key
        self._release_lock = Lock()
        _register_lease(self)

    @property
    def kind(self) -> ProcessLeaseKind:
        return self._kind

    @property
    def released(self) -> bool:
        with self._release_lock:
            return self._descriptor is None

    def release(self) -> bool:
        with self._release_lock:
            descriptor = self._descriptor
            if descriptor is None:
                return False
            self._descriptor = None
        _unregister_lease(self)
        failure: OSError | None = None
        if _fcntl is not None:
            try:
                _fcntl.flock(descriptor, _fcntl.LOCK_UN)
            except OSError as error:
                failure = error
        try:
            os.close(descriptor)
        except OSError as error:
            failure = failure or error
        _release_local_claim(self._local_key)
        if failure is not None:
            raise ProcessLeaseError("unavailable") from failure
        return True

    def __enter__(self) -> ProcessLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        del exc_type, exc_value, traceback
        self.release()
        return False

    def __reduce__(self) -> Any:
        raise TypeError("process leases cannot be serialized")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(kind={self._kind.value!r}, "
            f"released={self.released!r})"
        )

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass

    def _invalidate_after_fork(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        self._release_lock = Lock()
        self._local_key = ""
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def canonical_root_lock_key(root: Path) -> str:
    try:
        canonical = Path(root).resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError("root is not a directory")
    except (OSError, ValueError) as error:
        raise ProcessLeaseError("unavailable") from error
    return hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()


def acquire_application_instance_lease(
    paths: MacOSApplicationPaths,
    *,
    now: Callable[[], datetime] | None = None,
    nonce_factory: Callable[[], str] | None = None,
) -> ProcessLease:
    return _acquire(
        paths,
        paths.application_instance_lock,
        ProcessLeaseKind.APPLICATION_INSTANCE,
        root_key=None,
        now=now,
        nonce_factory=nonce_factory,
    )


def acquire_root_process_lease(
    paths: MacOSApplicationPaths,
    root: Path,
    *,
    now: Callable[[], datetime] | None = None,
    nonce_factory: Callable[[], str] | None = None,
) -> ProcessLease:
    root_key = canonical_root_lock_key(root)
    return _acquire(
        paths,
        paths.root_lock_path(root_key),
        ProcessLeaseKind.ROOT,
        root_key=root_key,
        now=now,
        nonce_factory=nonce_factory,
    )


def _acquire(
    paths: MacOSApplicationPaths,
    lock_path: Path,
    kind: ProcessLeaseKind,
    *,
    root_key: str | None,
    now: Callable[[], datetime] | None,
    nonce_factory: Callable[[], str] | None,
) -> ProcessLease:
    if _fcntl is None or os.name != "posix":
        raise ProcessLeaseError("unsupported_platform")
    try:
        paths.create_lock_directories()
    except ApplicationPathError as error:
        code = (
            "unsafe_lock_path"
            if error.code == "unsafe_path"
            else "unavailable"
        )
        raise ProcessLeaseError(code) from error

    local_key = os.fspath(lock_path)
    _claim_local(local_key)
    try:
        descriptor = _open_lock_file(lock_path)
        try:
            _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise ProcessLeaseError("already_held") from None
            raise ProcessLeaseError("unavailable") from error
        metadata = _metadata(
            kind,
            root_key=root_key,
            now=now,
            nonce_factory=nonce_factory,
        )
        _write_metadata(descriptor, metadata)
        return ProcessLease(descriptor, kind, lock_path, local_key)
    except Exception:
        descriptor = locals().get("descriptor")
        if isinstance(descriptor, int):
            try:
                os.close(descriptor)
            except OSError:
                pass
        _release_local_claim(local_key)
        raise


def _open_lock_file(lock_path: Path) -> int:
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(lock_path.parent, directory_flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ProcessLeaseError("unsafe_lock_path") from None
        raise ProcessLeaseError("unavailable") from error
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_details = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_details.st_mode)
            or directory_details.st_uid != os.geteuid()
            or stat.S_IMODE(directory_details.st_mode) & 0o077
        ):
            raise ProcessLeaseError("unsafe_lock_path")
        descriptor = os.open(
            lock_path.name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        if error.errno in {
            errno.ELOOP,
            errno.EISDIR,
            errno.ENOTDIR,
        }:
            raise ProcessLeaseError("unsafe_lock_path") from None
        raise ProcessLeaseError("unavailable") from error
    finally:
        os.close(directory_descriptor)
    try:
        os.set_inheritable(descriptor, False)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ProcessLeaseError("unsafe_lock_path")
        if details.st_uid != os.geteuid():
            raise ProcessLeaseError("unsafe_lock_path")
        if details.st_nlink != 1:
            raise ProcessLeaseError("unsafe_lock_path")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ProcessLeaseError("unsafe_lock_path")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _metadata(
    kind: ProcessLeaseKind,
    *,
    root_key: str | None,
    now: Callable[[], datetime] | None,
    nonce_factory: Callable[[], str] | None,
) -> dict[str, object]:
    timestamp = (now or (lambda: datetime.now(timezone.utc)))()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ProcessLeaseError("unavailable")
    nonce = (nonce_factory or (lambda: secrets.token_hex(16)))()
    if (
        not isinstance(nonce, str)
        or len(nonce) != 32
        or not nonce.isascii()
        or any(character not in "0123456789abcdef" for character in nonce)
    ):
        raise ProcessLeaseError("unavailable")
    metadata: dict[str, object] = {
        "created_at": timestamp.astimezone(timezone.utc).isoformat(),
        "instance_nonce": nonce,
        "lease_kind": kind.value,
        "process_id": os.getpid(),
    }
    if root_key is not None:
        metadata["root_key"] = root_key
    return metadata


def _write_metadata(descriptor: int, metadata: dict[str, object]) -> None:
    try:
        encoded = (
            json.dumps(
                metadata,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
        if len(encoded) > _MAX_METADATA_BYTES:
            raise ValueError("lease metadata is too large")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("lease metadata write did not progress")
            view = view[written:]
        os.fsync(descriptor)
    except (OSError, TypeError, ValueError) as error:
        raise ProcessLeaseError("unavailable") from error


_ACTIVE_LEASES: weakref.WeakSet[ProcessLease] = weakref.WeakSet()
_ACTIVE_LEASES_LOCK = Lock()
_LOCAL_HELD_KEYS: set[str] = set()
_LOCAL_HELD_KEYS_LOCK = Lock()


def _register_lease(lease: ProcessLease) -> None:
    with _ACTIVE_LEASES_LOCK:
        _ACTIVE_LEASES.add(lease)


def _unregister_lease(lease: ProcessLease) -> None:
    with _ACTIVE_LEASES_LOCK:
        _ACTIVE_LEASES.discard(lease)


def _claim_local(local_key: str) -> None:
    with _LOCAL_HELD_KEYS_LOCK:
        if local_key in _LOCAL_HELD_KEYS:
            raise ProcessLeaseError("already_held")
        _LOCAL_HELD_KEYS.add(local_key)


def _release_local_claim(local_key: str) -> None:
    if not local_key:
        return
    with _LOCAL_HELD_KEYS_LOCK:
        _LOCAL_HELD_KEYS.discard(local_key)


def _after_fork_in_child() -> None:
    global _ACTIVE_LEASES, _ACTIVE_LEASES_LOCK
    global _LOCAL_HELD_KEYS, _LOCAL_HELD_KEYS_LOCK
    inherited = tuple(_ACTIVE_LEASES)
    _ACTIVE_LEASES = weakref.WeakSet()
    _ACTIVE_LEASES_LOCK = Lock()
    _LOCAL_HELD_KEYS = set()
    _LOCAL_HELD_KEYS_LOCK = Lock()
    for lease in inherited:
        lease._invalidate_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)
