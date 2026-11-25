import json
import tempfile
import unittest
from pathlib import Path

from organizer.application.scan_service import scan_root
from organizer.application.standards_read_service import (
    EXISTING_FOLDER_POLICY_STANDARD_ID,
)
from organizer.organization_rules import ORGANIZATION_RULES_RELATIVE_PATH


class StandardsReadServiceTests(unittest.TestCase):
    def test_missing_storage_projects_no_saved_standards_but_keeps_built_in_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "summary_notes.txt").write_text("notes", encoding="utf-8")
            (root / "summary_slides.pdf").write_text("slides", encoding="utf-8")

            result = scan_root(root)

        saved = [
            item for item in result.standards.items
            if item.preset_type.startswith("legacy_")
        ]
        built_ins = [
            item for item in result.standards.items
            if item.preset_type in {"built_in_protection", "existing_folder_policy"}
        ]
        self.assertEqual(result.organization_rules_load_result.status, "missing")
        self.assertEqual(saved, [])
        self.assertTrue(built_ins)
        self.assertTrue(all(item.active and not item.mutable for item in built_ins))
        self.assertEqual(result.report["organization_rules"]["ignored_terms"], [])
        self.assertIn(
            "Summary",
            {
                item["anchor"]
                for item in result.report["anchor_decisions"]["ignored_terms"]
            },
        )

    def test_built_ins_are_active_immutable_and_include_existing_folder_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = scan_root(Path(directory))

        standards = result.standards
        self.assertEqual(standards.status, "available")
        self.assertTrue(standards.items)
        self.assertTrue(all(item.active and not item.mutable for item in standards.items))
        existing = next(
            item for item in standards.items
            if item.id == EXISTING_FOLDER_POLICY_STANDARD_ID
        )
        self.assertEqual(existing.preset_type, "existing_folder_policy")

    def test_legacy_v1_projection_is_mutable_safe_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "locked_anchors": ["CS1010X"],
                        "ignored_terms": ["Scratch"],
                        "anchor_aliases": {"Programming Methodology": "CS1010X"},
                        "preferred_granularities": ["course_code"],
                    }
                ),
                encoding="utf-8",
            )

            first = scan_root(root).standards
            second = scan_root(root).standards

        self.assertEqual(first.to_dict(), second.to_dict())
        legacy = [item for item in first.items if item.preset_type.startswith("legacy_")]
        self.assertEqual(
            {item.preset_type for item in legacy},
            {"legacy_locked_anchor", "legacy_ignored_term", "legacy_alias"},
        )
        self.assertTrue(all(item.active and item.mutable for item in legacy))
        serialized = json.dumps(first.to_dict()).lower()
        for forbidden in (
            "preferred_granularities",
            "organization_rules",
            "python",
            "regex",
            str(root).lower(),
        ):
            self.assertNotIn(forbidden, serialized)

    def test_unavailable_projection_has_only_safe_attention_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            config.write_text('{"secret":', encoding="utf-8")

            standards = scan_root(root).standards

        self.assertEqual(standards.status, "unavailable")
        self.assertEqual(standards.error_code, "standards_unavailable")
        serialized = json.dumps(standards.to_dict())
        self.assertIn("Fix them before organizing", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn(str(root), serialized)


if __name__ == "__main__":
    unittest.main()
