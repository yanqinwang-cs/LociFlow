import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from organizer.application.activity_service import list_activity_entries
from organizer.application.duplicate_bin_movement_service import (
    DuplicateBinMovementError,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from tests.test_application_duplicate_bin_movement_service import (
    ISSUED,
    _execute,
    _owners,
    _preview,
)


class DuplicateBinActivityTests(unittest.TestCase):
    def test_approval_preview_and_preflight_failure_create_no_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            preview = _preview(owners, issued=ISSUED)
            retained = next(
                member
                for group in owners.batch.groups
                for member in group.members
                if member.member_id in group.retained_member_ids
            )
            (owners.root / retained.relative_path).write_text(
                "changed", encoding="utf-8"
            )
            with self.assertRaises(DuplicateBinMovementError):
                _execute(owners, preview, now=ISSUED + timedelta(minutes=1))

            result = _activity(owners)
            self.assertEqual(result.to_dict(), {"activity": []})

    def test_verified_movement_projects_safe_truthful_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=2)
            preview = _preview(owners, issued=ISSUED)
            operation = _execute(
                owners,
                preview,
                now=ISSUED + timedelta(minutes=1),
            )

            entries = _activity(owners).activity
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry.id, operation.id)
            self.assertEqual(entry.type, "duplicate_bin_move")
            self.assertEqual(entry.title, "Moved 2 copies to Duplicate Bin")
            self.assertEqual(entry.files_moved, 2)
            self.assertFalse(entry.undoable)
            serialized = json.dumps(entry.to_dict()).lower()
            self.assertNotIn("sha256", serialized)
            self.assertNotIn("duplicate_bin/items", serialized)
            self.assertNotIn(owners.batch.batch_id, serialized)
            self.assertNotIn("original", serialized)

    def test_partial_movement_appears_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=2)
            preview = _preview(owners, issued=ISSUED)
            real_move = shutil.move
            count = 0

            def partial(source: str, destination: str) -> str:
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("injected")
                return str(real_move(source, destination))

            with mock.patch("organizer.executor.shutil.move", side_effect=partial):
                _execute(owners, preview, now=ISSUED + timedelta(minutes=1))
            entry = _activity(owners).activity[0]
            self.assertEqual(entry.title, "Duplicate Bin move partly completed")
            self.assertEqual((entry.files_moved, entry.files_failed), (1, 1))
            self.assertFalse(entry.undoable)

    def test_failed_attempt_needs_attention_and_is_not_undoable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            preview = _preview(owners, issued=ISSUED)
            with mock.patch(
                "organizer.executor.shutil.move",
                side_effect=OSError("injected"),
            ):
                _execute(owners, preview, now=ISSUED + timedelta(minutes=1))
            entry = _activity(owners).activity[0]
            self.assertEqual(entry.title, "Duplicate Bin move needs attention")
            self.assertEqual((entry.files_moved, entry.files_failed), (0, 1))
            self.assertFalse(entry.undoable)

    def test_interruption_before_first_item_attempt_creates_no_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            preview = _preview(owners, issued=ISSUED)

            def interrupt_before_item(plan, root, *, log_dir_name, recorder):
                del root, log_dir_name
                recorder.before_batch(tuple(plan))
                raise RuntimeError("injected before first item")

            with mock.patch(
                "organizer.application.duplicate_bin_movement_service.apply_move_plan",
                side_effect=interrupt_before_item,
            ):
                operation = _execute(
                    owners,
                    preview,
                    now=ISSUED + timedelta(minutes=1),
                )
            self.assertEqual(operation.status, "interrupted")
            self.assertIsNone(operation.started_at)
            self.assertEqual(_activity(owners).activity, ())


def _activity(owners):
    return list_activity_entries(
        operation_repository=OrganizationOperationRepository(owners.root),
        undo_repository=OrganizationUndoRepository(owners.root),
        duplicate_bin_operation_repository=owners.operations,
    )


if __name__ == "__main__":
    unittest.main()
