from __future__ import annotations

from threading import Lock

from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
    RecoveryAdmissionSnapshot,
    RecoveryAdmissionState,
)


class CurrentRecoveryAdmissionStatus:
    """Application-owned coarse recovery status refreshed from durable state."""

    def __init__(self, inspector: FilesystemOperationInspector) -> None:
        if not isinstance(inspector, FilesystemOperationInspector):
            raise ValueError("recovery status inspector is invalid")
        self._inspector = inspector
        self._lock = Lock()
        self._snapshot = self._inspect_fail_closed()

    def snapshot(self) -> RecoveryAdmissionSnapshot:
        with self._lock:
            return self._snapshot

    def refresh(self) -> RecoveryAdmissionSnapshot:
        candidate = self._inspect_fail_closed()
        with self._lock:
            self._snapshot = candidate
            return candidate

    def _inspect_fail_closed(self) -> RecoveryAdmissionSnapshot:
        try:
            snapshot = self._inspector.inspect()
        except Exception:
            return RecoveryAdmissionSnapshot(
                RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE
            )
        if not isinstance(snapshot, RecoveryAdmissionSnapshot):
            return RecoveryAdmissionSnapshot(
                RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE
            )
        return snapshot
