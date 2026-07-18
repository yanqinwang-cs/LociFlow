from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
from threading import Lock
from typing import Callable

from organizer.application.standards_models import (
    PREVIEW_LIFETIME_SECONDS,
    PreparedStandardsPreviewToken,
    ResolvedCreationScope,
    StandardsApplicationError,
    StandardsCreationContext,
    StandardsScopeChoice,
)
from organizer.models import FileMetadata
from organizer.scope import is_protected_context_path


@dataclass(frozen=True)
class _ScopeBinding:
    session_id: str
    root_identity: str
    scan_generation: str
    expected_revision: int
    context_generation: str
    expires_at: datetime
    kind: str
    label: str
    relative_path: str | None


@dataclass(frozen=True)
class _CreationContextBinding:
    generation: str
    scope_ids: frozenset[str]
    expires_at: datetime


@dataclass(frozen=True)
class _PreviewBinding:
    session_id: str
    root_identity: str
    scan_generation: str
    expected_revision: int
    application_preview_id: str


class StandardsCreationStore:
    """Own opaque, expiring browser references for guided Standards creation."""

    def __init__(
        self,
        root: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = root.resolve(strict=True)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._scopes: dict[str, _ScopeBinding] = {}
        self._session_contexts: dict[str, _CreationContextBinding] = {}
        self._previews: dict[str, _PreviewBinding] = {}

    def issue_creation_context(
        self,
        *,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
        metadata_items: tuple[FileMetadata, ...],
    ) -> StandardsCreationContext:
        issued_at = _aware(self._now())
        expires_at = issued_at + timedelta(seconds=PREVIEW_LIFETIME_SECONDS)
        context_generation = _opaque_id()
        prepared: list[tuple[StandardsScopeChoice, _ScopeBinding]] = []
        prepared.append(
            (
                StandardsScopeChoice(_opaque_id(), "Selected folder", "root"),
                _ScopeBinding(
                    session_id=session_id,
                    root_identity=root_identity,
                    scan_generation=scan_generation,
                    expected_revision=expected_revision,
                    context_generation=context_generation,
                    expires_at=expires_at,
                    kind="root",
                    label="Selected folder",
                    relative_path=None,
                ),
            )
        )
        for item in sorted(metadata_items, key=lambda value: value.relative_path.as_posix()):
            if not _safe_folder(item, self._root):
                continue
            relative = item.relative_path.as_posix()
            label = " › ".join(item.relative_path.parts)
            prepared.append(
                (
                    StandardsScopeChoice(_opaque_id(), label, "folder"),
                    _ScopeBinding(
                        session_id=session_id,
                        root_identity=root_identity,
                        scan_generation=scan_generation,
                        expected_revision=expected_revision,
                        context_generation=context_generation,
                        expires_at=expires_at,
                        kind="folder",
                        label=label,
                        relative_path=relative,
                    ),
                )
            )
        scope_ids = frozenset(choice.id for choice, _ in prepared)
        context = _CreationContextBinding(
            generation=context_generation,
            scope_ids=scope_ids,
            expires_at=expires_at,
        )
        with self._lock:
            previous = self._session_contexts.get(session_id)
            self._scopes.update({choice.id: binding for choice, binding in prepared})
            self._session_contexts[session_id] = context
            if previous is not None:
                for stale_id in previous.scope_ids:
                    self._scopes.pop(stale_id, None)
        return StandardsCreationContext(
            scope_choices=tuple(choice for choice, _ in prepared),
            expires_at=expires_at.isoformat(),
            revision=expected_revision,
        )

    def validate_scope_reference(
        self,
        *,
        scope_id: str,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
    ) -> None:
        self._binding_for(
            scope_id=scope_id,
            session_id=session_id,
            root_identity=root_identity,
            scan_generation=scan_generation,
            expected_revision=expected_revision,
        )

    def resolve_scope(
        self,
        *,
        scope_id: str,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
        metadata_items: tuple[FileMetadata, ...],
    ) -> ResolvedCreationScope:
        binding = self._binding_for(
            scope_id=scope_id,
            session_id=session_id,
            root_identity=root_identity,
            scan_generation=scan_generation,
            expected_revision=expected_revision,
        )
        if binding.kind == "root":
            return ResolvedCreationScope("root", None, binding.label)
        folder = next(
            (
                item
                for item in metadata_items
                if item.is_dir and item.relative_path.as_posix() == binding.relative_path
            ),
            None,
        )
        if folder is None or not _safe_folder(folder, self._root):
            raise StandardsApplicationError("folder_scope_stale")
        return ResolvedCreationScope("folder", folder, binding.label)

    def prepare_preview_token(
        self,
        *,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
        application_preview_id: str,
    ) -> PreparedStandardsPreviewToken:
        return PreparedStandardsPreviewToken(
            token=_opaque_id(),
            session_id=session_id,
            root_identity=root_identity,
            scan_generation=scan_generation,
            expected_revision=expected_revision,
            application_preview_id=application_preview_id,
        )

    def commit_preview_token(self, prepared: PreparedStandardsPreviewToken) -> None:
        binding = _PreviewBinding(
            session_id=prepared.session_id,
            root_identity=prepared.root_identity,
            scan_generation=prepared.scan_generation,
            expected_revision=prepared.expected_revision,
            application_preview_id=prepared.application_preview_id,
        )
        with self._lock:
            if prepared.token in self._previews:
                raise RuntimeError("Standards preview token collision")
            self._previews[prepared.token] = binding

    def rollback_preview_token(self, prepared: PreparedStandardsPreviewToken) -> None:
        expected = _PreviewBinding(
            session_id=prepared.session_id,
            root_identity=prepared.root_identity,
            scan_generation=prepared.scan_generation,
            expected_revision=prepared.expected_revision,
            application_preview_id=prepared.application_preview_id,
        )
        with self._lock:
            if self._previews.get(prepared.token) == expected:
                self._previews.pop(prepared.token, None)

    def issue_preview_token(
        self,
        *,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
        application_preview_id: str,
    ) -> str:
        prepared = self.prepare_preview_token(
            session_id=session_id,
            root_identity=root_identity,
            scan_generation=scan_generation,
            expected_revision=expected_revision,
            application_preview_id=application_preview_id,
        )
        self.commit_preview_token(prepared)
        return prepared.token

    def resolve_preview_token(
        self,
        *,
        token: str,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
    ) -> str:
        with self._lock:
            binding = self._previews.get(token)
        if (
            binding is None
            or binding.session_id != session_id
            or binding.root_identity != root_identity
            or binding.scan_generation != scan_generation
        ):
            raise StandardsApplicationError("preview_expired")
        if binding.expected_revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
        return binding.application_preview_id

    def _binding_for(
        self,
        *,
        scope_id: str,
        session_id: str,
        root_identity: str,
        scan_generation: str,
        expected_revision: int,
    ) -> _ScopeBinding:
        now = _aware(self._now())
        with self._lock:
            binding = self._scopes.get(scope_id)
            context = self._session_contexts.get(session_id)
        if (
            binding is None
            or context is None
            or binding.session_id != session_id
            or binding.root_identity != root_identity
            or binding.scan_generation != scan_generation
            or binding.context_generation != context.generation
            or scope_id not in context.scope_ids
            or now >= binding.expires_at
            or now >= context.expires_at
        ):
            raise StandardsApplicationError("folder_scope_stale")
        if binding.expected_revision != expected_revision:
            raise StandardsApplicationError("standard_revision_mismatch")
        return binding


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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Standards clock must be timezone-aware")
    return value
