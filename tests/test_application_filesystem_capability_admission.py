from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock

from organizer.application.duplicate_bin_movement_service import (
    preview_duplicate_bin_movement,
)
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
)
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.duplicate_bin_restore_service import (
    preview_duplicate_bin_activity_undo,
    preview_duplicate_bin_restore,
)
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.execution_service import issue_movement_authorization
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationBlockedError,
    FilesystemOperationInspector,
    RecoveryAdmissionSnapshot,
    RecoveryAdmissionState,
)
from organizer.application.organization_operation_models import (
    STATUS_PARTIAL_MOVEMENT,
)
from organizer.application.organization_undo_models import UNDO_PARTIAL
from organizer.application.organization_movement_service import (
    OrganizationMovementError,
    execute_organization_movement,
    preview_organization_movement,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.undo_service import preview_organization_undo
from organizer.web.organization_decision_store import OrganizationDecisionStore
from tests.test_application_duplicate_bin_approval_service import (
    _projection,
    _scan,
    _select_first_copy,
)
from tests.test_application_duplicate_bin_movement_service import (
    ISSUED,
    _owners,
)
from tests.test_application_duplicate_bin_restore_service import _moved
from tests.test_application_execution_service import _approved_plan
from tests.test_application_filesystem_operation_admission import (
    _ordinary_operation,
    _ordinary_undo,
)
from tests.test_application_undo_service import _verified_operation


_BLOCKERS = (
    ("ordinary_movement", RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION),
    ("ordinary_undo", RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION),
    ("duplicate_movement", RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION),
    ("duplicate_restore", RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION),
    ("malformed", RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE),
    ("inconsistent", RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE),
)


class _Admission:
    def __init__(self, root: Path, coordinator, state) -> None:
        self.root = root
        self.coordinator = coordinator
        self.state = state
        self.calls = 0

    def inspect_and_require_clear(self):
        self.calls += 1
        if self.coordinator.try_acquire():
            self.coordinator.release()
            raise AssertionError("recovery inspection ran outside the coordinator")
        if self.state is not RecoveryAdmissionState.CLEAR:
            raise FilesystemOperationBlockedError(self.state)
        return RecoveryAdmissionSnapshot(self.state)


class _PausingAdmission(_Admission):
    def __init__(self, root, coordinator, entered, proceed) -> None:
        super().__init__(root, coordinator, RecoveryAdmissionState.CLEAR)
        self.entered = entered
        self.proceed = proceed

    def inspect_and_require_clear(self):
        snapshot = super().inspect_and_require_clear()
        self.entered.set()
        if not self.proceed.wait(2):
            raise AssertionError("test did not release recovery inspection")
        return snapshot


class FilesystemCapabilityAdmissionTests(unittest.TestCase):
    def test_partial_movement_and_undo_block_capability_issuance(self):
        for family in ("movement", "undo"):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operations, operation = _ordinary_operation(root)
                if family == "movement":
                    operations.save(
                        replace(operation, status=STATUS_PARTIAL_MOVEMENT)
                    )
                else:
                    undos, undo = _ordinary_undo(root, operation)
                    undos.save(replace(undo, status=UNDO_PARTIAL))
                coordinator = OrganizationExecutionCoordinator()
                admission = FilesystemOperationAdmission(
                    FilesystemOperationInspector(root)
                )
                plan = _approved_plan(root, ("new.txt",))

                with self.assertRaises(FilesystemOperationBlockedError):
                    issue_movement_authorization(
                        plan,
                        coordinator,
                        filesystem_operation_admission=admission,
                        configured_root_identity=(
                            operations.configured_root_identity
                        ),
                    )

                self.assertEqual(coordinator._authorizations, {})
                self._released(coordinator)

    def test_authorization_write_exception_releases_coordinator_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            admission = _Admission(
                owners.root, owners.coordinator, RecoveryAdmissionState.CLEAR
            )
            with (
                mock.patch.object(
                    owners.authorizations,
                    "issue",
                    side_effect=OSError("injected authorization write failure"),
                ),
                self.assertRaisesRegex(OSError, "injected authorization"),
            ):
                preview_duplicate_bin_movement(
                    batch_id=owners.batch.batch_id,
                    browser_session_id="browser",
                    batch_repository=owners.batches,
                    authorization_repository=owners.authorizations,
                    operation_repository=owners.operations,
                    coordinator=owners.coordinator,
                    filesystem_operation_admission=admission,
                    issued_at=ISSUED.isoformat(),
                )

            self.assertEqual(_bytes(owners.authorizations.path), None)
            self._released(owners.coordinator)

    def test_preview_serializes_confirmation_until_token_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            repository = OrganizationOperationRepository(root)
            coordinator = OrganizationExecutionCoordinator()
            clear = _Admission(root, coordinator, RecoveryAdmissionState.CLEAR)
            first = preview_organization_movement(
                approved_plan=plan,
                browser_session_id="browser",
                current_scan_generation=plan.scan_generation,
                current_decision_revision=plan.decision_revision,
                coordinator=coordinator,
                operation_repository=repository,
                filesystem_operation_admission=clear,
            )
            entered = Event()
            proceed = Event()
            pausing = _PausingAdmission(root, coordinator, entered, proceed)
            previews = []
            worker = Thread(
                target=lambda: previews.append(
                    preview_organization_movement(
                        approved_plan=plan,
                        browser_session_id="browser",
                        current_scan_generation=plan.scan_generation,
                        current_decision_revision=plan.decision_revision,
                        coordinator=coordinator,
                        operation_repository=repository,
                        filesystem_operation_admission=pausing,
                    )
                )
            )
            worker.start()
            self.assertTrue(entered.wait(1))

            with self.assertRaisesRegex(
                OrganizationMovementError, "movement_or_undo_in_progress"
            ):
                execute_organization_movement(
                    approved_plan=plan,
                    browser_session_id="browser",
                    current_scan_generation=plan.scan_generation,
                    current_decision_revision=plan.decision_revision,
                    confirmation_token=first.confirmation_token,
                    confirmation="MOVE APPROVED FILES",
                    coordinator=coordinator,
                    operation_repository=repository,
                    filesystem_operation_admission=clear,
                )
            self.assertEqual(repository.list_records(), ())
            proceed.set()
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(previews), 1)
            self.assertNotEqual(
                previews[0].confirmation_token, first.confirmation_token
            )
            self._released(coordinator)

    def test_approval_serializes_confirmation_until_snapshot_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            coordinator = OrganizationExecutionCoordinator()
            plan = _approved_plan(root, ("a.txt",))
            operations = OrganizationOperationRepository(root)
            clear = _Admission(root, coordinator, RecoveryAdmissionState.CLEAR)
            authorization = issue_movement_authorization(
                plan,
                coordinator,
                filesystem_operation_admission=clear,
                browser_session_id="browser",
                configured_root_identity=operations.configured_root_identity,
            )
            projection = _projection(root, "scan-two")
            scan = _scan(root, projection, "scan-two")
            entered = Event()
            proceed = Event()
            pausing = _PausingAdmission(root, coordinator, entered, proceed)
            store = OrganizationDecisionStore(
                runtime_lifecycle=coordinator.runtime_lifecycle,
                execution_coordinator=coordinator,
                filesystem_operation_admission=pausing,
            )
            selected = _select_first_copy(store, "second-browser", scan, projection)
            approvals = []
            worker = Thread(
                target=lambda: approvals.append(
                    store.approve(
                        "second-browser",
                        scan,
                        projection,
                        scan_generation="scan-two",
                        expected_revision=selected.session.decision_revision,
                    )
                )
            )
            worker.start()
            self.assertTrue(entered.wait(1))

            with self.assertRaisesRegex(
                OrganizationMovementError, "movement_or_undo_in_progress"
            ):
                execute_organization_movement(
                    approved_plan=plan,
                    browser_session_id="browser",
                    current_scan_generation=plan.scan_generation,
                    current_decision_revision=plan.decision_revision,
                    confirmation_token=authorization.authorization_id,
                    confirmation="MOVE APPROVED FILES",
                    coordinator=coordinator,
                    operation_repository=operations,
                    filesystem_operation_admission=clear,
                )
            self.assertEqual(operations.list_records(), ())
            proceed.set()
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0].session.status, "ready_to_move")
            self._released(coordinator)

    def test_every_blocker_rejects_approval_before_batch_or_snapshot_publication(self):
        for label, state in _BLOCKERS:
            with self.subTest(blocker=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository = DuplicateBinRepository(root)
                coordinator = OrganizationExecutionCoordinator()
                admission = _Admission(root, coordinator, state)
                store = OrganizationDecisionStore(
                    repository,
                    runtime_lifecycle=coordinator.runtime_lifecycle,
                    execution_coordinator=coordinator,
                    filesystem_operation_admission=admission,
                )
                projection = _projection(root, "scan-one")
                scan = _scan(root, projection, "scan-one")
                selected = _select_first_copy(store, "browser", scan, projection)
                before = _bytes(repository.catalog_path)

                with self.assertRaises(FilesystemOperationBlockedError):
                    store.approve(
                        "browser",
                        scan,
                        projection,
                        scan_generation="scan-one",
                        expected_revision=selected.session.decision_revision,
                    )

                current = store.current("browser", scan, projection)
                self.assertEqual(current.session.status, "ready_for_approval")
                self.assertEqual(
                    current.session.decision_revision,
                    selected.session.decision_revision,
                )
                self.assertIsNone(current.session.approved_plan)
                self.assertEqual(_bytes(repository.catalog_path), before)
                self._released(coordinator)

    def test_idempotent_approval_is_also_freshly_gated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            projection = _projection(root, "scan-one")
            scan = _scan(root, projection, "scan-one")
            coordinator = OrganizationExecutionCoordinator()
            admission = _Admission(root, coordinator, RecoveryAdmissionState.CLEAR)
            store = OrganizationDecisionStore(
                runtime_lifecycle=coordinator.runtime_lifecycle,
                execution_coordinator=coordinator,
                filesystem_operation_admission=admission,
            )
            selected = _select_first_copy(store, "browser", scan, projection)
            approved = store.approve(
                "browser",
                scan,
                projection,
                scan_generation="scan-one",
                expected_revision=selected.session.decision_revision,
            )
            admission.state = RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION

            with self.assertRaises(FilesystemOperationBlockedError):
                store.approve(
                    "browser",
                    scan,
                    projection,
                    scan_generation="scan-one",
                    expected_revision=approved.session.decision_revision,
                )

            self.assertIs(
                store.current("browser", scan, projection).session.approved_plan,
                approved.session.approved_plan,
            )
            self._released(coordinator)

    def test_every_blocker_rejects_ordinary_movement_preview_and_public_issue(self):
        for label, state in _BLOCKERS:
            with self.subTest(blocker=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                plan = _approved_plan(root, ("a.txt",))
                repository = OrganizationOperationRepository(root)
                coordinator = OrganizationExecutionCoordinator()
                admission = _Admission(root, coordinator, state)

                with self.assertRaises(FilesystemOperationBlockedError):
                    preview_organization_movement(
                        approved_plan=plan,
                        browser_session_id="browser",
                        current_scan_generation=plan.scan_generation,
                        current_decision_revision=plan.decision_revision,
                        coordinator=coordinator,
                        operation_repository=repository,
                        filesystem_operation_admission=admission,
                    )
                with self.assertRaises(FilesystemOperationBlockedError):
                    issue_movement_authorization(
                        plan,
                        coordinator,
                        filesystem_operation_admission=admission,
                        configured_root_identity=repository.configured_root_identity,
                    )

                self.assertEqual(coordinator._authorizations, {})
                self.assertEqual(repository.list_records(), ())
                self._released(coordinator)

    def test_every_blocker_rejects_ordinary_undo_preview(self):
        for label, state in _BLOCKERS:
            with self.subTest(blocker=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operations, undos, coordinator, record = _verified_operation(root, 1)
                admission = _Admission(root, coordinator, state)

                with self.assertRaises(FilesystemOperationBlockedError):
                    preview_organization_undo(
                        operation_id=record.operation_id,
                        browser_session_id="browser",
                        coordinator=coordinator,
                        operation_repository=operations,
                        undo_repository=undos,
                        filesystem_operation_admission=admission,
                    )

                self.assertEqual(coordinator._undo_authorizations, {})
                self.assertEqual(undos.list_records(), ())
                self._released(coordinator)

    def test_every_blocker_rejects_duplicate_movement_preview_without_catalog_write(self):
        for label, state in _BLOCKERS:
            with self.subTest(blocker=label), tempfile.TemporaryDirectory() as directory:
                owners = _owners(Path(directory).resolve(), selected=1)
                admission = _Admission(owners.root, owners.coordinator, state)
                before = _bytes(owners.authorizations.path)

                with self.assertRaises(FilesystemOperationBlockedError):
                    preview_duplicate_bin_movement(
                        batch_id=owners.batch.batch_id,
                        browser_session_id="browser",
                        batch_repository=owners.batches,
                        authorization_repository=owners.authorizations,
                        operation_repository=owners.operations,
                        coordinator=owners.coordinator,
                        filesystem_operation_admission=admission,
                        issued_at=ISSUED.isoformat(),
                    )

                self.assertEqual(_bytes(owners.authorizations.path), before)
                self.assertEqual(owners.operations.list_records(), ())
                self._released(owners.coordinator)

    def test_every_blocker_rejects_restore_and_activity_undo_preview(self):
        for label, state in _BLOCKERS:
            for activity in (False, True):
                with self.subTest(blocker=label, activity=activity), tempfile.TemporaryDirectory() as directory:
                    owners = _moved(Path(directory).resolve(), selected=1)
                    restores = DuplicateBinRestoreRepository(owners.root)
                    admission = _Admission(owners.root, owners.coordinator, state)
                    before = _bytes(restores.authorization_path)
                    movement_id = owners.operations.list_records()[0].operation_id
                    item_id = owners.batch.items[0].bin_item_id

                    with self.assertRaises(FilesystemOperationBlockedError):
                        if activity:
                            preview_duplicate_bin_activity_undo(
                                movement_operation_id=movement_id,
                                browser_session_id="browser",
                                batch_repository=owners.batches,
                                movement_repository=owners.operations,
                                restore_repository=restores,
                                coordinator=owners.coordinator,
                                filesystem_operation_admission=admission,
                            )
                        else:
                            preview_duplicate_bin_restore(
                                item_ids=(item_id,),
                                browser_session_id="browser",
                                batch_repository=owners.batches,
                                movement_repository=owners.operations,
                                restore_repository=restores,
                                coordinator=owners.coordinator,
                                filesystem_operation_admission=admission,
                            )

                    self.assertEqual(_bytes(restores.authorization_path), before)
                    self.assertEqual(restores.list_operations(), ())
                    self._released(owners.coordinator)

    def _released(self, coordinator) -> None:
        self.assertEqual(coordinator.runtime_lifecycle.snapshot().active_count, 0)
        self.assertTrue(coordinator.try_acquire())
        coordinator.release()


def _bytes(path: Path):
    return path.read_bytes() if path.exists() else None


if __name__ == "__main__":
    unittest.main()
