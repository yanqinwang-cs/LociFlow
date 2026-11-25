from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock

from organizer.application.organization_decision_models import ApprovedOrganizationPlan
from organizer.application.organization_operation_models import (
    AUTHORIZATION_AVAILABLE,
    AUTHORIZATION_CONSUMED,
    AUTHORIZATION_INVALIDATED,
    MovementAuthorization,
)
from organizer.application.organization_undo_models import (
    UNDO_AUTHORIZATION_AVAILABLE,
    UNDO_AUTHORIZATION_CONSUMED,
    UndoAuthorization,
)
from organizer.application.runtime_lifecycle import (
    OperationLease,
    RuntimeLifecycle,
    RuntimeOperationKind,
)


class OrganizationExecutionCoordinator:
    """Own one process-wide execution lock and ephemeral authorizations."""

    def __init__(self, runtime_lifecycle: RuntimeLifecycle | None = None) -> None:
        self.runtime_lifecycle = runtime_lifecycle or RuntimeLifecycle()
        self._execution_lock = Lock()
        self._execution_lease: OperationLease | None = None
        self._authorization_lock = Lock()
        self._authorizations: dict[str, MovementAuthorization] = {}
        self._undo_authorizations: dict[str, UndoAuthorization] = {}

    def issue_authorization(
        self,
        approved_plan: ApprovedOrganizationPlan,
        *,
        browser_session_id: str | None = None,
        configured_root_identity: str | None = None,
        issued_at: str | None = None,
    ) -> MovementAuthorization:
        authorization = MovementAuthorization(
            authorization_id=str(uuid.uuid4()),
            scan_generation=approved_plan.scan_generation,
            decision_revision=approved_plan.decision_revision,
            approved_at=approved_plan.approved_at,
            approved_plan_identity=approved_plan_identity(approved_plan),
            issued_at=issued_at or _utc_timestamp(),
            consumed_at=None,
            status=AUTHORIZATION_AVAILABLE,
            browser_session_id=browser_session_id,
            configured_root_identity=configured_root_identity,
        )
        with self._authorization_lock:
            self._authorizations[authorization.authorization_id] = authorization
        return authorization

    def try_acquire(
        self,
        operation_kind: RuntimeOperationKind | None = None,
    ) -> bool:
        lease = (
            self.runtime_lifecycle.begin_operation(operation_kind)
            if operation_kind is not None
            else None
        )
        acquired = self._execution_lock.acquire(blocking=False)
        if not acquired:
            if lease is not None:
                lease.finish()
            return False
        self._execution_lease = lease
        return True

    def release(self) -> None:
        lease = self._execution_lease
        self._execution_lease = None
        try:
            self._execution_lock.release()
        finally:
            if lease is not None:
                lease.finish()

    def issue_undo_authorization(
        self,
        *,
        browser_session_id: str,
        configured_root_identity: str,
        operation_id: str,
        issued_at: str | None = None,
    ) -> UndoAuthorization:
        authorization = UndoAuthorization(
            authorization_id=str(uuid.uuid4()),
            browser_session_id=browser_session_id,
            configured_root_identity=configured_root_identity,
            operation_id=operation_id,
            issued_at=issued_at or _utc_timestamp(),
            consumed_at=None,
            status=UNDO_AUTHORIZATION_AVAILABLE,
        )
        with self._authorization_lock:
            self._undo_authorizations[authorization.authorization_id] = authorization
        return authorization

    def undo_authorization_snapshot(
        self,
        authorization_id: str,
    ) -> UndoAuthorization | None:
        with self._authorization_lock:
            return self._undo_authorizations.get(authorization_id)

    def consume_undo_authorization(
        self,
        authorization_id: str,
        *,
        consumed_at: str | None = None,
    ) -> bool:
        with self._authorization_lock:
            current = self._undo_authorizations.get(authorization_id)
            if current is None or current.status != UNDO_AUTHORIZATION_AVAILABLE:
                return False
            self._undo_authorizations[authorization_id] = replace(
                current,
                status=UNDO_AUTHORIZATION_CONSUMED,
                consumed_at=consumed_at or _utc_timestamp(),
            )
            return True

    def authorization_snapshot(self, authorization_id: str) -> MovementAuthorization | None:
        with self._authorization_lock:
            return self._authorizations.get(authorization_id)

    def has_available_movement_authorization(
        self,
        configured_root_identity: str,
    ) -> bool:
        with self._authorization_lock:
            return any(
                authorization.status == AUTHORIZATION_AVAILABLE
                and authorization.configured_root_identity == configured_root_identity
                for authorization in self._authorizations.values()
            )

    def consume(self, authorization_id: str, *, consumed_at: str | None = None) -> bool:
        with self._authorization_lock:
            current = self._authorizations.get(authorization_id)
            if current is None or current.status != AUTHORIZATION_AVAILABLE:
                return False
            self._authorizations[authorization_id] = replace(
                current,
                status=AUTHORIZATION_CONSUMED,
                consumed_at=consumed_at or _utc_timestamp(),
            )
            return True

    def invalidate(self, authorization_id: str) -> bool:
        with self._authorization_lock:
            current = self._authorizations.get(authorization_id)
            if current is None or current.status != AUTHORIZATION_AVAILABLE:
                return False
            self._authorizations[authorization_id] = replace(
                current,
                status=AUTHORIZATION_INVALIDATED,
            )
            return True


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def approved_plan_identity(approved_plan: ApprovedOrganizationPlan) -> str:
    payload = {
        "status": approved_plan.status,
        "scan_generation": approved_plan.scan_generation,
        "decision_revision": approved_plan.decision_revision,
        "approved_at": approved_plan.approved_at,
        "reviewed_move_items": [
            {
                "id": item.id,
                "category": item.category,
                "decision": item.decision,
                "source": str(item.plan_item.source.resolve(strict=False)),
                "destination": str(item.plan_item.destination.resolve(strict=False)),
                "reason": item.plan_item.reason,
                "confidence": item.plan_item.confidence,
                "operation": item.plan_item.operation,
                "overwrite_risk": item.plan_item.overwrite_risk,
            }
            for item in approved_plan.reviewed_move_items
        ],
        "source_fingerprints": [
            {
                "item_id": item.item_id,
                "relative_path": item.relative_path.as_posix(),
                "size_bytes": item.size_bytes,
                "modified_time": item.modified_time,
            }
            for item in approved_plan.source_fingerprints
        ],
        "duplicate_decisions": [
            {
                "choice_id": item.choice_id,
                "option_id": item.option_id,
                "selected_copy_ids": list(item.selected_copy_ids),
                "selected_paths": [
                    str(path.resolve(strict=False)) for path in item.selected_paths
                ],
                "group_sha256": item.group_sha256,
                "group_size_bytes": item.group_size_bytes,
            }
            for item in approved_plan.duplicate_decisions
        ],
        "summary": approved_plan.summary.to_dict(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
