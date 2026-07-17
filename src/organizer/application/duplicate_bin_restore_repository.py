from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Lock
import uuid

from organizer.application.duplicate_bin_restore_models import (
    AUTH_AVAILABLE,
    AUTH_CONSUMED,
    AUTH_RECORD_TYPE,
    AUTH_STATUSES,
    AUTH_SUPERSEDED,
    ITEM_ATTEMPTING,
    ITEM_PENDING,
    ITEM_UNATTEMPTED,
    OP_RECORD_TYPE,
    RESTORE_ITEM_STATES,
    RESTORE_SCHEMA_VERSION,
    RESTORE_STATUSES,
    DuplicateBinRestoreAuthorization,
    DuplicateBinRestoreItem,
    DuplicateBinRestoreOperation,
    DuplicateBinRestoreVerification,
    RestoreAuthorizationItem,
)
from organizer.safety import validate_under_root
from organizer.scope import DUPLICATE_BIN_RELATIVE_ROOT


AUTHORIZATION_LIFETIME = timedelta(minutes=15)


class DuplicateBinRestoreRepositoryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DuplicateBinRestoreRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("restore root is not a directory")
        self.directory = self.root / DUPLICATE_BIN_RELATIVE_ROOT / "restores"
        self.authorization_directory = self.directory / "authorizations"
        self.authorization_path = self.authorization_directory / "catalog.json"
        self.operation_directory = self.directory / "operations"
        self._lock = Lock()

    @property
    def configured_root_identity(self) -> str:
        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()

    def issue_authorization(
        self,
        items: tuple[RestoreAuthorizationItem, ...],
        *,
        browser_session_id: str,
        issued_at: str | None = None,
    ) -> DuplicateBinRestoreAuthorization:
        if not items:
            raise DuplicateBinRestoreRepositoryError("no_restorable_items")
        if not isinstance(browser_session_id, str) or not browser_session_id:
            raise DuplicateBinRestoreRepositoryError("invalid_browser_session")
        ordered = tuple(sorted(items, key=lambda item: item.bin_item_id))
        if len({item.bin_item_id for item in ordered}) != len(ordered):
            raise DuplicateBinRestoreRepositoryError("duplicate_restore_item")
        issued = _timestamp(issued_at or datetime.now(timezone.utc).isoformat())
        authorization_id = str(uuid.uuid4())
        authorization = DuplicateBinRestoreAuthorization(
            authorization_id=authorization_id,
            browser_session_id=browser_session_id,
            configured_root_identity=self.configured_root_identity,
            selected_bin_item_ids=tuple(item.bin_item_id for item in ordered),
            parent_movement_operation_ids=tuple(
                sorted({item.movement_operation_id for item in ordered})
            ),
            items=ordered,
            issued_at=issued.isoformat(),
            expires_at=(issued + AUTHORIZATION_LIFETIME).isoformat(),
            status=AUTH_AVAILABLE,
            superseded_by=None,
            restore_operation_id=None,
        )
        with self._lock:
            records = self._load_authorizations_unlocked()
            selected = set(authorization.selected_bin_item_ids)
            records = tuple(
                replace(
                    record,
                    status=AUTH_SUPERSEDED,
                    superseded_by=authorization_id,
                )
                if record.status == AUTH_AVAILABLE
                and selected.intersection(record.selected_bin_item_ids)
                else record
                for record in records
            ) + (authorization,)
            self._write_authorizations(records)
        return authorization

    def load_authorization(
        self, authorization_id: str
    ) -> DuplicateBinRestoreAuthorization:
        requested = _uuid(authorization_id, "authorization_id")
        with self._lock:
            record = next(
                (
                    item
                    for item in self._load_authorizations_unlocked()
                    if item.authorization_id == requested
                ),
                None,
            )
        if record is None:
            raise DuplicateBinRestoreRepositoryError("invalid_confirmation_token")
        return record

    def consume_authorization(
        self, authorization_id: str, restore_operation_id: str
    ) -> None:
        _uuid(restore_operation_id, "restore_operation_id")
        with self._lock:
            records = self._load_authorizations_unlocked()
            current = next(
                (item for item in records if item.authorization_id == authorization_id),
                None,
            )
            if current is None or current.status != AUTH_AVAILABLE:
                raise DuplicateBinRestoreRepositoryError("invalid_confirmation_token")
            consumed = replace(
                current,
                status=AUTH_CONSUMED,
                restore_operation_id=restore_operation_id,
            )
            self._write_authorizations(
                tuple(
                    consumed if item.authorization_id == authorization_id else item
                    for item in records
                )
            )

    def create_operation(self, record: DuplicateBinRestoreOperation) -> None:
        path = self._operation_path(record.restore_operation_id)
        if os.path.lexists(path):
            raise DuplicateBinRestoreRepositoryError("restore_operation_exists")
        if self.find_for_authorization(record.authorization_id) is not None:
            raise DuplicateBinRestoreRepositoryError(
                "restore_authorization_has_operation"
            )
        if any(
            self.find_for_item(item) is not None
            for item in record.selected_bin_item_ids
        ):
            raise DuplicateBinRestoreRepositoryError("restore_item_already_owned")
        self._write_operation(path, _operation_to_dict(_validate_operation(record)))

    def save_operation(self, record: DuplicateBinRestoreOperation) -> None:
        path = self._operation_path(record.restore_operation_id)
        if path.is_symlink() or not path.exists():
            raise DuplicateBinRestoreRepositoryError("restore_operation_unsafe")
        existing = self.load_operation(record.restore_operation_id)
        if _operation_binding(existing) != _operation_binding(record):
            raise DuplicateBinRestoreRepositoryError(
                "restore_operation_binding_changed"
            )
        self._write_operation(path, _operation_to_dict(_validate_operation(record)))

    def load_operation(self, restore_operation_id: str) -> DuplicateBinRestoreOperation:
        path = self._operation_path(restore_operation_id)
        if path.is_symlink():
            raise DuplicateBinRestoreRepositoryError("restore_operation_unsafe")
        try:
            resolved = path.resolve(strict=True)
            validate_under_root(resolved, self.root)
            record = _operation_from_dict(
                json.loads(resolved.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise DuplicateBinRestoreRepositoryError(
                "restore_operation_unavailable"
            ) from error
        if (
            record.restore_operation_id != restore_operation_id
            or record.configured_root_identity != self.configured_root_identity
        ):
            raise DuplicateBinRestoreRepositoryError("restore_operation_wrong_root")
        return record

    def list_operations(self) -> tuple[DuplicateBinRestoreOperation, ...]:
        if not self.operation_directory.exists():
            return ()
        if (
            self.operation_directory.is_symlink()
            or not self.operation_directory.is_dir()
        ):
            raise DuplicateBinRestoreRepositoryError("restore_operation_unsafe")
        paths = tuple(sorted(self.operation_directory.glob("*.json")))
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise DuplicateBinRestoreRepositoryError("restore_operation_unsafe")
        records = tuple(self.load_operation(path.stem) for path in paths)
        if len({item.authorization_id for item in records}) != len(records):
            raise DuplicateBinRestoreRepositoryError("restore_operation_duplicate_auth")
        claimed = [item for record in records for item in record.selected_bin_item_ids]
        if len(set(claimed)) != len(claimed):
            raise DuplicateBinRestoreRepositoryError("restore_item_already_owned")
        return records

    def find_for_authorization(
        self, authorization_id: str
    ) -> DuplicateBinRestoreOperation | None:
        _uuid(authorization_id, "authorization_id")
        return next(
            (
                item
                for item in self.list_operations()
                if item.authorization_id == authorization_id
            ),
            None,
        )

    def find_for_item(self, bin_item_id: str) -> DuplicateBinRestoreOperation | None:
        requested = _uuid(bin_item_id, "bin_item_id")
        return next(
            (
                item
                for item in self.list_operations()
                if requested in item.selected_bin_item_ids
            ),
            None,
        )

    def _load_authorizations_unlocked(
        self,
    ) -> tuple[DuplicateBinRestoreAuthorization, ...]:
        if not os.path.lexists(self.authorization_path):
            return ()
        if self.authorization_path.is_symlink():
            raise DuplicateBinRestoreRepositoryError("restore_authorization_unsafe")
        try:
            data = json.loads(self.authorization_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {
                "schema_version",
                "configured_root_identity",
                "authorizations",
            }:
                raise ValueError("authorization catalog fields")
            if (
                data["schema_version"] != RESTORE_SCHEMA_VERSION
                or data["configured_root_identity"] != self.configured_root_identity
                or not isinstance(data["authorizations"], list)
            ):
                raise ValueError("authorization catalog identity")
            records = tuple(
                _authorization_from_dict(item) for item in data["authorizations"]
            )
            if (
                tuple(sorted(records, key=lambda item: item.authorization_id))
                != records
            ):
                raise ValueError("authorization ordering")
            if len({item.authorization_id for item in records}) != len(records):
                raise ValueError("authorization IDs")
            return records
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise DuplicateBinRestoreRepositoryError(
                "restore_authorization_malformed"
            ) from error

    def _write_authorizations(
        self, records: tuple[DuplicateBinRestoreAuthorization, ...]
    ) -> None:
        _safe_directory(self.authorization_directory, self.root)
        _atomic_json(
            self.authorization_path,
            {
                "schema_version": RESTORE_SCHEMA_VERSION,
                "configured_root_identity": self.configured_root_identity,
                "authorizations": [
                    _authorization_to_dict(item)
                    for item in sorted(records, key=lambda item: item.authorization_id)
                ],
            },
        )

    def _operation_path(self, restore_operation_id: str) -> Path:
        return (
            self.operation_directory
            / f"{_uuid(restore_operation_id, 'restore_operation_id')}.json"
        )

    def _write_operation(self, path: Path, data: object) -> None:
        _safe_directory(self.operation_directory, self.root)
        _atomic_json(path, data)


def _authorization_to_dict(record: DuplicateBinRestoreAuthorization) -> dict:
    data = asdict(record)
    data["selected_bin_item_ids"] = list(record.selected_bin_item_ids)
    data["parent_movement_operation_ids"] = list(record.parent_movement_operation_ids)
    data["items"] = [asdict(item) for item in record.items]
    return data


def _authorization_from_dict(data: object) -> DuplicateBinRestoreAuthorization:
    required = set(DuplicateBinRestoreAuthorization.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("authorization fields")
    if (
        data["schema_version"] != RESTORE_SCHEMA_VERSION
        or data["record_type"] != AUTH_RECORD_TYPE
    ):
        raise ValueError("authorization schema")
    items_raw = data["items"]
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError("authorization items")
    items = tuple(_authorization_item(item) for item in items_raw)
    selected = _uuid_tuple(data["selected_bin_item_ids"], "selected items")
    parents = _uuid_tuple(data["parent_movement_operation_ids"], "parent operations")
    if selected != tuple(item.bin_item_id for item in items):
        raise ValueError("authorization item binding")
    if parents != tuple(sorted({item.movement_operation_id for item in items})):
        raise ValueError("authorization parents")
    status = _string(data["status"], "status")
    if status not in AUTH_STATUSES:
        raise ValueError("authorization status")
    superseded = _optional_uuid(data["superseded_by"], "superseded_by")
    operation = _optional_uuid(data["restore_operation_id"], "restore_operation_id")
    if status == AUTH_AVAILABLE and (superseded is not None or operation is not None):
        raise ValueError("available authorization links")
    if status == AUTH_SUPERSEDED and (superseded is None or operation is not None):
        raise ValueError("superseded authorization links")
    if status == AUTH_CONSUMED and (operation is None or superseded is not None):
        raise ValueError("consumed authorization links")
    issued = _timestamp(data["issued_at"])
    expires = _timestamp(data["expires_at"])
    if expires - issued != AUTHORIZATION_LIFETIME:
        raise ValueError("authorization lifetime")
    return DuplicateBinRestoreAuthorization(
        authorization_id=_uuid(data["authorization_id"], "authorization_id"),
        browser_session_id=_string(data["browser_session_id"], "session"),
        configured_root_identity=_digest(data["configured_root_identity"], "root"),
        selected_bin_item_ids=selected,
        parent_movement_operation_ids=parents,
        items=items,
        issued_at=issued.isoformat(),
        expires_at=expires.isoformat(),
        status=status,
        superseded_by=superseded,
        restore_operation_id=operation,
    )


def _authorization_item(data: object) -> RestoreAuthorizationItem:
    required = set(RestoreAuthorizationItem.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("authorization item fields")
    version = data["batch_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("batch version")
    return RestoreAuthorizationItem(
        bin_item_id=_uuid(data["bin_item_id"], "bin_item_id"),
        movement_operation_id=_uuid(
            data["movement_operation_id"], "movement operation"
        ),
        batch_id=_uuid(data["batch_id"], "batch_id"),
        batch_version=version,
        item_state=_string(data["item_state"], "item state"),
    )


def _operation_to_dict(record: DuplicateBinRestoreOperation) -> dict:
    data = asdict(record)
    data["selected_bin_item_ids"] = list(record.selected_bin_item_ids)
    data["parent_movement_operation_ids"] = list(record.parent_movement_operation_ids)
    data["items"] = [
        {
            **asdict(item),
            "validated_parent_directories": list(item.validated_parent_directories),
        }
        for item in record.items
    ]
    data["verification"] = {
        **asdict(record.verification),
        "mismatch_codes": list(record.verification.mismatch_codes),
    }
    return data


def _operation_from_dict(data: object) -> DuplicateBinRestoreOperation:
    required = set(DuplicateBinRestoreOperation.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("restore operation fields")
    if (
        data["schema_version"] != RESTORE_SCHEMA_VERSION
        or data["record_type"] != OP_RECORD_TYPE
    ):
        raise ValueError("restore operation schema")
    items_raw = data["items"]
    verification = data["verification"]
    if (
        not isinstance(items_raw, list)
        or not items_raw
        or not isinstance(verification, dict)
    ):
        raise ValueError("restore operation collections")
    items = tuple(_restore_item(item) for item in items_raw)
    selected = _ordered_uuid_tuple(data["selected_bin_item_ids"], "selected items")
    parents = _uuid_tuple(data["parent_movement_operation_ids"], "parents")
    if selected != tuple(item.bin_item_id for item in items):
        raise ValueError("restore operation items")
    if parents != tuple(sorted({item.movement_operation_id for item in items})):
        raise ValueError("restore operation parents")
    status = _string(data["status"], "status")
    if status not in RESTORE_STATUSES:
        raise ValueError("restore operation status")
    if set(verification) != {"status", "verified_count", "mismatch_codes"}:
        raise ValueError("restore verification fields")
    mismatch = verification["mismatch_codes"]
    count = verification["verified_count"]
    if not isinstance(mismatch, list) or not all(
        isinstance(item, str) for item in mismatch
    ):
        raise ValueError("restore mismatches")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or count > len(items)
    ):
        raise ValueError("restore verified count")
    return DuplicateBinRestoreOperation(
        restore_operation_id=_uuid(data["restore_operation_id"], "restore operation"),
        authorization_id=_uuid(data["authorization_id"], "authorization"),
        configured_root_identity=_digest(data["configured_root_identity"], "root"),
        selected_bin_item_ids=selected,
        parent_movement_operation_ids=parents,
        items=items,
        preflight_digest=_digest(data["preflight_digest"], "preflight"),
        status=status,
        created_at=_timestamp_text(data["created_at"]),
        started_at=_optional_timestamp(data["started_at"]),
        completed_at=_optional_timestamp(data["completed_at"]),
        verification=DuplicateBinRestoreVerification(
            status=_string(verification["status"], "verification status"),
            verified_count=count,
            mismatch_codes=tuple(mismatch),
        ),
        executor_log_path=_optional_relative(data["executor_log_path"], "executor log"),
        failure_code=_optional_string(data["failure_code"], "failure code"),
        recovery_state=_optional_string(data["recovery_state"], "recovery state"),
    )


def _restore_item(data: object) -> DuplicateBinRestoreItem:
    required = set(DuplicateBinRestoreItem.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("restore item fields")
    state = _string(data["state"], "state")
    if state not in RESTORE_ITEM_STATES:
        raise ValueError("restore item state")
    size = data["expected_size_bytes"]
    mtime = data["source_modified_time_ns"]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (size, mtime)
    ):
        raise ValueError("restore item metadata")
    parents_raw = data["validated_parent_directories"]
    if not isinstance(parents_raw, list):
        raise ValueError("restore parent directories")
    parents = tuple(_relative(item, "validated parent") for item in parents_raw)
    attempted = _optional_timestamp(data["attempted_at"])
    completed = _optional_timestamp(data["completed_at"])
    if state == ITEM_PENDING and (attempted is not None or completed is not None):
        raise ValueError("pending timestamps")
    if state == ITEM_ATTEMPTING and (attempted is None or completed is not None):
        raise ValueError("attempting timestamps")
    if state == ITEM_UNATTEMPTED and (attempted is not None or completed is None):
        raise ValueError("unattempted timestamps")
    if state not in {ITEM_PENDING, ITEM_ATTEMPTING, ITEM_UNATTEMPTED} and (
        attempted is None or completed is None
    ):
        raise ValueError("restore item timestamps")
    return DuplicateBinRestoreItem(
        bin_item_id=_uuid(data["bin_item_id"], "bin item"),
        movement_operation_id=_uuid(
            data["movement_operation_id"], "movement operation"
        ),
        batch_id=_uuid(data["batch_id"], "batch"),
        protected_source=_relative(data["protected_source"], "protected source"),
        previous_destination=_relative(
            data["previous_destination"], "previous destination"
        ),
        expected_sha256=_digest(data["expected_sha256"], "expected hash"),
        expected_size_bytes=size,
        source_modified_time_ns=mtime,
        validated_parent_directories=parents,
        state=state,
        attempted_at=attempted,
        completed_at=completed,
        failure_code=_optional_string(data["failure_code"], "failure code"),
        recovery_state=_optional_string(data["recovery_state"], "recovery state"),
    )


def _validate_operation(
    record: DuplicateBinRestoreOperation,
) -> DuplicateBinRestoreOperation:
    if _operation_from_dict(_operation_to_dict(record)) != record:
        raise ValueError("restore operation is not canonical")
    return record


def _operation_binding(record: DuplicateBinRestoreOperation) -> tuple:
    return (
        record.restore_operation_id,
        record.authorization_id,
        record.configured_root_identity,
        record.selected_bin_item_ids,
        record.parent_movement_operation_ids,
        record.preflight_digest,
        record.created_at,
        tuple(
            (
                item.bin_item_id,
                item.movement_operation_id,
                item.batch_id,
                item.protected_source,
                item.previous_destination,
                item.expected_sha256,
                item.expected_size_bytes,
                item.source_modified_time_ns,
                item.validated_parent_directories,
            )
            for item in record.items
        ),
    )


def _safe_directory(directory: Path, root: Path) -> None:
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise DuplicateBinRestoreRepositoryError("restore_repository_unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    validate_under_root(directory.resolve(strict=True), root)


def _atomic_json(path: Path, data: object) -> None:
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _uuid_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} is invalid")
    result = tuple(_uuid(item, label) for item in value)
    if tuple(sorted(result)) != result or len(set(result)) != len(result):
        raise ValueError(f"{label} is not canonical")
    return result


def _ordered_uuid_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} is invalid")
    result = tuple(_uuid(item, label) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} is not canonical")
    return result


def _uuid(value: object, label: str) -> str:
    text = _string(value, label)
    parsed = uuid.UUID(text)
    if str(parsed) != text:
        raise ValueError(f"{label} is invalid")
    return text


def _optional_uuid(value: object, label: str) -> str | None:
    return None if value is None else _uuid(value, label)


def _digest(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{label} is invalid")
    return text


def _relative(value: object, label: str) -> str:
    text = _string(value, label)
    path = Path(text)
    if (
        "\\" in text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} is invalid")
    return path.as_posix()


def _optional_relative(value: object, label: str) -> str | None:
    return None if value is None else _relative(value, label)


def _timestamp(value: object) -> datetime:
    text = _string(value, "timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed


def _timestamp_text(value: object) -> str:
    return _timestamp(value).isoformat()


def _optional_timestamp(value: object) -> str | None:
    return None if value is None else _timestamp_text(value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)
