from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.execution_service import (
    CONFIRM_MOVE_APPROVED_FILES,
    OrganizationExecutionError,
    inspect_incomplete_organization_operation,
    issue_movement_authorization,
    move_approved_organization,
)
from organizer.application.organization_decision_models import (
    ApprovedOrganizationPlan,
    DuplicateChoiceDecision,
    OrganizationApprovalSummary,
    SourceFingerprint,
)
from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_UNATTEMPTED,
    STATUS_INTERRUPTED,
    STATUS_MOVEMENT_FAILED,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_VERIFIED,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.models import MovePlanItem, ReviewedPlanItem


class ApplicationExecutionServiceTests(unittest.TestCase):
    def test_complete_approved_batch_is_durable_before_move_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt", "b.txt"), duplicate=True)
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            real_move = shutil.move
            observed: list[tuple[str, str]] = []

            def checked_move(source: str, destination: str) -> str:
                record = repository.list_records()[0]
                index = len(observed)
                self.assertEqual(record.status, "moving")
                self.assertEqual(record.expected_moves[index].result_state, "attempting")
                observed.append((source, destination))
                return str(real_move(source, destination))

            with mock.patch("organizer.executor.shutil.move", side_effect=checked_move):
                outcome = move_approved_organization(
                    approved_plan=plan,
                    current_scan_generation="scan-one",
                    root=root,
                    authorization=authorization,
                    coordinator=coordinator,
                    operation_repository=repository,
                    confirmation=CONFIRM_MOVE_APPROVED_FILES,
                )

            record = repository.load(outcome.operation_id)
            self.assertEqual(outcome.status, STATUS_VERIFIED)
            self.assertEqual(outcome.files_moved, 2)
            self.assertEqual(record.verification.status, "passed")
            self.assertEqual([item.result_state for item in record.expected_moves], ["moved", "moved"])
            self.assertEqual([item.change_id for item in record.expected_moves], ["O1", "O2"])
            self.assertEqual(record.duplicate_actions_deferred, 1)
            self.assertIsNotNone(record.legacy_operation_log)
            self.assertFalse((root / "a.txt").exists())
            self.assertFalse((root / "b.txt").exists())
            self.assertEqual((root / "Organized" / "a.txt").read_bytes(), b"a.txt-bytes")
            self.assertEqual((root / "Organized" / "b.txt").read_bytes(), b"b.txt-bytes")
            self.assertNotIn(str(root), json.dumps(outcome.__dict__))

    def test_wrong_confirmation_creates_nothing_consumes_nothing_and_unlocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)

            with self.assertRaisesRegex(OrganizationExecutionError, "confirmation_required"):
                _move(plan, root, authorization, coordinator, repository, "yes")

            self.assertTrue((root / "a.txt").exists())
            self.assertFalse(repository.directory.exists())
            self.assertEqual(
                coordinator.authorization_snapshot(authorization.authorization_id).status,
                "available",
            )
            self.assertTrue(coordinator.try_acquire())
            coordinator.release()

    def test_generation_status_and_restart_authorization_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            with self.assertRaisesRegex(OrganizationExecutionError, "approved_plan_stale"):
                move_approved_organization(
                    approved_plan=plan,
                    current_scan_generation="new-scan",
                    root=root,
                    authorization=authorization,
                    coordinator=coordinator,
                    operation_repository=repository,
                    confirmation=CONFIRM_MOVE_APPROVED_FILES,
                )
            with self.assertRaisesRegex(OrganizationExecutionError, "approved_plan_not_found"):
                issue_movement_authorization(replace(plan, status="in_progress"), coordinator)

            restarted = OrganizationExecutionCoordinator()
            with self.assertRaisesRegex(OrganizationExecutionError, "approved_plan_not_found"):
                _move(plan, root, authorization, restarted, repository)
            self.assertTrue((root / "a.txt").exists())

    def test_authorization_rejects_modified_or_reordered_approved_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            first, second = plan.reviewed_move_items
            modified = replace(
                plan,
                reviewed_move_items=(
                    replace(
                        first,
                        plan_item=replace(
                            first.plan_item,
                            destination=root / "Organized" / "other.txt",
                        ),
                    ),
                    second,
                ),
            )
            reordered = replace(
                plan,
                reviewed_move_items=(second, first),
                source_fingerprints=tuple(reversed(plan.source_fingerprints)),
            )

            for candidate in (modified, reordered):
                with self.subTest(candidate=candidate.reviewed_move_items[0].id):
                    with self.assertRaisesRegex(
                        OrganizationExecutionError,
                        "approved_plan_stale",
                    ):
                        _move(candidate, root, authorization, coordinator, repository)

            self.assertTrue((root / "a.txt").exists())
            self.assertTrue((root / "b.txt").exists())
            self.assertFalse(repository.directory.exists())

    def test_second_execution_and_concurrent_execution_do_not_move_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            first = _move(plan, root, authorization, coordinator, repository)
            self.assertEqual(first.status, STATUS_VERIFIED)

            with self.assertRaisesRegex(OrganizationExecutionError, "movement_already_started"):
                _move(plan, root, authorization, coordinator, repository)
            self.assertEqual(len(repository.list_records()), 1)
            replay_authorization = issue_movement_authorization(plan, coordinator)
            replay = _move(
                plan,
                root,
                replay_authorization,
                coordinator,
                repository,
            )
            self.assertEqual(replay.operation_id, first.operation_id)
            self.assertEqual(len(repository.list_records()), 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            self.assertTrue(coordinator.try_acquire())
            try:
                with self.assertRaisesRegex(OrganizationExecutionError, "movement_already_started"):
                    _move(plan, root, authorization, coordinator, repository)
            finally:
                coordinator.release()
            self.assertTrue((root / "a.txt").exists())

    def test_changed_protected_and_occupied_inputs_fail_before_record_or_consumption(self) -> None:
        cases = ("changed", "protected", "occupied")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_name = "shared/a.txt" if case == "protected" else "a.txt"
                plan = _approved_plan(root, (source_name,))
                coordinator = OrganizationExecutionCoordinator()
                repository = OrganizationOperationRepository(root)
                authorization = issue_movement_authorization(plan, coordinator)
                if case == "changed":
                    (root / source_name).write_bytes(b"changed-size")
                    expected = "source_changed"
                elif case == "protected":
                    expected = "protected_item_selected"
                else:
                    (root / "Organized").mkdir()
                    (root / "Organized" / Path(source_name).name).write_text("occupied")
                    expected = "overwrite_detected"

                with self.assertRaisesRegex(OrganizationExecutionError, expected):
                    _move(plan, root, authorization, coordinator, repository)
                self.assertFalse(repository.directory.exists())
                self.assertEqual(
                    coordinator.authorization_snapshot(authorization.authorization_id).status,
                    "available",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("project/a.txt",))
            (root / "project" / "package.json").write_text("{}", encoding="utf-8")
            _assert_preflight_error(self, plan, root, "protected_item_selected")

    def test_mtime_symlink_root_and_batch_conflicts_fail_before_movement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            source = root / "a.txt"
            stat = source.stat()
            os.utime(source, (stat.st_atime, stat.st_mtime + 10))
            _assert_preflight_error(self, plan, root, "source_changed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            source = root / "a.txt"
            target = root / "target.txt"
            target.write_bytes(source.read_bytes())
            source.unlink()
            try:
                source.symlink_to(target)
            except (NotImplementedError, OSError):
                pass
            else:
                _assert_preflight_error(self, plan, root, "source_changed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            destination = plan.reviewed_move_items[0].plan_item.destination
            destination.parent.mkdir()
            try:
                destination.symlink_to(root / "missing-target.txt")
            except (NotImplementedError, OSError):
                pass
            else:
                _assert_preflight_error(self, plan, root, "overwrite_detected")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            plan = _approved_plan(root, ("a.txt",))
            reviewed = plan.reviewed_move_items[0]
            outside_source = outside / "a.txt"
            outside_source.write_bytes(b"a.txt-bytes")
            changed_item = replace(
                reviewed,
                plan_item=replace(reviewed.plan_item, source=outside_source),
            )
            outside_plan = replace(plan, reviewed_move_items=(changed_item,))
            _assert_preflight_error(self, outside_plan, root, "approved_plan_stale")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            plan = _approved_plan(root, ("a.txt",))
            link = root / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError):
                continue_symlink = False
            else:
                continue_symlink = True
            if continue_symlink:
                item = plan.reviewed_move_items[0]
                unsafe = replace(
                    plan,
                    reviewed_move_items=(
                        replace(
                            item,
                            plan_item=replace(
                                item.plan_item,
                                destination=link / "a.txt",
                            ),
                        ),
                    ),
                )
                _assert_preflight_error(self, unsafe, root, "approved_plan_stale")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            first, second = plan.reviewed_move_items
            duplicate_destination = replace(
                plan,
                reviewed_move_items=(
                    first,
                    replace(
                        second,
                        plan_item=replace(
                            second.plan_item,
                            destination=first.plan_item.destination,
                        ),
                    ),
                ),
            )
            _assert_preflight_error(self, duplicate_destination, root, "approved_plan_stale")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            first, second = plan.reviewed_move_items
            move_chain = replace(
                plan,
                reviewed_move_items=(
                    first,
                    replace(
                        second,
                        plan_item=replace(
                            second.plan_item,
                            destination=first.plan_item.source,
                        ),
                    ),
                ),
            )
            _assert_preflight_error(self, move_chain, root, "approved_plan_stale")

    def test_partial_and_zero_success_failures_are_durable_and_not_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt", "b.txt", "c.txt"))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            real_move = shutil.move
            calls = 0

            def fail_second(source: str, destination: str) -> str:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected failure text must stay internal")
                return str(real_move(source, destination))

            with mock.patch("organizer.executor.shutil.move", side_effect=fail_second):
                outcome = _move(plan, root, authorization, coordinator, repository)
            record = repository.load(outcome.operation_id)
            self.assertEqual(outcome.status, STATUS_PARTIAL_MOVEMENT)
            self.assertEqual(
                [item.result_state for item in record.expected_moves],
                [MOVE_MOVED, MOVE_FAILED, MOVE_UNATTEMPTED],
            )
            self.assertEqual(record.verification.status, "passed")
            self.assertNotIn("injected", json.dumps(outcome.__dict__))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            with mock.patch("organizer.executor.shutil.move", side_effect=OSError("no")):
                outcome = _move(plan, root, authorization, coordinator, repository)
            record = repository.load(outcome.operation_id)
            self.assertEqual(outcome.status, STATUS_MOVEMENT_FAILED)
            self.assertEqual(
                [item.result_state for item in record.expected_moves],
                [MOVE_FAILED, MOVE_UNATTEMPTED],
            )
            self.assertEqual(record.verification.status, "not_run")

    def test_complete_move_with_filesystem_mismatch_becomes_verification_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            real_save = repository.save
            changed = False

            def change_after_movement(record):
                nonlocal changed
                real_save(record)
                if record.status == "moved_unverified" and not changed:
                    changed = True
                    (root / "Organized" / "a.txt").write_bytes(b"different-size")

            with mock.patch.object(repository, "save", side_effect=change_after_movement):
                outcome = _move(plan, root, authorization, coordinator, repository)

            record = repository.load(outcome.operation_id)
            self.assertEqual(outcome.status, "verification_failed")
            self.assertEqual(record.verification.status, "failed")
            self.assertIn(
                "destination_size_changed:O1",
                record.verification.mismatch_codes,
            )

    def test_initial_record_failure_moves_nothing_and_does_not_consume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            with mock.patch.object(repository, "create", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OrganizationExecutionError, "operation_record_failed"):
                    _move(plan, root, authorization, coordinator, repository)
            self.assertTrue((root / "a.txt").exists())
            self.assertEqual(
                coordinator.authorization_snapshot(authorization.authorization_id).status,
                "available",
            )
            self.assertTrue(coordinator.try_acquire())
            coordinator.release()

    def test_recorder_failure_after_move_leaves_attempting_record_for_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(plan, coordinator)
            real_save = repository.save
            failed = False

            def fail_first_moved(record):
                nonlocal failed
                if not failed and record.expected_moves[0].result_state == MOVE_MOVED:
                    failed = True
                    raise OSError("injected durable update failure")
                return real_save(record)

            with mock.patch.object(repository, "save", side_effect=fail_first_moved):
                outcome = _move(plan, root, authorization, coordinator, repository)

            record = repository.load(outcome.operation_id)
            self.assertEqual(outcome.status, STATUS_INTERRUPTED)
            self.assertEqual(record.expected_moves[0].result_state, MOVE_ATTEMPTING)
            self.assertFalse((root / "a.txt").exists())
            self.assertTrue((root / "Organized" / "a.txt").exists())
            self.assertTrue((root / "b.txt").exists())

            restarted_repository = OrganizationOperationRepository(root)
            inspection = inspect_incomplete_organization_operation(
                operation_id=record.operation_id,
                root=root,
                operation_repository=restarted_repository,
            )
            self.assertEqual(inspection.likely_moved, 1)
            self.assertEqual(inspection.status, STATUS_INTERRUPTED)
            with self.assertRaisesRegex(OrganizationExecutionError, "movement_recovery_required"):
                new_authorization = issue_movement_authorization(plan, coordinator)
                _move(plan, root, new_authorization, coordinator, restarted_repository)
            self.assertTrue((root / "b.txt").exists())


def _move(
    plan: ApprovedOrganizationPlan,
    root: Path,
    authorization,
    coordinator: OrganizationExecutionCoordinator,
    repository: OrganizationOperationRepository,
    confirmation: str = CONFIRM_MOVE_APPROVED_FILES,
):
    return move_approved_organization(
        approved_plan=plan,
        current_scan_generation=plan.scan_generation,
        root=root,
        authorization=authorization,
        coordinator=coordinator,
        operation_repository=repository,
        confirmation=confirmation,
    )


def _assert_preflight_error(
    test: unittest.TestCase,
    plan: ApprovedOrganizationPlan,
    root: Path,
    code: str,
) -> None:
    coordinator = OrganizationExecutionCoordinator()
    repository = OrganizationOperationRepository(root)
    authorization = issue_movement_authorization(plan, coordinator)
    with test.assertRaisesRegex(OrganizationExecutionError, code):
        _move(plan, root, authorization, coordinator, repository)
    test.assertFalse(repository.directory.exists())
    test.assertEqual(
        coordinator.authorization_snapshot(authorization.authorization_id).status,
        "available",
    )


def _approved_plan(
    root: Path,
    source_names: tuple[str, ...],
    *,
    duplicate: bool = False,
) -> ApprovedOrganizationPlan:
    reviewed: list[ReviewedPlanItem] = []
    fingerprints: list[SourceFingerprint] = []
    for index, source_name in enumerate(source_names, start=1):
        source = root / source_name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"{Path(source_name).name}-bytes".encode("utf-8"))
        destination = root / "Organized" / Path(source_name).name
        reviewed.append(
            ReviewedPlanItem(
                id=f"O{index}",
                category="organization",
                plan_item=MovePlanItem(
                    source=source,
                    destination=destination,
                    reason=f"approved move {index}",
                    confidence=90,
                    operation="dry-run move",
                    overwrite_risk=False,
                ),
                decision="approved",
            )
        )
        stat = source.stat()
        fingerprints.append(
            SourceFingerprint(
                item_id=f"scan-one:file-{index}",
                relative_path=Path(source_name),
                size_bytes=stat.st_size,
                modified_time=stat.st_mtime,
            )
        )
    duplicates = (
        (
            DuplicateChoiceDecision(
                choice_id="D1",
                option_id="choose-for-bin",
                selected_copy_ids=("copy-1",),
                selected_paths=(root / "duplicate.txt",),
                group_sha256="a" * 64,
                group_size_bytes=5,
            ),
        )
        if duplicate
        else ()
    )
    return ApprovedOrganizationPlan(
        status="ready_to_move",
        scan_generation="scan-one",
        decision_revision=3,
        approved_at="2026-07-15T01:00:00+00:00",
        reviewed_move_items=tuple(reviewed),
        duplicate_decisions=duplicates,
        source_fingerprints=tuple(fingerprints),
        summary=OrganizationApprovalSummary(
            pending_recommendations=0,
            pending_choices=0,
            accepted_moves=len(reviewed),
            kept_recommendations=0,
            duplicate_copies_for_future_bin=1 if duplicate else 0,
            choices_remaining=0,
            ready_for_approval=True,
            files_to_move=len(reviewed),
        ),
    )
