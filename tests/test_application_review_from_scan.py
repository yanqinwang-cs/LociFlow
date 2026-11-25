from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organizer.application.review_service import (
    create_review_session_from_scan_result,
    get_review_item_metadata,
    get_review_source_key,
    get_review_view,
    update_review_filter,
    update_review_page_size,
    update_review_sort,
)
from organizer.application.scan_service import scan_root
from organizer.application.standards_read_models import OrganizationUnavailableError
from organizer.organization_rules import ORGANIZATION_RULES_RELATIVE_PATH


class ReviewFromScanTests(unittest.TestCase):
    def test_unavailable_scan_stays_unavailable_after_rules_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            config.write_text('{"version":', encoding="utf-8")
            (root / "evosim_notes.txt").write_text("notes", encoding="utf-8")
            (root / "evosim_slides.pdf").write_text("slides", encoding="utf-8")
            unavailable_scan = scan_root(root)

            config.write_text(
                '{"version": 1, "locked_anchors": ["Evosim"]}',
                encoding="utf-8",
            )

            with self.assertRaises(OrganizationUnavailableError):
                create_review_session_from_scan_result(unavailable_scan)

            recovered_scan = scan_root(root)
            recovered_session = create_review_session_from_scan_result(recovered_scan)

        self.assertTrue(
            any(item.category == "organization" for item in recovered_session.items)
        )

    def test_review_uses_scan_bound_rules_after_durable_rules_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ORGANIZATION_RULES_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            for anchor in ("alpha", "beta"):
                (root / f"{anchor}_notes.txt").write_text("notes", encoding="utf-8")
                (root / f"{anchor}_slides.pdf").write_text("slides", encoding="utf-8")
            config.write_text(
                '{"version": 1, "locked_anchors": ["alpha"]}',
                encoding="utf-8",
            )
            scan_with_rules_a = scan_root(root)
            config.write_text(
                '{"version": 1, "locked_anchors": ["beta"]}',
                encoding="utf-8",
            )

            with mock.patch(
                "organizer.application.review_service.load_organization_rules",
                side_effect=AssertionError("review must not reload Standards"),
            ):
                session_a = create_review_session_from_scan_result(scan_with_rules_a)

            scan_with_rules_b = scan_root(root)
            session_b = create_review_session_from_scan_result(scan_with_rules_b)

        sources_a = {
            item.plan_item.source.name
            for item in session_a.items
            if item.category == "organization"
        }
        sources_b = {
            item.plan_item.source.name
            for item in session_b.items
            if item.category == "organization"
        }
        self.assertEqual(sources_a, {"alpha_notes.txt", "alpha_slides.pdf"})
        self.assertEqual(sources_b, {"beta_notes.txt", "beta_slides.pdf"})

    def test_rows_come_from_authoritative_report_without_rescanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")
            (root / "backup copy.txt").write_text("copy", encoding="utf-8")
            result = scan_root(root)

            with mock.patch(
                "organizer.application.review_service.scan_directory",
                side_effect=AssertionError("review explorer must not rescan"),
            ):
                session = create_review_session_from_scan_result(result)

            self.assertEqual(
                tuple(item.id for item in session.items),
                ("D1", "R1"),
            )
            self.assertEqual(session.items[1].review_category, "backup_or_copy")
            self.assertTrue(session.persist_review_state)
            self.assertEqual(session.metadata_items, result.metadata_items)
            self.assertEqual(get_review_source_key(session, "D1"), "b.txt")

            view = get_review_view(
                update_review_page_size(
                    update_review_sort(
                        update_review_filter(session, "category", "duplicate"),
                        "source",
                        "desc",
                    ),
                    "25",
                )
            )
            self.assertEqual(view.matching_count, 1)
            self.assertEqual(view.rows[0].id, "D1")

    def test_report_adapter_does_not_write_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = scan_root(root)
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            create_review_session_from_scan_result(result)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertEqual(before, after)

    def test_review_session_keeps_scan_time_metadata_without_live_stat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "b.txt"
            (root / "a.txt").write_text("same", encoding="utf-8")
            source.write_text("same", encoding="utf-8")
            result = scan_root(root)
            scanned = next(
                item for item in result.metadata_items if item.relative_path == Path("b.txt")
            )
            session = create_review_session_from_scan_result(result)
            source.write_text("changed after scan", encoding="utf-8")

            with mock.patch.object(
                Path,
                "stat",
                side_effect=AssertionError("review metadata must use the scan snapshot"),
            ):
                item_metadata = get_review_item_metadata(session, "D1")

            self.assertEqual(item_metadata.size_bytes, scanned.size_bytes)
            self.assertEqual(item_metadata.modified_time, scanned.modified_time)


if __name__ == "__main__":
    unittest.main()
