from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from organizer.application.activity_service import (
    calculate_undo_eligibility,
    list_activity_entries,
)
from organizer.application.current_recovery_admission_status import (
    CurrentRecoveryAdmissionStatus,
)
from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationInspector,
)
from organizer.application.organization_identity_execution import (
    OrganizationIdentityExecutionService,
)
from organizer.application.organization_movement_service import (
    execute_organization_movement,
    preview_organization_movement,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_identity_authorization import (
    V2UndoAuthorizationStore,
)
from organizer.application.organization_undo_identity_orchestration import (
    OrganizationIdentityUndoOrchestrationService,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.undo_service import (
    preview_organization_undo,
    undo_organization_operation,
)
from tests.test_application_organization_movement_service import _approved_plan


class V2ProductionCutoverTests(unittest.TestCase):
    def test_live_forward_activity_and_undo_complete_v2_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("alpha.txt", "beta.txt"))
            owners = _owners(root)
            preview = preview_organization_movement(
                approved_plan=plan,
                browser_session_id="session",
                current_scan_generation=plan.scan_generation,
                current_decision_revision=plan.decision_revision,
                coordinator=owners.coordinator,
                operation_repository=owners.operations,
                filesystem_operation_admission=owners.admission,
            )
            movement = execute_organization_movement(
                approved_plan=plan,
                browser_session_id="session",
                current_scan_generation=plan.scan_generation,
                current_decision_revision=plan.decision_revision,
                confirmation_token=preview.confirmation_token,
                confirmation=preview.confirmation,
                coordinator=owners.coordinator,
                operation_repository=owners.operations,
                filesystem_operation_admission=owners.admission,
                identity_execution_service=owners.movement,
                recovery_status=owners.status,
            )

            self.assertEqual(movement.status, "verified")
            parent = owners.operations.load(movement.operation_id)
            self.assertEqual(parent.schema_version, 2)
            self.assertTrue(
                all(item.operation == "move" for item in parent.expected_moves)
            )
            self.assertFalse(owners.status.snapshot().is_blocked)
            owners.operations.save(replace(parent, legacy_operation_log=None))

            eligibility = calculate_undo_eligibility(
                operation_repository=owners.operations,
                undo_repository=owners.undos,
            )
            self.assertTrue(eligibility.undoable)
            self.assertIsNone(eligibility.legacy_log_path)
            activity = list_activity_entries(
                operation_repository=owners.operations,
                undo_repository=owners.undos,
            )
            self.assertEqual(len(activity.activity), 1)
            self.assertTrue(activity.activity[0].undoable)

            undo_preview = preview_organization_undo(
                operation_id=parent.operation_id,
                browser_session_id="session",
                coordinator=owners.coordinator,
                operation_repository=owners.operations,
                undo_repository=owners.undos,
                filesystem_operation_admission=owners.admission,
                identity_orchestration_service=owners.undo,
            )
            outcome = undo_organization_operation(
                operation_id=parent.operation_id,
                browser_session_id="session",
                confirmation_token=undo_preview.confirmation_token,
                confirmation=undo_preview.confirmation_required,
                coordinator=owners.coordinator,
                operation_repository=owners.operations,
                undo_repository=owners.undos,
                filesystem_operation_admission=owners.admission,
                identity_orchestration_service=owners.undo,
                recovery_status=owners.status,
            )

            self.assertEqual(outcome.status, "restored")
            self.assertEqual(owners.undos.load(outcome.undo_id).schema_version, 2)
            self.assertFalse(owners.status.snapshot().is_blocked)
            self.assertTrue((root / "alpha.txt").is_file())
            self.assertFalse((root / "Organized/alpha.txt").exists())
            self.assertFalse(
                calculate_undo_eligibility(
                    operation_repository=owners.operations,
                    undo_repository=owners.undos,
                ).undoable
            )
            final_activity = list_activity_entries(
                operation_repository=owners.operations,
                undo_repository=owners.undos,
            )
            self.assertEqual(len(final_activity.activity), 2)

    def test_indeterminate_forward_verification_updates_live_attention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("alpha.txt",))
            owners = _owners(root)
            movement = OrganizationIdentityExecutionService(
                owners.operations,
                consume_authorization=owners.coordinator.consume,
                publish_verification=mock.Mock(side_effect=OSError("private")),
                refresh_recovery_status=owners.status.refresh,
            )
            preview = preview_organization_movement(
                approved_plan=plan,
                browser_session_id="session",
                current_scan_generation=plan.scan_generation,
                current_decision_revision=plan.decision_revision,
                coordinator=owners.coordinator,
                operation_repository=owners.operations,
                filesystem_operation_admission=owners.admission,
            )
            result = execute_organization_movement(
                approved_plan=plan,
                browser_session_id="session",
                current_scan_generation=plan.scan_generation,
                current_decision_revision=plan.decision_revision,
                confirmation_token=preview.confirmation_token,
                confirmation=preview.confirmation,
                coordinator=owners.coordinator,
                operation_repository=owners.operations,
                filesystem_operation_admission=owners.admission,
                identity_execution_service=movement,
                recovery_status=owners.status,
            )
            self.assertEqual(result.status, "moved_unverified")
            self.assertTrue(owners.status.snapshot().is_blocked)


class _Owners:
    pass


def _owners(root: Path) -> _Owners:
    owners = _Owners()
    owners.operations = OrganizationOperationRepository(root)
    owners.undos = OrganizationUndoRepository(root)
    owners.coordinator = OrganizationExecutionCoordinator()
    owners.inspector = FilesystemOperationInspector(
        root,
        organization_operations=owners.operations,
        organization_undos=owners.undos,
    )
    owners.admission = FilesystemOperationAdmission(owners.inspector)
    owners.status = CurrentRecoveryAdmissionStatus(owners.inspector)
    owners.movement = OrganizationIdentityExecutionService(
        owners.operations,
        consume_authorization=owners.coordinator.consume,
        refresh_recovery_status=owners.status.refresh,
    )
    store = V2UndoAuthorizationStore(
        owners.operations.configured_root_identity
    )
    owners.undo = OrganizationIdentityUndoOrchestrationService(
        owners.operations,
        owners.undos,
        store,
        refresh_recovery_status=owners.status.refresh,
    )
    return owners


if __name__ == "__main__":
    unittest.main()
