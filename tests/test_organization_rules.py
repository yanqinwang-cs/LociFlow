from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

from organizer.organization_rules import (
    ORGANIZATION_RULES_RELATIVE_PATH,
    canonical_anchor,
    load_organization_rules,
    organization_rules_from_data,
)


class OrganizationRulesTests(unittest.TestCase):
    def test_missing_file_is_valid_empty_rules_without_creating_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            result = load_organization_rules(root)

            self.assertEqual(result.status, "missing")
            self.assertTrue(result.available)
            self.assertIsNone(result.source_path)
            self.assertIn("summary", result.rules.ignored_terms)
            self.assertFalse((root / ORGANIZATION_RULES_RELATIVE_PATH).exists())

    def test_valid_config_loads_locked_ignored_and_aliases(self) -> None:
        data = {
            "version": 1,
            "locked_anchors": ["EvoSim"],
            "ignored_terms": ["Scratch"],
            "anchor_aliases": {"cs1010x": "CS1010X"},
        }

        rules, warnings = organization_rules_from_data(data)

        self.assertEqual(warnings, [])
        self.assertIn("evosim", rules.locked_anchors)
        self.assertIn("scratch", rules.ignored_terms)
        self.assertEqual(canonical_anchor("CS1010x", rules), "cs1010x")

    def test_invalid_sections_warn_and_preserve_valid_entries(self) -> None:
        data = {
            "version": 1,
            "locked_anchors": ["ValidAnchor", "", "../bad", 3],
            "ignored_terms": "bad",
            "anchor_aliases": {"Alias": "ValidAnchor", "bad/path": "Target"},
        }

        rules, warnings = organization_rules_from_data(data)

        self.assertIn("validanchor", rules.locked_anchors)
        self.assertEqual(rules.anchor_aliases["alias"], "validanchor")
        self.assertTrue(warnings)

    def test_ignored_terms_win_over_locked_and_aliases_to_ignored(self) -> None:
        data = {
            "version": 1,
            "locked_anchors": ["Summary", "ProjectX"],
            "ignored_terms": ["ProjectX"],
            "anchor_aliases": {"Alias": "Summary"},
        }

        rules, warnings = organization_rules_from_data(data)

        self.assertNotIn("summary", rules.locked_anchors)
        self.assertNotIn("projectx", rules.locked_anchors)
        self.assertNotIn("alias", rules.anchor_aliases)
        self.assertTrue(any("ignored" in warning for warning in warnings))

    def test_alias_cycles_are_rejected_with_warning(self) -> None:
        data = {
            "version": 1,
            "locked_anchors": [],
            "ignored_terms": [],
            "anchor_aliases": {"a": "b", "b": "a", "c": "Course"},
        }

        rules, warnings = organization_rules_from_data(data)

        self.assertNotIn("a", rules.anchor_aliases)
        self.assertNotIn("b", rules.anchor_aliases)
        self.assertEqual(rules.anchor_aliases["c"], "course")
        self.assertTrue(any("cycle" in warning for warning in warnings))

    def test_file_loader_reads_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "locked_anchors": ["ResearchX"],
                        "ignored_terms": [],
                        "anchor_aliases": {},
                    }
                ),
                encoding="utf-8",
            )

            result = load_organization_rules(root)

            self.assertEqual(result.status, "loaded")
            self.assertEqual(result.source_path, config.resolve())
            self.assertIn("researchx", result.rules.locked_anchors)

    def test_malformed_json_is_unavailable_without_rewrite_or_raw_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            original = '{"private-secret":'
            config.write_text(original, encoding="utf-8")

            result = load_organization_rules(root)

            self.assertEqual(result.status, "unavailable")
            self.assertFalse(result.available)
            self.assertIsNone(result.rules)
            self.assertEqual(result.error_code, "standards_unavailable")
            self.assertEqual(
                result.message,
                "Standards could not be loaded. Fix them before organizing.",
            )
            safe_text = " ".join([result.message, *result.warnings])
            self.assertNotIn(str(root), safe_text)
            self.assertNotIn("private-secret", safe_text)
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_wrong_version_and_unknown_fields_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            for data in (
                {"version": 2},
                {"version": 1, "unexpected": True},
            ):
                with self.subTest(data=data):
                    config.write_text(json.dumps(data), encoding="utf-8")
                    result = load_organization_rules(root)
                    self.assertEqual(result.status, "unavailable")
                    self.assertIsNone(result.rules)

    def test_unreadable_existing_file_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            config.write_text('{"version": 1}', encoding="utf-8")

            with mock.patch.object(Path, "open", side_effect=PermissionError):
                result = load_organization_rules(root)

            self.assertEqual(result.status, "unavailable")
            self.assertIn("could not be read", result.warnings[0].lower())

    def test_symlinked_rules_storage_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.json"
            outside.write_text('{"version": 1}', encoding="utf-8")
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            config.symlink_to(outside)

            result = load_organization_rules(root)

            self.assertEqual(result.status, "unavailable")
            self.assertIsNone(result.rules)


if __name__ == "__main__":
    unittest.main()
