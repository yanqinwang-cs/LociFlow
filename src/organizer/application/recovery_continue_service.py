from __future__ import annotations

from dataclasses import dataclass

from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
)
from organizer.application.recovery_continue_context import (
    RECOVERY_CONTINUE_LABEL,
    RecoveryContinueContextError,
    RecoveryContinueContextStore,
)
from organizer.application.recovery_continue_eligibility import (
    RecoveryContinueEligibilityInspector,
    RecoveryContinueEligibilityState,
)
from organizer.application.runtime_lifecycle import (
    RuntimeLifecycle,
    RuntimeOperationKind,
)


class RecoveryContinueError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Continue is not available.")


@dataclass(frozen=True)
class RecoveryContinueContextResponse:
    case_id: str
    action_id: str
    expires_at: str
    label: str = RECOVERY_CONTINUE_LABEL

    def to_dict(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "action_id": self.action_id,
            "expires_at": self.expires_at,
            "label": self.label,
        }


class RecoveryContinueContextService:
    """Issue disconnected Continue authority from a fresh eligibility proof."""

    def __init__(
        self,
        runtime_lifecycle: RuntimeLifecycle,
        coordinator: OrganizationExecutionCoordinator,
        eligibility: RecoveryContinueEligibilityInspector,
        contexts: RecoveryContinueContextStore,
    ) -> None:
        if coordinator.runtime_lifecycle is not runtime_lifecycle:
            raise ValueError("Continue lifecycle ownership is invalid")
        if eligibility.configured_root_identity != contexts.configured_root_identity:
            raise ValueError("Continue root ownership is invalid")
        self._runtime_lifecycle = runtime_lifecycle
        self._coordinator = coordinator
        self._eligibility = eligibility
        self._contexts = contexts

    def create_context(
        self,
        *,
        browser_session_id: str,
    ) -> RecoveryContinueContextResponse:
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
            if not self._coordinator.try_acquire():
                raise RecoveryContinueError()
            try:
                result = self._eligibility.inspect()
                if result.state is not RecoveryContinueEligibilityState.ELIGIBLE:
                    raise RecoveryContinueError()
                try:
                    context = self._contexts.issue(
                        browser_session_id=browser_session_id,
                        target_family=result.family,
                        operation_id=result.operation_id,
                        parent_operation_id=result.parent_operation_id,
                        durable_record_digest=result.durable_record_digest,
                        completed_subset_digest=result.completed_subset_digest,
                        pending_subset_digest=result.pending_subset_digest,
                        filesystem_observation_digest=(
                            result.filesystem_observation_digest
                        ),
                    )
                except (RecoveryContinueContextError, ValueError) as error:
                    raise RecoveryContinueError() from error
            finally:
                self._coordinator.release()
        return RecoveryContinueContextResponse(
            context.case_id,
            context.action_id,
            context.expires_at,
        )
