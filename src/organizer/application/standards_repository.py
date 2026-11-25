from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from organizer.organization_rules import (
    ORGANIZATION_RULES_RELATIVE_PATH,
    ORGANIZATION_RULES_STATUS_LOADED,
    ORGANIZATION_RULES_STATUS_MISSING,
    OrganizationRulesLoadResult,
    StandardsCatalog,
    load_organization_rules,
    standards_catalog_from_data,
    standards_catalog_to_data,
)
from organizer.safety import validate_under_root


class StandardsRepositoryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StandardsStorageSnapshot:
    load_result: OrganizationRulesLoadResult
    catalog: StandardsCatalog | None

    @property
    def revision(self) -> int:
        return self.catalog.revision if self.catalog is not None else 0

    @property
    def is_v1(self) -> bool:
        return (
            self.load_result.status == ORGANIZATION_RULES_STATUS_LOADED
            and self.catalog is None
        )


def configured_root_identity(root: Path) -> str:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("configured root is not a directory")
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


class StandardsRepository:
    """Own strict, atomic v1/v2 Standards storage for one configured root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("configured root is not a directory")
        self.path = self.root / ORGANIZATION_RULES_RELATIVE_PATH
        self.directory = self.path.parent
        self._lock = Lock()

    @property
    def configured_root_identity(self) -> str:
        return configured_root_identity(self.root)

    def load(self) -> StandardsStorageSnapshot:
        with self._lock:
            return self._load_unlocked()

    def save_catalog(
        self,
        catalog: StandardsCatalog,
        *,
        expected_revision: int,
    ) -> StandardsCatalog:
        with self._lock:
            current = self._load_unlocked()
            if current.revision != expected_revision:
                raise StandardsRepositoryError("standard_revision_mismatch")
            if catalog.configured_root_identity != self.configured_root_identity:
                raise StandardsRepositoryError("wrong_root_scope")
            if catalog.revision != expected_revision + 1:
                raise StandardsRepositoryError("standard_revision_mismatch")
            try:
                data = standards_catalog_to_data(catalog)
                validated = standards_catalog_from_data(data)
            except ValueError as error:
                raise StandardsRepositoryError(
                    "standards_storage_unavailable"
                ) from error
            if validated != catalog:
                raise StandardsRepositoryError("standards_storage_unavailable")
            self._atomic_write(data)
            loaded = self._load_unlocked().catalog
            if loaded != catalog:
                raise StandardsRepositoryError("standards_storage_unavailable")
            return catalog

    def _load_unlocked(self) -> StandardsStorageSnapshot:
        result = load_organization_rules(self.root)
        if not result.available:
            raise StandardsRepositoryError(
                "wrong_root_scope"
                if result.error_code == "wrong_root_scope"
                else "standards_storage_unavailable"
            )
        if result.status == ORGANIZATION_RULES_STATUS_MISSING:
            return StandardsStorageSnapshot(result, None)
        catalog = result.catalog
        if (
            catalog is not None
            and catalog.configured_root_identity != self.configured_root_identity
        ):
            raise StandardsRepositoryError("wrong_root_scope")
        return StandardsStorageSnapshot(result, catalog)

    def _atomic_write(self, data: dict[str, object]) -> None:
        nearest = self.directory
        while not nearest.exists():
            nearest = nearest.parent
        try:
            validate_under_root(nearest.resolve(strict=True), self.root)
            current = self.root
            for part in ORGANIZATION_RULES_RELATIVE_PATH.parent.parts:
                current = current / part
                if os.path.lexists(current) and current.is_symlink():
                    raise ValueError("symlinked Standards storage")
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.directory.is_symlink() or not self.directory.is_dir():
                raise ValueError("unsafe Standards directory")
            validate_under_root(self.directory.resolve(strict=True), self.root)
            if os.path.lexists(self.path) and (
                self.path.is_symlink() or not self.path.is_file()
            ):
                raise ValueError("unsafe Standards file")
        except (OSError, ValueError) as error:
            raise StandardsRepositoryError("standards_storage_unavailable") from error

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.directory,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except Exception as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
            if isinstance(error, StandardsRepositoryError):
                raise
            raise StandardsRepositoryError("standards_storage_unavailable") from error
