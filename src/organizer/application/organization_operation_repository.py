from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.organization_operation_models import (
    BLOCKING_OPERATION_STATUSES,
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    ORGANIZATION_OPERATION_RECORD_TYPE,
    ORGANIZATION_OPERATION_SCHEMA_VERSION,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    REPLAYABLE_TERMINAL_STATUSES,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVING,
    STATUS_PREPARED,
    VERIFICATION_FAILED,
    VERIFICATION_NOT_RUN,
    VERIFICATION_PASSED,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.safety import validate_under_root

DEFAULT_OPERATION_DIRECTORY = Path("AI_Review") / "organization_operations"


class OrganizationOperationRepository:
    """Persist root-bound Loci Flow operation state with atomic JSON replacement."""

    def __init__(self, root: Path) -> None:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"organization root is not a directory: {root}")
        self.root = resolved_root
        self.directory = resolved_root / DEFAULT_OPERATION_DIRECTORY

    @property
    def configured_root_identity(self) -> str:
        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()

    def create(self, record: OrganizationOperationRecord) -> None:
        path = self._record_path(record.operation_id)
        if os.path.lexists(path):
            raise ValueError("organization operation already exists")
        self._atomic_write(path, self._validated_data(record))

    def save(self, record: OrganizationOperationRecord) -> None:
        path = self._record_path(record.operation_id)
        if path.is_symlink():
            raise ValueError("organization operation path is a symlink")
        self._atomic_write(path, self._validated_data(record))

    def load(self, operation_id: str) -> OrganizationOperationRecord:
        path = self._record_path(operation_id)
        if path.is_symlink():
            raise ValueError("organization operation path is a symlink")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as error:
            raise ValueError("organization operation not found") from error
        validate_under_root(resolved, self.root)
        if not resolved.is_file():
            raise ValueError("organization operation is not a regular file")
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("organization operation could not be read") from error
        record = _record_from_dict(data, self.root)
        if record.operation_id != operation_id:
            raise ValueError("organization operation ID does not match its artifact")
        if record.configured_root_identity != self.configured_root_identity:
            raise ValueError("organization operation belongs to another root")
        return record

    def list_records(self) -> tuple[OrganizationOperationRecord, ...]:
        if not os.path.lexists(self.directory):
            return ()
        self._validate_directory()
        paths = tuple(sorted(self.directory.glob("*.json")))
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("organization operation record is unsafe")
        records = [self.load(path.stem) for path in paths]
        return tuple(records)

    def find_for_plan(
        self,
        scan_generation: str,
        decision_revision: int,
    ) -> OrganizationOperationRecord | None:
        matches = [
            record
            for record in self.list_records()
            if record.scan_generation == scan_generation
            and record.decision_revision == decision_revision
        ]
        return matches[-1] if matches else None

    def find_for_authorization(
        self,
        authorization_id: str,
    ) -> OrganizationOperationRecord | None:
        matches = [
            record
            for record in self.list_records()
            if record.authorization_id == authorization_id
        ]
        return matches[-1] if matches else None

    def blocking_record(self) -> OrganizationOperationRecord | None:
        for record in reversed(self.list_records()):
            if record.status in BLOCKING_OPERATION_STATUSES:
                return record
        return None

    def _record_path(self, operation_id: str) -> Path:
        try:
            parsed = uuid.UUID(operation_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid organization operation ID") from error
        if str(parsed) != operation_id:
            raise ValueError("invalid organization operation ID")
        return self.directory / f"{operation_id}.json"

    def _validate_directory(self) -> None:
        if self.directory.is_symlink():
            raise ValueError("organization operation directory is a symlink")
        resolved = self.directory.resolve()
        validate_under_root(resolved, self.root)
        if not resolved.is_dir():
            raise ValueError("organization operation directory is not a directory")

    def _validated_data(self, record: OrganizationOperationRecord) -> dict[str, Any]:
        if record.configured_root_identity != self.configured_root_identity:
            raise ValueError("organization operation belongs to another root")
        data = _record_to_dict(record)
        validated = _record_from_dict(data, self.root)
        if validated != record:
            raise ValueError("organization operation is not canonical")
        return data

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        existing_parent = _nearest_existing_parent(self.directory)
        validate_under_root(existing_parent.resolve(strict=False), self.root)
        if not existing_parent.is_dir():
            raise ValueError("organization operation parent is not a directory")
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


def _record_to_dict(record: OrganizationOperationRecord) -> dict[str, Any]:
    data = asdict(record)
    data["expected_moves"] = [asdict(item) for item in record.expected_moves]
    if record.schema_version == ORGANIZATION_OPERATION_SCHEMA_VERSION:
        for item in data["expected_moves"]:
            item.pop("content_identity")
    elif record.schema_version != ORGANIZATION_OPERATION_SCHEMA_VERSION_V2:
        raise ValueError("unsupported organization operation schema version")
    data["verification"] = asdict(record.verification)
    data["verification"]["mismatch_codes"] = list(record.verification.mismatch_codes)
    return data


def _record_from_dict(data: object, root: Path) -> OrganizationOperationRecord:
    if not isinstance(data, dict):
        raise ValueError("organization operation must contain an object")
    schema_version = data.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValueError("unsupported organization operation schema version")
    if schema_version == ORGANIZATION_OPERATION_SCHEMA_VERSION:
        return _record_from_versioned_dict(
            data,
            root,
            schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION,
            move_parser=_move_from_dict_v1,
        )
    if schema_version == ORGANIZATION_OPERATION_SCHEMA_VERSION_V2:
        return _record_from_versioned_dict(
            data,
            root,
            schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
            move_parser=_move_from_dict_v2,
        )
    raise ValueError("unsupported organization operation schema version")


def _record_from_versioned_dict(
    data: dict[str, Any],
    root: Path,
    *,
    schema_version: int,
    move_parser,
) -> OrganizationOperationRecord:
    required = {
        "schema_version",
        "record_type",
        "operation_id",
        "authorization_id",
        "status",
        "scan_generation",
        "decision_revision",
        "approved_at",
        "created_at",
        "started_at",
        "completed_at",
        "configured_root_identity",
        "expected_moves",
        "verification",
        "legacy_operation_log",
        "failure_code",
        "duplicate_actions_deferred",
    }
    if set(data) != required:
        raise ValueError(
            f"organization operation fields do not match schema version {schema_version}"
        )
    if data["schema_version"] != schema_version:
        raise ValueError("unsupported organization operation schema version")
    if data["record_type"] != ORGANIZATION_OPERATION_RECORD_TYPE:
        raise ValueError("unexpected organization operation record type")
    status = _string(data["status"], "status")
    if status not in {
        STATUS_PREPARED,
        STATUS_MOVING,
        STATUS_MOVED_UNVERIFIED,
        STATUS_INTERRUPTED,
        *REPLAYABLE_TERMINAL_STATUSES,
    }:
        raise ValueError("organization operation status is invalid")
    expected_moves = data["expected_moves"]
    verification = data["verification"]
    if not isinstance(expected_moves, list) or not isinstance(verification, dict):
        raise ValueError("organization operation collections are invalid")
    moves = tuple(move_parser(item, root) for item in expected_moves)
    if set(verification) != {"status", "verified_count", "mismatch_codes"}:
        raise ValueError("organization operation verification fields are invalid")
    mismatch_codes = verification["mismatch_codes"]
    if not isinstance(mismatch_codes, list) or not all(
        isinstance(item, str) for item in mismatch_codes
    ):
        raise ValueError("organization operation verification mismatches are invalid")
    operation_id = _uuid_string(data["operation_id"], "operation_id")
    authorization_id = _uuid_string(data["authorization_id"], "authorization_id")
    verification_status = _string(verification["status"], "verification status")
    if verification_status not in {
        VERIFICATION_NOT_RUN,
        VERIFICATION_PASSED,
        VERIFICATION_FAILED,
    }:
        raise ValueError("organization operation verification status is invalid")
    return OrganizationOperationRecord(
        operation_id=operation_id,
        authorization_id=authorization_id,
        status=status,
        scan_generation=_string(data["scan_generation"], "scan_generation"),
        decision_revision=_integer(data["decision_revision"], "decision_revision"),
        approved_at=_string(data["approved_at"], "approved_at"),
        created_at=_string(data["created_at"], "created_at"),
        started_at=_optional_string(data["started_at"], "started_at"),
        completed_at=_optional_string(data["completed_at"], "completed_at"),
        configured_root_identity=_string(
            data["configured_root_identity"], "configured_root_identity"
        ),
        expected_moves=moves,
        verification=OrganizationOperationVerification(
            status=verification_status,
            verified_count=_integer(
                verification["verified_count"], "verification verified_count"
            ),
            mismatch_codes=tuple(mismatch_codes),
        ),
        legacy_operation_log=_optional_relative_path(
            data["legacy_operation_log"], root, "legacy_operation_log"
        ),
        failure_code=_optional_string(data["failure_code"], "failure_code"),
        duplicate_actions_deferred=_integer(
            data["duplicate_actions_deferred"], "duplicate_actions_deferred"
        ),
        schema_version=schema_version,
        record_type=ORGANIZATION_OPERATION_RECORD_TYPE,
    )


def _move_from_dict_v1(data: object, root: Path) -> OrganizationExpectedMove:
    if not isinstance(data, dict):
        raise ValueError("organization expected move must contain an object")
    required = {
        "change_id",
        "source",
        "destination",
        "source_size_bytes",
        "source_modified_time",
        "reason",
        "confidence",
        "operation",
        "overwrite_risk",
        "result_state",
        "failure_code",
        "recovery_state",
    }
    if set(data) != required:
        raise ValueError(
            "organization expected move fields are invalid for schema version 1"
        )
    return _move_from_versioned_dict(data, root, content_identity=None)


def _move_from_dict_v2(data: object, root: Path) -> OrganizationExpectedMove:
    if not isinstance(data, dict):
        raise ValueError("organization expected move must contain an object")
    required = {
        "change_id",
        "source",
        "destination",
        "source_size_bytes",
        "source_modified_time",
        "reason",
        "confidence",
        "operation",
        "overwrite_risk",
        "result_state",
        "failure_code",
        "recovery_state",
        "content_identity",
    }
    if set(data) != required:
        raise ValueError(
            "organization expected move fields are invalid for schema version 2"
        )
    return _move_from_versioned_dict(
        data,
        root,
        content_identity=RegularFileContentIdentity.from_dict(data["content_identity"]),
    )


def _move_from_versioned_dict(
    data: dict[str, Any],
    root: Path,
    *,
    content_identity: RegularFileContentIdentity | None,
) -> OrganizationExpectedMove:
    modified_time = data["source_modified_time"]
    if isinstance(modified_time, bool) or not isinstance(modified_time, (int, float)):
        raise ValueError("source_modified_time must be numeric")
    overwrite_risk = data["overwrite_risk"]
    if not isinstance(overwrite_risk, bool):
        raise ValueError("overwrite_risk must be boolean")
    result_state = _string(data["result_state"], "result_state")
    if result_state not in {
        MOVE_PENDING,
        MOVE_ATTEMPTING,
        MOVE_MOVED,
        MOVE_FAILED,
        MOVE_UNATTEMPTED,
    }:
        raise ValueError("organization expected move result_state is invalid")
    return OrganizationExpectedMove(
        change_id=_string(data["change_id"], "change_id"),
        source=_relative_path(data["source"], root, "source"),
        destination=_relative_path(data["destination"], root, "destination"),
        source_size_bytes=_integer(data["source_size_bytes"], "source_size_bytes"),
        source_modified_time=float(modified_time),
        reason=_string(data["reason"], "reason"),
        confidence=_integer(data["confidence"], "confidence"),
        operation=_string(data["operation"], "operation"),
        overwrite_risk=overwrite_risk,
        result_state=result_state,
        failure_code=_optional_string(data["failure_code"], "failure_code"),
        recovery_state=_optional_string(data["recovery_state"], "recovery_state"),
        content_identity=content_identity,
    )


def _relative_path(value: object, root: Path, label: str) -> str:
    text = _string(value, label)
    if "\\" in text:
        raise ValueError(f"{label} must be a safe relative path")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a safe relative path")
    # Artifact paths are validated lexically here so durable records remain
    # readable even when the live filesystem later contains a hostile symlink.
    # Execution and eligibility owners perform the current-filesystem checks.
    del root
    return path.as_posix()


def _optional_relative_path(value: object, root: Path, label: str) -> str | None:
    return None if value is None else _relative_path(value, root, label)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _uuid_string(value: object, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise ValueError(f"{label} must be an opaque UUID") from error
    if str(parsed) != text:
        raise ValueError(f"{label} must be an opaque UUID")
    return text


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current
