import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from organizer.application.organization_slice_service import (
    build_internal_organization_projection,
)
from organizer.application.scan_service import scan_root
from organizer.duplicates import find_exact_duplicates
from organizer.scanner import scan_directory
from organizer.scope import (
    is_actionable_destination_path,
    validate_duplicate_bin_item_destination,
)


class DuplicateBinScopeTests(unittest.TestCase):
    def test_scanner_prunes_reserved_subtree_before_recording_or_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bin_root = root / "AI_Review" / "duplicate_bin"
            bin_root.mkdir(parents=True)
            (bin_root / "copy.txt").write_text("same", encoding="utf-8")
            (root / "ordinary.txt").write_text("same", encoding="utf-8")
            unrelated = root / "AI_Review" / "notes.txt"
            unrelated.write_text("review", encoding="utf-8")

            metadata = scan_directory(root)
            relative_paths = {item.relative_path.as_posix() for item in metadata}

            self.assertNotIn("AI_Review/duplicate_bin", relative_paths)
            self.assertNotIn("AI_Review/duplicate_bin/copy.txt", relative_paths)
            self.assertIn("AI_Review", relative_paths)
            self.assertIn("AI_Review/notes.txt", relative_paths)
            self.assertEqual(find_exact_duplicates(metadata), [])

            result = scan_root(root)
            projection = build_internal_organization_projection(
                scan_result=result,
                scan_generation="scan-one",
                completed_at=datetime(2026, 7, 16, tzinfo=UTC),
            )
            self.assertNotIn("duplicate_bin", json.dumps(result.report))
            self.assertTrue(
                all(
                    "duplicate_bin" not in item.relative_path.as_posix()
                    for item in projection.metadata_items
                )
            )
            self.assertTrue(
                all(
                    "duplicate_bin" not in candidate.source_metadata.relative_path.as_posix()
                    for candidate in projection.recommendations_by_id.values()
                )
            )

    def test_ordinary_destination_rejects_every_reserved_bin_path(self) -> None:
        for relative in (
            Path("AI_Review/duplicate_bin"),
            Path("AI_Review/duplicate_bin/items"),
            Path("AI_Review/duplicate_bin/items/x/file.txt"),
        ):
            with self.subTest(relative=relative):
                self.assertFalse(is_actionable_destination_path(relative, ()))
        self.assertTrue(
            is_actionable_destination_path(Path("AI_Review/other/report.txt"), ())
        )

    def test_purpose_specific_item_validator_accepts_only_exact_new_layout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            item_id = str(uuid.uuid4())
            valid = (
                root / "AI_Review" / "duplicate_bin" / "items" / item_id / "copy.txt"
            )

            self.assertEqual(
                validate_duplicate_bin_item_destination(valid, root), valid
            )
            self.assertFalse(valid.parent.exists())

            invalid = (
                root / "AI_Review" / "duplicate_bin" / item_id / "copy.txt",
                root
                / "AI_Review"
                / "duplicate_bin"
                / "items"
                / "not-a-uuid"
                / "copy.txt",
                root
                / "AI_Review"
                / "duplicate_bin"
                / "items"
                / item_id
                / "nested"
                / "copy.txt",
            )
            for path in invalid:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    validate_duplicate_bin_item_destination(path, root)

    def test_item_validator_rejects_symlink_and_normalized_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            outside = root.parent / f"outside-{uuid.uuid4()}"
            outside.mkdir()
            review = root / "AI_Review"
            try:
                review.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError):
                outside.rmdir()
                self.skipTest("symlinks are unavailable")
            item_id = str(uuid.uuid4())
            path = review / "duplicate_bin" / "items" / item_id / "copy.txt"
            with self.assertRaises(ValueError):
                validate_duplicate_bin_item_destination(path, root)
            review.unlink()
            outside.rmdir()

            items = root / "AI_Review" / "duplicate_bin" / "items"
            items.mkdir(parents=True)
            (items / item_id.upper()).mkdir()
            with self.assertRaises(ValueError):
                validate_duplicate_bin_item_destination(
                    items / item_id / "copy.txt",
                    root,
                )


if __name__ == "__main__":
    unittest.main()
