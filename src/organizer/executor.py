from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import unicodedata
from typing import Protocol

from organizer.executor_identity import (
    IdentityMovePlanItem,
    StrictExecutorError,
    StrictExecutorInterrupted,
    ValidatedIdentityMovePlan,
    validate_same_volume_identity_move_plan,
)
from organizer.file_identity import (
    RequiredRegularFileIdentity,
    RegularFileStatFingerprint,
    StableFileIdentityError,
    fingerprint_from_stat,
    open_verified_regular_file,
    verify_open_regular_file_descriptor,
)
from organizer.models import MovePlanItem, MoveResult, OperationLog


_DIR_FD_FUNCTIONS = (os.open, os.mkdir, os.stat, os.link, os.unlink)
_LINK_FUNCTION = os.link
_STAT_FUNCTION = os.stat


class MovementRecorder(Protocol):
    def before_batch(self, plan_items: tuple[MovePlanItem, ...]) -> None: ...

    def before_move(self, index: int, item: MovePlanItem) -> None: ...

    def after_success(
        self,
        index: int,
        item: MovePlanItem,
        result: MoveResult,
    ) -> None: ...

    def after_failure(
        self,
        index: int,
        item: MovePlanItem,
        result: MoveResult,
    ) -> None: ...

    def after_batch(self, operation_log: OperationLog) -> None: ...


class UndoRecorder(Protocol):
    def before_batch(self, items: tuple[MoveResult, ...]) -> None: ...

    def before_restore(self, index: int, item: MoveResult) -> None: ...

    def after_success(
        self,
        index: int,
        item: MoveResult,
        result: MoveResult,
    ) -> None: ...

    def after_failure(
        self,
        index: int,
        item: MoveResult,
        result: MoveResult,
    ) -> None: ...

    def after_batch(self, operation_log: OperationLog) -> None: ...


class MovementRecordingError(RuntimeError):
    def __init__(
        self,
        phase: str,
        *,
        operation_log: OperationLog | None = None,
        filesystem_changed: bool = False,
    ) -> None:
        super().__init__(f"durable movement recording failed during {phase}")
        self.phase = phase
        self.operation_log = operation_log
        self.filesystem_changed = filesystem_changed


class UndoRecordingError(RuntimeError):
    def __init__(
        self,
        phase: str,
        *,
        operation_log: OperationLog | None = None,
        filesystem_changed: bool = False,
    ) -> None:
        super().__init__(f"durable undo recording failed during {phase}")
        self.phase = phase
        self.operation_log = operation_log
        self.filesystem_changed = filesystem_changed


@dataclass(frozen=True, repr=False)
class _SameVolumeIdentityPublication:
    completed: bool
    destination_parent_created: bool

    def __repr__(self) -> str:
        return "_SameVolumeIdentityPublication(<completed>)"


def _prepare_same_volume_identity_batch(
    plan_items: tuple[IdentityMovePlanItem, ...],
    root: Path,
) -> ValidatedIdentityMovePlan:
    _require_strict_platform_support()
    _reject_existing_source_hard_links(plan_items, root)
    validated = validate_same_volume_identity_move_plan(plan_items, root)
    for classified in validated.items:
        item = classified.item
        try:
            current = os.lstat(item.plan_item.source)
            observed = fingerprint_from_stat(current)
        except (OSError, StableFileIdentityError, ValueError):
            raise StrictExecutorError("source_identity_changed") from None
        if current.st_nlink != 1:
            raise StrictExecutorError("unsupported_item")
        if observed != item.captured_stat:
            raise StrictExecutorError("source_identity_changed")
    return validated


def _reject_existing_source_hard_links(
    plan_items: tuple[IdentityMovePlanItem, ...],
    root: Path,
) -> None:
    if (
        not isinstance(plan_items, tuple)
        or not plan_items
        or any(not isinstance(item, IdentityMovePlanItem) for item in plan_items)
    ):
        return
    try:
        resolved_root = Path(root).resolve(strict=True)
    except (OSError, ValueError):
        return
    for item in plan_items:
        source = Path(item.plan_item.source)
        try:
            resolved_source = source.resolve(strict=True)
            if (
                resolved_source != resolved_root
                and resolved_root not in resolved_source.parents
            ):
                continue
            current = os.lstat(source)
        except OSError:
            continue
        if stat.S_ISREG(current.st_mode) and current.st_nlink != 1:
            raise StrictExecutorError("unsupported_item")


def _execute_same_volume_identity_batch(
    plan_items: tuple[IdentityMovePlanItem, ...],
    root: Path,
) -> tuple[_SameVolumeIdentityPublication, ...]:
    """Private A2-A engine; intentionally has no recorder or operation log."""

    validated = _prepare_same_volume_identity_batch(plan_items, root)
    requested_root = Path(os.path.abspath(root))
    resolved_root = Path(root).resolve(strict=True)
    root_descriptor = _open_root_directory(resolved_root)
    try:
        return tuple(
            _execute_same_volume_identity_item(
                classified.item,
                resolved_root,
                requested_root,
                root_descriptor,
            )
            for classified in validated.items
        )
    finally:
        _close_descriptor_quietly(root_descriptor)


def _execute_same_volume_identity_item(
    item: IdentityMovePlanItem,
    root: Path,
    requested_root: Path,
    root_descriptor: int,
) -> _SameVolumeIdentityPublication:
    source = _root_relative_lexical_path(
        item.plan_item.source,
        requested_root,
        root,
    )
    destination = _root_relative_lexical_path(
        item.plan_item.destination,
        requested_root,
        root,
    )
    source_parent_descriptor: int | None = None
    destination_parent_descriptor: int | None = None
    destination_descriptor: int | None = None
    destination_parent_created = False
    published = False
    try:
        source_parent_descriptor, _ = _open_directory_chain(
            root_descriptor,
            _parent_parts(source, root),
            create=False,
            failure_code="source_unstable",
        )
        required = RequiredRegularFileIdentity(
            digest=item.expected_digest,
            stat_fingerprint=item.captured_stat,
        )
        try:
            source_context = open_verified_regular_file(source, root, required)
            source_handle = source_context.__enter__()
        except StableFileIdentityError as error:
            raise _source_identity_failure(error) from None
        try:
            destination_parent_descriptor, destination_parent_created = (
                _open_directory_chain(
                    root_descriptor,
                    _parent_parts(destination, root),
                    create=True,
                    failure_code="destination_publication_failed",
                )
            )
            _require_directory_binding(
                source.parent,
                source_parent_descriptor,
                root,
                failure_code="source_unstable",
            )
            _require_directory_binding(
                destination.parent,
                destination_parent_descriptor,
                root,
                failure_code="destination_publication_failed",
                filesystem_changed=destination_parent_created,
            )
            _require_source_binding(
                source_parent_descriptor,
                source.name,
                source_handle.fileno(),
                expected_fingerprint=item.captured_stat,
                expected_link_count=1,
                failure_code="source_identity_changed",
            )
            _require_destination_absent(
                destination_parent_descriptor,
                destination.name,
                filesystem_changed=destination_parent_created,
            )
            source_stat = os.fstat(source_handle.fileno())
            destination_parent_stat = os.fstat(destination_parent_descriptor)
            if source_stat.st_dev != destination_parent_stat.st_dev:
                raise StrictExecutorError(
                    "cross_volume_unsupported",
                    filesystem_changed=destination_parent_created,
                )
            _require_directory_binding(
                destination.parent,
                destination_parent_descriptor,
                root,
                failure_code="destination_publication_failed",
                filesystem_changed=destination_parent_created,
            )
            _publish_hard_link(
                source_parent_descriptor,
                source.name,
                destination_parent_descriptor,
                destination.name,
                filesystem_changed=destination_parent_created,
            )
            published = True

            destination_descriptor = _open_destination_descriptor(
                destination_parent_descriptor,
                destination.name,
            )
            _verify_destination_binding(
                destination_parent_descriptor,
                destination.name,
                destination_descriptor,
                source_handle.fileno(),
                item.expected_digest,
                expected_link_count=2,
            )
            _fsync_after_publication(destination_descriptor)
            _fsync_after_publication(destination_parent_descriptor)

            _require_directory_binding(
                source.parent,
                source_parent_descriptor,
                root,
                failure_code="source_path_changed",
                filesystem_changed=True,
                recovery_required=True,
            )
            _require_directory_binding(
                destination.parent,
                destination_parent_descriptor,
                root,
                failure_code="destination_verification_failed",
                filesystem_changed=True,
                recovery_required=True,
            )
            _require_source_binding(
                source_parent_descriptor,
                source.name,
                source_handle.fileno(),
                expected_fingerprint=None,
                expected_link_count=2,
                failure_code="source_path_changed",
                recovery_required=True,
            )
            _verify_destination_binding(
                destination_parent_descriptor,
                destination.name,
                destination_descriptor,
                source_handle.fileno(),
                item.expected_digest,
                expected_link_count=2,
            )
            try:
                os.unlink(source.name, dir_fd=source_parent_descriptor)
            except OSError:
                raise _recovery_failure("source_removal_failed") from None
            _require_source_absent(source_parent_descriptor, source.name)
            _require_descriptor_link_count(source_handle.fileno(), 1)
            _fsync_after_publication(source_parent_descriptor)

            _require_directory_binding(
                source.parent,
                source_parent_descriptor,
                root,
                failure_code="source_path_changed",
                filesystem_changed=True,
                recovery_required=True,
            )
            _require_directory_binding(
                destination.parent,
                destination_parent_descriptor,
                root,
                failure_code="destination_verification_failed",
                filesystem_changed=True,
                recovery_required=True,
            )
            _verify_destination_binding(
                destination_parent_descriptor,
                destination.name,
                destination_descriptor,
                source_handle.fileno(),
                item.expected_digest,
                expected_link_count=1,
            )
            _fsync_after_publication(destination_descriptor)
            _fsync_after_publication(destination_parent_descriptor)
            return _SameVolumeIdentityPublication(
                completed=True,
                destination_parent_created=destination_parent_created,
            )
        except StrictExecutorInterrupted:
            raise
        except (KeyboardInterrupt, SystemExit):
            if published or _leaf_exists(
                destination_parent_descriptor,
                destination.name,
            ):
                raise StrictExecutorInterrupted() from None
            raise
        finally:
            active_exception = sys.exc_info()
            try:
                source_context.__exit__(*active_exception)
            except BaseException as close_error:
                if active_exception[0] is not None:
                    raise active_exception[1].with_traceback(active_exception[2])
                if published:
                    if isinstance(close_error, (KeyboardInterrupt, SystemExit)):
                        raise StrictExecutorInterrupted() from None
                    raise _recovery_failure(
                        "filesystem_changed_recovery_required"
                    ) from None
                raise
    finally:
        if destination_descriptor is not None:
            _close_descriptor_quietly(destination_descriptor)
        if destination_parent_descriptor is not None:
            _close_descriptor_quietly(destination_parent_descriptor)
        if source_parent_descriptor is not None:
            _close_descriptor_quietly(source_parent_descriptor)


def _require_strict_platform_support() -> None:
    if (
        not all(function in os.supports_dir_fd for function in _DIR_FD_FUNCTIONS)
        or _LINK_FUNCTION not in os.supports_follow_symlinks
        or _STAT_FUNCTION not in os.supports_follow_symlinks
        or not getattr(os, "O_DIRECTORY", 0)
        or not getattr(os, "O_NOFOLLOW", 0)
    ):
        raise StrictExecutorError("unsupported_item")


def _open_root_directory(root: Path) -> int:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(root, flags)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError
        return descriptor
    except BaseException as error:
        if "descriptor" in locals():
            _close_descriptor_quietly(descriptor)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise StrictExecutorError("invalid_strict_plan") from None


def _parent_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise StrictExecutorError("invalid_strict_plan") from None
    parts = relative.parts[:-1]
    if not relative.name or any(part in {"", ".", ".."} for part in parts):
        raise StrictExecutorError("invalid_strict_plan")
    return parts


def _root_relative_lexical_path(
    path: Path,
    requested_root: Path,
    resolved_root: Path,
) -> Path:
    absolute = Path(os.path.abspath(path))
    for candidate_root in (requested_root, resolved_root):
        try:
            relative = absolute.relative_to(candidate_root)
        except ValueError:
            continue
        if any(part in {"", ".", ".."} for part in relative.parts):
            break
        return resolved_root.joinpath(*relative.parts)
    raise StrictExecutorError("invalid_strict_plan")


def _open_directory_chain(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    create: bool,
    failure_code: str,
) -> tuple[int, bool]:
    current = os.dup(root_descriptor)
    created = False
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        for part in parts:
            try:
                following = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise StrictExecutorError(failure_code) from None
                try:
                    os.mkdir(part, 0o777, dir_fd=current)
                    created = True
                    following = os.open(part, flags, dir_fd=current)
                    try:
                        _fsync_before_publication(following)
                        _fsync_before_publication(current)
                    except BaseException:
                        _close_descriptor_quietly(following)
                        raise
                except OSError:
                    raise StrictExecutorError(
                        failure_code,
                        filesystem_changed=created,
                    ) from None
            except OSError:
                raise StrictExecutorError(
                    failure_code,
                    filesystem_changed=created,
                ) from None
            try:
                following_stat = os.fstat(following)
            except BaseException:
                _close_descriptor_quietly(following)
                raise
            if not stat.S_ISDIR(following_stat.st_mode):
                _close_descriptor_quietly(following)
                raise StrictExecutorError(
                    failure_code,
                    filesystem_changed=created,
                )
            _close_descriptor_quietly(current)
            current = following
        return current, created
    except BaseException:
        _close_descriptor_quietly(current)
        raise


def _require_source_binding(
    parent_descriptor: int,
    leaf: str,
    source_descriptor: int,
    *,
    expected_fingerprint: RegularFileStatFingerprint | None,
    expected_link_count: int,
    failure_code: str,
    recovery_required: bool = False,
) -> None:
    try:
        path_stat = os.stat(
            leaf,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        descriptor_stat = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or not stat.S_ISREG(descriptor_stat.st_mode)
            or _inode_identity(path_stat) != _inode_identity(descriptor_stat)
            or _stable_stat_evidence(path_stat) != _stable_stat_evidence(
                descriptor_stat
            )
            or descriptor_stat.st_nlink != expected_link_count
            or path_stat.st_nlink != expected_link_count
            or (
                expected_fingerprint is not None
                and fingerprint_from_stat(descriptor_stat) != expected_fingerprint
            )
        ):
            raise OSError
    except (OSError, StableFileIdentityError, ValueError):
        if recovery_required:
            raise _recovery_failure(failure_code) from None
        raise StrictExecutorError(failure_code) from None


def _require_directory_binding(
    path: Path,
    descriptor: int,
    root: Path,
    *,
    failure_code: str,
    filesystem_changed: bool = False,
    recovery_required: bool = False,
) -> None:
    try:
        path_stat = os.lstat(path)
        descriptor_stat = os.fstat(descriptor)
        resolved = path.resolve(strict=True)
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or not stat.S_ISDIR(descriptor_stat.st_mode)
            or _inode_identity(path_stat) != _inode_identity(descriptor_stat)
            or (resolved != root and root not in resolved.parents)
        ):
            raise OSError
    except OSError:
        if recovery_required:
            raise _recovery_failure(failure_code) from None
        raise StrictExecutorError(
            failure_code,
            filesystem_changed=filesystem_changed,
        ) from None


def _require_destination_absent(
    parent_descriptor: int,
    leaf: str,
    *,
    filesystem_changed: bool,
) -> None:
    try:
        os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise StrictExecutorError(
            "destination_publication_failed",
            filesystem_changed=filesystem_changed,
        ) from None
    raise StrictExecutorError(
        "destination_collision",
        filesystem_changed=filesystem_changed,
    )


def _publish_hard_link(
    source_parent_descriptor: int,
    source_leaf: str,
    destination_parent_descriptor: int,
    destination_leaf: str,
    *,
    filesystem_changed: bool,
) -> None:
    try:
        os.link(
            source_leaf,
            destination_leaf,
            src_dir_fd=source_parent_descriptor,
            dst_dir_fd=destination_parent_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError:
        raise StrictExecutorError(
            "destination_collision",
            filesystem_changed=filesystem_changed,
        ) from None
    except OSError as error:
        if _leaf_exists(destination_parent_descriptor, destination_leaf):
            raise _recovery_failure(
                "filesystem_changed_recovery_required"
            ) from None
        code = (
            "cross_volume_unsupported"
            if error.errno == errno.EXDEV
            else "destination_publication_failed"
        )
        raise StrictExecutorError(
            code,
            filesystem_changed=filesystem_changed,
        ) from None


def _open_destination_descriptor(parent_descriptor: int, leaf: str) -> int:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        return os.open(leaf, flags, dir_fd=parent_descriptor)
    except OSError:
        raise _recovery_failure("destination_verification_failed") from None


def _verify_destination_binding(
    parent_descriptor: int,
    leaf: str,
    destination_descriptor: int,
    source_descriptor: int,
    expected_digest: str,
    *,
    expected_link_count: int,
) -> None:
    try:
        path_before = os.stat(
            leaf,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        source_stat = os.fstat(source_descriptor)
        destination_stat = os.fstat(destination_descriptor)
        if (
            not stat.S_ISREG(path_before.st_mode)
            or not stat.S_ISREG(destination_stat.st_mode)
            or _inode_identity(path_before) != _inode_identity(source_stat)
            or _inode_identity(destination_stat) != _inode_identity(source_stat)
            or source_stat.st_nlink != expected_link_count
            or destination_stat.st_nlink != expected_link_count
            or path_before.st_nlink != expected_link_count
        ):
            raise OSError
        verified = verify_open_regular_file_descriptor(
            destination_descriptor,
            expected_digest,
        )
        path_after = os.stat(
            leaf,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        destination_after = os.fstat(destination_descriptor)
        if (
            _inode_identity(path_after) != _inode_identity(source_stat)
            or _inode_identity(destination_after) != _inode_identity(source_stat)
            or _stable_stat_evidence(path_after)
            != _stable_stat_evidence(destination_after)
            or destination_after.st_nlink != expected_link_count
            or verified.device != source_stat.st_dev
            or verified.inode != source_stat.st_ino
        ):
            raise OSError
    except StrictExecutorError:
        raise
    except (OSError, StableFileIdentityError, ValueError):
        raise _recovery_failure("destination_verification_failed") from None


def _require_source_absent(parent_descriptor: int, leaf: str) -> None:
    try:
        os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise _recovery_failure("source_removal_failed") from None
    raise _recovery_failure("source_removal_failed")


def _require_descriptor_link_count(descriptor: int, expected: int) -> None:
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != expected:
            raise OSError
    except OSError:
        raise _recovery_failure("filesystem_changed_recovery_required") from None


def _fsync_after_publication(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        raise _recovery_failure("filesystem_changed_recovery_required") from None


def _fsync_before_publication(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        raise StrictExecutorError(
            "destination_publication_failed",
            filesystem_changed=True,
        ) from None


def _leaf_exists(parent_descriptor: int | None, leaf: str) -> bool:
    if parent_descriptor is None:
        return False
    try:
        os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
        return True
    except OSError:
        return False


def _source_identity_failure(error: StableFileIdentityError) -> StrictExecutorError:
    code = (
        error.code
        if error.code
        in {"source_identity_changed", "source_unstable", "unsupported_item"}
        else "source_identity_changed"
    )
    return StrictExecutorError(code)


def _recovery_failure(code: str) -> StrictExecutorError:
    return StrictExecutorError(
        code,
        filesystem_changed=True,
        recovery_required=True,
    )


def _inode_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _stable_stat_evidence(
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


def _close_descriptor_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def apply_move_plan(
    plan_items: list[MovePlanItem],
    root: Path,
    log_dir_name: str = "operation_logs",
    *,
    recorder: MovementRecorder | None = None,
) -> OperationLog:
    resolved_root = root.resolve()
    validated_items = [
        _validate_plan_item(item, resolved_root) for item in plan_items
    ]
    _reject_colliding_batch_paths(validated_items)

    operations: list[MoveResult] = []
    log_path = _next_log_path(resolved_root, log_dir_name, "operation_log")
    if recorder is not None:
        try:
            recorder.before_batch(tuple(validated_items))
        except Exception as error:
            raise MovementRecordingError("before_batch") from error

    for index, item in enumerate(validated_items):
        if recorder is not None:
            try:
                recorder.before_move(index, item)
            except Exception as error:
                raise MovementRecordingError("before_move") from error
        try:
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            _move_without_replacing(item.source, item.destination)
        except Exception as error:
            result = MoveResult(
                source=item.source,
                destination=item.destination,
                success=False,
                message=f"move failed: {error}",
            )
            operations.append(result)
            _write_operation_log(log_path, operations)
            if recorder is not None:
                try:
                    recorder.after_failure(index, item, result)
                except Exception as recording_error:
                    operation_log = _write_operation_log(log_path, operations)
                    raise MovementRecordingError(
                        "after_failure",
                        operation_log=operation_log,
                    ) from recording_error
            break

        result = MoveResult(
            source=item.source,
            destination=item.destination,
            success=True,
            message="moved",
        )
        operations.append(result)
        # Written after every move so an interrupted batch still leaves a
        # log that undo can read.
        _write_operation_log(log_path, operations)
        if recorder is not None:
            try:
                recorder.after_success(index, item, result)
            except Exception as error:
                operation_log = _write_operation_log(log_path, operations)
                raise MovementRecordingError(
                    "after_success",
                    operation_log=operation_log,
                    filesystem_changed=True,
                ) from error

    operation_log = _write_operation_log(log_path, operations)
    if recorder is not None:
        try:
            recorder.after_batch(operation_log)
        except Exception as error:
            raise MovementRecordingError(
                "after_batch",
                operation_log=operation_log,
                filesystem_changed=any(item.success for item in operations),
            ) from error
    return operation_log


def apply_same_volume_identity_move_plan(
    plan_items: tuple[IdentityMovePlanItem, ...],
    root: Path,
    log_dir_name: str = "operation_logs",
    *,
    recorder: MovementRecorder | None = None,
) -> OperationLog:
    """Apply a fully validated same-volume identity-bearing move batch."""

    validated = _prepare_same_volume_identity_batch(plan_items, root)
    requested_root = Path(os.path.abspath(root))
    resolved_root = Path(root).resolve(strict=True)
    root_descriptor = _open_root_directory(resolved_root)
    underlying_items = tuple(
        classified.item.plan_item for classified in validated.items
    )
    operations: list[MoveResult] = []
    filesystem_changed = False
    try:
        log_path = _next_log_path(
            resolved_root,
            log_dir_name,
            "operation_log",
        )
        if recorder is not None:
            try:
                recorder.before_batch(underlying_items)
            except Exception as error:
                raise MovementRecordingError("before_batch") from error

        for index, classified in enumerate(validated.items):
            identity_item = classified.item
            item = identity_item.plan_item
            if recorder is not None:
                try:
                    recorder.before_move(index, item)
                except Exception as error:
                    raise MovementRecordingError("before_move") from error
            try:
                publication = _execute_same_volume_identity_item(
                    identity_item,
                    resolved_root,
                    requested_root,
                    root_descriptor,
                )
            except StrictExecutorError as error:
                if error.recovery_required:
                    raise
                filesystem_changed = filesystem_changed or error.filesystem_changed
                result = MoveResult(
                    source=item.source,
                    destination=item.destination,
                    success=False,
                    message=error.safe_message,
                )
                operations.append(result)
                if recorder is not None:
                    try:
                        recorder.after_failure(index, item, result)
                    except Exception as recording_error:
                        operation_log = _write_operation_log(log_path, operations)
                        raise MovementRecordingError(
                            "after_failure",
                            operation_log=operation_log,
                            filesystem_changed=filesystem_changed,
                        ) from recording_error
                break

            if not publication.completed:
                raise _recovery_failure("filesystem_changed_recovery_required")
            filesystem_changed = True
            result = MoveResult(
                source=item.source,
                destination=item.destination,
                success=True,
                message="moved",
            )
            operations.append(result)
            if recorder is not None:
                try:
                    recorder.after_success(index, item, result)
                except Exception as error:
                    operation_log = _write_operation_log(log_path, operations)
                    raise MovementRecordingError(
                        "after_success",
                        operation_log=operation_log,
                        filesystem_changed=True,
                    ) from error

        operation_log = _write_operation_log(log_path, operations)
        if recorder is not None:
            try:
                recorder.after_batch(operation_log)
            except Exception as error:
                raise MovementRecordingError(
                    "after_batch",
                    operation_log=operation_log,
                    filesystem_changed=filesystem_changed,
                ) from error
        return operation_log
    finally:
        _close_descriptor_quietly(root_descriptor)


def undo_operation_log(
    log_path: Path,
    root: Path,
    *,
    recorder: UndoRecorder | None = None,
) -> OperationLog:
    resolved_root = root.resolve()
    if log_path.is_symlink():
        raise ValueError(f"operation log is a symlink: {log_path}")
    resolved_log_path = _validate_existing_path_under_root(log_path, resolved_root)
    if not resolved_log_path.is_file():
        raise ValueError(f"operation log is not a regular file: {log_path}")

    with resolved_log_path.open("r", encoding="utf-8") as file:
        log_data = json.load(file)

    logged_results = _load_move_results(log_data)
    successful_results = [result for result in logged_results if result.success]

    for result in successful_results:
        _validate_path_under_root(result.source, resolved_root)
        _validate_path_under_root(result.destination, resolved_root)

    undo_results: list[MoveResult] = []
    result_log_path = _next_log_path(resolved_root, "operation_logs", "undo_result_log")
    if recorder is not None:
        try:
            recorder.before_batch(tuple(successful_results))
        except Exception as error:
            raise UndoRecordingError("before_batch") from error

    for index, result in enumerate(successful_results):
        if recorder is not None:
            try:
                recorder.before_restore(index, result)
            except Exception as error:
                operation_log = _write_operation_log(result_log_path, undo_results)
                raise UndoRecordingError(
                    "before_restore",
                    operation_log=operation_log,
                    filesystem_changed=any(item.success for item in undo_results),
                ) from error
        if os.path.lexists(result.source):
            undo_result = MoveResult(
                source=result.destination,
                destination=result.source,
                success=False,
                message="original source path already exists",
            )
            undo_results.append(undo_result)
            _record_undo_failure(
                recorder, index, result, undo_result, result_log_path, undo_results
            )
            continue
        if result.destination.is_symlink() or not result.destination.is_file():
            undo_result = MoveResult(
                source=result.destination,
                destination=result.source,
                success=False,
                message="logged destination is not a regular file",
            )
            undo_results.append(undo_result)
            _record_undo_failure(
                recorder, index, result, undo_result, result_log_path, undo_results
            )
            continue

        _validate_destination_path(result.source, resolved_root)
        try:
            result.source.parent.mkdir(parents=True, exist_ok=True)
            _move_without_replacing(result.destination, result.source)
        except Exception as error:
            undo_result = MoveResult(
                source=result.destination,
                destination=result.source,
                success=False,
                message=f"undo failed: {error}",
            )
            undo_results.append(undo_result)
            _record_undo_failure(
                recorder, index, result, undo_result, result_log_path, undo_results
            )
            continue

        undo_result = MoveResult(
            source=result.destination,
            destination=result.source,
            success=True,
            message="restored",
        )
        undo_results.append(undo_result)
        if recorder is not None:
            try:
                recorder.after_success(index, result, undo_result)
            except Exception as error:
                operation_log = _write_operation_log(result_log_path, undo_results)
                raise UndoRecordingError(
                    "after_success",
                    operation_log=operation_log,
                    filesystem_changed=True,
                ) from error

    operation_log = _write_operation_log(result_log_path, undo_results)
    if recorder is not None:
        try:
            recorder.after_batch(operation_log)
        except Exception as error:
            raise UndoRecordingError(
                "after_batch",
                operation_log=operation_log,
                filesystem_changed=any(item.success for item in undo_results),
            ) from error
    return operation_log


def _record_undo_failure(
    recorder: UndoRecorder | None,
    index: int,
    item: MoveResult,
    result: MoveResult,
    log_path: Path,
    results: list[MoveResult],
) -> None:
    if recorder is None:
        return
    try:
        recorder.after_failure(index, item, result)
    except Exception as error:
        operation_log = _write_operation_log(log_path, results)
        raise UndoRecordingError(
            "after_failure",
            operation_log=operation_log,
            filesystem_changed=any(current.success for current in results),
        ) from error


def _collision_key(path: Path) -> str:
    # macOS and Windows volumes are case-insensitive and may store names in a
    # different Unicode form than the one requested, so two spellings can be
    # one file there.
    return unicodedata.normalize("NFC", str(path)).casefold()


def _reject_colliding_batch_paths(items: list[MovePlanItem]) -> None:
    destinations: dict[str, Path] = {}
    for item in items:
        key = _collision_key(item.destination)
        if key in destinations:
            raise ValueError(
                f"destination collides with another move: {item.destination}"
            )
        destinations[key] = item.destination
    for item in items:
        key = _collision_key(item.source)
        if key in destinations and key != _collision_key(item.destination):
            raise ValueError(
                f"destination collides with another move's source: "
                f"{destinations[key]}"
            )


def _move_without_replacing(source: Path, destination: Path) -> None:
    """Move a file, refusing to replace anything already at destination.

    shutil.move renames over an existing file on POSIX, so the destination
    is first claimed with an exclusive create, which fails if anything (a
    case-variant spelling included) is already there. The move then replaces
    only our own empty placeholder.
    """
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        raise FileExistsError(f"destination already exists: {destination}") from None
    os.close(descriptor)
    try:
        shutil.move(str(source), str(destination))
    except BaseException:
        if os.path.lexists(source):
            # Nothing was moved, so the placeholder is still ours to remove.
            try:
                os.unlink(destination)
            except OSError:
                pass
        raise


def _validate_plan_item(item: MovePlanItem, root: Path) -> MovePlanItem:
    _validate_source_path(item.source, root)
    _validate_destination_path(item.destination, root)
    if os.path.lexists(item.destination):
        raise ValueError(f"destination already exists: {item.destination}")
    return item


def _validate_source_path(path: Path, root: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"source is a symlink: {path}")
    resolved_path = _validate_existing_path_under_root(path, root)
    if not path.exists():
        raise ValueError(f"source does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"source is not a regular file: {path}")
    return resolved_path


def _validate_existing_path_under_root(path: Path, root: Path) -> Path:
    resolved_path = path.resolve()
    if not _is_under_root(resolved_path, root):
        raise ValueError(f"path is outside root: {path}")
    return resolved_path


def _validate_path_under_root(path: Path, root: Path) -> Path:
    resolved_path = path.resolve(strict=False)
    if not _is_under_root(resolved_path, root):
        raise ValueError(f"path is outside root: {path}")
    return resolved_path


def _validate_destination_path(path: Path, root: Path) -> Path:
    resolved_path = _validate_path_under_root(path, root)
    existing_parent = _nearest_existing_parent(path.parent)
    resolved_parent = existing_parent.resolve()
    if not _is_under_root(resolved_parent, root):
        raise ValueError(f"destination parent is outside root: {path}")
    if not existing_parent.is_dir():
        raise ValueError(f"destination parent is not a directory: {existing_parent}")
    return resolved_path


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _is_under_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _next_log_path(root: Path, log_dir_name: str, prefix: str) -> Path:
    log_dir = root / "AI_Review" / log_dir_name
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = log_dir / f"{prefix}_{timestamp}.json"
    counter = 1
    while candidate.exists():
        candidate = log_dir / f"{prefix}_{timestamp}_{counter}.json"
        counter += 1
    return candidate


def _write_operation_log(log_path: Path, operations: list[MoveResult]) -> OperationLog:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_data = {
        "operations": [
            {
                "source": str(result.source),
                "destination": str(result.destination),
                "success": result.success,
                "message": result.message,
            }
            for result in operations
        ]
    }
    with log_path.open("w", encoding="utf-8") as file:
        json.dump(log_data, file, indent=2, sort_keys=True)
        file.write("\n")
    return OperationLog(log_path=log_path, operations=operations)


def _load_move_results(log_data: object) -> list[MoveResult]:
    if not isinstance(log_data, dict):
        raise ValueError("operation log must contain an object")
    operations = log_data.get("operations")
    if not isinstance(operations, list):
        raise ValueError("operation log must contain an operations list")

    results: list[MoveResult] = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("operation entry must contain an object")
        if set(operation) != {"source", "destination", "success", "message"}:
            raise ValueError("operation entry fields are invalid")
        source = operation["source"]
        destination = operation["destination"]
        success = operation["success"]
        message = operation["message"]
        if not isinstance(source, str) or not isinstance(destination, str):
            raise ValueError("operation paths must be strings")
        if not isinstance(success, bool):
            raise ValueError("operation success must be boolean")
        if not isinstance(message, str):
            raise ValueError("operation message must be a string")
        results.append(
            MoveResult(
                source=Path(source),
                destination=Path(destination),
                success=success,
                message=message,
            )
        )
    return results
