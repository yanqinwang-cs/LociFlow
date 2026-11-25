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
    OrganizationOperationRecord,
)
from organizer.application.organization_undo_identity_intent import (
    validate_v2_organization_undo_parent,
)
from organizer.application.organization_undo_models import (
    ORGANIZATION_UNDO_RECORD_TYPE,
    ORGANIZATION_UNDO_SCHEMA_VERSION_V2,
    UNDO_ITEM_RESTORED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_VERIFICATION_NOT_RUN,
    OrganizationUndoItem,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.file_identity import DEFAULT_HASH_CHUNK_SIZE, is_canonical_sha256


class IdentityUndoVerificationState(str, Enum):
    PASSED = "passed"
    MISMATCH = "mismatch"
    INDETERMINATE = "indeterminate"


class IdentityUndoMismatchCategory(str, Enum):
    MOVED_FROM_PRESENT = "moved_from_present"
    RESTORED_TARGET_MISSING = "restored_target_missing"
    RESTORED_TARGET_UNSAFE = "restored_target_unsafe"
    RESTORED_TARGET_SYMLINK = "restored_target_symlink"
    RESTORED_TARGET_NOT_REGULAR = "restored_target_not_regular"
    RESTORED_IDENTITY_MISMATCH = "restored_identity_mismatch"
    ITEM_MAPPING_MISMATCH = "item_mapping_mismatch"


@dataclass(frozen=True)
class IdentityUndoVerificationResult:
    state: IdentityUndoVerificationState
    affected_items: int
    verified_count: int
    mismatch_categories: tuple[IdentityUndoMismatchCategory, ...]


class IdentityUndoVerificationEligibilityError(ValueError):
    """The durable parent and Undo are not a complete v2 verification pair."""

    def __init__(self) -> None:
        super().__init__("The restore is not eligible for identity verification.")


class _InspectionUnavailable(Exception):
    pass


class _Mismatch(Exception):
    def __init__(self, category: IdentityUndoMismatchCategory) -> None:
        self.category = category
        super().__init__(category.value)


@dataclass(frozen=True)
class _ItemObservation:
    mismatches: tuple[IdentityUndoMismatchCategory, ...] = ()
    indeterminate: bool = False


def verify_identity_organization_undo(
    undo_record: OrganizationUndoRecord,
    parent_record: OrganizationOperationRecord,
    root: Path,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> IdentityUndoVerificationResult:
    """Inspect a complete v2 Undo without changing filesystem or durable state."""

    _validate_chunk_size(chunk_size)
    resolved_root = require_identity_organization_undo_verification_eligible(
        undo_record,
        parent_record,
        root,
    )

    mismatch_categories: set[IdentityUndoMismatchCategory] = set()
    verified_count = 0
    any_indeterminate = False
    root_descriptor = _open_root(resolved_root)
    try:
        for item in undo_record.expected_restores:
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
        for category in IdentityUndoMismatchCategory
        if category in mismatch_categories
    )
    state = (
        IdentityUndoVerificationState.MISMATCH
        if ordered
        else IdentityUndoVerificationState.INDETERMINATE
        if any_indeterminate
        else IdentityUndoVerificationState.PASSED
    )
    return IdentityUndoVerificationResult(
        state=state,
        affected_items=len(undo_record.expected_restores),
        verified_count=verified_count,
        mismatch_categories=ordered,
    )


def require_identity_organization_undo_verification_eligible(
    undo_record: OrganizationUndoRecord,
    parent_record: OrganizationOperationRecord,
    root: Path,
) -> Path:
    """Validate a v2 Undo verification pair without inspecting its files."""

    resolved_root, root_identity = _eligible_root(
        undo_record,
        parent_record,
        root,
    )
    _validate_records(undo_record, parent_record, root_identity)
    return resolved_root


def _eligible_root(
    undo_record: OrganizationUndoRecord,
    parent_record: OrganizationOperationRecord,
    root: Path,
) -> tuple[Path, str]:
    try:
        resolved_root = Path(root).resolve(strict=True)
        root_stat = os.stat(resolved_root, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        raise IdentityUndoVerificationEligibilityError() from None
    if not stat.S_ISDIR(root_stat.st_mode):
        raise IdentityUndoVerificationEligibilityError()
    root_identity = hashlib.sha256(
        str(resolved_root).encode("utf-8")
    ).hexdigest()
    if (
        not isinstance(undo_record, OrganizationUndoRecord)
        or not isinstance(parent_record, OrganizationOperationRecord)
        or undo_record.configured_root_identity != root_identity
        or parent_record.configured_root_identity != root_identity
    ):
        raise IdentityUndoVerificationEligibilityError()
    return resolved_root, root_identity


def _validate_records(
    undo_record: OrganizationUndoRecord,
    parent_record: OrganizationOperationRecord,
    root_identity: str,
) -> None:
    try:
        validate_v2_organization_undo_parent(parent_record, root_identity)
    except (TypeError, ValueError):
        raise IdentityUndoVerificationEligibilityError() from None

    if (
        undo_record.record_type != ORGANIZATION_UNDO_RECORD_TYPE
        or undo_record.schema_version != ORGANIZATION_UNDO_SCHEMA_VERSION_V2
        or undo_record.status != UNDO_RESTORED_UNVERIFIED
        or undo_record.operation_id != parent_record.operation_id
        or not _is_canonical_uuid(undo_record.undo_id)
        or not _is_canonical_uuid(undo_record.authorization_id)
        or not _nonempty_string(undo_record.created_at)
        or not _nonempty_string(undo_record.started_at)
        or undo_record.completed_at is not None
        or undo_record.failure_code is not None
        or not isinstance(undo_record.verification, UndoVerificationResult)
        or undo_record.verification.status != UNDO_VERIFICATION_NOT_RUN
        or undo_record.verification.verified_count != 0
        or undo_record.verification.mismatch_codes
        or not isinstance(undo_record.expected_restores, tuple)
        or not undo_record.expected_restores
        or len(undo_record.expected_restores) != len(parent_record.expected_moves)
    ):
        raise IdentityUndoVerificationEligibilityError()

    for parent_item, undo_item in zip(
        parent_record.expected_moves,
        undo_record.expected_restores,
    ):
        if not _eligible_undo_item(undo_item):
            raise IdentityUndoVerificationEligibilityError()
        if (
            undo_item.change_id != parent_item.change_id
            or undo_item.moved_from != parent_item.destination
            or undo_item.restore_target != parent_item.source
            or undo_item.baseline_size_bytes != parent_item.source_size_bytes
            or undo_item.baseline_modified_time
            != parent_item.source_modified_time
            or undo_item.content_identity != parent_item.content_identity
        ):
            raise IdentityUndoVerificationEligibilityError()


def _eligible_undo_item(item: object) -> bool:
    if not isinstance(item, OrganizationUndoItem):
        return False
    identity = item.content_identity
    return (
        isinstance(item.change_id, str)
        and bool(item.change_id)
        and _safe_relative_path_value(item.restore_target)
        and _safe_relative_path_value(item.moved_from)
        and isinstance(item.baseline_size_bytes, int)
        and not isinstance(item.baseline_size_bytes, bool)
        and item.baseline_size_bytes >= 0
        and isinstance(item.baseline_modified_time, (int, float))
        and not isinstance(item.baseline_modified_time, bool)
        and math.isfinite(item.baseline_modified_time)
        and item.result_state == UNDO_ITEM_RESTORED
        and item.failure_code is None
        and item.recovery_state is None
        and isinstance(identity, RegularFileContentIdentity)
        and identity.kind == REGULAR_FILE_IDENTITY_KIND
        and identity.algorithm == SHA256_IDENTITY_ALGORITHM
        and is_canonical_sha256(identity.digest)
    )


def _safe_relative_path_value(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return (
        not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == value
    )


def _inspect_item(
    root_descriptor: int,
    item: OrganizationUndoItem,
    *,
    chunk_size: int,
) -> _ItemObservation:
    mismatches: list[IdentityUndoMismatchCategory] = []
    indeterminate = False
    try:
        _require_moved_from_absent(
            root_descriptor,
            tuple(Path(item.moved_from).parts),
        )
    except _Mismatch as error:
        mismatches.append(error.category)
    except _InspectionUnavailable:
        indeterminate = True

    try:
        _verify_restored_target(
            root_descriptor,
            tuple(Path(item.restore_target).parts),
            item.content_identity.digest,
            chunk_size=chunk_size,
        )
    except _Mismatch as error:
        mismatches.append(error.category)
    except _InspectionUnavailable:
        indeterminate = True
    return _ItemObservation(tuple(mismatches), indeterminate)


def _require_moved_from_absent(
    root_descriptor: int,
    parts: tuple[str, ...],
) -> None:
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
        raise _Mismatch(IdentityUndoMismatchCategory.MOVED_FROM_PRESENT)
    finally:
        _close_quietly(parent)


def _verify_restored_target(
    root_descriptor: int,
    parts: tuple[str, ...],
    expected_digest: str,
    *,
    chunk_size: int,
) -> None:
    try:
        parent = _open_parent(root_descriptor, parts[:-1])
    except FileNotFoundError:
        raise _Mismatch(
            IdentityUndoMismatchCategory.RESTORED_TARGET_MISSING
        ) from None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _Mismatch(
                IdentityUndoMismatchCategory.RESTORED_TARGET_UNSAFE
            ) from None
        raise _InspectionUnavailable() from None

    descriptor: int | None = None
    try:
        initial_path_stat = _target_lstat(parent, parts[-1])
        if stat.S_ISLNK(initial_path_stat.st_mode):
            raise _Mismatch(
                IdentityUndoMismatchCategory.RESTORED_TARGET_SYMLINK
            )
        if not stat.S_ISREG(initial_path_stat.st_mode):
            raise _Mismatch(
                IdentityUndoMismatchCategory.RESTORED_TARGET_NOT_REGULAR
            )
        descriptor = _open_target(parent, parts[-1])
        initial_descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(initial_descriptor_stat.st_mode):
            raise _Mismatch(
                IdentityUndoMismatchCategory.RESTORED_TARGET_NOT_REGULAR
            )
        if not _same_path_object(initial_path_stat, initial_descriptor_stat):
            raise _Mismatch(
                IdentityUndoMismatchCategory.RESTORED_IDENTITY_MISMATCH
            )

        digest = _hash_descriptor(descriptor, chunk_size)
        final_descriptor_stat = os.fstat(descriptor)
        if _stability_evidence(initial_descriptor_stat) != _stability_evidence(
            final_descriptor_stat
        ):
            raise _InspectionUnavailable()
        final_path_stat = _reinspect_target(root_descriptor, parts)
        if not _same_path_object(final_path_stat, final_descriptor_stat):
            raise _InspectionUnavailable()
        if digest != expected_digest:
            raise _Mismatch(
                IdentityUndoMismatchCategory.RESTORED_IDENTITY_MISMATCH
            )
    except (KeyboardInterrupt, SystemExit):
        raise
    except (_Mismatch, _InspectionUnavailable):
        raise
    except OSError:
        raise _InspectionUnavailable() from None
    finally:
        if descriptor is not None:
            _close_quietly(descriptor)
        _close_quietly(parent)


def _target_lstat(parent: int, leaf: str) -> os.stat_result:
    try:
        return os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        raise _Mismatch(
            IdentityUndoMismatchCategory.RESTORED_TARGET_MISSING
        ) from None
    except OSError:
        raise _InspectionUnavailable() from None


def _open_target(parent: int, leaf: str) -> int:
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
            raise _Mismatch(
                IdentityUndoMismatchCategory.RESTORED_TARGET_SYMLINK
            ) from None
        if error.errno in {errno.EISDIR, errno.ENXIO}:
            raise _Mismatch(
                IdentityUndoMismatchCategory.RESTORED_TARGET_NOT_REGULAR
            ) from None
        raise _InspectionUnavailable() from None


def _reinspect_target(
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
        raise IdentityUndoVerificationEligibilityError() from None


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
        raise ValueError("identity Undo verification chunk size must be positive")


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


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
