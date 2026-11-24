from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import re
from threading import Lock
from typing import Callable
import uuid

from organizer.application.recovery_continue_eligibility import (
    RecoveryContinueFamily,
)


RECOVERY_CONTINUE_ACTION = "continue"
RECOVERY_CONTINUE_LABEL = "Continue"

_DIGEST = re.compile(r"[0-9a-f]{64}")
_ROOT_IDENTITY = re.compile(r"[0-9a-f]{64}")


class RecoveryContinueContextState(StrEnum):
    AVAILABLE = "available"
    CONSUMED = "consumed"


class RecoveryContinueContextError(RuntimeError):
    _CODES = {
        "binding_mismatch",
        "capacity_unavailable",
        "consumed",
        "expired",
        "not_found",
    }

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("Continue context error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class RecoveryContinueContext:
    case_id: str
    action_id: str
    browser_session_id: str
    configured_root_identity: str
    action_kind: str
    target_family: RecoveryContinueFamily
    operation_id: str
    parent_operation_id: str | None
    durable_record_digest: str
    completed_subset_digest: str
    pending_subset_digest: str
    filesystem_observation_digest: str
    issued_at: str
    expires_at: str
    state: RecoveryContinueContextState
    consumed_at: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.case_id)
        _uuid(self.action_id)
        _uuid(self.operation_id)
        if self.parent_operation_id is not None:
            _uuid(self.parent_operation_id)
        if not isinstance(self.browser_session_id, str) or not self.browser_session_id:
            raise ValueError("Continue session is invalid")
        if (
            not isinstance(self.configured_root_identity, str)
            or _ROOT_IDENTITY.fullmatch(self.configured_root_identity) is None
        ):
            raise ValueError("Continue root is invalid")
        if self.action_kind != RECOVERY_CONTINUE_ACTION:
            raise ValueError("Continue action is invalid")
        if not isinstance(self.target_family, RecoveryContinueFamily):
            raise ValueError("Continue family is invalid")
        if (
            self.target_family is RecoveryContinueFamily.ORGANIZATION_UNDO
            and self.parent_operation_id is None
        ) or (
            self.target_family is RecoveryContinueFamily.ORGANIZATION_MOVEMENT
            and self.parent_operation_id is not None
        ):
            raise ValueError("Continue parent binding is invalid")
        for digest in (
            self.durable_record_digest,
            self.completed_subset_digest,
            self.pending_subset_digest,
            self.filesystem_observation_digest,
        ):
            if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
                raise ValueError("Continue digest binding is invalid")
        issued = _timestamp(self.issued_at)
        expires = _timestamp(self.expires_at)
        if expires <= issued:
            raise ValueError("Continue expiry is invalid")
        if not isinstance(self.state, RecoveryContinueContextState):
            raise ValueError("Continue context state is invalid")
        if self.state is RecoveryContinueContextState.CONSUMED:
            if self.consumed_at is None:
                raise ValueError("Continue consumption is invalid")
            _timestamp(self.consumed_at)
        elif self.consumed_at is not None:
            raise ValueError("Continue consumption is invalid")

    def __repr__(self) -> str:
        return f"RecoveryContinueContext(state={self.state.value!r})"


class RecoveryContinueContextStore:
    """Root-scoped, bounded and one-use Continue contexts."""

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
            raise ValueError("Continue root is invalid")
        if not isinstance(lifetime, timedelta) or lifetime <= timedelta(0):
            raise ValueError("Continue lifetime is invalid")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("Continue capacity is invalid")
        self.configured_root_identity = configured_root_identity
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._id_source = id_source or (lambda: str(uuid.uuid4()))
        self._lifetime = lifetime
        self._capacity = capacity
        self._lock = Lock()
        self._contexts: dict[str, RecoveryContinueContext] = {}

    def issue(
        self,
        *,
        browser_session_id: str,
        target_family: RecoveryContinueFamily,
        operation_id: str,
        parent_operation_id: str | None,
        durable_record_digest: str,
        completed_subset_digest: str,
        pending_subset_digest: str,
        filesystem_observation_digest: str,
    ) -> RecoveryContinueContext:
        issued = _aware(self._now())
        context = RecoveryContinueContext(
            case_id=self._id_source(),
            action_id=self._id_source(),
            browser_session_id=browser_session_id,
            configured_root_identity=self.configured_root_identity,
            action_kind=RECOVERY_CONTINUE_ACTION,
            target_family=target_family,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            durable_record_digest=durable_record_digest,
            completed_subset_digest=completed_subset_digest,
            pending_subset_digest=pending_subset_digest,
            filesystem_observation_digest=filesystem_observation_digest,
            issued_at=issued.isoformat(),
            expires_at=(issued + self._lifetime).isoformat(),
            state=RecoveryContinueContextState.AVAILABLE,
        )
        with self._lock:
            self._prune_locked(issued)
            if (
                len(self._contexts) >= self._capacity
                or context.case_id in self._contexts
            ):
                raise RecoveryContinueContextError("capacity_unavailable")
            self._contexts[context.case_id] = context
        return context

    def consume_bound(
        self,
        *,
        case_id: str,
        action_id: str,
        browser_session_id: str,
        configured_root_identity: str,
        action_kind: str = RECOVERY_CONTINUE_ACTION,
    ) -> RecoveryContinueContext:
        now = _aware(self._now())
        with self._lock:
            current = self._contexts.get(case_id)
            if current is None:
                raise RecoveryContinueContextError("not_found")
            if (
                current.action_id != action_id
                or current.browser_session_id != browser_session_id
                or current.configured_root_identity != configured_root_identity
                or current.action_kind != action_kind
            ):
                raise RecoveryContinueContextError("binding_mismatch")
            if current.state is not RecoveryContinueContextState.AVAILABLE:
                raise RecoveryContinueContextError("consumed")
            consumed = replace(
                current,
                state=RecoveryContinueContextState.CONSUMED,
                consumed_at=now.isoformat(),
            )
            self._contexts[case_id] = consumed
            if now >= _timestamp(current.expires_at):
                raise RecoveryContinueContextError("expired")
            return consumed

    def snapshot(self, case_id: str) -> RecoveryContinueContext | None:
        with self._lock:
            return self._contexts.get(case_id)

    def _prune_locked(self, now: datetime) -> None:
        removable = [
            case_id
            for case_id, context in self._contexts.items()
            if context.state is RecoveryContinueContextState.CONSUMED
            or now >= _timestamp(context.expires_at)
        ]
        for case_id in removable:
            self._contexts.pop(case_id, None)


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Continue ID is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ValueError("Continue ID is invalid") from error
    if str(parsed) != value:
        raise ValueError("Continue ID is invalid")
    return value


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Continue clock is invalid")
    return value.astimezone(timezone.utc)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Continue timestamp is invalid") from error
    return _aware(parsed)
