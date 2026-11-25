from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Lock

from organizer.application.organization_operation_models import (
    OrganizationOperationRecord,
)
from organizer.application.organization_undo_identity_eligibility import (
    IdentityUndoEligibility,
)

IDENTITY_V2_UNDO_MODE = "identity_v2"
V2_UNDO_AUTHORIZATION_AVAILABLE = "available"
V2_UNDO_AUTHORIZATION_CONSUMED = "consumed"
V2_UNDO_AUTHORIZATION_INVALIDATED = "invalidated"
V2_UNDO_AUTHORIZATION_EXPIRED = "expired"

_DIGEST = re.compile(r"[0-9a-f]{64}")
_ROOT_IDENTITY = re.compile(r"[0-9a-f]{64}")
_STATES = {
    V2_UNDO_AUTHORIZATION_AVAILABLE,
    V2_UNDO_AUTHORIZATION_CONSUMED,
    V2_UNDO_AUTHORIZATION_INVALIDATED,
    V2_UNDO_AUTHORIZATION_EXPIRED,
}


class V2UndoAuthorizationError(RuntimeError):
    _CODES = {"not_found", "unavailable", "expired", "binding_mismatch"}

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("v2 Undo authorization error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class V2UndoAuthorization:
    authorization_id: str
    browser_session_id: str
    configured_root_identity: str
    parent_operation_id: str
    parent_record_digest: str
    reverse_evidence_digest: str
    mode: str
    issued_at: str
    expires_at: str
    state: str
    consumed_at: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.authorization_id)
        _uuid(self.parent_operation_id)
        if not isinstance(self.browser_session_id, str) or not self.browser_session_id:
            raise ValueError("v2 Undo authorization session is invalid")
        if (
            not isinstance(self.configured_root_identity, str)
            or _ROOT_IDENTITY.fullmatch(self.configured_root_identity) is None
        ):
            raise ValueError("v2 Undo authorization root is invalid")
        for digest in (self.parent_record_digest, self.reverse_evidence_digest):
            if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
                raise ValueError("v2 Undo authorization binding is invalid")
        if self.mode != IDENTITY_V2_UNDO_MODE:
            raise ValueError("v2 Undo authorization mode is invalid")
        issued = _timestamp(self.issued_at)
        expires = _timestamp(self.expires_at)
        if expires <= issued:
            raise ValueError("v2 Undo authorization expiry is invalid")
        if self.state not in _STATES:
            raise ValueError("v2 Undo authorization state is invalid")
        if self.state == V2_UNDO_AUTHORIZATION_CONSUMED:
            if self.consumed_at is None:
                raise ValueError("v2 Undo authorization consumption is invalid")
            _timestamp(self.consumed_at)
        elif self.consumed_at is not None:
            raise ValueError("v2 Undo authorization consumption is invalid")

    def __repr__(self) -> str:
        return f"V2UndoAuthorization(state={self.state!r}, mode={self.mode!r})"


class V2UndoAuthorizationStore:
    """Root-scoped, short-lived, one-use identity-v2 Undo authorizations."""

    def __init__(
        self,
        configured_root_identity: str,
        *,
        now: Callable[[], datetime] | None = None,
        id_source: Callable[[], str] | None = None,
        lifetime: timedelta = timedelta(minutes=5),
        capacity: int = 256,
    ) -> None:
        if (
            not isinstance(configured_root_identity, str)
            or _ROOT_IDENTITY.fullmatch(configured_root_identity) is None
        ):
            raise ValueError("v2 Undo authorization root is invalid")
        if not isinstance(lifetime, timedelta) or lifetime <= timedelta(0):
            raise ValueError("v2 Undo authorization lifetime is invalid")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("v2 Undo authorization capacity is invalid")
        self.configured_root_identity = configured_root_identity
        self._now = now or (lambda: datetime.now(UTC))
        self._id_source = id_source or (lambda: str(uuid.uuid4()))
        self._lifetime = lifetime
        self._capacity = capacity
        self._lock = Lock()
        self._authorizations: dict[str, V2UndoAuthorization] = {}

    def issue(
        self,
        *,
        browser_session_id: str,
        parent_operation_id: str,
        parent_record_digest: str,
        reverse_evidence_digest: str,
    ) -> V2UndoAuthorization:
        issued = _aware(self._now())
        authorization = V2UndoAuthorization(
            authorization_id=self._id_source(),
            browser_session_id=browser_session_id,
            configured_root_identity=self.configured_root_identity,
            parent_operation_id=parent_operation_id,
            parent_record_digest=parent_record_digest,
            reverse_evidence_digest=reverse_evidence_digest,
            mode=IDENTITY_V2_UNDO_MODE,
            issued_at=issued.isoformat(),
            expires_at=(issued + self._lifetime).isoformat(),
            state=V2_UNDO_AUTHORIZATION_AVAILABLE,
        )
        with self._lock:
            self._prune_locked(issued)
            if len(self._authorizations) >= self._capacity:
                raise V2UndoAuthorizationError("unavailable")
            if authorization.authorization_id in self._authorizations:
                raise V2UndoAuthorizationError("unavailable")
            self._authorizations[authorization.authorization_id] = authorization
        return authorization

    def require_available(
        self,
        authorization_id: str,
        *,
        browser_session_id: str,
        parent_operation_id: str,
        mode: str = IDENTITY_V2_UNDO_MODE,
    ) -> V2UndoAuthorization:
        now = _aware(self._now())
        with self._lock:
            current = self._authorizations.get(authorization_id)
            if current is None:
                raise V2UndoAuthorizationError("not_found")
            if current.state != V2_UNDO_AUTHORIZATION_AVAILABLE:
                raise V2UndoAuthorizationError("unavailable")
            if now >= _timestamp(current.expires_at):
                self._authorizations[authorization_id] = replace(
                    current, state=V2_UNDO_AUTHORIZATION_EXPIRED
                )
                raise V2UndoAuthorizationError("expired")
            if (
                current.browser_session_id != browser_session_id
                or current.configured_root_identity != self.configured_root_identity
                or current.parent_operation_id != parent_operation_id
                or current.mode != mode
            ):
                raise V2UndoAuthorizationError("binding_mismatch")
            return current

    def consume(self, authorization_id: str) -> bool:
        now = _aware(self._now())
        with self._lock:
            current = self._authorizations.get(authorization_id)
            if (
                current is None
                or current.state != V2_UNDO_AUTHORIZATION_AVAILABLE
                or now >= _timestamp(current.expires_at)
            ):
                if (
                    current is not None
                    and current.state == V2_UNDO_AUTHORIZATION_AVAILABLE
                ):
                    self._authorizations[authorization_id] = replace(
                        current, state=V2_UNDO_AUTHORIZATION_EXPIRED
                    )
                return False
            self._authorizations[authorization_id] = replace(
                current,
                state=V2_UNDO_AUTHORIZATION_CONSUMED,
                consumed_at=now.isoformat(),
            )
            return True

    def invalidate(self, authorization_id: str) -> bool:
        with self._lock:
            current = self._authorizations.get(authorization_id)
            if current is None or current.state != V2_UNDO_AUTHORIZATION_AVAILABLE:
                return False
            self._authorizations[authorization_id] = replace(
                current, state=V2_UNDO_AUTHORIZATION_INVALIDATED
            )
            return True

    def snapshot(self, authorization_id: str) -> V2UndoAuthorization | None:
        with self._lock:
            return self._authorizations.get(authorization_id)

    def _prune_locked(self, now: datetime) -> None:
        expired = [
            key
            for key, value in self._authorizations.items()
            if value.state != V2_UNDO_AUTHORIZATION_AVAILABLE
            or now >= _timestamp(value.expires_at)
        ]
        for key in expired:
            self._authorizations.pop(key, None)


def canonical_parent_record_digest(record: OrganizationOperationRecord) -> str:
    if not isinstance(record, OrganizationOperationRecord):
        raise ValueError("v2 Undo parent binding is invalid")
    return _canonical_digest(asdict(record))


def canonical_reverse_evidence_digest(
    eligibility: IdentityUndoEligibility,
) -> str:
    if not isinstance(eligibility, IdentityUndoEligibility):
        raise ValueError("v2 Undo evidence binding is invalid")
    payload = {
        "state": eligibility.state.value,
        "reason": None if eligibility.reason is None else eligibility.reason.value,
        "volume_admission": (
            "same_volume" if eligibility.eligible else "not_admitted"
        ),
        "pairs": [
            {
                "source": str(pair.source_relative_path),
                "destination": str(pair.destination_relative_path),
            }
            for pair in eligibility.reverse_path_pairs
        ],
        "captures": [
            {
                "change_id": capture.change_id,
                "content_identity": asdict(capture.observed_content_identity),
                "stat_fingerprint": asdict(capture.stat_fingerprint),
            }
            for capture in eligibility.captures
        ],
    }
    return _canonical_digest(payload)


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("v2 Undo authorization ID is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ValueError("v2 Undo authorization ID is invalid") from error
    if str(parsed) != value:
        raise ValueError("v2 Undo authorization ID is invalid")
    return value


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("v2 Undo authorization clock is invalid")
    return value.astimezone(UTC)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("v2 Undo authorization timestamp is invalid") from error
    return _aware(parsed)
