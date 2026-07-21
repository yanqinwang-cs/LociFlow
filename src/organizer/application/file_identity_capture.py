from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat

from organizer.application.content_identity import (
    REGULAR_FILE_IDENTITY_KIND,
    SHA256_IDENTITY_ALGORITHM,
    RegularFileContentIdentity,
)
from organizer.safety import validate_under_root


DEFAULT_IDENTITY_CAPTURE_CHUNK_SIZE = 1024 * 1024
IDENTITY_CAPTURE_SAFE_MESSAGE = (
    "The file changed before it could be moved. Check the folder and try again."
)
_CAPTURE_ERROR_CODES = {
    "unsupported_item",
    "unsafe_source",
    "source_symlink",
    "source_missing",
    "source_unreadable",
    "source_changed_during_hash",
    "identity_capture_failed",
}


@dataclass(frozen=True)
class RegularFileStatFingerprint:
    """Transient opened-file evidence; this is never durable content identity."""

    file_type: str
    device: int
    inode: int
    size_bytes: int
    modified_time_ns: int
    changed_time_ns: int | None


@dataclass(frozen=True)
class CapturedRegularFileIdentity:
    content_identity: RegularFileContentIdentity
    stat_fingerprint: RegularFileStatFingerprint


class FileIdentityCaptureError(Exception):
    """A classified failure whose rendered message contains no filesystem detail."""

    def __init__(self, code: str) -> None:
        if code not in _CAPTURE_ERROR_CODES:
            raise ValueError("identity capture error code is invalid")
        self.code = code
        self.safe_message = IDENTITY_CAPTURE_SAFE_MESSAGE
        super().__init__(self.safe_message)


class FileIdentityCaptureInterrupted(KeyboardInterrupt):
    """Typed interruption that retains normal KeyboardInterrupt semantics."""

    code = "identity_capture_interrupted"
    safe_message = IDENTITY_CAPTURE_SAFE_MESSAGE

    def __str__(self) -> str:
        return self.safe_message


def capture_regular_file_identity(
    path: Path,
    root: Path,
    *,
    chunk_size: int = DEFAULT_IDENTITY_CAPTURE_CHUNK_SIZE,
) -> CapturedRegularFileIdentity:
    """Capture stable SHA-256 identity from one safely revalidated descriptor.

    This proves continuity only from this explicit capture onward. A later strict
    executor must revalidate the recorded identity before moving the pathname.
    """

    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or chunk_size <= 0
    ):
        raise ValueError("identity capture chunk size must be a positive integer")

    source = Path(path)
    try:
        resolved_root = Path(root).resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError
        if not source.is_absolute():
            raise ValueError
        source = Path(os.path.abspath(source))
    except (OSError, ValueError):
        raise FileIdentityCaptureError("unsafe_source") from None

    try:
        path_stat_before = os.lstat(source)
    except FileNotFoundError:
        raise FileIdentityCaptureError("source_missing") from None
    except PermissionError:
        raise FileIdentityCaptureError("source_unreadable") from None
    except OSError:
        raise FileIdentityCaptureError("identity_capture_failed") from None
    _require_regular_path(path_stat_before)
    _require_safe_parent(source, resolved_root)

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    result: CapturedRegularFileIdentity | None = None
    pending_error: BaseException | None = None
    try:
        try:
            descriptor = os.open(source, flags)
        except FileNotFoundError:
            raise FileIdentityCaptureError("source_changed_during_hash") from None
        except PermissionError:
            raise FileIdentityCaptureError("source_unreadable") from None
        except OSError as error:
            code = (
                "source_symlink"
                if error.errno in {errno.ELOOP, getattr(errno, "EMLINK", -1)}
                else "source_unreadable"
                if error.errno in {errno.EACCES, errno.EPERM}
                else "identity_capture_failed"
            )
            raise FileIdentityCaptureError(code) from None

        try:
            descriptor_stat_before = os.fstat(descriptor)
        except OSError:
            raise FileIdentityCaptureError("identity_capture_failed") from None
        _require_stable_regular_file(path_stat_before, descriptor_stat_before)
        fingerprint = _fingerprint(descriptor_stat_before)

        digest = hashlib.sha256()
        while True:
            try:
                block = os.read(descriptor, chunk_size)
            except OSError:
                raise FileIdentityCaptureError("source_unreadable") from None
            if not block:
                break
            digest.update(block)

        try:
            descriptor_stat_after = os.fstat(descriptor)
        except OSError:
            raise FileIdentityCaptureError("identity_capture_failed") from None
        _require_stable_regular_file(descriptor_stat_before, descriptor_stat_after)
        try:
            path_stat_after = os.lstat(source)
        except OSError:
            raise FileIdentityCaptureError("source_changed_during_hash") from None
        _require_stable_regular_file(descriptor_stat_after, path_stat_after)
        _require_safe_parent(source, resolved_root, changed=True)
        result = CapturedRegularFileIdentity(
            content_identity=RegularFileContentIdentity(
                kind=REGULAR_FILE_IDENTITY_KIND,
                algorithm=SHA256_IDENTITY_ALGORITHM,
                digest=digest.hexdigest(),
            ),
            stat_fingerprint=fingerprint,
        )
    except KeyboardInterrupt:
        pending_error = FileIdentityCaptureInterrupted()
        pending_error.__cause__ = None
    except BaseException as error:
        pending_error = error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                if pending_error is None:
                    pending_error = FileIdentityCaptureError("identity_capture_failed")

    if pending_error is not None:
        raise pending_error
    if result is None:  # Defensive fail-closed guard.
        raise FileIdentityCaptureError("identity_capture_failed")
    return result


def _require_regular_path(value: os.stat_result) -> None:
    if stat.S_ISLNK(value.st_mode):
        raise FileIdentityCaptureError("source_symlink")
    if not stat.S_ISREG(value.st_mode):
        raise FileIdentityCaptureError("unsupported_item")


def _require_safe_parent(source: Path, root: Path, *, changed: bool = False) -> None:
    try:
        resolved_parent = source.parent.resolve(strict=True)
        validate_under_root(resolved_parent, root)
    except (OSError, ValueError):
        code = "source_changed_during_hash" if changed else "unsafe_source"
        raise FileIdentityCaptureError(code) from None


def _require_stable_regular_file(
    before: os.stat_result,
    after: os.stat_result,
) -> None:
    if (
        not stat.S_ISREG(after.st_mode)
        or _stat_evidence(before) != _stat_evidence(after)
    ):
        raise FileIdentityCaptureError("source_changed_during_hash")


def _stat_evidence(value: os.stat_result) -> tuple[int, int, int, int, int, int | None]:
    return (
        stat.S_IFMT(value.st_mode),
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        getattr(value, "st_ctime_ns", None),
    )


def _fingerprint(value: os.stat_result) -> RegularFileStatFingerprint:
    return RegularFileStatFingerprint(
        file_type=REGULAR_FILE_IDENTITY_KIND,
        device=value.st_dev,
        inode=value.st_ino,
        size_bytes=value.st_size,
        modified_time_ns=value.st_mtime_ns,
        changed_time_ns=getattr(value, "st_ctime_ns", None),
    )
