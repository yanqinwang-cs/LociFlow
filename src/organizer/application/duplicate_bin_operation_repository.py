from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

from organizer.application.duplicate_bin_movement_models import (
    DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION,
    MOVE_ATTEMPTING,
    MOVE_PENDING,
    MOVE_STATES,
    MOVE_UNATTEMPTED,
    OPERATION_RECORD_TYPE,
    OP_STATUSES,
    DuplicateBinExpectedMove,
    DuplicateBinOperationRecord,
    DuplicateBinVerification,
)
from organizer.safety import validate_under_root
from organizer.scope import DUPLICATE_BIN_RELATIVE_ROOT


class DuplicateBinOperationRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("duplicate bin root is not a directory")
        self.directory = self.root / DUPLICATE_BIN_RELATIVE_ROOT / "operations"

    @property
    def configured_root_identity(self) -> str:
        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()

    def create(self, record: DuplicateBinOperationRecord) -> None:
        path = self._path(record.operation_id)
        if os.path.lexists(path):
            raise ValueError("duplicate bin operation already exists")
        if self.find_for_authorization(record.authorization_id) is not None:
            raise ValueError("duplicate bin authorization already has an operation")
        self._write(path, self._validated(record))

    def save(self, record: DuplicateBinOperationRecord) -> None:
        path = self._path(record.operation_id)
        if path.is_symlink() or not path.exists():
            raise ValueError("duplicate bin operation path is unsafe")
        existing = self.load(record.operation_id)
        if not _same_operation_binding(existing, record):
            raise ValueError("duplicate bin operation binding cannot change")
        self._write(path, self._validated(record))

    def load(self, operation_id: str) -> DuplicateBinOperationRecord:
        path = self._path(operation_id)
        if path.is_symlink():
            raise ValueError("duplicate bin operation path is unsafe")
        try:
            resolved = path.resolve(strict=True)
            validate_under_root(resolved, self.root)
            data = json.loads(resolved.read_text(encoding="utf-8"))
            record = _record_from_dict(data)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("duplicate bin operation could not be read") from error
        if record.operation_id != operation_id:
            raise ValueError("duplicate bin operation ID mismatch")
        if record.configured_root_identity != self.configured_root_identity:
            raise ValueError("duplicate bin operation belongs to another root")
        return record

    def list_records(self) -> tuple[DuplicateBinOperationRecord, ...]:
        if not self.directory.exists():
            return ()
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError("duplicate bin operation directory is unsafe")
        paths = tuple(sorted(self.directory.glob("*.json")))
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("duplicate bin operation record is unsafe")
        records = tuple(self.load(path.stem) for path in paths)
        if len({record.authorization_id for record in records}) != len(records):
            raise ValueError("duplicate bin authorization has multiple operations")
        if len({record.batch_id for record in records}) != len(records):
            raise ValueError("duplicate bin batch has multiple operations")
        return records

    def find_for_authorization(
        self, authorization_id: str
    ) -> DuplicateBinOperationRecord | None:
        _uuid(authorization_id, "authorization_id")
        return next(
            (
                record
                for record in self.list_records()
                if record.authorization_id == authorization_id
            ),
            None,
        )

    def find_for_batch(self, batch_id: str) -> DuplicateBinOperationRecord | None:
        _uuid(batch_id, "batch_id")
        return next(
            (
                record
                for record in reversed(self.list_records())
                if record.batch_id == batch_id
            ),
            None,
        )

    def _path(self, operation_id: str) -> Path:
        return self.directory / f"{_uuid(operation_id, 'operation_id')}.json"

    def _validated(self, record: DuplicateBinOperationRecord) -> dict[str, Any]:
        if record.configured_root_identity != self.configured_root_identity:
            raise ValueError("duplicate bin operation belongs to another root")
        data = _record_to_dict(record)
        if _record_from_dict(data) != record:
            raise ValueError("duplicate bin operation is not canonical")
        return data

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        _safe_directory(self.directory, self.root)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                dir=self.directory, prefix=f".{path.name}.", suffix=".tmp"
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            temporary = None
            descriptor = os.open(
                self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _record_to_dict(record: DuplicateBinOperationRecord) -> dict[str, Any]:
    return {
        **asdict(record),
        "selected_bin_item_ids": list(record.selected_bin_item_ids),
        "expected_moves": [asdict(item) for item in record.expected_moves],
        "verification": {
            **asdict(record.verification),
            "mismatch_codes": list(record.verification.mismatch_codes),
        },
    }


def _record_from_dict(data: object) -> DuplicateBinOperationRecord:
    required = {
        "schema_version",
        "record_type",
        "operation_id",
        "authorization_id",
        "batch_id",
        "configured_root_identity",
        "approved_batch_digest",
        "batch_version",
        "preflight_digest",
        "selected_bin_item_ids",
        "expected_moves",
        "status",
        "created_at",
        "started_at",
        "completed_at",
        "verification",
        "executor_log_path",
        "failure_code",
        "recovery_state",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("duplicate bin operation fields are invalid")
    if (
        data["schema_version"] != DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION
        or data["record_type"] != OPERATION_RECORD_TYPE
    ):
        raise ValueError("duplicate bin operation schema is invalid")
    status = _string(data["status"], "status")
    if status not in OP_STATUSES:
        raise ValueError("duplicate bin operation status is invalid")
    selected = data["selected_bin_item_ids"]
    moves = data["expected_moves"]
    verification = data["verification"]
    if (
        not isinstance(selected, list)
        or not isinstance(moves, list)
        or not isinstance(verification, dict)
    ):
        raise ValueError("duplicate bin operation collections are invalid")
    selected_ids = tuple(_uuid(item, "selected item") for item in selected)
    expected_moves = tuple(_move_from_dict(item) for item in moves)
    if (
        not selected_ids
        or tuple(sorted(selected_ids)) != selected_ids
        or selected_ids != tuple(item.bin_item_id for item in expected_moves)
        or len({item.member_id for item in expected_moves}) != len(expected_moves)
        or len({item.source for item in expected_moves}) != len(expected_moves)
        or len({item.destination for item in expected_moves}) != len(expected_moves)
    ):
        raise ValueError("duplicate bin operation selected items are inconsistent")
    if set(verification) != {"status", "verified_count", "mismatch_codes"}:
        raise ValueError("duplicate bin verification fields are invalid")
    mismatches = verification["mismatch_codes"]
    if not isinstance(mismatches, list) or not all(
        isinstance(item, str) for item in mismatches
    ):
        raise ValueError("duplicate bin verification mismatches are invalid")
    batch_version = data["batch_version"]
    verified_count = verification["verified_count"]
    if (
        isinstance(batch_version, bool)
        or not isinstance(batch_version, int)
        or batch_version < 1
    ):
        raise ValueError("duplicate bin batch version is invalid")
    if (
        isinstance(verified_count, bool)
        or not isinstance(verified_count, int)
        or verified_count < 0
        or verified_count > len(expected_moves)
    ):
        raise ValueError("duplicate bin verification count is invalid")
    verification_status = _string(verification["status"], "verification status")
    if verification_status not in {"not_run", "passed", "failed"}:
        raise ValueError("duplicate bin verification status is invalid")
    verified_moves = sum(item.result_state == "verified" for item in expected_moves)
    if verified_count != verified_moves:
        raise ValueError("duplicate bin verified count is inconsistent")
    if verification_status == "passed" and (
        status != "moved" or verified_count != len(expected_moves)
    ):
        raise ValueError("duplicate bin passed verification is inconsistent")
    return DuplicateBinOperationRecord(
        operation_id=_uuid(data["operation_id"], "operation_id"),
        authorization_id=_uuid(data["authorization_id"], "authorization_id"),
        batch_id=_uuid(data["batch_id"], "batch_id"),
        configured_root_identity=_digest(
            data["configured_root_identity"], "root identity"
        ),
        approved_batch_digest=_digest(data["approved_batch_digest"], "batch digest"),
        batch_version=batch_version,
        preflight_digest=_digest(data["preflight_digest"], "preflight digest"),
        selected_bin_item_ids=selected_ids,
        expected_moves=expected_moves,
        status=status,
        created_at=_timestamp(data["created_at"], "created_at"),
        started_at=_optional_timestamp(data["started_at"], "started_at"),
        completed_at=_optional_timestamp(data["completed_at"], "completed_at"),
        verification=DuplicateBinVerification(
            status=verification_status,
            verified_count=verified_count,
            mismatch_codes=tuple(mismatches),
        ),
        executor_log_path=_optional_relative(data["executor_log_path"], "executor log"),
        failure_code=_optional_string(data["failure_code"], "failure code"),
        recovery_state=_optional_string(data["recovery_state"], "recovery state"),
    )


def _move_from_dict(data: object) -> DuplicateBinExpectedMove:
    required = {
        "bin_item_id",
        "member_id",
        "source",
        "destination",
        "expected_sha256",
        "source_size_bytes",
        "source_modified_time_ns",
        "result_state",
        "attempted_at",
        "completed_at",
        "failure_code",
        "recovery_state",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("duplicate bin expected move fields are invalid")
    state = _string(data["result_state"], "move state")
    if state not in MOVE_STATES:
        raise ValueError("duplicate bin move state is invalid")
    size = data["source_size_bytes"]
    mtime = data["source_modified_time_ns"]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (size, mtime)
    ):
        raise ValueError("duplicate bin move metadata is invalid")
    source = _relative(data["source"], "source")
    destination = _relative(data["destination"], "destination")
    item_id = _uuid(data["bin_item_id"], "bin_item_id")
    expected_destination = (
        f"AI_Review/duplicate_bin/items/{item_id}/{Path(source).name}"
    )
    if destination != expected_destination:
        raise ValueError("duplicate bin operation destination is invalid")
    attempted_at = _optional_timestamp(data["attempted_at"], "attempted_at")
    completed_at = _optional_timestamp(data["completed_at"], "completed_at")
    if state == MOVE_PENDING and (attempted_at is not None or completed_at is not None):
        raise ValueError("pending move has timestamps")
    if state == MOVE_ATTEMPTING and (attempted_at is None or completed_at is not None):
        raise ValueError("attempting move timestamps are invalid")
    if state == MOVE_UNATTEMPTED and (attempted_at is not None or completed_at is None):
        raise ValueError("unattempted move timestamps are invalid")
    if state not in {MOVE_PENDING, MOVE_ATTEMPTING, MOVE_UNATTEMPTED} and (
        attempted_at is None or completed_at is None
    ):
        raise ValueError("attempted move timestamps are invalid")
    return DuplicateBinExpectedMove(
        bin_item_id=item_id,
        member_id=_uuid(data["member_id"], "member_id"),
        source=source,
        destination=destination,
        expected_sha256=_digest(data["expected_sha256"], "expected hash"),
        source_size_bytes=size,
        source_modified_time_ns=mtime,
        result_state=state,
        attempted_at=attempted_at,
        completed_at=completed_at,
        failure_code=_optional_string(data["failure_code"], "failure code"),
        recovery_state=_optional_string(data["recovery_state"], "recovery state"),
    )


def _same_operation_binding(
    existing: DuplicateBinOperationRecord,
    candidate: DuplicateBinOperationRecord,
) -> bool:
    operation_fields = (
        "operation_id",
        "authorization_id",
        "batch_id",
        "configured_root_identity",
        "approved_batch_digest",
        "batch_version",
        "preflight_digest",
        "selected_bin_item_ids",
        "created_at",
    )
    if any(
        getattr(existing, field) != getattr(candidate, field)
        for field in operation_fields
    ):
        return False
    move_fields = (
        "bin_item_id",
        "member_id",
        "source",
        "destination",
        "expected_sha256",
        "source_size_bytes",
        "source_modified_time_ns",
    )
    return len(existing.expected_moves) == len(candidate.expected_moves) and all(
        all(getattr(left, field) == getattr(right, field) for field in move_fields)
        for left, right in zip(existing.expected_moves, candidate.expected_moves)
    )


def _safe_directory(directory: Path, root: Path) -> None:
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise ValueError("duplicate bin operation directory is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    validate_under_root(directory.resolve(strict=True), root)


def _relative(value: object, label: str) -> str:
    text = _string(value, label)
    path = Path(text)
    if (
        "\\" in text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} is not a safe relative path")
    return path.as_posix()


def _optional_relative(value: object, label: str) -> str | None:
    return None if value is None else _relative(value, label)


def _timestamp(value: object, label: str) -> str:
    from datetime import datetime

    text = _string(value, label)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} lacks timezone")
    return text


def _optional_timestamp(value: object, label: str) -> str | None:
    return None if value is None else _timestamp(value, label)


def _uuid(value: object, label: str) -> str:
    text = _string(value, label)
    parsed = uuid.UUID(text)
    if str(parsed) != text:
        raise ValueError(f"{label} is invalid")
    return text


def _digest(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{label} is invalid")
    return text


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)
