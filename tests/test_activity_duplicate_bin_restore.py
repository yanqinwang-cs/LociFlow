from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from organizer.application.activity_service import list_activity_entries
from organizer.application.duplicate_bin_models import BATCH_COMPLETED, BATCH_MOVED
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from tests.test_application_duplicate_bin_movement_service import ISSUED
from tests.test_application_duplicate_bin_restore_service import (
    _approve_current_duplicate_group,
    _moved,
    _restore,
    _restore_preview,
)


class DuplicateBinRestoreActivityTests(unittest.TestCase):
    def test_eligible_movement_is_undoable_then_restore_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=2)
            restores = DuplicateBinRestoreRepository(owners.root)

            before = _activity(owners, restores).activity
            movement = next(
                item for item in before if item.type == "duplicate_bin_move"
            )
            self.assertTrue(movement.undoable)

            preview = _restore_preview(
                owners,
                restores,
                tuple(item.bin_item_id for item in owners.batch.items),
            )
            self.assertEqual(len(_activity(owners, restores).activity), 1)
            outcome = _restore(owners, restores, preview)

            after = _activity(owners, restores).activity
            restore = next(item for item in after if item.id == outcome.id)
            movement = next(item for item in after if item.type == "duplicate_bin_move")
            self.assertEqual(restore.title, "Restored 2 copies")
            self.assertEqual(restore.files_restored, 2)
            self.assertFalse(restore.undoable)
            self.assertFalse(movement.undoable)

            historical_ids = {item.id for item in after}
            movement_records = owners.operations.list_records()
            restore_records = restores.list_operations()
            terminal_batch = owners.batches.load(owners.batch.batch_id)
            owners.batches.save_execution_state(
                owners.batch.batch_id,
                expected_version=terminal_batch.batch_version,
                status=BATCH_MOVED,
                items=terminal_batch.items,
                updated_at=(ISSUED + timedelta(minutes=2)).isoformat(),
            )
            copy_contents = {
                member.relative_path: (owners.root / member.relative_path).read_bytes()
                for group in owners.batch.groups
                for member in group.members
            }
            with mock.patch(
                "organizer.executor.shutil.move"
            ) as unexpected_move, mock.patch(
                "organizer.application.duplicate_bin_restore_service.apply_move_plan"
            ) as unexpected_restore:
                owners.batches = DuplicateBinRepository(owners.root)
            unexpected_move.assert_not_called()
            unexpected_restore.assert_not_called()
            self.assertEqual(
                owners.batches.load(owners.batch.batch_id).status,
                BATCH_COMPLETED,
            )
            self.assertEqual(owners.operations.list_records(), movement_records)
            self.assertEqual(restores.list_operations(), restore_records)
            self.assertEqual(
                {item.id for item in _activity(owners, restores).activity},
                historical_ids,
            )
            self.assertEqual(
                {
                    member.relative_path: (
                        owners.root / member.relative_path
                    ).read_bytes()
                    for group in owners.batch.groups
                    for member in group.members
                },
                copy_contents,
            )
            replacement = _approve_current_duplicate_group(
                owners,
                scan_generation="scan-after-restore",
            )
            assert replacement is not None
            self.assertEqual(
                owners.batches.load(owners.batch.batch_id).status,
                BATCH_COMPLETED,
            )
            after_approval = _activity(owners, restores).activity
            self.assertEqual({item.id for item in after_approval}, historical_ids)
            self.assertFalse(
                next(item for item in after_approval if item.id == outcome.id).undoable
            )

    def test_partial_restore_is_truthful_and_not_undoable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _moved(Path(directory).resolve(), selected=2)
            restores = DuplicateBinRestoreRepository(owners.root)
            preview = _restore_preview(
                owners,
                restores,
                tuple(item.bin_item_id for item in owners.batch.items),
            )
            real_move = shutil.move
            calls = 0

            def partial(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected")
                return real_move(source, destination)

            with mock.patch("organizer.executor.shutil.move", side_effect=partial):
                _restore(
                    owners,
                    restores,
                    preview,
                    now=ISSUED + timedelta(minutes=1),
                )
            restore = next(
                item
                for item in _activity(owners, restores).activity
                if item.type == "duplicate_bin_restore"
            )
            self.assertEqual(restore.title, "Duplicate Bin restore partly completed")
            self.assertEqual((restore.files_restored, restore.files_failed), (1, 1))
            self.assertFalse(restore.undoable)


def _activity(owners, restores):
    return list_activity_entries(
        operation_repository=OrganizationOperationRepository(owners.root),
        undo_repository=OrganizationUndoRepository(owners.root),
        duplicate_bin_operation_repository=owners.operations,
        duplicate_bin_batch_repository=owners.batches,
        duplicate_bin_restore_repository=restores,
    )
