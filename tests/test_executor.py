from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from organizer.executor import (
    MovementRecordingError,
    UndoRecordingError,
    apply_move_plan,
    undo_operation_log,
)
from organizer.models import MovePlanItem, MoveResult


def make_plan_item(source: Path, destination: Path) -> MovePlanItem:
    return MovePlanItem(
        source=source,
        destination=destination,
        reason="exact duplicate of a.txt",
        confidence=100,
        operation="dry-run move",
        overwrite_risk=destination.exists(),
    )


class ExecutorApplyTests(unittest.TestCase):
    def test_optional_recorder_is_called_around_each_filesystem_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.txt"
            destination = root / "Organized" / "a.txt"
            source.write_text("content", encoding="utf-8")
            events: list[str] = []

            class Recorder:
                def before_batch(self, plan_items):
                    events.append("before_batch")

                def before_move(self, index, item):
                    self_test.assertTrue(source.exists())
                    events.append("attempting")

                def after_success(self, index, item, result):
                    self_test.assertFalse(source.exists())
                    events.append("moved")

                def after_failure(self, index, item, result):
                    events.append("failed")

                def after_batch(self, operation_log):
                    events.append("after_batch")

            self_test = self
            apply_move_plan(
                [make_plan_item(source, destination)],
                root,
                recorder=Recorder(),
            )

            self.assertEqual(
                events,
                ["before_batch", "attempting", "moved", "after_batch"],
            )

    def test_recorder_failure_before_move_prevents_filesystem_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.txt"
            destination = root / "Organized" / "a.txt"
            source.write_text("content", encoding="utf-8")

            class Recorder:
                def before_batch(self, plan_items):
                    return None

                def before_move(self, index, item):
                    raise OSError("record unavailable")

                def after_success(self, index, item, result):
                    return None

                def after_failure(self, index, item, result):
                    return None

                def after_batch(self, operation_log):
                    return None

            with self.assertRaisesRegex(MovementRecordingError, "before_move"):
                apply_move_plan(
                    [make_plan_item(source, destination)],
                    root,
                    recorder=Recorder(),
                )

            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_recorder_failure_after_move_writes_legacy_log_and_stops_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.txt"
            second = root / "b.txt"
            first_destination = root / "Organized" / "a.txt"
            second_destination = root / "Organized" / "b.txt"
            first.write_text("a", encoding="utf-8")
            second.write_text("b", encoding="utf-8")

            class Recorder:
                def before_batch(self, plan_items):
                    return None

                def before_move(self, index, item):
                    return None

                def after_success(self, index, item, result):
                    raise OSError("record unavailable")

                def after_failure(self, index, item, result):
                    return None

                def after_batch(self, operation_log):
                    return None

            with self.assertRaises(MovementRecordingError) as captured:
                apply_move_plan(
                    [
                        make_plan_item(first, first_destination),
                        make_plan_item(second, second_destination),
                    ],
                    root,
                    recorder=Recorder(),
                )

            self.assertTrue(captured.exception.filesystem_changed)
            self.assertIsNotNone(captured.exception.operation_log)
            self.assertTrue(captured.exception.operation_log.log_path.exists())
            self.assertTrue(first_destination.exists())
            self.assertTrue(second.exists())

    def test_apply_moves_planned_duplicate_into_review_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            destination = root / "AI_Review" / "duplicates" / "b.txt"
            source.write_text("same", encoding="utf-8")

            operation_log = apply_move_plan([make_plan_item(source, destination)], root)

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "same")
            self.assertEqual(len(operation_log.operations), 1)
            self.assertTrue(operation_log.operations[0].success)

    def test_apply_creates_destination_parent_directories_only_when_applying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "nested" / "b.txt"
            destination = root / "AI_Review" / "duplicates" / "nested" / "b.txt"
            source.parent.mkdir()
            source.write_text("same", encoding="utf-8")

            self.assertFalse((root / "AI_Review").exists())
            apply_move_plan([make_plan_item(source, destination)], root)

            self.assertTrue(destination.parent.exists())

    def test_apply_writes_operation_log_with_source_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            destination = root / "AI_Review" / "duplicates" / "b.txt"
            source.write_text("same", encoding="utf-8")

            operation_log = apply_move_plan([make_plan_item(source, destination)], root)

            self.assertTrue(operation_log.log_path.exists())
            log_data = json.loads(operation_log.log_path.read_text(encoding="utf-8"))
            self.assertEqual(log_data["operations"][0]["source"], str(source))
            self.assertEqual(log_data["operations"][0]["destination"], str(destination))
            self.assertTrue(log_data["operations"][0]["success"])
            self.assertEqual(log_data["operations"][0]["message"], "moved")

    def test_apply_refuses_to_overwrite_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            destination = root / "AI_Review" / "duplicates" / "b.txt"
            source.write_text("same", encoding="utf-8")
            destination.parent.mkdir(parents=True)
            destination.write_text("existing", encoding="utf-8")

            with self.assertRaises(ValueError):
                apply_move_plan([make_plan_item(source, destination)], root)

            self.assertTrue(source.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "existing")

    def test_apply_refuses_source_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside.txt"
            destination = root / "AI_Review" / "duplicates" / "outside.txt"
            root.mkdir()
            outside.write_text("outside", encoding="utf-8")

            with self.assertRaises(ValueError):
                apply_move_plan([make_plan_item(outside, destination)], root)

            self.assertTrue(outside.exists())
            self.assertFalse((root / "AI_Review").exists())

    def test_apply_refuses_destination_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            source = root / "b.txt"
            destination = base / "outside" / "b.txt"
            root.mkdir()
            source.write_text("same", encoding="utf-8")

            with self.assertRaises(ValueError):
                apply_move_plan([make_plan_item(source, destination)], root)

            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_apply_refuses_destination_parent_symlink_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            source = root / "b.txt"
            source.write_text("same", encoding="utf-8")
            link = root / "AI_Review"

            try:
                link.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("Symlinks are not supported on this OS or filesystem")

            destination = root / "AI_Review" / "duplicates" / "b.txt"
            with self.assertRaises(ValueError):
                apply_move_plan([make_plan_item(source, destination)], root)

            self.assertTrue(source.exists())
            self.assertFalse((outside / "duplicates" / "b.txt").exists())

    def test_apply_refuses_direct_symlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            link = root / "link.txt"
            destination = root / "AI_Review" / "duplicates" / "link.txt"
            target.write_text("same", encoding="utf-8")

            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError):
                self.skipTest("Symlinks are not supported on this OS or filesystem")

            with self.assertRaises(ValueError):
                apply_move_plan([make_plan_item(link, destination)], root)

            self.assertTrue(link.exists())
            self.assertFalse(destination.exists())

    def test_apply_does_not_move_anything_when_preflight_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            first = root / "first.txt"
            second = root / "second.txt"
            first_destination = root / "AI_Review" / "duplicates" / "first.txt"
            bad_destination = base / "outside" / "second.txt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            plan = [
                make_plan_item(first, first_destination),
                make_plan_item(second, bad_destination),
            ]

            with self.assertRaises(ValueError):
                apply_move_plan(plan, root)

            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertFalse(first_destination.exists())

    def test_apply_preserves_files_not_included_in_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            keep = root / "keep.txt"
            destination = root / "AI_Review" / "duplicates" / "b.txt"
            source.write_text("same", encoding="utf-8")
            keep.write_text("keep", encoding="utf-8")

            apply_move_plan([make_plan_item(source, destination)], root)

            self.assertEqual(keep.read_text(encoding="utf-8"), "keep")

    def test_apply_returns_partial_log_when_move_fails_after_partial_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            second = root / "second.txt"
            first_destination = root / "AI_Review" / "duplicates" / "first.txt"
            second_destination = root / "AI_Review" / "duplicates" / "second.txt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            real_move = shutil.move
            move_calls = 0

            def fake_move(source: str, destination: str) -> str:
                nonlocal move_calls
                move_calls += 1
                if move_calls == 1:
                    return str(real_move(source, destination))
                raise OSError("simulated failure")

            with mock.patch("organizer.executor.shutil.move", side_effect=fake_move):
                operation_log = apply_move_plan(
                    [
                        make_plan_item(first, first_destination),
                        make_plan_item(second, second_destination),
                    ],
                    root,
                )

            self.assertEqual(len(operation_log.operations), 2)
            self.assertTrue(operation_log.operations[0].success)
            self.assertFalse(operation_log.operations[1].success)
            self.assertTrue(first_destination.exists())
            self.assertTrue(second.exists())
            self.assertTrue(operation_log.log_path.exists())


class ExecutorUndoTests(unittest.TestCase):
    def test_optional_undo_recorder_wraps_each_restore_and_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            destination = root / "Organized" / "b.txt"
            source.write_text("same", encoding="utf-8")
            operation_log = apply_move_plan([make_plan_item(source, destination)], root)
            events: list[str] = []

            class Recorder:
                def before_batch(self, items): events.append("batch")
                def before_restore(self, index, item):
                    self_test.assertTrue(destination.exists())
                    events.append("attempting")
                def after_success(self, index, item, result): events.append("restored")
                def after_failure(self, index, item, result): events.append("failed")
                def after_batch(self, log): events.append("complete")

            self_test = self
            undo = undo_operation_log(operation_log.log_path, root, recorder=Recorder())
            self.assertEqual(events, ["batch", "attempting", "restored", "complete"])
            self.assertTrue(undo.operations[0].success)

    def test_recorder_failure_before_restore_prevents_filesystem_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            destination = root / "Organized" / "b.txt"
            source.write_text("same", encoding="utf-8")
            operation_log = apply_move_plan([make_plan_item(source, destination)], root)

            class Recorder:
                def before_batch(self, items): pass
                def before_restore(self, index, item): raise OSError("durability")

            with self.assertRaises(UndoRecordingError):
                undo_operation_log(operation_log.log_path, root, recorder=Recorder())
            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())

    def test_recorder_failure_after_restore_reports_recoverable_filesystem_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            destination = root / "Organized" / "b.txt"
            source.write_text("same", encoding="utf-8")
            operation_log = apply_move_plan([make_plan_item(source, destination)], root)

            class Recorder:
                def before_batch(self, items): pass
                def before_restore(self, index, item): pass
                def after_success(self, index, item, result): raise OSError("durability")

            with self.assertRaises(UndoRecordingError) as raised:
                undo_operation_log(operation_log.log_path, root, recorder=Recorder())
            self.assertTrue(raised.exception.filesystem_changed)
            self.assertIsNotNone(raised.exception.operation_log)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_undo_rejects_direct_log_symlink_and_non_boolean_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "AI_Review" / "operation_logs"
            logs.mkdir(parents=True)
            target = logs / "target.json"
            target.write_text('{"operations": []}', encoding="utf-8")
            if hasattr(os, "symlink"):
                link = logs / "link.json"
                link.symlink_to(target)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    undo_operation_log(link, root)

            malformed = logs / "malformed.json"
            malformed.write_text(json.dumps({"operations": [{
                "source": str(root / "a.txt"),
                "destination": str(root / "b.txt"),
                "success": "true",
                "message": "moved",
            }]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "boolean"):
                undo_operation_log(malformed, root)

    def test_undo_restores_moved_file_to_original_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            destination = root / "AI_Review" / "duplicates" / "b.txt"
            source.write_text("same", encoding="utf-8")
            operation_log = apply_move_plan([make_plan_item(source, destination)], root)

            undo_log = undo_operation_log(operation_log.log_path, root)

            self.assertTrue(source.exists())
            self.assertEqual(source.read_text(encoding="utf-8"), "same")
            self.assertFalse(destination.exists())
            self.assertEqual(len(undo_log.operations), 1)
            self.assertTrue(undo_log.operations[0].success)
            self.assertTrue(undo_log.log_path.exists())

    def test_undo_refuses_to_overwrite_existing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            destination = root / "AI_Review" / "duplicates" / "b.txt"
            source.write_text("same", encoding="utf-8")
            operation_log = apply_move_plan([make_plan_item(source, destination)], root)
            source.write_text("new file", encoding="utf-8")

            undo_log = undo_operation_log(operation_log.log_path, root)

            self.assertFalse(undo_log.operations[0].success)
            self.assertEqual(source.read_text(encoding="utf-8"), "new file")
            self.assertTrue(destination.exists())

    def test_undo_refuses_log_path_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside.json"
            root.mkdir()
            outside.write_text('{"operations": []}', encoding="utf-8")

            with self.assertRaises(ValueError):
                undo_operation_log(outside, root)

    def test_undo_ignores_unsuccessful_log_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "AI_Review" / "duplicates" / "b.txt"
            log_path = root / "AI_Review" / "operation_logs" / "operation_log.json"
            destination.parent.mkdir(parents=True)
            log_path.parent.mkdir(parents=True)
            destination.write_text("same", encoding="utf-8")
            log_path.write_text(
                json.dumps(
                    {
                        "operations": [
                            {
                                "source": str(root / "b.txt"),
                                "destination": str(destination),
                                "success": False,
                                "message": "failed",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            undo_log = undo_operation_log(log_path, root)

            self.assertEqual(undo_log.operations, [])
            self.assertTrue(destination.exists())
            self.assertFalse((root / "b.txt").exists())

    def test_undo_does_not_remove_unrelated_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            keep = root / "keep.txt"
            destination = root / "AI_Review" / "duplicates" / "b.txt"
            source.write_text("same", encoding="utf-8")
            keep.write_text("keep", encoding="utf-8")
            operation_log = apply_move_plan([make_plan_item(source, destination)], root)

            undo_operation_log(operation_log.log_path, root)

            self.assertEqual(keep.read_text(encoding="utf-8"), "keep")


class ExecutorCliTests(unittest.TestCase):
    def test_cli_apply_without_confirmation_does_not_move_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")

            result = run_cli(root, "--apply-duplicate-plan")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Apply refused", result.stdout)
            self.assertTrue((root / "a.txt").exists())
            self.assertTrue((root / "b.txt").exists())
            self.assertFalse((root / "AI_Review").exists())

    def test_cli_apply_with_exact_confirmation_moves_duplicate_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")

            result = run_cli(
                root,
                "--apply-duplicate-plan",
                "--confirm",
                "APPLY_DUPLICATE_PLAN",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Apply completed.", result.stdout)
            self.assertTrue((root / "a.txt").exists())
            self.assertFalse((root / "b.txt").exists())
            self.assertTrue((root / "AI_Review" / "duplicates" / "b.txt").exists())

    def test_cli_reports_apply_completed_with_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.txt"
            second = root / "b.txt"
            third = root / "c.txt"
            second_destination = root / "AI_Review" / "duplicates" / "b.txt"
            third_destination = root / "AI_Review" / "duplicates" / "c.txt"
            first.write_text("same", encoding="utf-8")
            second.write_text("same", encoding="utf-8")
            third.write_text("same", encoding="utf-8")
            real_move = shutil.move
            move_calls = 0

            def fake_move(source: str, destination: str) -> str:
                nonlocal move_calls
                move_calls += 1
                if move_calls == 1:
                    return str(real_move(source, destination))
                raise OSError("simulated failure")

            with mock.patch("organizer.executor.shutil.move", side_effect=fake_move):
                with mock.patch(
                    "sys.argv",
                    [
                        "organizer.cli",
                        str(root),
                        "--apply-duplicate-plan",
                        "--confirm",
                        "APPLY_DUPLICATE_PLAN",
                    ],
                ):
                    from organizer.cli import main

                    with mock.patch("sys.stdout") as fake_stdout:
                        main()

            output = "".join(
                str(call.args[0]) for call in fake_stdout.write.call_args_list
            )
            self.assertIn("Apply completed with failures.", output)
            self.assertTrue(second_destination.exists())
            self.assertTrue(third.exists())
            self.assertFalse(third_destination.exists())

    def test_cli_undo_log_restores_moved_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")

            apply_result = run_cli(
                root,
                "--apply-duplicate-plan",
                "--confirm",
                "APPLY_DUPLICATE_PLAN",
            )
            self.assertEqual(apply_result.returncode, 0, apply_result.stderr)
            log_path = extract_operation_log_path(apply_result.stdout)

            undo_result = run_cli(root, "--undo-log", str(log_path))

            self.assertEqual(undo_result.returncode, 0, undo_result.stderr)
            self.assertIn("Undo operation results", undo_result.stdout)
            self.assertTrue((root / "b.txt").exists())

    def test_cli_plan_duplicates_remains_dry_run_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")

            result = run_cli(root, "--plan-duplicates")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Dry-run only", result.stdout)
            self.assertTrue((root / "a.txt").exists())
            self.assertTrue((root / "b.txt").exists())
            self.assertFalse((root / "AI_Review").exists())


def run_cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    return subprocess.run(
        [sys.executable, "-m", "organizer.cli", str(root), *args],
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )


def extract_operation_log_path(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("Operation log: "):
            return Path(line.removeprefix("Operation log: "))
    raise AssertionError("Operation log path not found in CLI output")


if __name__ == "__main__":
    unittest.main()
