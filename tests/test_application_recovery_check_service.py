from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from organizer.application.activity_service import list_activity_entries
from organizer.application.current_recovery_admission_status import (
    CurrentRecoveryAdmissionStatus,
)
from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
)
from organizer.application.identity_verification_publication import (
    OrganizationIdentityVerificationPublicationService,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_models import (
    UNDO_ITEM_RESTORED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_VERIFICATION_NOT_RUN,
    UndoVerificationResult,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.recovery_check_context import (
    RecoveryCheckContextState,
    RecoveryCheckContextStore,
)
from organizer.application.recovery_check_service import (
    RecoveryCheckEligibilityInspector,
    RecoveryCheckError,
    RecoveryCheckResultState,
    RecoveryCheckService,
)
from organizer.application.recovery_evidence import RecoveryEvidenceInspector
from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.application.undo_identity_verification_publication import (
    OrganizationUndoIdentityVerificationPublicationService,
)
from tests.test_application_filesystem_operation_admission import (
    _ordinary_operation,
    _ordinary_undo,
)
from tests.test_application_identity_verification_publication import (
    _operation,
)
from tests.test_application_recovery_evidence import (
    _duplicate_move_state,
    _ordinary_movement,
)
from tests.test_application_undo_identity_verification_publication import (
    _stored_pair,
)


class RecoveryCheckServiceTests(unittest.TestCase):
    def test_movement_context_and_passed_check_update_existing_row_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            before = list_activity_entries(
                operation_repository=owners.operations,
                undo_repository=owners.undos,
            )
            filesystem_before = _user_files(root)

            context = owners.service.create_context(browser_session_id="session")
            after_context = list_activity_entries(
                operation_repository=owners.operations,
                undo_repository=owners.undos,
            )
            with mock.patch.object(
                owners.movement_publication,
                "publish",
                wraps=owners.movement_publication.publish,
            ) as publish:
                result = owners.service.check_result(
                    browser_session_id="session",
                    case_id=context.case_id,
                    action_id=context.action_id,
                )
            after = list_activity_entries(
                operation_repository=owners.operations,
                undo_repository=owners.undos,
            )

            self.assertEqual(result.state, RecoveryCheckResultState.CONFIRMED)
            self.assertFalse(result.recovery_attention)
            self.assertEqual(
                owners.operations.load(owners.record.operation_id).status, "verified"
            )
            self.assertEqual(
                [item.id for item in before.activity],
                [item.id for item in after.activity],
            )
            self.assertEqual(before, after_context)
            self.assertEqual(_user_files(root), filesystem_before)
            publish.assert_called_once_with(owners.record.operation_id)

    def test_deterministic_movement_mismatch_remains_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            destination = root / owners.record.expected_moves[0].destination
            destination.unlink()

            context = owners.service.create_context(browser_session_id="session")
            result = owners.service.check_result(
                browser_session_id="session",
                case_id=context.case_id,
                action_id=context.action_id,
            )

            self.assertEqual(
                result.state,
                RecoveryCheckResultState.NOT_CONFIRMED,
            )
            self.assertFalse(result.recovery_attention)
            self.assertEqual(
                owners.operations.load(owners.record.operation_id).status,
                "verification_failed",
            )

    def test_indeterminate_movement_is_unchanged_and_remains_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(
                root,
                movement_publication=OrganizationIdentityVerificationPublicationService(
                    OrganizationOperationRepository(root),
                    verifier=mock.Mock(side_effect=OSError("private")),
                ),
            )
            context = owners.service.create_context(browser_session_id="session")

            result = owners.service.check_result(
                browser_session_id="session",
                case_id=context.case_id,
                action_id=context.action_id,
            )

            self.assertEqual(
                result.state,
                RecoveryCheckResultState.COULD_NOT_CHECK,
            )
            self.assertTrue(result.recovery_attention)
            self.assertEqual(
                owners.operations.load(owners.record.operation_id).status,
                "moved_unverified",
            )

    def test_changed_observation_rejects_and_consumes_without_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            before = list_activity_entries(
                operation_repository=owners.operations,
                undo_repository=owners.undos,
            )
            context = owners.service.create_context(browser_session_id="session")
            destination = root / owners.record.expected_moves[0].destination
            destination.write_bytes(b"changed")

            with self.assertRaises(RecoveryCheckError) as raised:
                owners.service.check_result(
                    browser_session_id="session",
                    case_id=context.case_id,
                    action_id=context.action_id,
                )

            self.assertEqual(
                raised.exception.code,
                "recovery_details_changed",
            )
            self.assertEqual(
                owners.contexts.snapshot(context.case_id).state,
                RecoveryCheckContextState.CONSUMED,
            )
            self.assertEqual(
                list_activity_entries(
                    operation_repository=owners.operations,
                    undo_repository=owners.undos,
                ),
                before,
            )
            self.assertTrue(owners.status.snapshot().is_blocked)

    def test_wrong_session_does_not_consume_or_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            context = owners.service.create_context(browser_session_id="session")
            with mock.patch.object(
                owners.status,
                "refresh",
                wraps=owners.status.refresh,
            ) as refresh:
                with self.assertRaises(RecoveryCheckError) as raised:
                    owners.service.check_result(
                        browser_session_id="other",
                        case_id=context.case_id,
                        action_id=context.action_id,
                    )
            self.assertEqual(raised.exception.code, "context_unavailable")
            self.assertEqual(
                owners.contexts.snapshot(context.case_id).state,
                RecoveryCheckContextState.AVAILABLE,
            )
            refresh.assert_not_called()

    def test_coordinator_rejection_precedes_consumption_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            context = owners.service.create_context(browser_session_id="session")
            self.assertTrue(owners.coordinator.try_acquire())
            try:
                with mock.patch.object(
                    owners.status,
                    "refresh",
                ) as refresh:
                    with self.assertRaises(RecoveryCheckError) as raised:
                        owners.service.check_result(
                            browser_session_id="session",
                            case_id=context.case_id,
                            action_id=context.action_id,
                        )
            finally:
                owners.coordinator.release()
            self.assertEqual(raised.exception.code, "check_unavailable")
            self.assertEqual(
                owners.contexts.snapshot(context.case_id).state,
                RecoveryCheckContextState.AVAILABLE,
            )
            refresh.assert_not_called()

    def test_record_change_and_new_unresolved_work_are_stale_and_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            context = owners.service.create_context(browser_session_id="session")
            owners.operations.save(replace(owners.record, scan_generation="changed"))
            with self.assertRaises(RecoveryCheckError) as raised:
                owners.service.check_result(
                    browser_session_id="session",
                    case_id=context.case_id,
                    action_id=context.action_id,
                )
            self.assertEqual(
                raised.exception.code,
                "recovery_details_changed",
            )
            self.assertEqual(
                owners.contexts.snapshot(context.case_id).state,
                RecoveryCheckContextState.CONSUMED,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            context = owners.service.create_context(browser_session_id="session")
            _operation(root, (b"other",))
            with self.assertRaises(RecoveryCheckError) as raised:
                owners.service.check_result(
                    browser_session_id="session",
                    case_id=context.case_id,
                    action_id=context.action_id,
                )
            self.assertEqual(
                raised.exception.code,
                "recovery_details_changed",
            )
            self.assertEqual(
                owners.contexts.snapshot(context.case_id).state,
                RecoveryCheckContextState.CONSUMED,
            )

    def test_undo_context_and_passed_check_use_existing_undo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent, undo = _stored_pair(
                root,
                (b"restored",),
            )
            owners = _owners(root, operations, undos)
            before = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )

            context = owners.service.create_context(browser_session_id="session")
            result = owners.service.check_result(
                browser_session_id="session",
                case_id=context.case_id,
                action_id=context.action_id,
            )
            after = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )

            self.assertEqual(result.state, RecoveryCheckResultState.CONFIRMED)
            self.assertEqual(undos.load(undo.undo_id).status, "restored")
            self.assertEqual(parent.operation_id, undo.operation_id)
            self.assertEqual(
                [item.id for item in before.activity],
                [item.id for item in after.activity],
            )

    def test_deterministic_undo_mismatch_remains_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, undo = _stored_pair(
                root,
                (b"restored",),
            )
            owners = _owners(root, operations, undos)
            restored = root / undo.expected_restores[0].restore_target
            restored.write_bytes(b"replaced")

            context = owners.service.create_context(browser_session_id="session")
            result = owners.service.check_result(
                browser_session_id="session",
                case_id=context.case_id,
                action_id=context.action_id,
            )

            self.assertEqual(
                result.state,
                RecoveryCheckResultState.NOT_CONFIRMED,
            )
            self.assertEqual(
                undos.load(undo.undo_id).status,
                "undo_verification_failed",
            )

    def test_refresh_failure_does_not_mask_primary_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            context = owners.service.create_context(browser_session_id="session")
            with mock.patch.object(
                owners.status,
                "refresh",
                side_effect=OSError("private"),
            ):
                result = owners.service.check_result(
                    browser_session_id="session",
                    case_id=context.case_id,
                    action_id=context.action_id,
                )
            self.assertEqual(result.state, RecoveryCheckResultState.CONFIRMED)
            self.assertTrue(result.recovery_attention)
            self.assertEqual(
                owners.operations.load(owners.record.operation_id).status,
                "verified",
            )

    def test_v1_multiple_and_shutdown_are_ineligible_without_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, _record = _ordinary_movement(
                root,
                "moved_unverified",
                "completed",
            )
            undos = OrganizationUndoRepository(root)
            owners = _owners(root, operations, undos)
            with self.assertRaises(RecoveryCheckError):
                owners.service.create_context(browser_session_id="session")
            self.assertEqual(owners.contexts._contexts, {})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            _operation(root, (b"other",))
            with self.assertRaises(RecoveryCheckError):
                owners.service.create_context(browser_session_id="session")
            self.assertEqual(owners.contexts._contexts, {})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            owners.lifecycle.begin_shutdown()
            with self.assertRaises(ApplicationStoppingError):
                owners.service.create_context(browser_session_id="session")
            self.assertEqual(owners.contexts._contexts, {})

    def test_v1_undo_and_duplicate_bin_are_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, parent = _ordinary_operation(root)
            undos, undo = _ordinary_undo(root, parent)
            undos.save(
                replace(
                    undo,
                    status=UNDO_RESTORED_UNVERIFIED,
                    completed_at=None,
                    expected_restores=tuple(
                        replace(item, result_state=UNDO_ITEM_RESTORED)
                        for item in undo.expected_restores
                    ),
                    verification=UndoVerificationResult(
                        status=UNDO_VERIFICATION_NOT_RUN
                    ),
                )
            )
            owners = _owners(root, operations, undos)
            with self.assertRaises(RecoveryCheckError):
                owners.service.create_context(browser_session_id="session")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _duplicate_move_state(root, "moved_unverified", "completed")
            owners = _owners(
                root,
                OrganizationOperationRepository(root),
                OrganizationUndoRepository(root),
            )
            with self.assertRaises(RecoveryCheckError):
                owners.service.create_context(browser_session_id="session")

    def test_context_creation_holds_lifecycle_and_coordinator_without_verifier(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _movement_owners(root)
            real_inspect = owners.eligibility.inspect

            def inspect():
                snapshot = owners.lifecycle.snapshot()
                self.assertEqual(
                    snapshot.active_kinds,
                    (RuntimeOperationKind.STATE_MUTATION,),
                )
                self.assertFalse(owners.coordinator.try_acquire())
                return real_inspect()

            with (
                mock.patch.object(
                    owners.eligibility,
                    "inspect",
                    side_effect=inspect,
                ),
                mock.patch.object(
                    owners.movement_publication,
                    "publish",
                ) as publish,
                mock.patch.object(
                    owners.undo_publication,
                    "publish",
                ) as undo_publish,
            ):
                owners.service.create_context(browser_session_id="session")
            publish.assert_not_called()
            undo_publish.assert_not_called()
            self.assertEqual(owners.lifecycle.snapshot().active_count, 0)


class _Owners:
    pass


def _movement_owners(
    root: Path,
    *,
    movement_publication=None,
) -> _Owners:
    operations, record = _operation(root, (b"recorded",))
    owners = _owners(
        root,
        operations,
        OrganizationUndoRepository(root),
        movement_publication=movement_publication,
    )
    owners.record = record
    return owners


def _owners(
    root: Path,
    operations: OrganizationOperationRepository,
    undos: OrganizationUndoRepository,
    *,
    movement_publication=None,
) -> _Owners:
    owners = _Owners()
    owners.operations = operations
    owners.undos = undos
    owners.lifecycle = RuntimeLifecycle()
    owners.coordinator = OrganizationExecutionCoordinator(owners.lifecycle)
    inspector = FilesystemOperationInspector(
        root,
        organization_operations=operations,
        organization_undos=undos,
    )
    owners.status = CurrentRecoveryAdmissionStatus(inspector)
    owners.eligibility = RecoveryCheckEligibilityInspector(
        RecoveryEvidenceInspector(inspector),
        operations,
        undos,
    )
    owners.contexts = RecoveryCheckContextStore(operations.configured_root_identity)
    owners.movement_publication = (
        movement_publication
        or OrganizationIdentityVerificationPublicationService(operations)
    )
    owners.undo_publication = OrganizationUndoIdentityVerificationPublicationService(
        operations,
        undos,
    )
    owners.service = RecoveryCheckService(
        owners.lifecycle,
        owners.coordinator,
        owners.eligibility,
        owners.contexts,
        owners.movement_publication,
        owners.undo_publication,
        owners.status,
    )
    return owners


def _user_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).parts[0] != "AI_Review"
    }


if __name__ == "__main__":
    unittest.main()
