from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REGULAR_FILE_TYPE = "regular_file"
SHA256_ALGORITHM = "sha256"
DEFAULT_HASH_CHUNK_SIZE = 1024 * 1024

_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}")
_ERROR_MESSAGES = {
    "missing_identity": "The file could not be moved safely. Check the folder and try again.",
    "malformed_identity": "The file could not be moved safely. Check the folder and try again.",
    "source_identity_changed": (
        "The file changed before it could be moved. Check the folder and try again."
    ),
    "source_unstable": (
        "The file changed before it could be moved. Check the folder and try again."
    ),
    "unsafe_source": "The file could not be moved safely. Check the folder and try again.",
    "unsupported_item": "The file could not be moved safely. Check the folder and try again.",
    "source_symlink": "The file could not be moved safely. Check the folder and try again.",
    "source_missing": (
        "The file changed before it could be moved. Check the folder and try again."
    ),
    "source_unreadable": "The file could not be moved safely. Check the folder and try again.",
    "identity_capture_failed": (
        "The file could not be moved safely. Check the folder and try again."
    ),
}


class StableFileIdentityError(Exception):
    """A typed identity failure with bounded, filesystem-neutral copy."""

    def __init__(self, code: str) -> None:
        if code not in _ERROR_MESSAGES:
            raise ValueError("stable file identity error code is invalid")
        self.code = code
        self.safe_message = _ERROR_MESSAGES[code]
        super().__init__(self.safe_message)


class StableFileIdentityInterrupted(KeyboardInterrupt):
    code = "identity_capture_interrupted"
    safe_message = (
        "The file could not be checked safely. Check the folder and try again."
    )

    def __str__(self) -> str:
        return self.safe_message


@dataclass(frozen=True)
class RegularFileStatFingerprint:
    """Transient descriptor evidence; never a durable content identity."""

    file_type: str
    device: int
    inode: int
    size_bytes: int
    modified_time_ns: int
    changed_time_ns: int | None

    def __post_init__(self) -> None:
        if self.file_type != REGULAR_FILE_TYPE:
            raise ValueError("regular file fingerprint type is invalid")
        for value, label in (
            (self.device, "device"),
            (self.inode, "inode"),
            (self.size_bytes, "size"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"regular file fingerprint {label} is invalid")
        if isinstance(self.modified_time_ns, bool) or not isinstance(
            self.modified_time_ns, int
        ):
            raise ValueError("regular file fingerprint modified time is invalid")
        if self.changed_time_ns is not None and (
            isinstance(self.changed_time_ns, bool)
            or not isinstance(self.changed_time_ns, int)
        ):
            raise ValueError("regular file fingerprint changed time is invalid")


@dataclass(frozen=True)
class StableRegularFileIdentity:
    digest: str
    stat_fingerprint: RegularFileStatFingerprint

    def __post_init__(self) -> None:
        if (
            not isinstance(self.digest, str)
            or _SHA256_DIGEST.fullmatch(self.digest) is None
        ):
            raise ValueError("stable regular file digest is invalid")
        if not isinstance(self.stat_fingerprint, RegularFileStatFingerprint):
            raise ValueError("stable regular file fingerprint is invalid")


@dataclass(frozen=True)
class RequiredRegularFileIdentity:
    digest: str
    stat_fingerprint: RegularFileStatFingerprint

    def __post_init__(self) -> None:
        if (
            not isinstance(self.digest, str)
            or _SHA256_DIGEST.fullmatch(self.digest) is None
        ):
            raise StableFileIdentityError("malformed_identity")
        if not isinstance(self.stat_fingerprint, RegularFileStatFingerprint):
            raise StableFileIdentityError("malformed_identity")


@dataclass(frozen=True, repr=False)
class VerifiedRegularFileHandle:
    descriptor: int
    identity: StableRegularFileIdentity

    def fileno(self) -> int:
        return self.descriptor

    def rewind(self) -> None:
        os.lseek(self.descriptor, 0, os.SEEK_SET)

    def __repr__(self) -> str:
        return "VerifiedRegularFileHandle(<verified>)"


@contextmanager
def capture_open_regular_file(
    path: Path,
    root: Path,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> Iterator[VerifiedRegularFileHandle]:
    descriptor, identity = _open_stable_regular_file(
        path,
        root,
        required=None,
        chunk_size=chunk_size,
    )
    try:
        yield VerifiedRegularFileHandle(descriptor, identity)
    except BaseException:
        _close_quietly(descriptor)
        raise
    else:
        _close_after_context(descriptor)


@contextmanager
def open_verified_regular_file(
    path: Path,
    root: Path,
    required: RequiredRegularFileIdentity,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> Iterator[VerifiedRegularFileHandle]:
    if required is None:
        raise StableFileIdentityError("missing_identity")
    if not isinstance(required, RequiredRegularFileIdentity):
        raise StableFileIdentityError("malformed_identity")
    descriptor, identity = _open_stable_regular_file(
        path,
        root,
        required=required,
        chunk_size=chunk_size,
    )
    try:
        yield VerifiedRegularFileHandle(descriptor, identity)
    except BaseException:
        _close_quietly(descriptor)
        raise
    else:
        _close_after_context(descriptor)


def fingerprint_from_stat(value: os.stat_result) -> RegularFileStatFingerprint:
    if not stat.S_ISREG(value.st_mode):
        raise StableFileIdentityError("unsupported_item")
    return RegularFileStatFingerprint(
        file_type=REGULAR_FILE_TYPE,
        device=value.st_dev,
        inode=value.st_ino,
        size_bytes=value.st_size,
        modified_time_ns=value.st_mtime_ns,
        changed_time_ns=getattr(value, "st_ctime_ns", None),
    )


def is_canonical_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_DIGEST.fullmatch(value) is not None


def verify_open_regular_file_descriptor(
    descriptor: int,
    required_digest: str,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> RegularFileStatFingerprint:
    """Verify an already-open regular file without taking descriptor ownership."""

    if not is_canonical_sha256(required_digest):
        raise StableFileIdentityError("malformed_identity")
    _validate_chunk_size(chunk_size)
    before = _fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise StableFileIdentityError("unsupported_item")
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError:
        raise StableFileIdentityError("identity_capture_failed") from None
    try:
        digest = _hash_descriptor(descriptor, chunk_size)
        after = _fstat(descriptor)
        _require_stable_stat(before, after, code="source_unstable")
        if digest != required_digest:
            raise StableFileIdentityError("source_identity_changed")
        return fingerprint_from_stat(after)
    except KeyboardInterrupt:
        raise StableFileIdentityInterrupted() from None


def _open_stable_regular_file(
    path: Path,
    root: Path,
    *,
    required: RequiredRegularFileIdentity | None,
    chunk_size: int,
) -> tuple[int, StableRegularFileIdentity]:
    _validate_chunk_size(chunk_size)
    source, resolved_root = _validate_source_input(path, root)
    path_stat_before = _lstat_initial(source)
    _require_regular_path(path_stat_before)
    _require_safe_parent(source, resolved_root)

    descriptor: int | None = None
    try:
        descriptor = _open_source(source)
        descriptor_stat_before = _fstat(descriptor)
        _require_stable_stat(
            path_stat_before,
            descriptor_stat_before,
            code="source_unstable",
        )
        observed_stat = fingerprint_from_stat(descriptor_stat_before)
        if required is not None and observed_stat != required.stat_fingerprint:
            raise StableFileIdentityError("source_identity_changed")

        digest = _hash_descriptor(descriptor, chunk_size)
        descriptor_stat_after = _fstat(descriptor)
        _require_stable_stat(
            descriptor_stat_before,
            descriptor_stat_after,
            code="source_unstable",
        )
        path_stat_after = _lstat_after_hash(source)
        _require_stable_stat(
            descriptor_stat_after,
            path_stat_after,
            code="source_unstable",
        )
        _require_safe_parent(source, resolved_root, unstable=True)
        if required is not None and digest != required.digest:
            raise StableFileIdentityError("source_identity_changed")
        identity = StableRegularFileIdentity(digest, observed_stat)
    except KeyboardInterrupt:
        if descriptor is not None:
            _close_quietly(descriptor)
        raise StableFileIdentityInterrupted() from None
    except BaseException:
        if descriptor is not None:
            _close_quietly(descriptor)
        raise
    return descriptor, identity


def _validate_chunk_size(chunk_size: int) -> None:
    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or chunk_size <= 0
    ):
        raise ValueError("file identity chunk size must be a positive integer")


def _validate_source_input(path: Path, root: Path) -> tuple[Path, Path]:
    source = Path(path)
    try:
        resolved_root = Path(root).resolve(strict=True)
        if not resolved_root.is_dir() or not source.is_absolute():
            raise ValueError
        source = Path(os.path.abspath(source))
    except (OSError, ValueError):
        raise StableFileIdentityError("unsafe_source") from None
    return source, resolved_root


def _lstat_initial(source: Path) -> os.stat_result:
    try:
        return os.lstat(source)
    except FileNotFoundError:
        raise StableFileIdentityError("source_missing") from None
    except PermissionError:
        raise StableFileIdentityError("source_unreadable") from None
    except OSError:
        raise StableFileIdentityError("identity_capture_failed") from None


def _lstat_after_hash(source: Path) -> os.stat_result:
    try:
        return os.lstat(source)
    except OSError:
        raise StableFileIdentityError("source_unstable") from None


def _require_regular_path(value: os.stat_result) -> None:
    if stat.S_ISLNK(value.st_mode):
        raise StableFileIdentityError("source_symlink")
    if not stat.S_ISREG(value.st_mode):
        raise StableFileIdentityError("unsupported_item")


def _require_safe_parent(
    source: Path,
    root: Path,
    *,
    unstable: bool = False,
) -> None:
    try:
        resolved_parent = source.parent.resolve(strict=True)
        _validate_under_root(resolved_parent, root)
    except (OSError, ValueError):
        code = "source_unstable" if unstable else "unsafe_source"
        raise StableFileIdentityError(code) from None


def _open_source(source: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        return os.open(source, flags)
    except FileNotFoundError:
        raise StableFileIdentityError("source_unstable") from None
    except PermissionError:
        raise StableFileIdentityError("source_unreadable") from None
    except OSError as error:
        code = (
            "source_symlink"
            if error.errno in {errno.ELOOP, getattr(errno, "EMLINK", -1)}
            else "source_unreadable"
            if error.errno in {errno.EACCES, errno.EPERM}
            else "identity_capture_failed"
        )
        raise StableFileIdentityError(code) from None


def _fstat(descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError:
        raise StableFileIdentityError("identity_capture_failed") from None


def _hash_descriptor(descriptor: int, chunk_size: int) -> str:
    digest = hashlib.sha256()
    while True:
        try:
            block = os.read(descriptor, chunk_size)
        except OSError:
            raise StableFileIdentityError("source_unreadable") from None
        if not block:
            return digest.hexdigest()
        digest.update(block)


def _require_stable_stat(
    before: os.stat_result,
    after: os.stat_result,
    *,
    code: str,
) -> None:
    if not stat.S_ISREG(after.st_mode) or _stat_evidence(before) != _stat_evidence(
        after
    ):
        raise StableFileIdentityError(code)


def _stat_evidence(value: os.stat_result) -> tuple[int, int, int, int, int, int | None]:
    return (
        stat.S_IFMT(value.st_mode),
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        getattr(value, "st_ctime_ns", None),
    )


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _close_after_context(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        raise StableFileIdentityError("identity_capture_failed") from None


def _validate_under_root(path: Path, root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError("path is outside the selected root")
