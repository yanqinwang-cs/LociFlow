from __future__ import annotations

import errno
import hashlib
import math
import os
import stat
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from organizer.application.content_identity import (
    REGULAR_FILE_IDENTITY_KIND,
    SHA256_IDENTITY_ALGORITHM,
    RegularFileContentIdentity,
)
from organizer.application.organization_operation_models import (
    MOVE_MOVED,
    ORGANIZATION_OPERATION_RECORD_TYPE,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_MOVED_UNVERIFIED,
    VERIFICATION_NOT_RUN,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.file_identity import DEFAULT_HASH_CHUNK_SIZE, is_canonical_sha256


class IdentityVerificationState(str, Enum):
    PASSED = "passed"
    MISMATCH = "mismatch"
    INDETERMINATE = "indeterminate"


class IdentityMismatchCategory(str, Enum):
    SOURCE_PRESENT = "source_present"
    DESTINATION_MISSING = "destination_missing"
    DESTINATION_UNSAFE = "destination_unsafe"
    DESTINATION_SYMLINK = "destination_symlink"
    DESTINATION_NOT_REGULAR = "destination_not_regular"
    DESTINATION_IDENTITY_MISMATCH = "destination_identity_mismatch"
    ITEM_SHAPE_MISMATCH = "item_shape_mismatch"


@dataclass(frozen=True)
class IdentityOrganizationVerificationResult:
    state: IdentityVerificationState
    affected_items: int
    verified_count: int
    mismatch_categories: tuple[IdentityMismatchCategory, ...]


class IdentityVerificationEligibilityError(ValueError):
    """The durable operation is not a complete v2 verification candidate."""

    def __init__(self) -> None:
        super().__init__("The operation is not eligible for identity verification.")


class _InspectionUnavailable(Exception):
    pass


class _Mismatch(Exception):
    def __init__(self, category: IdentityMismatchCategory) -> None:
        self.category = category
        super().__init__(category.value)


@dataclass(frozen=True)
class _ItemObservation:
    mismatches: tuple[IdentityMismatchCategory, ...] = ()
    indeterminate: bool = False


def verify_identity_organization_operation(
    record: OrganizationOperationRecord,
    root: Path,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> IdentityOrganizationVerificationResult:
    """Inspect a complete v2 move without changing filesystem or durable state.

    Success describes only the filesystem state observed while each descriptor
    and its root-relative pathname were bound and checked. A later orchestrator
    must hold its lifecycle/coordinator ownership across verification and the
    immediate durable publication; this function makes no immutability claim.
    """

    _validate_chunk_size(chunk_size)
    resolved_root = require_identity_organization_verification_eligible(
        record,
        root,
    )

    mismatch_categories: set[IdentityMismatchCategory] = set()
    verified_count = 0
    any_indeterminate = False
    root_descriptor = _open_root(resolved_root)
    try:
        for item in record.expected_moves:
            observation = _inspect_item(
                root_descriptor,
                item,
                chunk_size=chunk_size,
            )
            mismatch_categories.update(observation.mismatches)
            any_indeterminate = any_indeterminate or observation.indeterminate
            if not observation.mismatches and not observation.indeterminate:
                verified_count += 1
    finally:
        _close_quietly(root_descriptor)

    ordered = tuple(
        category
        for category in IdentityMismatchCategory
        if category in mismatch_categories
    )
    state = (
        IdentityVerificationState.MISMATCH
        if ordered
        else IdentityVerificationState.INDETERMINATE
        if any_indeterminate
        else IdentityVerificationState.PASSED
    )
    return IdentityOrganizationVerificationResult(
        state=state,
        affected_items=len(record.expected_moves),
        verified_count=verified_count,
        mismatch_categories=ordered,
    )


def require_identity_organization_verification_eligible(
    record: OrganizationOperationRecord,
    root: Path,
) -> Path:
    """Validate a v2 verification candidate without inspecting its files."""

    resolved_root = _eligible_root(record, root)
    _validate_record(record)
    return resolved_root


def _eligible_root(record: OrganizationOperationRecord, root: Path) -> Path:
    try:
        resolved_root = Path(root).resolve(strict=True)
        root_stat = os.stat(resolved_root, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        raise IdentityVerificationEligibilityError() from None
    if not stat.S_ISDIR(root_stat.st_mode):
        raise IdentityVerificationEligibilityError()
    expected_root_identity = hashlib.sha256(
        str(resolved_root).encode("utf-8")
    ).hexdigest()
    if (
        not isinstance(record, OrganizationOperationRecord)
        or record.configured_root_identity != expected_root_identity
    ):
        raise IdentityVerificationEligibilityError()
    return resolved_root


def _validate_record(record: OrganizationOperationRecord) -> None:
    if (
        record.record_type != ORGANIZATION_OPERATION_RECORD_TYPE
        or record.schema_version != ORGANIZATION_OPERATION_SCHEMA_VERSION_V2
        or record.status != STATUS_MOVED_UNVERIFIED
        or not _is_canonical_uuid(record.operation_id)
        or not _is_canonical_uuid(record.authorization_id)
        or not _nonempty_string(record.scan_generation)
        or not _nonnegative_integer(record.decision_revision)
        or not _nonempty_string(record.approved_at)
        or not _nonempty_string(record.created_at)
        or not _nonnegative_integer(record.duplicate_actions_deferred)
        or not isinstance(record.expected_moves, tuple)
        or not record.expected_moves
        or not isinstance(record.verification, OrganizationOperationVerification)
        or not _nonempty_string(record.started_at)
        or record.completed_at is not None
        or record.failure_code is not None
        or record.verification.status != VERIFICATION_NOT_RUN
        or not _nonnegative_integer(record.verification.verified_count)
        or record.verification.verified_count != 0
        or not isinstance(record.verification.mismatch_codes, tuple)
        or bool(record.verification.mismatch_codes)
    ):
        raise IdentityVerificationEligibilityError()

    change_ids: set[str] = set()
    sources: set[str] = set()
    destinations: set[str] = set()
    for item in record.expected_moves:
        if not _eligible_item(item):
            raise IdentityVerificationEligibilityError()
        source = _safe_relative_path(item.source)
        destination = _safe_relative_path(item.destination)
        if (
            item.change_id in change_ids
            or source in sources
            or destination in destinations
        ):
            raise IdentityVerificationEligibilityError()
        change_ids.add(item.change_id)
        sources.add(source)
        destinations.add(destination)
    if sources & destinations:
        raise IdentityVerificationEligibilityError()


def _eligible_item(item: object) -> bool:
    if not isinstance(item, OrganizationExpectedMove):
        return False
    identity = item.content_identity
    return (
        isinstance(item.change_id, str)
        and bool(item.change_id)
        and _nonempty_string(item.reason)
        and _nonnegative_integer(item.confidence)
        and isinstance(item.overwrite_risk, bool)
        and _nonnegative_integer(item.source_size_bytes)
        and isinstance(item.source_modified_time, (int, float))
        and not isinstance(item.source_modified_time, bool)
        and math.isfinite(item.source_modified_time)
        and item.operation == "move"
        and item.result_state == MOVE_MOVED
        and item.failure_code is None
        and item.recovery_state is None
        and isinstance(identity, RegularFileContentIdentity)
        and identity.kind == REGULAR_FILE_IDENTITY_KIND
        and identity.algorithm == SHA256_IDENTITY_ALGORITHM
        and is_canonical_sha256(identity.digest)
    )


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise IdentityVerificationEligibilityError()
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise IdentityVerificationEligibilityError()
    canonical = path.as_posix()
    if canonical != value:
        raise IdentityVerificationEligibilityError()
    return canonical


def _inspect_item(
    root_descriptor: int,
    item: OrganizationExpectedMove,
    *,
    chunk_size: int,
) -> _ItemObservation:
    mismatches: list[IdentityMismatchCategory] = []
    indeterminate = False
    try:
        _require_source_absent(root_descriptor, tuple(Path(item.source).parts))
    except _Mismatch as error:
        mismatches.append(error.category)
    except _InspectionUnavailable:
        indeterminate = True

    try:
        _verify_destination(
            root_descriptor,
            tuple(Path(item.destination).parts),
            item.content_identity.digest,
            chunk_size=chunk_size,
        )
    except _Mismatch as error:
        mismatches.append(error.category)
    except _InspectionUnavailable:
        indeterminate = True
    return _ItemObservation(tuple(mismatches), indeterminate)


def _require_source_absent(root_descriptor: int, parts: tuple[str, ...]) -> None:
    try:
        parent = _open_parent(root_descriptor, parts[:-1])
    except FileNotFoundError:
        return
    except OSError:
        raise _InspectionUnavailable() from None
    try:
        try:
            os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise _InspectionUnavailable() from None
        raise _Mismatch(IdentityMismatchCategory.SOURCE_PRESENT)
    finally:
        _close_quietly(parent)


def _verify_destination(
    root_descriptor: int,
    parts: tuple[str, ...],
    expected_digest: str,
    *,
    chunk_size: int,
) -> None:
    try:
        parent = _open_parent(root_descriptor, parts[:-1])
    except FileNotFoundError:
        raise _Mismatch(IdentityMismatchCategory.DESTINATION_MISSING) from None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _Mismatch(IdentityMismatchCategory.DESTINATION_UNSAFE) from None
        raise _InspectionUnavailable() from None

    descriptor: int | None = None
    try:
        initial_path_stat = _destination_lstat(parent, parts[-1])
        if stat.S_ISLNK(initial_path_stat.st_mode):
            raise _Mismatch(IdentityMismatchCategory.DESTINATION_SYMLINK)
        if not stat.S_ISREG(initial_path_stat.st_mode):
            raise _Mismatch(IdentityMismatchCategory.DESTINATION_NOT_REGULAR)
        descriptor = _open_destination(parent, parts[-1])
        initial_descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(initial_descriptor_stat.st_mode):
            raise _Mismatch(IdentityMismatchCategory.DESTINATION_NOT_REGULAR)
        if not _same_path_object(initial_path_stat, initial_descriptor_stat):
            raise _Mismatch(
                IdentityMismatchCategory.DESTINATION_IDENTITY_MISMATCH
            )

        digest = _hash_descriptor(descriptor, chunk_size)
        final_descriptor_stat = os.fstat(descriptor)
        if _stability_evidence(initial_descriptor_stat) != _stability_evidence(
            final_descriptor_stat
        ):
            raise _InspectionUnavailable()
        final_path_stat = _reinspect_destination(root_descriptor, parts)
        if not _same_path_object(final_path_stat, final_descriptor_stat):
            raise _InspectionUnavailable()
        if digest != expected_digest:
            raise _Mismatch(
                IdentityMismatchCategory.DESTINATION_IDENTITY_MISMATCH
            )
    except (KeyboardInterrupt, SystemExit):
        raise
    except _Mismatch:
        raise
    except _InspectionUnavailable:
        raise
    except OSError:
        raise _InspectionUnavailable() from None
    finally:
        if descriptor is not None:
            _close_quietly(descriptor)
        _close_quietly(parent)


def _destination_lstat(parent: int, leaf: str) -> os.stat_result:
    try:
        return os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        raise _Mismatch(IdentityMismatchCategory.DESTINATION_MISSING) from None
    except OSError:
        raise _InspectionUnavailable() from None


def _open_destination(parent: int, leaf: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        return os.open(leaf, flags, dir_fd=parent)
    except FileNotFoundError:
        raise _InspectionUnavailable() from None
    except OSError as error:
        if error.errno in {errno.ELOOP, getattr(errno, "EMLINK", -1)}:
            raise _Mismatch(IdentityMismatchCategory.DESTINATION_SYMLINK) from None
        if error.errno in {errno.EISDIR, errno.ENXIO}:
            raise _Mismatch(IdentityMismatchCategory.DESTINATION_NOT_REGULAR) from None
        raise _InspectionUnavailable() from None


def _reinspect_destination(
    root_descriptor: int,
    parts: tuple[str, ...],
) -> os.stat_result:
    try:
        parent = _open_parent(root_descriptor, parts[:-1])
    except OSError:
        raise _InspectionUnavailable() from None
    try:
        try:
            value = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except OSError:
            raise _InspectionUnavailable() from None
        if not stat.S_ISREG(value.st_mode):
            raise _InspectionUnavailable()
        return value
    finally:
        _close_quietly(parent)


def _open_root(root: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(root, flags)
    except OSError:
        raise IdentityVerificationEligibilityError() from None


def _open_parent(root_descriptor: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_descriptor)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        for part in parts:
            following = os.open(part, flags, dir_fd=current)
            _close_quietly(current)
            current = following
        return current
    except BaseException:
        _close_quietly(current)
        raise


def _hash_descriptor(descriptor: int, chunk_size: int) -> str:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError:
        raise _InspectionUnavailable() from None
    digest = hashlib.sha256()
    while True:
        try:
            block = os.read(descriptor, chunk_size)
        except OSError:
            raise _InspectionUnavailable() from None
        if not block:
            return digest.hexdigest()
        digest.update(block)


def _same_path_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_IFMT(left.st_mode),
        left.st_dev,
        left.st_ino,
    ) == (
        stat.S_IFMT(right.st_mode),
        right.st_dev,
        right.st_ino,
    )


def _stability_evidence(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int | None]:
    return (
        stat.S_IFMT(value.st_mode),
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        getattr(value, "st_ctime_ns", None),
    )


def _validate_chunk_size(chunk_size: int) -> None:
    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or chunk_size <= 0
    ):
        raise ValueError("identity verification chunk size must be positive")


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass
