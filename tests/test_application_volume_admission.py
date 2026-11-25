from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from organizer.application.activity_service import list_activity_entries
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.execution_service import (
    CONFIRM_MOVE_APPROVED_FILES,
    OrganizationExecutionError,
    issue_movement_authorization,
    move_approved_organization,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationInspector,
)
from organizer.application.organization_movement_service import (
    OrganizationMovementError,
    preview_organization_movement,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.volume_admission import (
    OrganizationVolumeAdmission,
    OrganizationVolumePathPair,
    VolumeAdmissionError,
    VolumeAdmissionState,
)
from tests.test_application_execution_service import _approved_plan


class ApplicationVolumeAdmissionTests(unittest.TestCase):
    def test_neutral_relative_path_batch_is_root_bound_and_creates_no_destination(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "Documents" / "a.txt"
            source.parent.mkdir()
            source.write_bytes(b"a")
            pairs = (
                OrganizationVolumePathPair(
                    "Documents/a.txt", "Inbox/a.txt"
                ),
            )

            result = OrganizationVolumeAdmission(root).inspect_path_pairs(pairs)

            self.assertEqual(result.state, VolumeAdmissionState.ALLOWED)
            self.assertFalse((root / "Inbox").exists())
            self.assertNotIn("Documents", repr(pairs[0]))

    def test_neutral_batch_rejects_cross_volume_unsafe_and_indeterminate_completely(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = root / "Documents" / "a.txt"
            second = root / "Documents" / "b.txt"
            first.parent.mkdir()
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            pairs = tuple(
                OrganizationVolumePathPair(
                    f"Documents/{name}.txt", f"Inbox/{name}.txt"
                )
                for name in ("a", "b")
            )
            root_device = os.lstat(root).st_dev
            cross = OrganizationVolumeAdmission(
                root,
                lstat_reader=_lstat_with_device(second, root_device + 1),
            )
            self.assertEqual(
                cross.inspect_path_pairs(pairs).state,
                VolumeAdmissionState.CROSS_VOLUME,
            )
            with self.assertRaises(VolumeAdmissionError):
                cross.require_same_volume_path_pairs(pairs)
            self.assertFalse((root / "Inbox").exists())

            unsafe = (
                OrganizationVolumePathPair("../outside", "Inbox/a.txt"),
            )
            self.assertEqual(
                OrganizationVolumeAdmission(root).inspect_path_pairs(unsafe).state,
                VolumeAdmissionState.UNSAFE,
            )

            def unreadable(path: Path):
                if Path(path) == root:
                    raise OSError("private device detail")
                return os.lstat(path)

            self.assertEqual(
                OrganizationVolumeAdmission(
                    root, lstat_reader=unreadable
                ).inspect_path_pairs(pairs).state,
                VolumeAdmissionState.INDETERMINATE,
            )

    def test_selected_root_and_same_device_plan_are_allowed_without_creating_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            destination_parent = root / "Organized"

            result = OrganizationVolumeAdmission(root).inspect(plan)

            self.assertEqual(result.state, VolumeAdmissionState.ALLOWED)
            self.assertFalse(destination_parent.exists())

    def test_source_destination_and_mixed_cross_volume_plans_reject_completely(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            root_device = os.lstat(root).st_dev
            first_source = plan.reviewed_move_items[0].plan_item.source

            source_cross = OrganizationVolumeAdmission(
                root,
                lstat_reader=_lstat_with_device(first_source, root_device + 1),
            )
            destination_cross = OrganizationVolumeAdmission(
                root,
                stat_reader=_stat_with_device(root, root_device + 1),
            )

            for admission in (source_cross, destination_cross):
                with self.subTest(admission=admission):
                    result = admission.inspect(plan)
                    self.assertEqual(result.state, VolumeAdmissionState.CROSS_VOLUME)
                    with self.assertRaises(VolumeAdmissionError) as raised:
                        admission.inspect_and_require_allowed(plan)
                    self.assertEqual(raised.exception.code, "cross_volume_plan")
                    self.assertEqual(len(plan.reviewed_move_items), 2)
                    self.assertFalse((root / "Organized").exists())

    def test_indeterminate_and_unsafe_ancestry_have_bounded_safe_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "root"
            root.mkdir()
            plan = _approved_plan(root, ("a.txt",))

            def unreadable_destination(path: Path):
                if Path(path) == root:
                    raise OSError(f"secret device failure at {path}")
                return os.stat(path)

            indeterminate = OrganizationVolumeAdmission(
                root,
                stat_reader=unreadable_destination,
            )
            with self.assertRaises(VolumeAdmissionError) as raised:
                indeterminate.inspect_and_require_allowed(plan)
            self.assertEqual(raised.exception.code, "indeterminate_volume")
            self.assertNotIn(str(root), str(raised.exception))
            self.assertNotIn("device", str(raised.exception).lower())

            outside = base / "outside"
            outside.mkdir()
            link = root / "unsafe"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError):
                return
            item = plan.reviewed_move_items[0]
            unsafe_plan = replace(
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
            with self.assertRaises(VolumeAdmissionError) as raised:
                OrganizationVolumeAdmission(root).inspect_and_require_allowed(
                    unsafe_plan
                )
            self.assertEqual(raised.exception.code, "unsafe_volume_path")
            self.assertNotIn(str(root), str(raised.exception))

    def test_authorization_rejection_issues_no_capability_or_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            admission = _cross_source_admission(root, plan)

            with (
                mock.patch.object(
                    coordinator,
                    "issue_authorization",
                    wraps=coordinator.issue_authorization,
                ) as issue,
                self.assertRaisesRegex(
                    OrganizationMovementError, "cross_volume_plan"
                ),
            ):
                preview_organization_movement(
                    approved_plan=plan,
                    browser_session_id="browser-one",
                    current_scan_generation=plan.scan_generation,
                    current_decision_revision=plan.decision_revision,
                    coordinator=coordinator,
                    operation_repository=repository,
                    volume_admission=admission,
                )

            issue.assert_not_called()
            self.assertEqual(repository.list_records(), ())
            self.assertEqual(
                list_activity_entries(
                    operation_repository=repository,
                    undo_repository=OrganizationUndoRepository(root),
                ).activity,
                (),
            )
            self.assertTrue((root / "a.txt").is_file())
            self.assertTrue((root / "b.txt").is_file())

    def test_public_authorization_owner_rejects_before_issuing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt",))
            coordinator = OrganizationExecutionCoordinator()

            with (
                mock.patch.object(
                    coordinator,
                    "issue_authorization",
                    wraps=coordinator.issue_authorization,
                ) as issue,
                self.assertRaisesRegex(
                    OrganizationExecutionError, "cross_volume_plan"
                ),
            ):
                issue_movement_authorization(
                    plan,
                    coordinator,
                    filesystem_operation_admission=FilesystemOperationAdmission(
                        FilesystemOperationInspector(root)
                    ),
                    volume_admission=_cross_source_admission(root, plan),
                )
            issue.assert_not_called()

    def test_confirmation_rechecks_topology_consumes_token_and_moves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan = _approved_plan(root, ("a.txt", "b.txt"))
            coordinator = OrganizationExecutionCoordinator()
            repository = OrganizationOperationRepository(root)
            authorization = issue_movement_authorization(
                plan,
                coordinator,
                filesystem_operation_admission=FilesystemOperationAdmission(
                    FilesystemOperationInspector(root)
                ),
            )

            with (
                mock.patch(
                    "organizer.application.execution_service.apply_move_plan"
                ) as executor,
                self.assertRaisesRegex(
                    OrganizationExecutionError, "cross_volume_plan"
                ),
            ):
                move_approved_organization(
                    approved_plan=plan,
                    current_scan_generation=plan.scan_generation,
                    root=root,
                    authorization=authorization,
                    coordinator=coordinator,
                    operation_repository=repository,
                    confirmation=CONFIRM_MOVE_APPROVED_FILES,
                    volume_admission=_cross_source_admission(root, plan),
                )

            executor.assert_not_called()
            self.assertEqual(repository.list_records(), ())
            self.assertEqual(
                coordinator.authorization_snapshot(
                    authorization.authorization_id
                ).status,
                "consumed",
            )
            self.assertEqual(coordinator.runtime_lifecycle.snapshot().active_count, 0)
            self.assertTrue(coordinator.try_acquire())
            coordinator.release()
            self.assertFalse((root / "AI_Review/operation_logs").exists())
            self.assertEqual(
                list_activity_entries(
                    operation_repository=repository,
                    undo_repository=OrganizationUndoRepository(root),
                ).activity,
                (),
            )
            self.assertTrue((root / "a.txt").is_file())
            self.assertTrue((root / "b.txt").is_file())
            with self.assertRaisesRegex(
                OrganizationExecutionError, "movement_already_started"
            ):
                move_approved_organization(
                    approved_plan=plan,
                    current_scan_generation=plan.scan_generation,
                    root=root,
                    authorization=authorization,
                    coordinator=coordinator,
                    operation_repository=repository,
                    confirmation=CONFIRM_MOVE_APPROVED_FILES,
                )
            self.assertTrue((root / "a.txt").is_file())
            self.assertTrue((root / "b.txt").is_file())


def _cross_source_admission(root: Path, plan) -> OrganizationVolumeAdmission:
    source = plan.reviewed_move_items[0].plan_item.source
    return OrganizationVolumeAdmission(
        root,
        lstat_reader=_lstat_with_device(source, os.lstat(root).st_dev + 1),
    )


def _lstat_with_device(changed_path: Path, device: int):
    def read(path: Path):
        current = os.lstat(path)
        if Path(path) != changed_path:
            return current
        return SimpleNamespace(st_mode=current.st_mode, st_dev=device)

    return read


def _stat_with_device(changed_path: Path, device: int):
    def read(path: Path):
        current = os.stat(path)
        if Path(path) != changed_path:
            return current
        return SimpleNamespace(st_mode=current.st_mode, st_dev=device)

    return read


if __name__ == "__main__":
    unittest.main()
