from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
)
from organizer.application.recovery_continue_context import (
    RecoveryContinueContextStore,
)
from organizer.application.recovery_continue_eligibility import (
    RecoveryContinueEligibilityInspector,
    RecoveryContinueEligibilityResult,
    RecoveryContinueEligibilityState,
)
from organizer.application.recovery_continue_service import (
    RecoveryContinueContextService,
    RecoveryContinueError,
)
from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from tests.test_application_recovery_continue_eligibility import (
    _movement_candidate,
)


class RecoveryContinueContextServiceTests(unittest.TestCase):
    def test_real_issuance_is_read_only_and_returns_only_bounded_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, record = _movement_candidate(root, "interrupted")
            before = _tree(root)
            lifecycle = RuntimeLifecycle()
            coordinator = OrganizationExecutionCoordinator(lifecycle)
            eligibility = RecoveryContinueEligibilityInspector(
                FilesystemOperationInspector(root)
            )
            contexts = RecoveryContinueContextStore(
                repository.configured_root_identity
            )
            service = RecoveryContinueContextService(
                lifecycle,
                coordinator,
                eligibility,
                contexts,
            )

            response = service.create_context(browser_session_id="session")

            self.assertEqual(response.label, "Continue")
            self.assertEqual(
                set(response.__dataclass_fields__),
                {"case_id", "action_id", "expires_at", "label"},
            )
            self.assertEqual(_tree(root), before)
            self.assertEqual(repository.load(record.operation_id), record)
            self.assertEqual(lifecycle.snapshot().active_count, 0)

    def test_issuance_holds_lifecycle_and_coordinator(self) -> None:
        lifecycle = RuntimeLifecycle()
        coordinator = OrganizationExecutionCoordinator(lifecycle)
        eligibility = mock.Mock()
        eligibility.configured_root_identity = "a" * 64

        def inspect():
            self.assertEqual(
                lifecycle.snapshot().active_kinds,
                (RuntimeOperationKind.STATE_MUTATION,),
            )
            self.assertFalse(coordinator.try_acquire())
            return RecoveryContinueEligibilityResult(
                RecoveryContinueEligibilityState.INELIGIBLE
            )

        eligibility.inspect.side_effect = inspect
        service = RecoveryContinueContextService(
            lifecycle,
            coordinator,
            eligibility,
            RecoveryContinueContextStore("a" * 64),
        )
        with self.assertRaises(RecoveryContinueError):
            service.create_context(browser_session_id="session")
        self.assertEqual(lifecycle.snapshot().active_count, 0)

    def test_ineligible_busy_and_shutdown_issue_no_context(self) -> None:
        for state in (
            RecoveryContinueEligibilityState.INELIGIBLE,
            RecoveryContinueEligibilityState.UNAVAILABLE,
        ):
            with self.subTest(state=state):
                lifecycle = RuntimeLifecycle()
                coordinator = OrganizationExecutionCoordinator(lifecycle)
                eligibility = mock.Mock()
                eligibility.configured_root_identity = "a" * 64
                eligibility.inspect.return_value = RecoveryContinueEligibilityResult(
                    state
                )
                contexts = RecoveryContinueContextStore("a" * 64)
                service = RecoveryContinueContextService(
                    lifecycle,
                    coordinator,
                    eligibility,
                    contexts,
                )
                with self.assertRaises(RecoveryContinueError):
                    service.create_context(browser_session_id="session")
                self.assertEqual(contexts._contexts, {})

        lifecycle = RuntimeLifecycle()
        coordinator = OrganizationExecutionCoordinator(lifecycle)
        eligibility = mock.Mock()
        eligibility.configured_root_identity = "a" * 64
        contexts = RecoveryContinueContextStore("a" * 64)
        service = RecoveryContinueContextService(
            lifecycle,
            coordinator,
            eligibility,
            contexts,
        )
        self.assertTrue(coordinator.try_acquire())
        try:
            with self.assertRaises(RecoveryContinueError):
                service.create_context(browser_session_id="session")
        finally:
            coordinator.release()
        eligibility.inspect.assert_not_called()
        self.assertEqual(contexts._contexts, {})

        lifecycle.begin_shutdown()
        with self.assertRaises(ApplicationStoppingError):
            service.create_context(browser_session_id="session")
        self.assertEqual(contexts._contexts, {})


def _tree(root: Path) -> tuple[tuple[str, bytes | None], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                path.read_bytes() if path.is_file() else None,
            )
            for path in root.rglob("*")
        )
    )


if __name__ == "__main__":
    unittest.main()
