from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from organizer.application.file_kinds import consumer_file_kind
from organizer.application.organization_slice_service import (
    build_read_only_organization_slice,
)
from organizer.application.scan_service import scan_root
from organizer.application.standards_read_models import OrganizationUnavailableError
from organizer.application.standards_repository import StandardsRepository
from organizer.application.standards_service import StandardsService
from organizer.organization_rules import (
    ORGANIZATION_RULES_RELATIVE_PATH,
    STANDARD_KEEP_FOLDER_UNCHANGED,
    STANDARD_KEEP_MATCHING_FILES_TOGETHER,
)

COMPLETED_AT = datetime(2026, 7, 14, 10, 20, 30, tzinfo=UTC)


class FileKindTests(unittest.TestCase):
    def test_consumer_file_kind_taxonomy_is_case_insensitive(self) -> None:
        cases = {
            ".PDF": "pdf",
            ".txt": "text",
            ".markdown": "markdown",
            ".docx": "word",
            ".CSV": "spreadsheet",
            ".heic": "image",
            ".pptx": "presentation",
            ".html": "source_code",
            ".yaml": "source_code",
            ".tgz": "archive",
            ".unknown": "document",
        }
        for extension, expected in cases.items():
            with self.subTest(extension=extension):
                self.assertEqual(consumer_file_kind(extension), expected)
        self.assertEqual(consumer_file_kind("", is_dir=True), "folder")


class OrganizationSliceServiceTests(unittest.TestCase):
    def _write_rules(self, root: Path, **rules: object) -> None:
        config = root / ORGANIZATION_RULES_RELATIVE_PATH
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locked_anchors": [],
                    "ignored_terms": [],
                    "anchor_aliases": {},
                    **rules,
                }
            ),
            encoding="utf-8",
        )

    def test_projection_uses_scan_time_metadata_without_restating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "Submissions" / "record.PDF"
            protected.parent.mkdir()
            protected.write_bytes(b"scanned")
            scanned_mtime = 1_700_000_000
            os.utime(protected, (scanned_mtime, scanned_mtime))
            result = scan_root(root)
            protected.write_bytes(b"changed after the scan")
            os.utime(protected, (scanned_mtime + 1000, scanned_mtime + 1000))

            with mock.patch.object(
                Path,
                "stat",
                side_effect=AssertionError("projection must not stat files"),
            ):
                projection = build_read_only_organization_slice(
                    scan_result=result,
                    scan_generation="scan-123",
                    completed_at=COMPLETED_AT,
                )

            item = projection.kept_by_standards[0].item
            self.assertEqual(item.size_bytes, len(b"scanned"))
            self.assertEqual(
                item.modified_at,
                datetime.fromtimestamp(scanned_mtime, UTC).isoformat(),
            )
            self.assertEqual(item.extension, ".pdf")
            self.assertEqual(item.relative_path, "Submissions/record.PDF")
            serialized = json.dumps(projection.to_dict())
            self.assertNotIn(str(root), serialized)

    def test_protections_are_kept_only_and_never_enter_visible_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative_path in (
                "Submissions/submission.pdf",
                "Shared/handout.txt",
                "project/package.json",
                "project/notes.txt",
            ):
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative_path, encoding="utf-8")
            result = scan_root(root)

            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-protected",
                completed_at=COMPLETED_AT,
            )

            kept_paths = {
                kept.item.relative_path for kept in projection.kept_by_standards
            }
            self.assertIn("Submissions/submission.pdf", kept_paths)
            self.assertIn("Shared/handout.txt", kept_paths)
            self.assertIn("project/notes.txt", kept_paths)
            visible_paths = {
                change.item.relative_path for change in projection.recommended_changes
            }
            visible_paths.update(
                choice.item.relative_path
                for choice in projection.choices_needed
                if choice.item is not None
            )
            visible_paths.update(
                copy.relative_path
                for choice in projection.choices_needed
                for copy in choice.copies
            )
            tree_paths = {
                path
                for entry in projection.summary.movement_tree
                for path in entry.files
            }
            self.assertTrue(kept_paths.isdisjoint(visible_paths))
            self.assertTrue(kept_paths.isdisjoint(tree_paths))
            self.assertEqual(
                projection.summary.kept_by_standards,
                len(projection.kept_by_standards),
            )

    def test_saved_ignored_term_suppresses_plan_before_public_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "Organized" / "EvoSim_project_slides" / "slides"
            destination.mkdir(parents=True)
            for name in (
                "EvoSim_project_slides.pptx",
                "EvoSim_project_slides_final.pptx",
            ):
                (root / name).write_text(name, encoding="utf-8")
            self._write_rules(root, ignored_terms=["EvoSim project slides"])

            result = scan_root(root)
            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-ignored",
                completed_at=COMPLETED_AT,
            )

            self.assertEqual(result.report["organization_suggestions"], [])
            self.assertFalse(projection.recommended_changes)
            self.assertFalse(projection.choices_needed)
            self.assertFalse(projection.summary.movement_tree)
            kept = [
                item for item in projection.kept_by_standards
                if item.reason_code == "saved_standard_kept"
            ]
            self.assertEqual(len(kept), 2)
            self.assertTrue(
                all(item.standard.name == "Do not organize by EvoSim project slides" for item in kept)
            )

    def test_saved_locked_course_anchor_shapes_grouping_before_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("lectures", "recitations", "exams"):
                (root / "Organized" / "CS1010X" / folder).mkdir(parents=True)
            for name in (
                "CS1010X lecture 1.pdf",
                "CS1010X recitation 1.pdf",
                "CS1010X finals 2026.pdf",
            ):
                (root / name).write_text(name, encoding="utf-8")
            self._write_rules(root, locked_anchors=["CS1010X"])

            result = scan_root(root)
            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-locked",
                completed_at=COMPLETED_AT,
            )

            self.assertEqual(result.report["project_groups"][0]["group_name"], "CS1010X")
            self.assertEqual(len(projection.recommended_changes), 3)
            self.assertTrue(
                all(
                    change.destination_folder.startswith("Organized/CS1010X/")
                    for change in projection.recommended_changes
                )
            )

    def test_v2_keep_folder_filters_before_projection_and_is_kept_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = root / "Documents"
            documents.mkdir()
            for folder in ("lectures", "recitations", "exams"):
                (root / "Organized" / "CS1010X" / folder).mkdir(parents=True)
            for name in (
                "CS1010X lecture 1.pdf",
                "CS1010X recitation 1.pdf",
                "CS1010X finals 2026.pdf",
            ):
                (documents / name).write_text(name, encoding="utf-8")
            initial_scan = scan_root(root)
            folder = next(
                item
                for item in initial_scan.metadata_items
                if item.relative_path == Path("Documents")
            )
            service = StandardsService(StandardsRepository(root))
            preview = service.preview(
                preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
                scope=service.resolve_folder_scope(folder, scan_generation="scan-before"),
                expected_revision=0,
            )
            service.save_preview(preview.preview_id, expected_revision=0)

            fresh_scan = scan_root(root)
            projection = build_read_only_organization_slice(
                scan_result=fresh_scan,
                scan_generation="scan-after",
                completed_at=COMPLETED_AT,
            )

            self.assertFalse(projection.recommended_changes)
            self.assertFalse(projection.choices_needed)
            kept = [
                item
                for item in projection.kept_by_standards
                if item.reason_code == "saved_standard_kept"
            ]
            self.assertEqual(len(kept), 3)
            self.assertTrue(
                all(item.standard.name == "Keep Documents unchanged" for item in kept)
            )

    def test_v2_keep_together_respects_its_scan_bound_folder_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = root / "Documents"
            documents.mkdir()
            for parent in (documents, root):
                (parent / "CS1010X notes.txt").write_text("notes", encoding="utf-8")
                (parent / "CS1010X slides.pdf").write_text("slides", encoding="utf-8")
            initial_scan = scan_root(root)
            folder = next(
                item
                for item in initial_scan.metadata_items
                if item.relative_path == Path("Documents")
            )
            service = StandardsService(StandardsRepository(root))
            preview = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_folder_scope(folder, scan_generation="scan-before"),
                matcher_kind="course_code",
                matcher_value="CS1010X",
                expected_revision=0,
            )
            service.save_preview(preview.preview_id, expected_revision=0)

            fresh_scan = scan_root(root)

            group = next(
                item
                for item in fresh_scan.report["project_groups"]
                if item["group_name"] == "CS1010X"
            )
            self.assertEqual(
                set(group["files"]),
                {"Documents/CS1010X notes.txt", "Documents/CS1010X slides.pdf"},
            )

    def test_saved_alias_resolves_in_same_grouping_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("notes", "other"):
                (root / "Organized" / "EvoSim" / folder).mkdir(parents=True)
            (root / "Evo_notes.txt").write_text("notes", encoding="utf-8")
            (root / "Evo_report.pdf").write_text("report", encoding="utf-8")
            self._write_rules(
                root,
                locked_anchors=["EvoSim"],
                anchor_aliases={"Evo": "EvoSim"},
            )

            result = scan_root(root)
            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-alias",
                completed_at=COMPLETED_AT,
            )

            self.assertEqual(result.report["project_groups"][0]["group_name"], "EvoSim")
            self.assertTrue(projection.recommended_changes)
            self.assertEqual(
                len(
                    [
                        item for item in result.standards.items
                        if item.preset_type == "legacy_alias"
                    ]
                ),
                1,
            )

    def test_built_in_protection_overrides_saved_locked_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            submissions = root / "Submissions"
            submissions.mkdir()
            (submissions / "CS1010X lecture 1.pdf").write_text("one", encoding="utf-8")
            (submissions / "CS1010X lecture 2.pdf").write_text("two", encoding="utf-8")
            self._write_rules(root, locked_anchors=["CS1010X"])

            result = scan_root(root)
            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-protected-rule",
                completed_at=COMPLETED_AT,
            )

            self.assertFalse(result.report["organization_suggestions"])
            self.assertFalse(projection.recommended_changes)
            self.assertEqual(len(projection.kept_by_standards), 2)
            self.assertTrue(
                all(
                    item.standard.id == "builtin-protect-submission-folders"
                    for item in projection.kept_by_standards
                )
            )

    def test_unavailable_standards_cannot_create_public_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("notes", encoding="utf-8")
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            config.write_text("{invalid", encoding="utf-8")
            result = scan_root(root)

            with self.assertRaises(OrganizationUnavailableError) as raised:
                build_read_only_organization_slice(
                    scan_result=result,
                    scan_generation="scan-unavailable",
                    completed_at=COMPLETED_AT,
                )

            self.assertEqual(raised.exception.code, "standards_unavailable")
            self.assertGreaterEqual(result.summary.file_count, 1)
            self.assertIn(
                "notes.txt",
                {item.relative_path.as_posix() for item in result.metadata_items},
            )
            self.assertEqual(result.summary.organization_suggestion_count, 0)

    def test_low_risk_existing_destination_is_recommended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "Organized" / "EvoSim_project_slides" / "slides"
            destination.mkdir(parents=True)
            (root / "EvoSim_project_slides.pptx").write_text("a", encoding="utf-8")
            (root / "EvoSim_project_slides_final.pptx").write_text("b", encoding="utf-8")
            result = scan_root(root)

            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-recommended",
                completed_at=COMPLETED_AT,
            )

            self.assertEqual(len(projection.recommended_changes), 2)
            self.assertTrue(
                all(change.action == "move" for change in projection.recommended_changes)
            )
            self.assertTrue(
                all(
                    change.destination_folder
                    == "Organized/EvoSim_project_slides/slides"
                    for change in projection.recommended_changes
                )
            )
            self.assertEqual(projection.summary.files_to_move, 2)
            self.assertEqual(projection.summary.folders_to_create, 0)
            self.assertEqual(projection.summary.names_to_change, 0)
            self.assertEqual(len(projection.summary.movement_tree), 1)

    def test_risky_concrete_move_becomes_choice_with_keep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "Organized" / "EvoSim_project_slides" / "slides"
            destination.mkdir(parents=True)
            (root / "EvoSim_project_slides.pptx").write_text("a", encoding="utf-8")
            (root / "EvoSim_project_slides_final.pptx").write_text("b", encoding="utf-8")
            result = scan_root(root)
            report = deepcopy(result.report)
            report["anchor_decisions"]["suggested_groups"][0]["evidence"] = "deterministic_group"
            result = replace(result, report=report)

            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-choice",
                completed_at=COMPLETED_AT,
            )

            self.assertFalse(projection.recommended_changes)
            destination_choices = [
                choice
                for choice in projection.choices_needed
                if choice.choice_type == "choose_destination"
            ]
            self.assertEqual(len(destination_choices), 2)
            for choice in destination_choices:
                self.assertEqual(
                    [option.action for option in choice.options],
                    ["move", "keep"],
                )

    def test_missing_or_overwrite_destination_is_not_manufactured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "EvoSim_project_slides.pptx").write_text("a", encoding="utf-8")
            (root / "EvoSim_project_slides_final.pptx").write_text("b", encoding="utf-8")
            result = scan_root(root)
            report = deepcopy(result.report)
            report["organization_suggestions"][0]["plan_items"][0]["overwrite_risk"] = True
            result = replace(result, report=report)

            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-no-action",
                completed_at=COMPLETED_AT,
            )

            self.assertFalse(projection.recommended_changes)
            self.assertFalse(
                [
                    choice
                    for choice in projection.choices_needed
                    if choice.choice_type == "choose_destination"
                ]
            )
            self.assertFalse(projection.summary.movement_tree)

    def test_multiple_safe_destinations_create_one_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "Organized" / "EvoSim_project_slides" / "slides"
            second = root / "Organized" / "Alternative" / "slides"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (root / "EvoSim_project_slides.pptx").write_text("a", encoding="utf-8")
            (root / "EvoSim_project_slides_final.pptx").write_text("b", encoding="utf-8")
            result = scan_root(root)
            report = deepcopy(result.report)
            plan_items = report["organization_suggestions"][0]["plan_items"]
            alternative = deepcopy(plan_items[0])
            alternative["destination"] = (
                "Organized/Alternative/slides/EvoSim_project_slides.pptx"
            )
            plan_items.append(alternative)
            result = replace(result, report=report)

            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-multiple",
                completed_at=COMPLETED_AT,
            )

            choice = next(
                choice
                for choice in projection.choices_needed
                if choice.item is not None
                and choice.item.relative_path == "EvoSim_project_slides.pptx"
            )
            self.assertEqual(choice.reason_code, "multiple_safe_destinations")
            self.assertEqual(
                [option.action for option in choice.options],
                ["move", "move", "keep"],
            )

    def test_duplicate_choice_is_neutral_hash_free_and_summary_counts_excess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Inbox").mkdir()
            (root / "Archive").mkdir()
            (root / "Inbox" / "notes-copy.txt").write_text("same", encoding="utf-8")
            (root / "Archive" / "notes-copy.txt").write_text("same", encoding="utf-8")
            result = scan_root(root)

            projection = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-duplicates",
                completed_at=COMPLETED_AT,
            )

            choice = next(
                choice
                for choice in projection.choices_needed
                if choice.choice_type == "exact_duplicate"
            )
            self.assertEqual(choice.identical_copy_count, 2)
            self.assertEqual(len(choice.copies), 2)
            self.assertEqual(
                [option.action for option in choice.options],
                ["keep_all_copies", "choose_copies_for_duplicate_bin"],
            )
            self.assertEqual(projection.summary.duplicate_copies, 1)
            serialized = json.dumps(projection.to_dict()).lower()
            self.assertNotIn("sha256", serialized)
            self.assertNotIn("original", serialized)

    def test_projection_is_deterministic_and_has_fixed_planning_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Submissions").mkdir()
            (root / "Submissions" / "work.txt").write_text("work", encoding="utf-8")
            result = scan_root(root)

            first = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-repeat",
                completed_at=COMPLETED_AT,
            ).to_dict()
            second = build_read_only_organization_slice(
                scan_result=result,
                scan_generation="scan-repeat",
                completed_at=COMPLETED_AT,
            ).to_dict()

            self.assertEqual(first, second)
            self.assertEqual(first["planning"]["strategy_id"], "current_deterministic")
            self.assertIsNone(first["planning"]["organization_style"])
            self.assertIsNone(first["freshness"]["last_organized_at"])
            self.assertNotIn("decision", json.dumps(first).lower())


if __name__ == "__main__":
    unittest.main()
