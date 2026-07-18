from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
from threading import Lock

from organizer.application.standards_models import StandardsApplicationError
from organizer.models import FileMetadata
from organizer.scope import is_protected_context_path
from organizer.web.scan_jobs import ScanJobSnapshot


@dataclass(frozen=True)
class StandardsScopeChoice:
    id: str
    label: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "kind": self.kind}


@dataclass(frozen=True)
class _ScopeBinding:
    session_id: str
    root_identity: str
    scan_generation: str
    kind: str
    label: str
    relative_path: str | None


@dataclass(frozen=True)
class _PreviewBinding:
    session_id: str
    root_identity: str
    scan_generation: str
    expected_revision: int
    application_preview_id: str


class StandardsCreationStore:
    """Own opaque, session- and scan-bound browser creation references."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve(strict=True)
        self._lock = Lock()
        self._scopes: dict[str, _ScopeBinding] = {}
        self._session_scope_ids: dict[str, set[str]] = {}
        self._previews: dict[str, _PreviewBinding] = {}

    def issue_scope_choices(
        self,
        *,
        session_id: str,
        root_identity: str,
        scan: ScanJobSnapshot,
    ) -> tuple[StandardsScopeChoice, ...]:
        generation, metadata = _completed_scan(scan)
        issued: list[tuple[StandardsScopeChoice, _ScopeBinding]] = []
        root_id = _opaque_id()
        issued.append(
            (
                StandardsScopeChoice(root_id, "Selected folder", "root"),
                _ScopeBinding(
                    session_id,
                    root_identity,
                    generation,
                    "root",
                    "Selected folder",
                    None,
                ),
            )
        )
        for item in sorted(metadata, key=lambda value: value.relative_path.as_posix()):
            if not _safe_folder(item, self._root):
                continue
            scope_id = _opaque_id()
            relative = item.relative_path.as_posix()
            label = " › ".join(item.relative_path.parts)
            issued.append(
                (
                    StandardsScopeChoice(
                        scope_id,
                        label,
                        "folder",
                    ),
                    _ScopeBinding(
                        session_id,
                        root_identity,
                        generation,
                        "folder",
                        label,
                        relative,
                    ),
                )
            )
        with self._lock:
            for stale_id in self._session_scope_ids.get(session_id, set()):
                self._scopes.pop(stale_id, None)
            ids = {choice.id for choice, _ in issued}
            self._session_scope_ids[session_id] = ids
            self._scopes.update({choice.id: binding for choice, binding in issued})
        return tuple(choice for choice, _ in issued)

    def resolve_scope(
        self,
        *,
        scope_id: str,
        session_id: str,
        root_identity: str,
        scan: ScanJobSnapshot,
    ) -> tuple[str, FileMetadata | None, str]:
        generation, metadata = _completed_scan(scan)
        with self._lock:
            binding = self._scopes.get(scope_id)
        if (
            binding is None
            or binding.session_id != session_id
            or binding.root_identity != root_identity
            or binding.scan_generation != generation
        ):
            raise StandardsApplicationError("folder_scope_stale")
        if binding.kind == "root":
            return "root", None, binding.label
        folder = next(
            (
                item
                for item in metadata
                if item.is_dir
                and item.relative_path.as_posix() == binding.relative_path
            ),
            None,
        )
        if folder is None or not _safe_folder(folder, self._root):
            raise StandardsApplicationError("folder_scope_stale")
        return "folder", folder, binding.label

    def issue_preview_token(
        self,
        *,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
        application_preview_id: str,
    ) -> str:
        token = _opaque_id()
        with self._lock:
            self._previews[token] = _PreviewBinding(
                session_id=session_id,
                root_identity=root_identity,
                scan_generation=scan_generation,
                expected_revision=expected_revision,
                application_preview_id=application_preview_id,
            )
        return token

    def resolve_preview_token(
        self,
        *,
        token: str,
        session_id: str,
        root_identity: str,
        scan: ScanJobSnapshot,
        expected_revision: int,
    ) -> str:
        generation, _ = _completed_scan(scan)
        with self._lock:
            binding = self._previews.get(token)
        if (
            binding is None
            or binding.session_id != session_id
            or binding.root_identity != root_identity
            or binding.scan_generation != generation
        ):
            raise StandardsApplicationError("preview_expired")
        if binding.expected_revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
        return binding.application_preview_id


def _completed_scan(
    scan: ScanJobSnapshot,
) -> tuple[str, tuple[FileMetadata, ...]]:
    if scan.status != "completed" or scan.job_id is None or scan.result is None:
        raise StandardsApplicationError("folder_scope_stale")
    return scan.job_id, scan.result.metadata_items


def _safe_folder(item: FileMetadata, root: Path) -> bool:
    if not item.is_dir or item.relative_path in {Path("."), Path("")}:
        return False
    if is_protected_context_path(item.relative_path):
        return False
    try:
        resolved = item.path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    return not item.path.is_symlink() and resolved.is_dir()


def _opaque_id() -> str:
    return secrets.token_urlsafe(24)
