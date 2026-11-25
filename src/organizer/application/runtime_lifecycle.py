from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Condition

APPLICATION_STOPPING_MESSAGE = "Loci Flow is closing. No new work can start."


class ApplicationStoppingError(RuntimeError):
    """Raised when new work is requested after shutdown has begun."""

    def __init__(self) -> None:
        super().__init__(APPLICATION_STOPPING_MESSAGE)


class RuntimeOperationKind(StrEnum):
    SCAN = "scan"
    STATE_MUTATION = "state_mutation"
    ORGANIZATION_MOVEMENT = "organization_movement"
    DUPLICATE_BIN_MOVEMENT = "duplicate_bin_movement"
    DUPLICATE_BIN_RESTORE = "duplicate_bin_restore"
    ACTIVITY_UNDO = "activity_undo"


@dataclass(frozen=True)
class RuntimeSnapshot:
    is_stopping: bool
    active_count: int
    active_kinds: tuple[RuntimeOperationKind, ...]


class OperationLease:
    """One exactly-once registration owned by authoritative operation work."""

    def __init__(self, lifecycle: RuntimeLifecycle, registration_id: int) -> None:
        self._lifecycle = lifecycle
        self._registration_id = registration_id

    def finish(self) -> bool:
        """Finish once; later calls are harmless and never decrement twice."""

        return self._lifecycle._finish(self._registration_id)

    def __enter__(self) -> OperationLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        del exc_type, exc_value, traceback
        self.finish()
        return False


class RuntimeLifecycle:
    """Own atomic operation admission and monotonic process shutdown state."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._is_stopping = False
        self._next_registration_id = 0
        self._active: dict[int, RuntimeOperationKind] = {}

    @property
    def is_stopping(self) -> bool:
        return self.snapshot().is_stopping

    def snapshot(self) -> RuntimeSnapshot:
        with self._condition:
            return self._snapshot_locked()

    def begin_operation(self, kind: RuntimeOperationKind) -> OperationLease:
        if not isinstance(kind, RuntimeOperationKind):
            raise TypeError("runtime operation kind is invalid")
        with self._condition:
            if self._is_stopping:
                raise ApplicationStoppingError()
            self._next_registration_id += 1
            registration_id = self._next_registration_id
            self._active[registration_id] = kind
            return OperationLease(self, registration_id)

    def begin_shutdown(self) -> RuntimeSnapshot:
        with self._condition:
            self._is_stopping = True
            return self._snapshot_locked()

    def wait_until_idle(self) -> RuntimeSnapshot:
        with self._condition:
            self._condition.wait_for(lambda: not self._active)
            return self._snapshot_locked()

    def _finish(self, registration_id: int) -> bool:
        with self._condition:
            if registration_id not in self._active:
                return False
            del self._active[registration_id]
            self._condition.notify_all()
            return True

    def _snapshot_locked(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            is_stopping=self._is_stopping,
            active_count=len(self._active),
            active_kinds=tuple(
                sorted(set(self._active.values()), key=lambda kind: kind.value)
            ),
        )
