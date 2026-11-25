import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organizer.cli import main
from organizer.executor import apply_move_plan
from organizer.models import MovePlanItem


def run_main(*args: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        mock.patch("sys.argv", ["organizer", *args]),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        try:
            code = main()
        except SystemExit as exit_request:
            code = exit_request.code
    return code, stdout.getvalue(), stderr.getvalue()


class FolderArgumentTests(unittest.TestCase):
    def test_missing_folder_is_a_usage_error_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = run_main(str(Path(directory) / "absent"))

        self.assertEqual(code, 2)
        self.assertIn("folder does not exist", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_file_given_as_folder_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "note.txt"
            file_path.write_text("x", encoding="utf-8")

            code, _, stderr = run_main(str(file_path))

        self.assertEqual(code, 2)
        self.assertIn("not a folder", stderr)

    @unittest.skipIf(os.geteuid() == 0, "root can read any folder")
    def test_unreadable_folder_is_a_usage_error_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            locked = Path(directory) / "locked"
            locked.mkdir()
            locked.chmod(0)
            try:
                code, _, stderr = run_main(str(locked))
            finally:
                locked.chmod(0o755)

        self.assertEqual(code, 2)
        self.assertIn("folder is not readable", stderr)
        self.assertNotIn("Traceback", stderr)


class OptionParsingTests(unittest.TestCase):
    def test_option_abbreviations_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = run_main(directory, "--dup")

        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --dup", stderr)

    def test_two_apply_actions_are_refused_instead_of_dropping_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = run_main(
                directory,
                "--apply-duplicate-plan",
                "--apply-organization-plan",
            )

        self.assertEqual(code, 2)
        self.assertIn("choose one action", stderr)
        self.assertIn("--apply-duplicate-plan", stderr)
        self.assertIn("--apply-organization-plan", stderr)

    def test_apply_that_would_skip_a_later_step_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = run_main(
                directory,
                "--apply-duplicate-plan",
                "--plan-organization",
            )

        self.assertEqual(code, 2)
        self.assertIn("cannot be combined", stderr)

    def test_empty_duplicate_plan_does_not_skip_the_organization_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CS1010-lecture1.pdf").write_text("one", encoding="utf-8")
            (root / "CS1010-lecture2.pdf").write_text("two", encoding="utf-8")

            code, stdout, _ = run_main(
                str(root), "--plan-duplicates", "--plan-organization"
            )

        self.assertEqual(code, 0)
        self.assertIn("Dry-run organization plan", stdout)


class UndoCommandTests(unittest.TestCase):
    def test_undo_with_missing_log_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = run_main(
                directory, "--undo-log", str(Path(directory) / "gone.json")
            )

        self.assertEqual(code, 1)
        self.assertIn("Undo failed", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_undo_with_malformed_log_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "operation_logs" / "bad.json"
            log_path.parent.mkdir()
            log_path.write_text("{not json", encoding="utf-8")

            code, _, stderr = run_main(str(root), "--undo-log", str(log_path))

        self.assertEqual(code, 1)
        self.assertIn("Undo failed", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_undo_exits_nonzero_when_a_restore_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.txt"
            destination = root / "Organized" / "a.txt"
            source.write_text("kept", encoding="utf-8")
            log = apply_move_plan(
                [
                    MovePlanItem(
                        source=source,
                        destination=destination,
                        reason="test",
                        confidence=100,
                        operation="move",
                        overwrite_risk=False,
                    )
                ],
                root,
            )
            # The original spot is taken, so undo cannot put the file back.
            source.write_text("someone else's file", encoding="utf-8")

            code, stdout, stderr = run_main(str(root), "--undo-log", str(log.log_path))

            self.assertEqual(code, 1)
            self.assertIn("failure", stdout)
            self.assertIn("Undo completed with failures", stderr)
            self.assertEqual(source.read_text(encoding="utf-8"), "someone else's file")

    def test_undo_exits_zero_when_every_restore_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.txt"
            source.write_text("kept", encoding="utf-8")
            log = apply_move_plan(
                [
                    MovePlanItem(
                        source=source,
                        destination=root / "Organized" / "a.txt",
                        reason="test",
                        confidence=100,
                        operation="move",
                        overwrite_risk=False,
                    )
                ],
                root,
            )

            code, _, _ = run_main(str(root), "--undo-log", str(log.log_path))

            self.assertEqual(code, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "kept")


if __name__ == "__main__":
    unittest.main()
