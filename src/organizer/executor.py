from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Protocol

from organizer.models import MovePlanItem, MoveResult, OperationLog


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
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(item.source), str(item.destination))
        except Exception as error:
            result = MoveResult(
                source=item.source,
                destination=item.destination,
                success=False,
                message=f"move failed: {error}",
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
                    ) from recording_error
            break

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
                filesystem_changed=any(item.success for item in operations),
            ) from error
    return operation_log


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
        result.source.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(result.destination), str(result.source))
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
