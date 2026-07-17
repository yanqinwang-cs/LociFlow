from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from organizer.application.activity_service import list_activity_entries
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
