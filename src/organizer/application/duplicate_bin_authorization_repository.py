from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from organizer.application.duplicate_bin_models import (
    ApprovedDuplicateBatch,
    canonical_digest,
)
from organizer.application.duplicate_bin_movement_models import (
    AUTH_AVAILABLE,
    AUTH_CONSUMED,
    AUTH_STATUSES,
    AUTH_SUPERSEDED,
    AUTHORIZATION_RECORD_TYPE,
    DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION,
    DuplicateBinMovementAuthorization,
)
from organizer.safety import validate_under_root
from organizer.scope import DUPLICATE_BIN_RELATIVE_ROOT

AUTHORIZATION_LIFETIME = timedelta(minutes=15)


class DuplicateBinAuthorizationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DuplicateBinAuthorizationRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("duplicate bin root is not a directory")
        self.directory = self.root / DUPLICATE_BIN_RELATIVE_ROOT / "authorizations"
        self.path = self.directory / "catalog.json"
        self._lock = Lock()

    @property
    def configured_root_identity(self) -> str:
        import hashlib

        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()

    def issue(
        self,
        batch: ApprovedDuplicateBatch,
        *,
        browser_session_id: str,
        issued_at: str | None = None,
    ) -> DuplicateBinMovementAuthorization:
        if not isinstance(browser_session_id, str) or not browser_session_id:
            raise DuplicateBinAuthorizationError("invalid_browser_session")
        if not batch.can_receive_authorization:
            raise DuplicateBinAuthorizationError("duplicate_batch_not_approved")
        if batch.configured_root_identity != self.configured_root_identity:
            raise DuplicateBinAuthorizationError("duplicate_batch_wrong_root")
        issued = _parse_timestamp(issued_at or datetime.now(UTC).isoformat())
        authorization_id = str(uuid.uuid4())
        authorization = DuplicateBinMovementAuthorization(
            authorization_id=authorization_id,
            browser_session_id=browser_session_id,
            configured_root_identity=self.configured_root_identity,
            batch_id=batch.batch_id,
            approved_batch_digest=approved_batch_digest(batch),
            selected_bin_item_ids=tuple(
                sorted(item.bin_item_id for item in batch.items)
            ),
            expected_batch_version=batch.batch_version,
            expected_batch_state=batch.status,
            issued_at=issued.isoformat(),
            expires_at=(issued + AUTHORIZATION_LIFETIME).isoformat(),
            status=AUTH_AVAILABLE,
            superseded_by=None,
            operation_id=None,
        )
        with self._lock:
            records = self._load_unlocked()
            records = tuple(
                replace(record, status=AUTH_SUPERSEDED, superseded_by=authorization_id)
                if record.batch_id == batch.batch_id and record.status == AUTH_AVAILABLE
                else record
                for record in records
            ) + (authorization,)
            self._write(tuple(sorted(records, key=lambda item: item.authorization_id)))
        return authorization

    def load(self, authorization_id: str) -> DuplicateBinMovementAuthorization:
        requested = _uuid(authorization_id, "authorization_id")
        with self._lock:
            record = next(
                (
                    item
                    for item in self._load_unlocked()
                    if item.authorization_id == requested
                ),
                None,
            )
        if record is None:
            raise DuplicateBinAuthorizationError("invalid_confirmation_token")
        return record

    def list_records(self) -> tuple[DuplicateBinMovementAuthorization, ...]:
        """Return every canonical authorization without changing the catalog."""

        with self._lock:
            return self._load_unlocked()

    def consume(
        self, authorization_id: str, operation_id: str
    ) -> DuplicateBinMovementAuthorization:
        _uuid(operation_id, "operation_id")
        with self._lock:
            records = self._load_unlocked()
            current = next(
                (item for item in records if item.authorization_id == authorization_id),
                None,
            )
            if current is None:
                raise DuplicateBinAuthorizationError("invalid_confirmation_token")
            if current.status == AUTH_CONSUMED and current.operation_id == operation_id:
                return current
            if current.status != AUTH_AVAILABLE:
                raise DuplicateBinAuthorizationError("invalid_confirmation_token")
            consumed = replace(current, status=AUTH_CONSUMED, operation_id=operation_id)
            self._write(
                tuple(
                    consumed if item.authorization_id == authorization_id else item
                    for item in records
                )
            )
            return consumed

    def _load_unlocked(self) -> tuple[DuplicateBinMovementAuthorization, ...]:
        if not os.path.lexists(self.path):
            return ()
        if self.path.is_symlink():
            raise DuplicateBinAuthorizationError("authorization_state_malformed")
        try:
            resolved = self.path.resolve(strict=True)
            validate_under_root(resolved, self.root)
            data = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {
                "schema_version",
                "record_type",
                "configured_root_identity",
                "authorizations",
            }:
                raise ValueError("authorization catalog fields are invalid")
            if data["schema_version"] != DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION:
                raise ValueError("authorization schema version is invalid")
            if data["record_type"] != "loci_duplicate_bin_authorization_catalog":
                raise ValueError("authorization catalog type is invalid")
            if data["configured_root_identity"] != self.configured_root_identity:
                raise ValueError("authorization catalog belongs to another root")
            raw = data["authorizations"]
            if not isinstance(raw, list):
                raise ValueError("authorizations must be a list")
            records = tuple(_authorization_from_dict(item) for item in raw)
            if (
                tuple(sorted(records, key=lambda item: item.authorization_id))
                != records
            ):
                raise ValueError("authorizations are not canonical")
            if len({item.authorization_id for item in records}) != len(records):
                raise ValueError("authorization IDs are duplicated")
            operation_ids = [
                item.operation_id for item in records if item.operation_id is not None
            ]
            if len(set(operation_ids)) != len(operation_ids):
                raise ValueError("authorization operation IDs are duplicated")
            available_batches = [
                item.batch_id for item in records if item.status == AUTH_AVAILABLE
            ]
            if len(set(available_batches)) != len(available_batches):
                raise ValueError("multiple active authorizations exist for a batch")
            by_id = {item.authorization_id: item for item in records}
            for item in records:
                if item.status == AUTH_SUPERSEDED:
                    successor = by_id.get(item.superseded_by)
                    if successor is None or successor.batch_id != item.batch_id:
                        raise ValueError("authorization successor is invalid")
            return records
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise DuplicateBinAuthorizationError(
                "authorization_state_malformed"
            ) from error

    def _write(self, records: tuple[DuplicateBinMovementAuthorization, ...]) -> None:
        _safe_directory(self.directory, self.root)
        data = {
            "schema_version": DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION,
            "record_type": "loci_duplicate_bin_authorization_catalog",
            "configured_root_identity": self.configured_root_identity,
            "authorizations": [
                {
                    **asdict(item),
                    "selected_bin_item_ids": list(item.selected_bin_item_ids),
                }
                for item in records
            ],
        }
        _atomic_json(self.path, data)


def approved_batch_digest(batch: ApprovedDuplicateBatch) -> str:
    return canonical_digest(
        {
            "schema": "loci-duplicate-approved-batch-binding-v1",
            "batch_id": batch.batch_id,
            "approved_plan_digest": batch.approved_plan_digest,
            "duplicate_batch_digest": batch.duplicate_batch_digest,
            "groups": [group.group_id for group in batch.groups],
            "items": [item.bin_item_id for item in batch.items],
        }
    )


def _authorization_from_dict(data: object) -> DuplicateBinMovementAuthorization:
    required = {
        "schema_version",
        "record_type",
        "authorization_id",
        "browser_session_id",
        "configured_root_identity",
        "batch_id",
        "approved_batch_digest",
        "selected_bin_item_ids",
        "expected_batch_version",
        "expected_batch_state",
        "issued_at",
        "expires_at",
        "status",
        "superseded_by",
        "operation_id",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("authorization fields are invalid")
    if (
        data["schema_version"] != DUPLICATE_BIN_MOVEMENT_SCHEMA_VERSION
        or data["record_type"] != AUTHORIZATION_RECORD_TYPE
    ):
        raise ValueError("authorization schema is invalid")
    selected = data["selected_bin_item_ids"]
    if not isinstance(selected, list) or not selected:
        raise ValueError("authorization selected items are invalid")
    selected_ids = tuple(_uuid(item, "selected bin item") for item in selected)
    if tuple(sorted(selected_ids)) != selected_ids or len(set(selected_ids)) != len(
        selected_ids
    ):
        raise ValueError("authorization selected items are not canonical")
    status = _string(data["status"], "status")
    if status not in AUTH_STATUSES:
        raise ValueError("authorization status is invalid")
    superseded = _optional_uuid(data["superseded_by"], "superseded_by")
    operation = _optional_uuid(data["operation_id"], "operation_id")
    if status == AUTH_AVAILABLE and (superseded is not None or operation is not None):
        raise ValueError("available authorization has lifecycle links")
    if status == AUTH_SUPERSEDED and (superseded is None or operation is not None):
        raise ValueError("superseded authorization lifecycle is invalid")
    if status == AUTH_CONSUMED and (operation is None or superseded is not None):
        raise ValueError("consumed authorization lifecycle is invalid")
    issued = _parse_timestamp(data["issued_at"])
    expires = _parse_timestamp(data["expires_at"])
    if expires - issued != AUTHORIZATION_LIFETIME:
        raise ValueError("authorization lifetime is invalid")
    version = data["expected_batch_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("authorization batch version is invalid")
    return DuplicateBinMovementAuthorization(
        authorization_id=_uuid(data["authorization_id"], "authorization_id"),
        browser_session_id=_string(data["browser_session_id"], "browser session"),
        configured_root_identity=_digest(
            data["configured_root_identity"], "root identity"
        ),
        batch_id=_uuid(data["batch_id"], "batch_id"),
        approved_batch_digest=_digest(data["approved_batch_digest"], "batch digest"),
        selected_bin_item_ids=selected_ids,
        expected_batch_version=version,
        expected_batch_state=_string(data["expected_batch_state"], "batch state"),
        issued_at=issued.isoformat(),
        expires_at=expires.isoformat(),
        status=status,
        superseded_by=superseded,
        operation_id=operation,
    )


def _safe_directory(directory: Path, root: Path) -> None:
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise DuplicateBinAuthorizationError("authorization_state_unsafe")
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


def _parse_timestamp(value: object) -> datetime:
    text = _string(value, "timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def _uuid(value: object, label: str) -> str:
    text = _string(value, label)
    parsed = uuid.UUID(text)
    if str(parsed) != text:
        raise ValueError(f"{label} is invalid")
    return text


def _optional_uuid(value: object, label: str) -> str | None:
    return None if value is None else _uuid(value, label)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{label} is invalid")
    return text
