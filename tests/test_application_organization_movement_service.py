from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
import uuid
from unittest import mock

import organizer.application.organization_movement_service as movement_service
from organizer.application.activity_service import list_activity_entries
from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
    approved_plan_identity,
)
from organizer.application.execution_service import move_approved_organization
from organizer.application.filesystem_operation_admission import (
    SAFE_RECOVERY_MESSAGE,
    FilesystemOperationBlockedError,
    RecoveryAdmissionState,
)
from organizer.application.organization_movement_service import (
    OrganizationMovementError,
    execute_organization_movement,
    preview_organization_movement,
    project_organization_execution,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.runtime_lifecycle import ApplicationStoppingError
from tests.test_application_execution_service import _approved_plan


class OrganizationMovementServiceTests(unittest.TestCase):
    def test_blocked_confirmation_keeps_token_and_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            token = _preview(plan, coordinator, repository).confirmation_token
            admission = mock.Mock()
            admission.root = root

            def blocked():
                self.assertFalse(coordinator.try_acquire())
                raise FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                )

            admission.inspect_and_require_clear.side_effect = blocked
            with self.assertRaisesRegex(
                FilesystemOperationBlockedError, SAFE_RECOVERY_MESSAGE
            ):
                _execute(
                    plan,
                    coordinator,
                    repository,
                    token,
                    filesystem_operation_admission=admission,
                )

            self.assertEqual(
                coordinator.authorization_snapshot(token).status, "available"
            )
            self.assertEqual(repository.list_records(), ())
            self.assertTrue((root / "a.txt").is_file())
            self.assertFalse((root / "Organized/a.txt").exists())
            self.assertEqual(coordinator.runtime_lifecycle.snapshot().active_count, 0)
            self.assertTrue(coordinator.try_acquire())
            coordinator.release()

    def test_admitted_preview_finishes_after_shutdown_begins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            issuing = Event()
            finish = Event()
            outcomes = []
            real_issue = movement_service._issue_movement_authorization

            def paused_issue(*args, **kwargs):
                self.assertEqual(
                    coordinator.runtime_lifecycle.snapshot().active_kinds,
                    (movement_service.RuntimeOperationKind.STATE_MUTATION,),
                )
                issuing.set()
                finish.wait(timeout=2)
                return real_issue(*args, **kwargs)

            worker = Thread(
                target=lambda: outcomes.append(
                    _preview(plan, coordinator, repository)
                )
            )
            with mock.patch.object(
                movement_service,
                "_issue_movement_authorization",
                side_effect=paused_issue,
            ):
                worker.start()
                self.assertTrue(issuing.wait(1))
                self.assertEqual(
                    coordinator.runtime_lifecycle.begin_shutdown().active_count,
                    1,
                )
                finish.set()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcomes), 1)
            self.assertIsNotNone(
                coordinator.authorization_snapshot(outcomes[0].confirmation_token)
            )
            self.assertEqual(
                coordinator.runtime_lifecycle.wait_until_idle().active_count,
                0,
            )

    def test_stopping_rejects_preview_without_replacing_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            existing = _preview(plan, coordinator, repository)
            before = dict(coordinator._authorizations)
            coordinator.runtime_lifecycle.begin_shutdown()

            with self.assertRaises(ApplicationStoppingError):
                _preview(plan, coordinator, repository)

            self.assertEqual(coordinator._authorizations, before)
            self.assertIsNotNone(
                coordinator.authorization_snapshot(existing.confirmation_token)
            )
            self.assertEqual(repository.list_records(), ())

    def test_preview_is_server_derived_bound_and_mutation_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt", "b.txt"), duplicate=True)
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)

            preview = _preview(plan, coordinator, repository)
            authorization = coordinator.authorization_snapshot(
                preview.confirmation_token
            )

            self.assertEqual(preview.files_to_move, 2)
            self.assertEqual(preview.duplicate_copies_deferred, 1)
            self.assertEqual(preview.confirmation, "MOVE APPROVED FILES")
            self.assertNotIn("scan_generation", preview.to_dict())
            self.assertNotIn("decision_revision", preview.to_dict())
            self.assertIsNotNone(authorization)
            assert authorization is not None
            self.assertEqual(authorization.browser_session_id, "browser-one")
            self.assertEqual(
                authorization.configured_root_identity,
                repository.configured_root_identity,
            )
            self.assertEqual(
                authorization.approved_plan_identity,
                approved_plan_identity(plan),
            )
            self.assertEqual(repository.list_records(), ())
            self.assertTrue((root / "a.txt").is_file())
            self.assertTrue((root / "b.txt").is_file())

    def test_preview_rejects_missing_stale_changed_and_empty_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            empty = replace(
                plan,
                reviewed_move_items=(),
                source_fingerprints=(),
                summary=replace(plan.summary, files_to_move=0, accepted_moves=0),
            )
            cases = (
                (
                    replace(plan, status="in_progress"),
                    plan.scan_generation,
                    plan.decision_revision,
                    "approved_plan_not_found",
                ),
                (
                    plan,
                    "scan-two",
                    plan.decision_revision,
                    "scan_generation_mismatch",
                ),
                (
                    plan,
                    plan.scan_generation,
                    plan.decision_revision + 1,
                    "decision_revision_mismatch",
                ),
                (
                    empty,
                    plan.scan_generation,
                    plan.decision_revision,
                    "no_supported_moves",
                ),
            )
            for candidate, generation, revision, code in cases:
                with self.subTest(code=code), self.assertRaisesRegex(
                    OrganizationMovementError,
                    code,
                ):
                    preview_organization_movement(
                        approved_plan=candidate,
                        browser_session_id="browser-one",
                        current_scan_generation=generation,
                        current_decision_revision=revision,
                        coordinator=coordinator,
                        operation_repository=repository,
                    )
            self.assertEqual(repository.list_records(), ())

    def test_authorization_rejects_wrong_session_root_and_changed_plan_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            token = _preview(plan, coordinator, repository).confirmation_token
            first, second = plan.reviewed_move_items
            changed = replace(
                plan,
                reviewed_move_items=(
                    replace(
                        first,
                        plan_item=replace(
                            first.plan_item,
                            destination=root / "Organized" / "changed.txt",
                        ),
                    ),
                    second,
                ),
            )
            for candidate, browser_session_id, target_repository in (
                (plan, "browser-two", repository),
                (changed, "browser-one", repository),
            ):
                with self.subTest(session=browser_session_id), self.assertRaisesRegex(
                    OrganizationMovementError,
                    "invalid_confirmation_token",
                ):
                    _execute(
                        candidate,
                        coordinator,
                        target_repository,
                        token,
                        browser_session_id=browser_session_id,
                    )
            with tempfile.TemporaryDirectory() as other_directory:
                other_repository = OrganizationOperationRepository(
                    Path(other_directory).resolve()
                )
                with self.assertRaisesRegex(
                    OrganizationMovementError,
                    "invalid_confirmation_token",
                ):
                    _execute(plan, coordinator, other_repository, token)
            self.assertEqual(repository.list_records(), ())
            self.assertEqual(
                coordinator.authorization_snapshot(token).status,
                "available",
            )

    def test_changed_scan_or_revision_invalidates_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            token = _preview(plan, coordinator, repository).confirmation_token
            cases = (
                ("scan-two", plan.decision_revision, "scan_generation_mismatch"),
                (plan.scan_generation, plan.decision_revision + 1, "decision_revision_mismatch"),
            )
            for generation, revision, code in cases:
                with self.subTest(code=code), self.assertRaisesRegex(
                    OrganizationMovementError,
                    code,
                ):
                    execute_organization_movement(
                        approved_plan=plan,
                        browser_session_id="browser-one",
                        current_scan_generation=generation,
                        current_decision_revision=revision,
                        confirmation_token=token,
                        confirmation="MOVE APPROVED FILES",
                        coordinator=coordinator,
                        operation_repository=repository,
                    )
            self.assertEqual(repository.list_records(), ())

    def test_exact_confirmation_is_case_and_whitespace_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            token = _preview(plan, coordinator, repository).confirmation_token
            for confirmation in (
                "",
                "move approved files",
                "MOVE APPROVED FILE",
                " MOVE APPROVED FILES",
                "MOVE APPROVED FILES ",
                "MOVE APPROVED FILES\n",
            ):
                with self.subTest(confirmation=confirmation), self.assertRaisesRegex(
                    OrganizationMovementError,
                    "invalid_confirmation",
                ):
                    _execute(
                        plan,
                        coordinator,
                        repository,
                        token,
                        confirmation=confirmation,
                    )
            self.assertEqual(repository.list_records(), ())
            self.assertEqual(
                coordinator.authorization_snapshot(token).status,
                "available",
            )

    def test_verified_execution_lost_response_replay_and_activity_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt", "b.txt"), duplicate=True)
            duplicate = root / "duplicate.txt"
            duplicate.write_bytes(b"unchanged")
            original = {
                "a.txt": (root / "a.txt").read_bytes(),
                "b.txt": (root / "b.txt").read_bytes(),
            }
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            token = _preview(plan, coordinator, repository).confirmation_token

            with mock.patch(
                "organizer.application.organization_movement_service.move_approved_organization",
                wraps=move_approved_organization,
            ) as owner:
                result = _execute(plan, coordinator, repository, token)
                replay = _execute(
                    plan,
                    coordinator,
                    OrganizationOperationRepository(root),
                    token,
                )

            self.assertEqual(owner.call_count, 2)
            self.assertEqual(result.status, "verified")
            self.assertEqual(replay, result)
            self.assertEqual(len(repository.list_records()), 1)
            self.assertEqual(
                repository.find_for_authorization(token).operation_id,
                result.operation_id,
            )
            self.assertEqual(
                (root / "Organized" / "a.txt").read_bytes(),
                original["a.txt"],
            )
            self.assertEqual(
                (root / "Organized" / "b.txt").read_bytes(),
                original["b.txt"],
            )
            self.assertTrue(duplicate.is_file())
            self.assertFalse((root / "Duplicate Bin").exists())
            activity = list_activity_entries(
                operation_repository=repository,
                undo_repository=OrganizationUndoRepository(root),
            )
            self.assertTrue(activity.activity[0].undoable)
            projected = project_organization_execution(
                approved_plan=plan,
                operation_repository=OrganizationOperationRepository(root),
            )
            self.assertEqual(projected, result)

    def test_second_authorization_cannot_start_the_approved_plan_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            first = _preview(plan, coordinator, repository).confirmation_token
            second = _preview(plan, coordinator, repository).confirmation_token

            completed = _execute(plan, coordinator, repository, first)
            replay = _execute(plan, coordinator, repository, first)
            with self.assertRaisesRegex(
                OrganizationMovementError,
                "movement_already_started",
            ):
                _execute(plan, coordinator, repository, second)

            self.assertEqual(replay.operation_id, completed.operation_id)
            self.assertEqual(len(repository.list_records()), 1)

    def test_failed_durable_result_is_truthful_and_not_reexecuted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            token = _preview(plan, coordinator, repository).confirmation_token

            with mock.patch(
                "organizer.executor.shutil.move",
                side_effect=OSError("injected internal failure"),
            ) as movement:
                result = _execute(plan, coordinator, repository, token)
                replay = _execute(plan, coordinator, repository, token)

            self.assertEqual(movement.call_count, 1)
            self.assertEqual(result.status, "movement_failed")
            self.assertEqual(replay, result)
            self.assertEqual(result.files_failed, 1)
            self.assertEqual(result.files_unattempted, 1)
            self.assertTrue((root / "a.txt").is_file())
            self.assertTrue((root / "b.txt").is_file())

    def test_changed_source_and_shared_coordinator_block_before_movement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            token = _preview(plan, coordinator, repository).confirmation_token
            self.assertTrue(coordinator.try_acquire())
            try:
                with self.assertRaisesRegex(
                    OrganizationMovementError,
                    "movement_or_undo_in_progress",
                ):
                    _execute(plan, coordinator, repository, token)
            finally:
                coordinator.release()
            self.assertEqual(
                coordinator.authorization_snapshot(token).status,
                "available",
            )
            (root / "a.txt").write_bytes(b"changed")
            with self.assertRaisesRegex(OrganizationMovementError, "source_changed"):
                _execute(plan, coordinator, repository, token)
            self.assertEqual(repository.list_records(), ())

    def test_unknown_and_malformed_authorization_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            for token in ("not-a-uuid", str(uuid.uuid4())):
                with self.subTest(token=token), self.assertRaisesRegex(
                    OrganizationMovementError,
                    "invalid_confirmation_token",
                ):
                    _execute(plan, coordinator, repository, token)
            self.assertEqual(repository.list_records(), ())


def _preview(plan, coordinator, repository):
    return preview_organization_movement(
        approved_plan=plan,
        browser_session_id="browser-one",
        current_scan_generation=plan.scan_generation,
        current_decision_revision=plan.decision_revision,
        coordinator=coordinator,
        operation_repository=repository,
    )


def _execute(
    plan,
    coordinator,
    repository,
    token,
    *,
    browser_session_id="browser-one",
    confirmation="MOVE APPROVED FILES",
    filesystem_operation_admission=None,
):
    return execute_organization_movement(
        approved_plan=plan,
        browser_session_id=browser_session_id,
        current_scan_generation=plan.scan_generation,
        current_decision_revision=plan.decision_revision,
        confirmation_token=token,
        confirmation=confirmation,
        coordinator=coordinator,
        operation_repository=repository,
        filesystem_operation_admission=filesystem_operation_admission,
    )


if __name__ == "__main__":
    unittest.main()
