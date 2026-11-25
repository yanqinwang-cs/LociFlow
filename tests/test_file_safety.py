"""File-safety regressions found by the LOCI-3 code review.

Each class owns one fact: moves never replace a file, a started batch always
leaves a log, undo works however the folder was named, an edited review file
cannot move protected paths, and untouched review rows are never approved.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organizer.executor import apply_move_plan, undo_operation_log
from organizer.models import MovePlanItem
from organizer.organization_apply_review import (
    CONFIRM_APPLY_ORGANIZATION_REVIEW,
    apply_approved_organization_review,
)
from organizer.organization_rules import load_organization_rules
from organizer.review_session import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    DECISION_UNDECIDED,
    build_review_session_items,
    reject_items,
)
from organizer.review_state import (
    apply_review_state_to_items,
    load_review_state,
    save_review_state,
    update_review_state_from_items,
)
from organizer.scanner import scan_directory

SOURCE_TREE = Path(__file__).resolve().parents[1] / "src"


def plan_item(source: Path, destination: Path) -> MovePlanItem:
    return MovePlanItem(
        source=source,
        destination=destination,
        reason="test move",
        confidence=100,
        operation="dry-run move",
        overwrite_risk=False,
    )


def run_cli(
    cwd: Path, folder: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SOURCE_TREE)
    return subprocess.run(
        [sys.executable, "-m", "organizer.cli", folder, *arguments],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=environment,
        check=False,
    )


class MoveNeverReplacesFilesTests(unittest.TestCase):
    def test_destinations_differing_only_by_case_are_refused_before_any_move(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "one.txt").write_text("CONTENT ONE", encoding="utf-8")
            (root / "two.txt").write_text("CONTENT TWO", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "collides"):
                apply_move_plan(
                    [
                        plan_item(root / "one.txt", root / "Organized/Set/a.txt"),
                        plan_item(root / "two.txt", root / "Organized/set/A.txt"),
                    ],
                    root,
                )

            self.assertEqual(
                (root / "one.txt").read_text(encoding="utf-8"), "CONTENT ONE"
            )
            self.assertEqual(
                (root / "two.txt").read_text(encoding="utf-8"), "CONTENT TWO"
            )
            self.assertFalse((root / "Organized").exists())

    def test_composed_and_decomposed_names_collide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "one.txt").write_text("1", encoding="utf-8")
            (root / "two.txt").write_text("2", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "collides"):
                apply_move_plan(
                    [
                        plan_item(root / "one.txt", root / "café.txt"),
                        plan_item(root / "two.txt", root / "café.txt"),
                    ],
                    root,
                )

            self.assertTrue((root / "one.txt").exists())
            self.assertTrue((root / "two.txt").exists())

    def test_destination_created_after_preflight_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "one.txt").write_text("MOVING", encoding="utf-8")
            destination = root / "Organized" / "one.txt"

            class LateWriter:
                def before_batch(self, plan_items) -> None: ...

                def before_move(self, index, item) -> None:
                    item.destination.parent.mkdir(parents=True)
                    item.destination.write_text("ALREADY HERE", encoding="utf-8")

                def after_success(self, index, item, result) -> None: ...

                def after_failure(self, index, item, result) -> None: ...

                def after_batch(self, operation_log) -> None: ...

            log = apply_move_plan(
                [plan_item(root / "one.txt", destination)],
                root,
                recorder=LateWriter(),
            )

            self.assertEqual([result.success for result in log.operations], [False])
            self.assertEqual(destination.read_text(encoding="utf-8"), "ALREADY HERE")
            self.assertEqual((root / "one.txt").read_text(encoding="utf-8"), "MOVING")


class StartedBatchAlwaysLeavesLogTests(unittest.TestCase):
    def test_folder_creation_failure_after_a_move_still_writes_a_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "one.txt").write_text("ONE", encoding="utf-8")
            (root / "two.txt").write_text("TWO", encoding="utf-8")

            class BlockSecondFolder:
                def before_batch(self, plan_items) -> None: ...

                def before_move(self, index, item) -> None:
                    if index == 1:
                        (root / "blocked").write_text("a file", encoding="utf-8")

                def after_success(self, index, item, result) -> None: ...

                def after_failure(self, index, item, result) -> None: ...

                def after_batch(self, operation_log) -> None: ...

            log = apply_move_plan(
                [
                    plan_item(root / "one.txt", root / "a" / "one.txt"),
                    plan_item(root / "two.txt", root / "blocked" / "two.txt"),
                ],
                root,
                recorder=BlockSecondFolder(),
            )

            self.assertTrue(log.log_path.is_file())
            logged = json.loads(log.log_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [entry["success"] for entry in logged["operations"]],
                [True, False],
            )
            undo_operation_log(log.log_path, root)
            self.assertEqual((root / "one.txt").read_text(encoding="utf-8"), "ONE")

    def test_log_exists_after_each_move_even_if_the_batch_is_interrupted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "one.txt").write_text("ONE", encoding="utf-8")
            (root / "two.txt").write_text("TWO", encoding="utf-8")

            class Interrupt:
                def before_batch(self, plan_items) -> None: ...

                def before_move(self, index, item) -> None:
                    if index == 1:
                        raise KeyboardInterrupt

                def after_success(self, index, item, result) -> None: ...

                def after_failure(self, index, item, result) -> None: ...

                def after_batch(self, operation_log) -> None: ...

            with self.assertRaises(KeyboardInterrupt):
                apply_move_plan(
                    [
                        plan_item(root / "one.txt", root / "a" / "one.txt"),
                        plan_item(root / "two.txt", root / "a" / "two.txt"),
                    ],
                    root,
                    recorder=Interrupt(),
                )

            logs = list((root / "AI_Review" / "operation_logs").glob("*.json"))
            self.assertEqual(len(logs), 1)
            undo_operation_log(logs[0], root)
            self.assertTrue((root / "one.txt").exists())


class OperationLogIsNeverPartialTests(unittest.TestCase):
    def test_interrupt_inside_a_log_rewrite_keeps_the_previous_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("one", "two", "three"):
                (root / f"{name}.txt").write_text(name, encoding="utf-8")
            real_dump = json.dump
            writes = 0

            def interrupted_dump(data, file, **options):
                nonlocal writes
                writes += 1
                if writes == 2:
                    file.write('{"operations": [')
                    raise KeyboardInterrupt
                return real_dump(data, file, **options)

            with mock.patch("organizer.executor.json.dump", interrupted_dump):
                with self.assertRaises(KeyboardInterrupt):
                    apply_move_plan(
                        [
                            plan_item(root / f"{name}.txt", root / "a" / f"{name}.txt")
                            for name in ("one", "two", "three")
                        ],
                        root,
                    )

            log_dir = root / "AI_Review" / "operation_logs"
            self.assertEqual(
                [p.name for p in log_dir.iterdir() if p.suffix == ".tmp"], []
            )
            (log,) = log_dir.glob("operation_log_*.json")
            logged = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(len(logged["operations"]), 1)
            undo_operation_log(log, root)
            self.assertTrue((root / "one.txt").exists())


class UndoWorksForDotFolderTests(unittest.TestCase):
    def test_apply_with_dot_folder_can_be_undone_from_any_directory(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as elsewhere,
        ):
            root = Path(directory).resolve()
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")

            applied = run_cli(
                root, ".", "--apply-duplicate-plan", "--confirm", "APPLY_DUPLICATE_PLAN"
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            log = next(
                (root / "AI_Review" / "operation_logs").glob("operation_log_*.json")
            )
            self.assertEqual(sorted(p.name for p in root.glob("*.txt")), ["a.txt"])

            undone = run_cli(Path(elsewhere), str(root), "--undo-log", str(log))

            self.assertEqual(undone.returncode, 0, undone.stdout + undone.stderr)
            self.assertEqual(
                sorted(p.name for p in root.glob("*.txt")), ["a.txt", "b.txt"]
            )

    def test_undo_with_dot_folder_and_relative_log_restores_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")
            applied = run_cli(
                root, ".", "--apply-duplicate-plan", "--confirm", "APPLY_DUPLICATE_PLAN"
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            log = next(
                (root / "AI_Review" / "operation_logs").glob("operation_log_*.json")
            )

            undone = run_cli(root, ".", "--undo-log", str(log.relative_to(root)))

            self.assertEqual(undone.returncode, 0, undone.stdout + undone.stderr)
            self.assertTrue((root / "b.txt").exists())

    def test_undo_exits_nonzero_when_a_restore_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")
            applied = run_cli(
                root, ".", "--apply-duplicate-plan", "--confirm", "APPLY_DUPLICATE_PLAN"
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            log = next(
                (root / "AI_Review" / "operation_logs").glob("operation_log_*.json")
            )
            logged = json.loads(log.read_text(encoding="utf-8"))
            original = Path(logged["operations"][0]["source"])
            if not original.is_absolute():
                original = root / original
            original.write_text("recreated", encoding="utf-8")

            undone = run_cli(root, str(root), "--undo-log", str(log))

            self.assertEqual(undone.returncode, 1, undone.stdout)


class ReviewApplyRefusesProtectedPathsTests(unittest.TestCase):
    def _review(self, source: str, destination: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "bootAI Stage 10.8 rule-aware organization review",
            "generated_at": "2026-07-10T12:00:00+00:00",
            "scan_root": ".",
            "instructions": "Review each organization suggestion.",
            "rules_loaded": False,
            "rules_path": None,
            "rule_audit_summary": {
                "locked_anchors": [],
                "preferred_granularities": [],
                "warnings": [],
            },
            "items": [
                {
                    "review_id": "org-000001",
                    "source": source,
                    "destination": destination,
                    "anchor": "Course",
                    "evidence": "locked_anchor",
                    "reason": "edited by hand",
                    "confidence": 80,
                    "risk_level": "low",
                    "overwrite_risk": False,
                    "decision": "approve",
                    "note": "",
                }
            ],
        }

    def _apply(self, root: Path, source: str, destination: str) -> None:
        review = root / "AI_Review" / "reviews" / "organization_review.approved.json"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text(
            json.dumps(self._review(source, destination)), encoding="utf-8"
        )
        apply_approved_organization_review(
            review, root, CONFIRM_APPLY_ORGANIZATION_REVIEW
        )

    def test_git_internals_cannot_be_moved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: main", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "protected"):
                self._apply(root, ".git/HEAD", "Organized/HEAD")

            self.assertEqual(
                (root / ".git" / "HEAD").read_text(encoding="utf-8"), "ref: main"
            )
            self.assertFalse((root / "Organized").exists())

    def test_operation_logs_cannot_be_moved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            logs = root / "AI_Review" / "operation_logs"
            logs.mkdir(parents=True)
            (logs / "operation_log_1.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "protected"):
                self._apply(
                    root,
                    "AI_Review/operation_logs/operation_log_1.json",
                    "Organized/log.json",
                )

            self.assertTrue((logs / "operation_log_1.json").exists())

    def test_file_inside_a_code_project_cannot_be_moved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "MyRepo"
            (project / "src").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
            (project / "src" / "main.py").write_text("print()", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "protected"):
                self._apply(root, "MyRepo/src/main.py", "Organized/main.py")

            self.assertTrue((project / "src" / "main.py").exists())

    def test_ordinary_row_still_applies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "a.txt").write_text("a", encoding="utf-8")

            self._apply(root, "a.txt", "Organized/Course/a.txt")

            self.assertTrue((root / "Organized" / "Course" / "a.txt").exists())


class BatchReviewStartsUndecidedTests(unittest.TestCase):
    def _items(self, root: Path):
        (root / "a.txt").write_text("same", encoding="utf-8")
        (root / "subdir").mkdir()
        (root / "subdir" / "b.txt").write_text("same", encoding="utf-8")
        (root / "evosim_notes.txt").write_text("notes", encoding="utf-8")
        (root / "evosim_report.pdf").write_text("report", encoding="utf-8")
        (root / "file.tmp").write_text("temporary", encoding="utf-8")
        config = root / "AI_Review" / "config" / "organization_rules.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            '{"version": 1, "locked_anchors": ["Evosim"], "ignored_terms": [],'
            ' "anchor_aliases": {}}\n',
            encoding="utf-8",
        )
        snapshot = load_organization_rules(root).require_rules()
        return build_review_session_items(
            scan_directory(root), root, organization_rules=snapshot
        )

    def test_every_fresh_row_is_undecided(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            items = self._items(Path(directory).resolve())

            self.assertGreaterEqual(len(items), 3)
            self.assertEqual({item.decision for item in items}, {DECISION_UNDECIDED})

    def test_saving_untouched_rows_stores_no_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            items = self._items(root)
            state = update_review_state_from_items(load_review_state(root), items, root)

            self.assertEqual(state.decisions, [])

    def test_remembered_rejection_stays_undecided_not_approved_when_file_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            items = self._items(root)
            target = next(item for item in items if item.category == "organization")
            rejected = reject_items(items, [target.id])
            state = update_review_state_from_items(
                load_review_state(root), rejected, root
            )
            save_review_state(state, root)
            (root / target.plan_item.source.name).write_text(
                "changed contents, longer", encoding="utf-8"
            )

            reloaded = apply_review_state_to_items(
                self._items_again(root), load_review_state(root), root
            )

            changed = next(item for item in reloaded if item.id == target.id)
            self.assertNotEqual(changed.decision, DECISION_APPROVED)
            self.assertEqual(changed.decision, DECISION_UNDECIDED)
            self.assertNotEqual(changed.decision, DECISION_REJECTED)

    def _items_again(self, root: Path):
        snapshot = load_organization_rules(root).require_rules()
        return build_review_session_items(
            scan_directory(root), root, organization_rules=snapshot
        )


if __name__ == "__main__":
    unittest.main()
