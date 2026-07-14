from pathlib import Path
import tempfile
import unittest

from organizer.scanner import scan_directory
from organizer.scope import (
    PROJECT_PROTECTION_REASON,
    PROJECT_PROTECTION_STANDARD_ID,
    SHARED_PROTECTION_REASON,
    SHARED_PROTECTION_STANDARD_ID,
    SUBMISSION_PROTECTION_REASON,
    SUBMISSION_PROTECTION_STANDARD_ID,
    evaluate_actionable_plan_eligibility,
    is_actionable_plan_eligible,
    is_generated_asset_context_path,
    is_project_context_path,
    is_protected_context_path,
)


class ProtectedContextPathTests(unittest.TestCase):
    def test_protected_directory_paths_are_detected(self) -> None:
        protected_paths = [
            "node_modules/pkg/index.js",
            "repo/.git/config",
            ".venv/lib/site.py",
            "venv/lib/site.py",
            "pkg/__pycache__/module.pyc",
            "App.app/Contents/file.txt",
            "Lib.framework/Resources/file.txt",
            "Plugin.bundle/Contents/file.txt",
            "Project.xcodeproj/project.pbxproj",
            "Protected_Workspaces/project/file.txt",
        ]

        for relative_path in protected_paths:
            with self.subTest(relative_path=relative_path):
                self.assertTrue(is_protected_context_path(Path(relative_path)))

    def test_ordinary_document_path_is_not_protected(self) -> None:
        self.assertFalse(is_protected_context_path(Path("documents/report.pdf")))


class ProjectContextPathTests(unittest.TestCase):
    def test_project_marker_ancestor_is_project_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
            (project / "notes.txt").write_text("notes", encoding="utf-8")
            metadata = scan_directory(root)

            self.assertTrue(is_project_context_path(Path("project/notes.txt"), metadata))

    def test_actionable_plan_eligible_excludes_project_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text("{}", encoding="utf-8")
            (project / "notes.txt").write_text("notes", encoding="utf-8")
            metadata = scan_directory(root)
            notes = [
                item for item in metadata if item.relative_path == Path("project/notes.txt")
            ][0]

            self.assertFalse(is_actionable_plan_eligible(notes, metadata))

    def test_actionable_plan_eligible_allows_ordinary_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("notes", encoding="utf-8")
            metadata = scan_directory(root)
            notes = [item for item in metadata if item.relative_path == Path("notes.txt")][0]

            self.assertTrue(is_actionable_plan_eligible(notes, metadata))

    def test_structured_project_protection_preserves_boolean_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text("{}", encoding="utf-8")
            (project / "notes.txt").write_text("notes", encoding="utf-8")
            metadata = scan_directory(root)
            notes = next(
                item for item in metadata if item.relative_path == Path("project/notes.txt")
            )

            evaluation = evaluate_actionable_plan_eligibility(notes, metadata)

            self.assertFalse(evaluation.allowed)
            self.assertEqual(evaluation.reason_code, PROJECT_PROTECTION_REASON)
            self.assertEqual(evaluation.standard_id, PROJECT_PROTECTION_STANDARD_ID)
            self.assertEqual(is_actionable_plan_eligible(notes, metadata), evaluation.allowed)

    def test_submission_and_shared_protections_emit_stable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative_path in ("Submissions/work.pdf", "Shared/notes.txt"):
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            metadata = scan_directory(root)
            by_path = {item.relative_path.as_posix(): item for item in metadata}

            submission = evaluate_actionable_plan_eligibility(
                by_path["Submissions/work.pdf"], metadata
            )
            shared = evaluate_actionable_plan_eligibility(
                by_path["Shared/notes.txt"], metadata
            )

            self.assertEqual(
                (submission.allowed, submission.reason_code, submission.standard_id),
                (False, SUBMISSION_PROTECTION_REASON, SUBMISSION_PROTECTION_STANDARD_ID),
            )
            self.assertEqual(
                (shared.allowed, shared.reason_code, shared.standard_id),
                (False, SHARED_PROTECTION_REASON, SHARED_PROTECTION_STANDARD_ID),
            )

    def test_allowed_file_has_no_protection_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("notes", encoding="utf-8")
            metadata = scan_directory(root)
            notes = next(item for item in metadata if item.name == "notes.txt")

            evaluation = evaluate_actionable_plan_eligibility(notes, metadata)

            self.assertTrue(evaluation.allowed)
            self.assertIsNone(evaluation.reason_code)
            self.assertIsNone(evaluation.standard_id)


class GeneratedAndProjectOutputContextTests(unittest.TestCase):
    def test_generated_asset_folder_paths_are_detected(self) -> None:
        generated_paths = [
            "Instagram_files/example.js",
            "YouTube_files/base.js",
            "SomePage_files/style.css",
            "Any_files/image.webp",
            "Any_files/font.woff",
            "Any_files/icon.svg",
        ]

        for relative_path in generated_paths:
            with self.subTest(relative_path=relative_path):
                self.assertTrue(is_generated_asset_context_path(Path(relative_path)))

    def test_generated_and_project_output_files_are_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [
                "Instagram_files/example.js",
                "YouTube_files/base.js",
                "SomePage_files/style.css",
                "project/src/main.py",
                "project/resources/icon.png",
                "archive_experiment_files/output.txt",
                "experiment_outputs/result.txt",
            ]
            for relative_path in paths:
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            metadata = scan_directory(root)

            actionable_paths = {
                item.relative_path.as_posix()
                for item in metadata
                if is_actionable_plan_eligible(item, metadata)
            }

            for relative_path in paths:
                self.assertNotIn(relative_path, actionable_paths)

    def test_ordinary_resources_document_folder_is_still_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "course_resources" / "CS1010X notes.pdf"
            path.parent.mkdir()
            path.write_text("notes", encoding="utf-8")
            metadata = scan_directory(root)
            file = [
                item for item in metadata if item.relative_path == Path("course_resources/CS1010X notes.pdf")
            ][0]

            self.assertTrue(is_actionable_plan_eligible(file, metadata))

    def test_loose_python_file_is_not_generated_asset_context(self) -> None:
        self.assertFalse(is_generated_asset_context_path(Path("practice.py")))


if __name__ == "__main__":
    unittest.main()
