from __future__ import annotations

from datetime import timedelta
from dataclasses import replace
from pathlib import Path
import json
import shutil
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock
import uuid

import organizer.application.duplicate_bin_restore_service as restore_service
from organizer.application.duplicate_bin_models import (
    BATCH_APPROVED,
    BATCH_COMPLETED,
    BATCH_MOVED,
    ApprovedDuplicateBatch,
    ApprovedDuplicateGroup,
    ApprovedDuplicateMember,
    SelectedDuplicateBinItem,
    canonical_digest,
)
from organizer.application.duplicate_bin_approval_service import (
    DuplicateBinApprovalError,
    create_or_recover_duplicate_batch,
)
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
    DuplicateBinRestoreRepositoryError,
)
from organizer.application.duplicate_bin_restore_service import (
    CONFIRM_RESTORE_COPIES,
    DuplicateBinRestoreError,
    execute_duplicate_bin_restore,
    list_duplicate_bin_items,
    preview_duplicate_bin_activity_undo,
    preview_duplicate_bin_restore,
)
from organizer.application.filesystem_operation_admission import (
    SAFE_RECOVERY_MESSAGE,
    FilesystemOperationBlockedError,
    RecoveryAdmissionState,
)
from organizer.application.organization_decision_models import RECOMMENDATION_KEEP
from organizer.application.organization_decision_service import (
    approve_organization_plan,
    create_organization_decision_session,
    update_organization_decision,
)
from organizer.application.organization_slice_service import (
    build_internal_organization_projection,
)
from organizer.application.scan_service import scan_root
from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeOperationKind,
)
from organizer.duplicates import hash_file
from tests.test_application_duplicate_bin_movement_service import (
    ISSUED,
    _Owners,
    _execute,
    _owners,
    _preview,
)


class DuplicateBinRestoreServiceTests(unittest.TestCase):
    def test_blocked_confirmation_does_not_reconcile_consume_or_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item = owners.batch.items[0]
            preview = _restore_preview(owners, restores, (item.bin_item_id,))
            authorization_before = restores.authorization_path.read_bytes()
            catalog_before = owners.batches.catalog_path.read_bytes()
            admission = mock.Mock()
            admission.root = owners.root
            admission.inspect_and_require_clear.side_effect = (
                FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE
                )
            )

            with self.assertRaisesRegex(
                FilesystemOperationBlockedError, SAFE_RECOVERY_MESSAGE
            ):
                _restore(
                    owners,
                    restores,
                    preview,
                    filesystem_operation_admission=admission,
                )

            self.assertEqual(restores.authorization_path.read_bytes(), authorization_before)
            self.assertEqual(owners.batches.catalog_path.read_bytes(), catalog_before)
            self.assertEqual(restores.list_operations(), ())
            self.assertTrue((owners.root / item.future_relative_path).is_file())
            self.assertFalse((owners.root / item.previous_relative_path).exists())
            self.assertEqual(
                owners.coordinator.runtime_lifecycle.snapshot().active_count, 0
            )
            self.assertTrue(owners.coordinator.try_acquire())
            owners.coordinator.release()

    def test_blocked_activity_undo_reuses_restore_owner_and_activity_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item = owners.batch.items[0]
            preview = _restore_preview(owners, restores, (item.bin_item_id,))
            admission = mock.Mock()
            admission.root = owners.root
            admission.inspect_and_require_clear.side_effect = (
                FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                )
            )

            with self.assertRaises(FilesystemOperationBlockedError):
                _restore(
                    owners,
                    restores,
                    preview,
                    runtime_operation_kind=RuntimeOperationKind.ACTIVITY_UNDO,
                    filesystem_operation_admission=admission,
                )

            self.assertEqual(
                owners.coordinator.runtime_lifecycle.snapshot().active_count, 0
            )
            self.assertEqual(
                restores.load_authorization(preview.confirmation_token).status,
                "available",
            )
            self.assertEqual(restores.list_operations(), ())

    def test_stopping_rejects_restore_previews_without_superseding_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item = owners.batch.items[0]
            existing = _restore_preview(owners, restores, (item.bin_item_id,))
            before = restores.authorization_path.read_bytes()
            owners.coordinator.runtime_lifecycle.begin_shutdown()

            for preview in (
                lambda: _restore_preview(owners, restores, (item.bin_item_id,)),
                lambda: preview_duplicate_bin_activity_undo(
                    movement_operation_id=owners.operations.list_records()[0].operation_id,
                    browser_session_id="browser-one",
                    batch_repository=owners.batches,
                    movement_repository=owners.operations,
                    restore_repository=restores,
                    coordinator=owners.coordinator,
                    issued_at=(ISSUED + timedelta(minutes=1)).isoformat(),
                ),
            ):
                with self.assertRaises(ApplicationStoppingError):
                    preview()

            self.assertEqual(restores.authorization_path.read_bytes(), before)
            self.assertEqual(
                restores.load_authorization(existing.confirmation_token).status,
                "available",
            )
            self.assertEqual(restores.list_operations(), ())

    def test_verified_items_preview_restore_verify_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=2)
            restores = DuplicateBinRestoreRepository(owners.root)
            item_ids = tuple(item.bin_item_id for item in owners.batch.items)
            preview = _restore_preview(owners, restores, item_ids)
            real_move = shutil.move
            observed = []

            def checked_move(source: str, destination: str):
                self.assertEqual(
                    owners.coordinator.runtime_lifecycle.snapshot().active_kinds,
                    (RuntimeOperationKind.DUPLICATE_BIN_RESTORE,),
                )
                operations = restores.list_operations()
                self.assertEqual(len(operations), 1)
                self.assertEqual(operations[0].status, "restoring")
                observed.append((source, destination))
                return real_move(source, destination)

            real_verify = restore_service._verify

            def checked_verify(*args, **kwargs):
                self.assertEqual(
                    owners.coordinator.runtime_lifecycle.snapshot().active_kinds,
                    (RuntimeOperationKind.DUPLICATE_BIN_RESTORE,),
                )
                return real_verify(*args, **kwargs)

            with (
                mock.patch("organizer.executor.shutil.move", side_effect=checked_move),
                mock.patch.object(
                    restore_service,
                    "_verify",
                    side_effect=checked_verify,
                ),
            ):
                result = _restore(owners, restores, preview)

            self.assertEqual(result.status, "restored")
            self.assertEqual(result.copies_restored, 2)
            self.assertEqual(len(observed), 2)
            operation = restores.load_operation(result.id)
            self.assertEqual(operation.verification.status, "passed")
            self.assertTrue(operation.executor_log_path)
            for item in owners.batch.items:
                self.assertTrue((owners.root / item.previous_relative_path).is_file())
                self.assertFalse((owners.root / item.future_relative_path).exists())
            durable_batch = owners.batches.load(owners.batch.batch_id)
            self.assertEqual(durable_batch.status, BATCH_COMPLETED)
            self.assertTrue(
                all(item.state == "restored" for item in durable_batch.items)
            )
            catalog = json.loads(
                owners.batches.catalog_path.read_text(encoding="utf-8")
            )
            self.assertEqual(catalog["active_claims"], [])
            restarted_batches = DuplicateBinRepository(owners.root)
            self.assertEqual(
                restarted_batches.load(owners.batch.batch_id).status,
                BATCH_COMPLETED,
            )
            self.assertEqual(
                list_duplicate_bin_items(owners.batches, owners.operations, restores),
                (),
            )
            self.assertEqual(
                owners.coordinator.runtime_lifecycle.snapshot().active_count,
                0,
            )

            with mock.patch(
                "organizer.application.duplicate_bin_restore_service.apply_move_plan"
            ) as apply_again:
                replay = _restore(
                    owners,
                    restores,
                    preview,
                    now=ISSUED + timedelta(minutes=2),
                )

            self.assertEqual(replay.id, result.id)
            apply_again.assert_not_called()

            replacement = _approve_current_duplicate_group(
                owners,
                scan_generation="scan-after-full-restore",
            )
            assert replacement is not None
            self.assertNotEqual(replacement.batch_id, owners.batch.batch_id)
            self.assertEqual(
                owners.batches.load(owners.batch.batch_id).status,
                BATCH_COMPLETED,
            )

    def test_stopping_rejects_before_restore_intent_or_filesystem_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item = owners.batch.items[0]
            preview = _restore_preview(
                owners,
                restores,
                (item.bin_item_id,),
            )
            owners.coordinator.runtime_lifecycle.begin_shutdown()

            with self.assertRaises(ApplicationStoppingError):
                _restore(owners, restores, preview)

            self.assertTrue((owners.root / item.future_relative_path).is_file())
            self.assertFalse((owners.root / item.previous_relative_path).exists())
            self.assertEqual(restores.list_operations(), ())
            self.assertEqual(
                owners.coordinator.runtime_lifecycle.snapshot().active_count,
                0,
            )

    def test_duplicate_bin_activity_undo_uses_activity_undo_runtime_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            preview = _restore_preview(
                owners,
                restores,
                (owners.batch.items[0].bin_item_id,),
            )
            observed = []
            real_verify = restore_service._verify

            def checked_verify(*args, **kwargs):
                observed.append(
                    owners.coordinator.runtime_lifecycle.snapshot().active_kinds
                )
                return real_verify(*args, **kwargs)

            with mock.patch.object(
                restore_service,
                "_verify",
                side_effect=checked_verify,
            ):
                result = _restore(
                    owners,
                    restores,
                    preview,
                    runtime_operation_kind=RuntimeOperationKind.ACTIVITY_UNDO,
                )

            self.assertEqual(result.status, "restored")
            self.assertEqual(observed, [(RuntimeOperationKind.ACTIVITY_UNDO,)])
            self.assertEqual(
                owners.coordinator.runtime_lifecycle.snapshot().active_count,
                0,
            )

    def test_authorization_expiry_overlap_and_nonoverlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=2)
            restores = DuplicateBinRestoreRepository(owners.root)
            first_id, second_id = sorted(
                item.bin_item_id for item in owners.batch.items
            )
            first = _restore_preview(owners, restores, (first_id,))
            nonoverlap = _restore_preview(
                owners,
                restores,
                (second_id,),
                issued=ISSUED + timedelta(minutes=1),
            )
            replacement = _restore_preview(
                owners,
                restores,
                (first_id,),
                issued=ISSUED + timedelta(minutes=2),
            )
            self.assertEqual(
                restores.load_authorization(first.confirmation_token).status,
                "superseded",
            )
            self.assertEqual(
                restores.load_authorization(nonoverlap.confirmation_token).status,
                "available",
            )
            self.assertEqual(
                timedelta(minutes=15),
                _time(replacement.expires_at) - (ISSUED + timedelta(minutes=2)),
            )
            with self.assertRaisesRegex(
                DuplicateBinRestoreError, "confirmation_expired"
            ):
                _restore(
                    owners,
                    restores,
                    nonoverlap,
                    now=ISSUED + timedelta(minutes=16),
                )

    def test_activity_preview_uses_all_remaining_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=2)
            restores = DuplicateBinRestoreRepository(owners.root)
            movement = owners.operations.find_for_batch(owners.batch.batch_id)
            assert movement is not None
            preview = preview_duplicate_bin_activity_undo(
                movement_operation_id=movement.operation_id,
                browser_session_id="browser-one",
                batch_repository=owners.batches,
                movement_repository=owners.operations,
                restore_repository=restores,
                coordinator=owners.coordinator,
                issued_at=ISSUED.isoformat(),
            )
            authorization = restores.load_authorization(preview.confirmation_token)
            self.assertEqual(
                authorization.selected_bin_item_ids,
                tuple(sorted(item.bin_item_id for item in owners.batch.items)),
            )
            selected_preview = _restore_preview(
                owners,
                restores,
                tuple(item.bin_item_id for item in owners.batch.items),
                issued=ISSUED + timedelta(minutes=1),
            )
            selected_authorization = restores.load_authorization(
                selected_preview.confirmation_token
            )
            self.assertEqual(
                selected_authorization.selected_bin_item_ids,
                authorization.selected_bin_item_ids,
            )

    def test_activity_preview_excludes_restored_and_other_operation_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=2)
            restores = DuplicateBinRestoreRepository(owners.root)
            item_a, item_b = owners.batch.items
            movement_one = owners.operations.find_for_batch(owners.batch.batch_id)
            assert movement_one is not None

            restored_a = _restore_preview(
                owners,
                restores,
                (item_a.bin_item_id,),
            )
            _restore(owners, restores, restored_a)
            partial = owners.batches.load(owners.batch.batch_id)
            self.assertEqual(partial.status, BATCH_MOVED)
            self.assertEqual(
                {item.state for item in partial.items},
                {"available", "restored"},
            )
            catalog = json.loads(
                owners.batches.catalog_path.read_text(encoding="utf-8")
            )
            batch_claims = [
                claim
                for claim in catalog["active_claims"]
                if claim["batch_id"] == owners.batch.batch_id
            ]
            expected_paths = {
                member.relative_path
                for group in owners.batch.groups
                for member in group.members
            }
            self.assertEqual(
                {claim["relative_path"] for claim in batch_claims},
                expected_paths,
            )
            with self.assertRaisesRegex(
                DuplicateBinApprovalError, "duplicate_batch_conflict"
            ):
                _approve_current_duplicate_group(
                    owners,
                    scan_generation="scan-after-partial-restore",
                )
            second = _additional_moved_batch(owners)
            item_c = second.batch.items[0]

            with mock.patch(
                "organizer.application.undo_service.undo_operation_log"
            ) as ordinary_undo_log:
                preview = preview_duplicate_bin_activity_undo(
                    movement_operation_id=movement_one.operation_id,
                    browser_session_id="browser-one",
                    batch_repository=owners.batches,
                    movement_repository=owners.operations,
                    restore_repository=restores,
                    coordinator=owners.coordinator,
                    issued_at=(ISSUED + timedelta(minutes=4)).isoformat(),
                )

            ordinary_undo_log.assert_not_called()
            authorization = restores.load_authorization(preview.confirmation_token)
            self.assertEqual(preview.copies_to_restore, 1)
            self.assertEqual(preview.confirmation, CONFIRM_RESTORE_COPIES)
            self.assertEqual(
                authorization.selected_bin_item_ids,
                (item_b.bin_item_id,),
            )
            self.assertNotIn(item_a.bin_item_id, authorization.selected_bin_item_ids)
            self.assertNotIn(item_c.bin_item_id, authorization.selected_bin_item_ids)

            _restore(
                owners,
                restores,
                preview,
                now=ISSUED + timedelta(minutes=5),
            )
            with self.assertRaisesRegex(
                DuplicateBinRestoreError, "no_restorable_items"
            ):
                preview_duplicate_bin_activity_undo(
                    movement_operation_id=movement_one.operation_id,
                    browser_session_id="browser-one",
                    batch_repository=owners.batches,
                    movement_repository=owners.operations,
                    restore_repository=restores,
                    coordinator=owners.coordinator,
                    issued_at=(ISSUED + timedelta(minutes=6)).isoformat(),
                )

    def test_cross_operation_restore_is_one_durable_ordered_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            second = _additional_moved_batch(owners)
            restores = DuplicateBinRestoreRepository(owners.root)
            first_id = owners.batch.items[0].bin_item_id
            second_id = second.batch.items[0].bin_item_id
            self.assertGreater(first_id, second_id)

            preview = _restore_preview(
                owners,
                restores,
                (second_id, first_id),
                issued=ISSUED + timedelta(minutes=4),
            )
            authorization = restores.load_authorization(preview.confirmation_token)
            self.assertEqual(len(authorization.parent_movement_operation_ids), 2)
            from organizer.application import duplicate_bin_restore_service

            with mock.patch.object(
                duplicate_bin_restore_service,
                "apply_move_plan",
                wraps=duplicate_bin_restore_service.apply_move_plan,
            ) as apply_plan:
                result = _restore(
                    owners,
                    restores,
                    preview,
                    now=ISSUED + timedelta(minutes=5),
                )

            apply_plan.assert_called_once()
            operation = restores.load_operation(result.id)
            self.assertEqual(operation.selected_bin_item_ids, (first_id, second_id))
            self.assertEqual(
                tuple(item.bin_item_id for item in operation.items),
                (first_id, second_id),
            )
            self.assertEqual(
                tuple(item.previous_destination for item in operation.items),
                (
                    owners.batch.items[0].previous_relative_path,
                    second.batch.items[0].previous_relative_path,
                ),
            )
            restarted = DuplicateBinRestoreRepository(owners.root)
            self.assertEqual(restarted.load_operation(result.id), operation)

    def test_partial_parent_movement_exposes_only_verified_available_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=2)
            preview = _preview(owners, issued=ISSUED)
            real_move = shutil.move
            calls = 0

            def partial(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected")
                return real_move(source, destination)

            with mock.patch("organizer.executor.shutil.move", side_effect=partial):
                _execute(owners, preview, now=ISSUED + timedelta(seconds=1))
            restores = DuplicateBinRestoreRepository(owners.root)
            batch = owners.batches.load(owners.batch.batch_id)
            available = tuple(item for item in batch.items if item.state == "available")
            rejected = tuple(item for item in batch.items if item.state != "available")
            self.assertEqual((len(available), len(rejected)), (1, 1))
            restore_preview = _restore_preview(
                owners, restores, (available[0].bin_item_id,)
            )
            self.assertEqual(restore_preview.copies_to_restore, 1)
            with self.assertRaisesRegex(
                DuplicateBinRestoreError, "bin_item_not_restorable"
            ):
                _restore_preview(owners, restores, (rejected[0].bin_item_id,))

    def test_authorization_is_session_and_root_bound_and_consumed_by_operation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item_id = owners.batch.items[0].bin_item_id
            preview = _restore_preview(owners, restores, (item_id,))
            with self.assertRaisesRegex(
                DuplicateBinRestoreError, "invalid_confirmation_token"
            ):
                execute_duplicate_bin_restore(
                    browser_session_id="browser-two",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_RESTORE_COPIES,
                    batch_repository=owners.batches,
                    movement_repository=owners.operations,
                    restore_repository=restores,
                    coordinator=owners.coordinator,
                    now=(ISSUED + timedelta(minutes=1)).isoformat(),
                )
            result = _restore(owners, restores, preview)
            authorization = restores.load_authorization(preview.confirmation_token)
            self.assertEqual(authorization.status, "consumed")
            self.assertEqual(authorization.restore_operation_id, result.id)

            with tempfile.TemporaryDirectory() as other_directory:
                other_root = Path(other_directory).resolve()
                other_restores = DuplicateBinRestoreRepository(other_root)
                other_restores.authorization_directory.mkdir(parents=True)
                shutil.copy2(
                    restores.authorization_path,
                    other_restores.authorization_path,
                )
                with self.assertRaisesRegex(
                    DuplicateBinRestoreRepositoryError,
                    "restore_authorization_malformed",
                ):
                    other_restores.load_authorization(preview.confirmation_token)

    def test_authorization_rejects_changed_batch_version_and_item_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=2)
            restores = DuplicateBinRestoreRepository(owners.root)
            first, second = owners.batches.load(owners.batch.batch_id).items
            preview = _restore_preview(owners, restores, (first.bin_item_id,))
            batch = owners.batches.load(owners.batch.batch_id)
            owners.batches.save_execution_state(
                batch.batch_id,
                expected_version=batch.batch_version,
                status=batch.status,
                items=tuple(
                    replace(item, state="attention")
                    if item.bin_item_id == second.bin_item_id
                    else item
                    for item in batch.items
                ),
                updated_at=(ISSUED + timedelta(seconds=30)).isoformat(),
            )
            with self.assertRaisesRegex(
                DuplicateBinRestoreError, "restore_authorization_stale"
            ):
                _restore(owners, restores, preview)
            self.assertEqual(restores.list_operations(), ())

    def test_zero_missing_attention_and_already_restored_are_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            with self.assertRaisesRegex(
                DuplicateBinRestoreError, "no_restorable_items"
            ):
                _restore_preview(owners, restores, ())
            with self.assertRaisesRegex(DuplicateBinRestoreError, "bin_item_not_found"):
                _restore_preview(
                    owners, restores, ("00000000-0000-0000-0000-000000000001",)
                )

            batch = owners.batches.load(owners.batch.batch_id)
            owners.batches.save_execution_state(
                batch.batch_id,
                expected_version=batch.batch_version,
                status=batch.status,
                items=tuple(replace(item, state="attention") for item in batch.items),
                updated_at=ISSUED.isoformat(),
            )
            with self.assertRaisesRegex(
                DuplicateBinRestoreError, "bin_item_not_restorable"
            ):
                _restore_preview(owners, restores, (batch.items[0].bin_item_id,))

        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item = owners.batch.items[0]
            preview = _restore_preview(owners, restores, (item.bin_item_id,))
            _restore(owners, restores, preview)
            with self.assertRaisesRegex(
                DuplicateBinRestoreError, "bin_item_not_restorable"
            ):
                _restore_preview(owners, restores, (item.bin_item_id,))

    def test_changed_item_and_occupied_destination_fail_before_operation(self) -> None:
        for case in ("changed", "occupied"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                owners = _moved(Path(directory).resolve(), selected=1)
                restores = DuplicateBinRestoreRepository(owners.root)
                item = owners.batch.items[0]
                preview = _restore_preview(owners, restores, (item.bin_item_id,))
                if case == "changed":
                    (owners.root / item.future_relative_path).write_text(
                        "changed", encoding="utf-8"
                    )
                    expected = "bin_item_changed"
                else:
                    destination = owners.root / item.previous_relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text("occupied", encoding="utf-8")
                    expected = "restore_destination_occupied"
                with self.assertRaisesRegex(DuplicateBinRestoreError, expected):
                    _restore(owners, restores, preview)
                self.assertEqual(restores.list_operations(), ())
                self.assertTrue((owners.root / item.future_relative_path).exists())

    def test_safe_missing_parent_is_created_only_after_durable_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item = owners.batch.items[0]
            previous_parent = (owners.root / item.previous_relative_path).parent
            previous_parent.rmdir()
            preview = _restore_preview(owners, restores, (item.bin_item_id,))
            self.assertFalse(previous_parent.exists())
            _restore(owners, restores, preview)
            self.assertTrue(previous_parent.is_dir())
            self.assertTrue((owners.root / item.previous_relative_path).is_file())

    def test_symlink_source_and_unsafe_or_occupied_destinations_reject(self) -> None:
        for case in (
            "source_symlink",
            "directory",
            "destination_symlink",
            "parent_symlink",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                owners = _moved(Path(directory).resolve(), selected=1)
                restores = DuplicateBinRestoreRepository(owners.root)
                item = owners.batch.items[0]
                source = owners.root / item.future_relative_path
                destination = owners.root / item.previous_relative_path
                if case == "source_symlink":
                    target = owners.root / "safe-target.txt"
                    target.write_bytes(source.read_bytes())
                    source.unlink()
                    try:
                        source.symlink_to(target)
                    except OSError:
                        self.skipTest("symlinks unavailable")
                    expected = "unsafe_restore_path"
                elif case == "directory":
                    destination.mkdir(parents=True)
                    expected = "restore_destination_occupied"
                elif case == "destination_symlink":
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    target = owners.root / "target.txt"
                    target.write_text("target", encoding="utf-8")
                    try:
                        destination.symlink_to(target)
                    except OSError:
                        self.skipTest("symlinks unavailable")
                    expected = "restore_destination_occupied"
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.parent.rmdir()
                    safe_parent = owners.root / "safe-parent"
                    safe_parent.mkdir()
                    try:
                        destination.parent.symlink_to(
                            safe_parent, target_is_directory=True
                        )
                    except OSError:
                        self.skipTest("symlinks unavailable")
                    expected = "unsafe_restore_parent"
                preview = _restore_preview(owners, restores, (item.bin_item_id,))
                with self.assertRaisesRegex(DuplicateBinRestoreError, expected):
                    _restore(owners, restores, preview)
                self.assertEqual(restores.list_operations(), ())

    def test_verification_failures_enter_attention_without_rollback(self) -> None:
        for case in (
            "source_present",
            "destination_missing",
            "destination_symlink",
            "hash_mismatch",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                owners = _moved(Path(directory).resolve(), selected=1)
                restores = DuplicateBinRestoreRepository(owners.root)
                item = owners.batch.items[0]
                preview = _restore_preview(owners, restores, (item.bin_item_id,))
                real_move = shutil.move

                def unverifiable(source, destination):
                    source_path = Path(source)
                    destination_path = Path(destination)
                    if case == "source_present":
                        destination_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_path, destination_path)
                        return str(destination_path)
                    result = real_move(source, destination)
                    if case == "destination_missing":
                        destination_path.unlink()
                    elif case == "destination_symlink":
                        destination_path.unlink()
                        target = owners.root / "restore-verification-target.txt"
                        target.write_text("target", encoding="utf-8")
                        destination_path.symlink_to(target)
                    else:
                        destination_path.write_text("changed", encoding="utf-8")
                    return result

                try:
                    with mock.patch(
                        "organizer.executor.shutil.move", side_effect=unverifiable
                    ):
                        result = _restore(owners, restores, preview)
                except OSError:
                    if case == "destination_symlink":
                        self.skipTest("symlinks unavailable")
                    raise
                self.assertEqual(result.status, "verification_failed")
                operation = restores.load_operation(result.id)
                self.assertEqual(operation.items[0].state, "attention")
                self.assertEqual(operation.verification.status, "failed")
                self.assertEqual(
                    owners.batches.load(owners.batch.batch_id).items[0].state,
                    "attention",
                )
                if case == "source_present":
                    self.assertTrue((owners.root / item.future_relative_path).exists())
                    self.assertTrue(
                        (owners.root / item.previous_relative_path).exists()
                    )
                    projection = list_duplicate_bin_items(
                        owners.batches, owners.operations, restores
                    )
                    self.assertEqual(len(projection), 1)
                    self.assertFalse(projection[0].restore_available)
                    self.assertEqual(
                        projection[0].attention_message,
                        "This copy needs attention.",
                    )
                else:
                    self.assertFalse((owners.root / item.future_relative_path).exists())

    def test_interrupted_operation_replays_without_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item = owners.batch.items[0]
            preview = _restore_preview(owners, restores, (item.bin_item_id,))
            with mock.patch(
                "organizer.application.duplicate_bin_restore_service.apply_move_plan",
                side_effect=RuntimeError("injected interruption"),
            ):
                result = _restore(owners, restores, preview)
            self.assertEqual(result.status, "interrupted")
            self.assertTrue(result.recovery_required)
            self.assertTrue((owners.root / item.future_relative_path).exists())
            self.assertFalse((owners.root / item.previous_relative_path).exists())

            restarted = DuplicateBinRestoreRepository(owners.root)
            with (
                mock.patch(
                    "organizer.application.duplicate_bin_restore_service.apply_move_plan"
                ) as retry,
                self.assertRaises(FilesystemOperationBlockedError),
            ):
                execute_duplicate_bin_restore(
                    browser_session_id="browser-one",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_RESTORE_COPIES,
                    batch_repository=owners.batches,
                    movement_repository=owners.operations,
                    restore_repository=restarted,
                    coordinator=owners.coordinator,
                    now=(ISSUED + timedelta(minutes=2)).isoformat(),
                )
            retry.assert_not_called()

    def test_inconsistent_terminal_replay_is_blocked_without_catalog_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item = owners.batch.items[0]
            preview = _restore_preview(owners, restores, (item.bin_item_id,))
            with mock.patch(
                "organizer.application.duplicate_bin_restore_service._update_batch_items",
                side_effect=RuntimeError("injected response-loss window"),
            ):
                with self.assertRaisesRegex(RuntimeError, "response-loss"):
                    _restore(owners, restores, preview)

            operation = restores.find_for_authorization(preview.confirmation_token)
            assert operation is not None
            self.assertEqual(operation.status, "restored")
            self.assertEqual(
                owners.batches.load(owners.batch.batch_id).items[0].state,
                "available",
            )
            restarted = DuplicateBinRestoreRepository(owners.root)
            before = owners.batches.catalog_path.read_bytes()
            with (
                mock.patch(
                    "organizer.application.duplicate_bin_restore_service.apply_move_plan"
                ) as retry,
                self.assertRaises(FilesystemOperationBlockedError),
            ):
                execute_duplicate_bin_restore(
                    browser_session_id="browser-one",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_RESTORE_COPIES,
                    batch_repository=owners.batches,
                    movement_repository=owners.operations,
                    restore_repository=restarted,
                    coordinator=owners.coordinator,
                    now=(ISSUED + timedelta(minutes=2)).isoformat(),
                )
            retry.assert_not_called()
            self.assertEqual(owners.batches.catalog_path.read_bytes(), before)

    def test_shared_coordinator_blocks_preview_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            item_id = owners.batch.items[0].bin_item_id
            self.assertTrue(owners.coordinator.try_acquire())
            try:
                with self.assertRaisesRegex(
                    DuplicateBinRestoreError, "movement_or_undo_in_progress"
                ):
                    _restore_preview(owners, restores, (item_id,))
            finally:
                owners.coordinator.release()
            preview = _restore_preview(owners, restores, (item_id,))
            self.assertTrue(owners.coordinator.try_acquire())
            try:
                with self.assertRaisesRegex(
                    DuplicateBinRestoreError, "movement_or_undo_in_progress"
                ):
                    _restore(owners, restores, preview)
            finally:
                owners.coordinator.release()
            self.assertEqual(restores.list_operations(), ())

    def test_admitted_restore_completes_after_shutdown_begins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=1)
            restores = DuplicateBinRestoreRepository(owners.root)
            preview = _restore_preview(
                owners,
                restores,
                (owners.batch.items[0].bin_item_id,),
            )
            verifying = Event()
            continue_verification = Event()
            outcomes = []
            real_verify = restore_service._verify

            def paused_verify(*args, **kwargs):
                verifying.set()
                continue_verification.wait(timeout=2)
                return real_verify(*args, **kwargs)

            worker = Thread(
                target=lambda: outcomes.append(_restore(owners, restores, preview))
            )
            with mock.patch.object(
                restore_service,
                "_verify",
                side_effect=paused_verify,
            ):
                worker.start()
                self.assertTrue(verifying.wait(1))
                stopping = owners.coordinator.runtime_lifecycle.begin_shutdown()
                self.assertEqual(stopping.active_count, 1)
                continue_verification.set()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0].status, "restored")
            self.assertEqual(
                owners.coordinator.runtime_lifecycle.wait_until_idle().active_count,
                0,
            )


def _moved(root: Path, *, selected: int):
    owners = _owners(root, selected=selected)
    movement_preview = _preview(owners, issued=ISSUED)
    _execute(owners, movement_preview, now=ISSUED + timedelta(seconds=1))
    return owners


def _approve_current_duplicate_group(owners, *, scan_generation: str):
    projection = build_internal_organization_projection(
        scan_result=scan_root(owners.root),
        scan_generation=scan_generation,
        completed_at=ISSUED,
    )
    session = create_organization_decision_session(
        projection,
        timestamp=(ISSUED + timedelta(minutes=10)).isoformat(),
    )
    for decision in session.recommendation_decisions:
        session = update_organization_decision(
            session,
            projection,
            kind="recommendation",
            change_id=decision.change_id,
            decision=RECOMMENDATION_KEEP,
        )
    for decision in session.destination_decisions:
        choice = projection.destination_choices_by_id[decision.choice_id]
        keep_option = next(
            option_id for option_id, move in choice.options if move is None
        )
        session = update_organization_decision(
            session,
            projection,
            kind="choice",
            choice_id=decision.choice_id,
            option_id=keep_option,
        )

    approved_paths = {
        member.relative_path
        for group in owners.batch.groups
        for member in group.members
    }
    selected_target = False
    for decision in session.duplicate_decisions:
        choice = projection.duplicate_choices_by_id[decision.choice_id]
        offered_paths = {item.relative_path.as_posix() for item in choice.files}
        is_target = bool(offered_paths.intersection(approved_paths))
        session = update_organization_decision(
            session,
            projection,
            kind="choice",
            choice_id=decision.choice_id,
            option_id="choose-for-bin" if is_target else "keep-all",
            copy_ids=(choice.copy_ids[0],) if is_target else (),
        )
        selected_target = selected_target or is_target
    if not selected_target:
        raise AssertionError("restored duplicate group was not projected")

    approved = approve_organization_plan(
        session,
        projection,
        timestamp=(ISSUED + timedelta(minutes=11)).isoformat(),
    )
    assert approved.approved_plan is not None
    return create_or_recover_duplicate_batch(
        approved_plan=approved.approved_plan,
        projection=projection,
        browser_session_id="later-browser",
        repository=owners.batches,
    )


def _additional_moved_batch(owners):
    content = b"another exact duplicate group"
    selected_path = "Second/copy.txt"
    retained_path = "Second/retained.txt"
    for relative in (selected_path, retained_path):
        path = owners.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    first_id = owners.batch.items[0].bin_item_id
    for seed in range(1000):
        batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"additional-batch:{seed}"))
        selected_member_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"additional-member:{seed}:selected")
        )
        bin_item_id = str(uuid.uuid5(uuid.UUID(batch_id), selected_member_id))
        if bin_item_id < first_id:
            break
    else:  # pragma: no cover - UUID ordering makes this practically unreachable.
        raise AssertionError("could not construct deterministic test ordering")
    retained_member_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"additional-member:{seed}:retained")
    )
    members = tuple(
        sorted(
            (
                ApprovedDuplicateMember(
                    member_id=selected_member_id,
                    relative_path=selected_path,
                    size_bytes=len(content),
                    modified_time=(owners.root / selected_path).stat().st_mtime,
                ),
                ApprovedDuplicateMember(
                    member_id=retained_member_id,
                    relative_path=retained_path,
                    size_bytes=len(content),
                    modified_time=(owners.root / retained_path).stat().st_mtime,
                ),
            ),
            key=lambda item: item.relative_path,
        )
    )
    digest = hash_file(owners.root / selected_path)
    item = SelectedDuplicateBinItem(
        bin_item_id=bin_item_id,
        selected_member_id=selected_member_id,
        previous_relative_path=selected_path,
        future_relative_path=(f"AI_Review/duplicate_bin/items/{bin_item_id}/copy.txt"),
        basename="copy.txt",
        size_bytes=len(content),
        kind="text",
        extension=".txt",
        approved_sha256=digest,
        approval_metadata_fingerprint=canonical_digest(
            {
                "schema": "loci-duplicate-approval-metadata-v1",
                "size_bytes": len(content),
                "modified_time": (owners.root / selected_path).stat().st_mtime,
            }
        ),
    )
    stamp = (ISSUED + timedelta(minutes=2)).isoformat()
    batch = ApprovedDuplicateBatch(
        batch_id=batch_id,
        configured_root_identity=owners.batches.configured_root_identity,
        created_by_session_id="approval-browser",
        originating_scan_generation="scan-two",
        decision_revision=2,
        approved_at=stamp,
        approved_plan_digest="c" * 64,
        duplicate_batch_digest="d" * 64,
        batch_version=1,
        status=BATCH_APPROVED,
        created_at=stamp,
        updated_at=stamp,
        groups=(
            ApprovedDuplicateGroup(
                group_id=str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"additional-group:{seed}")
                ),
                choice_id="D2",
                approved_sha256=digest,
                approved_size_bytes=len(content),
                members=members,
                selected_member_ids=(selected_member_id,),
                retained_member_ids=(retained_member_id,),
            ),
        ),
        items=(item,),
    )
    owners.batches.create_or_recover_approved_batch(batch)
    second = _Owners(
        owners.root,
        batch,
        owners.batches,
        owners.authorizations,
        owners.operations,
        owners.coordinator,
    )
    preview = _preview(second, issued=ISSUED + timedelta(minutes=2))
    _execute(second, preview, now=ISSUED + timedelta(minutes=3))
    return second


def _restore_preview(owners, restores, item_ids, *, issued=ISSUED):
    return preview_duplicate_bin_restore(
        item_ids=tuple(item_ids),
        browser_session_id="browser-one",
        batch_repository=owners.batches,
        movement_repository=owners.operations,
        restore_repository=restores,
        coordinator=owners.coordinator,
        issued_at=issued.isoformat(),
    )


def _restore(
    owners,
    restores,
    preview,
    *,
    now=ISSUED + timedelta(minutes=1),
    runtime_operation_kind=RuntimeOperationKind.DUPLICATE_BIN_RESTORE,
    filesystem_operation_admission=None,
):
    return execute_duplicate_bin_restore(
        browser_session_id="browser-one",
        confirmation_token=preview.confirmation_token,
        confirmation=CONFIRM_RESTORE_COPIES,
        batch_repository=owners.batches,
        movement_repository=owners.operations,
        restore_repository=restores,
        coordinator=owners.coordinator,
        now=now.isoformat(),
        runtime_operation_kind=runtime_operation_kind,
        filesystem_operation_admission=filesystem_operation_admission,
    )


def _time(value):
    from datetime import datetime

    return datetime.fromisoformat(value)
