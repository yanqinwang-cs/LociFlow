from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

from organizer.application.organization_undo_models import (
    ORGANIZATION_UNDO_RECORD_TYPE,
    ORGANIZATION_UNDO_SCHEMA_VERSION,
    UNDO_FAILED,
    UNDO_INTERRUPTED,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_FAILED,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_ITEM_UNATTEMPTED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
    UNDO_VERIFICATION_FAILED,
    UNDO_VERIFICATION_FAILED_STATE,
    UNDO_VERIFICATION_NOT_RUN,
    UNDO_VERIFICATION_PASSED,
    OrganizationUndoItem,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.safety import validate_under_root


DEFAULT_UNDO_DIRECTORY = Path("AI_Review") / "organization_undos"

_UNDO_STATUSES = {
    UNDO_PREPARED,
    UNDO_UNDOING,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_RESTORED,
    UNDO_PARTIAL,
    UNDO_FAILED,
    UNDO_VERIFICATION_FAILED,
    UNDO_INTERRUPTED,
}
_ITEM_STATES = {
    UNDO_ITEM_PENDING,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_RESTORED,
    UNDO_ITEM_FAILED,
    UNDO_ITEM_UNATTEMPTED,
}
_VERIFICATION_STATES = {
    UNDO_VERIFICATION_NOT_RUN,
    UNDO_VERIFICATION_PASSED,
    UNDO_VERIFICATION_FAILED_STATE,
}


class OrganizationUndoRepository:
    """Persist root-bound organization undo state with atomic replacement."""

    def __init__(self, root: Path) -> None:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"organization root is not a directory: {root}")
        self.root = resolved_root
        self.directory = resolved_root / DEFAULT_UNDO_DIRECTORY

    @property
    def configured_root_identity(self) -> str:
        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()

    def create(self, record: OrganizationUndoRecord) -> None:
        path = self._record_path(record.undo_id)
        if os.path.lexists(path):
            raise ValueError("organization undo already exists")
        self._atomic_write(path, self._validated_data(record))

    def save(self, record: OrganizationUndoRecord) -> None:
        path = self._record_path(record.undo_id)
        if path.is_symlink():
            raise ValueError("organization undo path is a symlink")
        self._atomic_write(path, self._validated_data(record))

    def load(self, undo_id: str) -> OrganizationUndoRecord:
        path = self._record_path(undo_id)
        if path.is_symlink():
            raise ValueError("organization undo path is a symlink")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as error:
            raise ValueError("organization undo not found") from error
        validate_under_root(resolved, self.root)
        if not resolved.is_file():
            raise ValueError("organization undo is not a regular file")
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("organization undo could not be read") from error
        record = _record_from_dict(data, self.root)
        if record.undo_id != undo_id:
            raise ValueError("organization undo ID does not match its artifact")
        if record.configured_root_identity != self.configured_root_identity:
            raise ValueError("organization undo belongs to another root")
        return record

    def list_records(self) -> tuple[OrganizationUndoRecord, ...]:
        if not os.path.lexists(self.directory):
            return ()
        self._validate_directory()
        paths = tuple(sorted(self.directory.glob("*.json")))
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("organization undo record is unsafe")
        return tuple(self.load(path.stem) for path in paths)

    def linked_records(self, operation_id: str) -> tuple[OrganizationUndoRecord, ...]:
        _uuid_string(operation_id, "operation_id")
        return tuple(
            record
            for record in self.list_records()
            if record.operation_id == operation_id
        )

    def _record_path(self, undo_id: str) -> Path:
        return self.directory / f"{_uuid_string(undo_id, 'undo_id')}.json"

    def _validate_directory(self) -> None:
        if self.directory.is_symlink():
            raise ValueError("organization undo directory is a symlink")
        resolved = self.directory.resolve()
        validate_under_root(resolved, self.root)
        if not resolved.is_dir():
            raise ValueError("organization undo directory is not a directory")

    def _validated_data(self, record: OrganizationUndoRecord) -> dict[str, Any]:
        if record.configured_root_identity != self.configured_root_identity:
            raise ValueError("organization undo belongs to another root")
        data = _record_to_dict(record)
        if _record_from_dict(data, self.root) != record:
            raise ValueError("organization undo is not canonical")
        return data

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        parent = _nearest_existing_parent(self.directory)
        validate_under_root(parent.resolve(strict=False), self.root)
        if not parent.is_dir():
            raise ValueError("organization undo parent is not a directory")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._validate_directory()
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.directory,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            _fsync_directory(self.directory)
        except Exception:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
            raise


def _record_to_dict(record: OrganizationUndoRecord) -> dict[str, Any]:
    data = asdict(record)
    data["expected_restores"] = [asdict(item) for item in record.expected_restores]
    data["verification"] = asdict(record.verification)
    data["verification"]["mismatch_codes"] = list(record.verification.mismatch_codes)
    return data


def _record_from_dict(data: object, root: Path) -> OrganizationUndoRecord:
    if not isinstance(data, dict):
        raise ValueError("organization undo must contain an object")
    required = {
        "schema_version", "record_type", "undo_id", "operation_id",
        "authorization_id", "configured_root_identity", "status", "created_at",
        "started_at", "completed_at", "expected_restores", "verification",
        "legacy_undo_log", "failure_code",
    }
    if set(data) != required:
        raise ValueError("organization undo fields do not match schema version 1")
    if data["schema_version"] != ORGANIZATION_UNDO_SCHEMA_VERSION:
        raise ValueError("unsupported organization undo schema version")
    if data["record_type"] != ORGANIZATION_UNDO_RECORD_TYPE:
        raise ValueError("unexpected organization undo record type")
    status = _string(data["status"], "status")
    if status not in _UNDO_STATUSES:
        raise ValueError("organization undo status is invalid")
    restores = data["expected_restores"]
    verification = data["verification"]
    if not isinstance(restores, list) or not isinstance(verification, dict):
        raise ValueError("organization undo collections are invalid")
    if set(verification) != {"status", "verified_count", "mismatch_codes"}:
        raise ValueError("organization undo verification fields are invalid")
    mismatch_codes = verification["mismatch_codes"]
    if not isinstance(mismatch_codes, list) or not all(isinstance(item, str) for item in mismatch_codes):
        raise ValueError("organization undo verification mismatches are invalid")
    verification_status = _string(verification["status"], "verification status")
    if verification_status not in _VERIFICATION_STATES:
        raise ValueError("organization undo verification status is invalid")
    return OrganizationUndoRecord(
        undo_id=_uuid_string(data["undo_id"], "undo_id"),
        operation_id=_uuid_string(data["operation_id"], "operation_id"),
        authorization_id=_uuid_string(data["authorization_id"], "authorization_id"),
        configured_root_identity=_string(data["configured_root_identity"], "configured_root_identity"),
        status=status,
        created_at=_string(data["created_at"], "created_at"),
        started_at=_optional_string(data["started_at"], "started_at"),
        completed_at=_optional_string(data["completed_at"], "completed_at"),
        expected_restores=tuple(_item_from_dict(item, root) for item in restores),
        verification=UndoVerificationResult(
            status=verification_status,
            verified_count=_integer(verification["verified_count"], "verified_count"),
            mismatch_codes=tuple(mismatch_codes),
        ),
        legacy_undo_log=_optional_relative_path(data["legacy_undo_log"], root, "legacy_undo_log"),
        failure_code=_optional_string(data["failure_code"], "failure_code"),
    )


def _item_from_dict(data: object, root: Path) -> OrganizationUndoItem:
    if not isinstance(data, dict):
        raise ValueError("organization undo item must contain an object")
    required = {
        "change_id", "restore_target", "moved_from", "baseline_size_bytes",
        "baseline_modified_time", "result_state", "failure_code", "recovery_state",
    }
    if set(data) != required:
        raise ValueError("organization undo item fields are invalid")
    state = _string(data["result_state"], "result_state")
    if state not in _ITEM_STATES:
        raise ValueError("organization undo item state is invalid")
    modified = data["baseline_modified_time"]
    if isinstance(modified, bool) or not isinstance(modified, (int, float)):
        raise ValueError("baseline_modified_time must be numeric")
    return OrganizationUndoItem(
        change_id=_string(data["change_id"], "change_id"),
        restore_target=_relative_path(data["restore_target"], root, "restore_target"),
        moved_from=_relative_path(data["moved_from"], root, "moved_from"),
        baseline_size_bytes=_integer(data["baseline_size_bytes"], "baseline_size_bytes"),
        baseline_modified_time=float(modified),
        result_state=state,
        failure_code=_optional_string(data["failure_code"], "failure_code"),
        recovery_state=_optional_string(data["recovery_state"], "recovery_state"),
    )


def _relative_path(value: object, root: Path, label: str) -> str:
    text = _string(value, label)
    if "\\" in text:
        raise ValueError(f"{label} must be a safe relative path")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a safe relative path")
    # Keep durable artifact parsing independent from later filesystem changes;
    # live restore and verification paths are checked by their owning services.
    del root
    return path.as_posix()


def _optional_relative_path(value: object, root: Path, label: str) -> str | None:
    return None if value is None else _relative_path(value, root, label)


def _uuid_string(value: object, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise ValueError(f"{label} must be an opaque UUID") from error
    if str(parsed) != text:
        raise ValueError(f"{label} must be an opaque UUID")
    return text


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
