from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import organizer.duplicates as duplicates
import organizer.scanner as scanner
from organizer.application.scan_service import scan_root, write_scan_report
from organizer.scan_boundary import (
    DIFFERENT_VOLUME_SUMMARY,
    ScanBoundary,
    ScanBoundaryKind,
    safe_boundary_summaries,
)
from organizer.scanner import scan_directory_with_boundaries


class ScanBoundaryTests(unittest.TestCase):
    def test_root_device_failure_is_bounded_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def unavailable(path: Path) -> int:
                raise OSError("private operating-system detail")

            with self.assertRaises(ValueError) as raised:
                scan_directory_with_boundaries(
                    root,
                    directory_device=unavailable,
                )

            self.assertEqual(
                str(raised.exception),
                "selected folder is not a readable directory",
            )
            self.assertNotIn(str(root), str(raised.exception))
            self.assertEqual(tuple(root.iterdir()), ())

    def test_selected_external_volume_style_root_is_the_device_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()
            (folder / "notes.txt").write_text("notes", encoding="utf-8")

            result = scan_directory_with_boundaries(
                root,
                directory_device=lambda path: 9001,
            )

            self.assertEqual(result.boundaries, ())
            self.assertEqual(
                tuple(item.relative_path.as_posix() for item in result.metadata_items),
                (".", "folder", "folder/notes.txt"),
            )

    def test_different_device_directory_is_boundary_before_recursion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary = root / "external"
            boundary.mkdir()
            hidden = boundary / "private.txt"
            hidden.write_text("private", encoding="utf-8")
            inspected: list[Path] = []

            def device(path: Path) -> int:
                inspected.append(path)
                return 2 if path == boundary.resolve() else 1

            result = scan_directory_with_boundaries(
                root,
                directory_device=device,
            )

            self.assertEqual(
                result.boundaries,
                (
                    ScanBoundary(
                        ScanBoundaryKind.DIFFERENT_VOLUME,
                        Path("external"),
                    ),
                ),
            )
            self.assertEqual(
                tuple(item.relative_path.as_posix() for item in result.metadata_items),
                (".",),
            )
            self.assertEqual(inspected, [root.resolve(), boundary.resolve()])
            self.assertNotIn(hidden, inspected)

    def test_same_device_sibling_remains_scannable_while_boundary_is_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "local"
            external = root / "external"
            local.mkdir()
            external.mkdir()
            (local / "keep.txt").write_text("keep", encoding="utf-8")
            (external / "skip.txt").write_text("skip", encoding="utf-8")

            result = scan_directory_with_boundaries(
                root,
                directory_device=(
                    lambda path: 2 if path == external.resolve() else 1
                ),
            )
            paths = {
                item.relative_path.as_posix() for item in result.metadata_items
            }

            self.assertIn("local/keep.txt", paths)
            self.assertNotIn("external", paths)
            self.assertNotIn("external/skip.txt", paths)

    def test_unavailable_directory_fails_closed_without_raw_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unavailable = root / "unavailable"
            unavailable.mkdir()
            (unavailable / "secret.txt").write_text("secret", encoding="utf-8")

            def device(path: Path) -> int:
                if path == unavailable.resolve():
                    raise PermissionError("private operating-system detail")
                return 1

            result = scan_directory_with_boundaries(
                root,
                directory_device=device,
            )

            self.assertEqual(
                result.boundaries,
                (
                    ScanBoundary(
                        ScanBoundaryKind.UNAVAILABLE,
                        Path("unavailable"),
                    ),
                ),
            )
            safe = safe_boundary_summaries(result.boundaries)
            self.assertNotIn(str(root), repr(safe))
            self.assertNotIn("operating-system", repr(safe))
            self.assertNotIn("secret.txt", repr(safe))

    def test_directory_disappearing_before_listing_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unstable = root / "unstable"
            unstable.mkdir()
            real_entries = scanner._directory_entries

            def listing(path: Path):
                if path == unstable.resolve():
                    raise FileNotFoundError("private path")
                return real_entries(path)

            with mock.patch(
                "organizer.scanner._directory_entries",
                side_effect=listing,
            ):
                result = scan_directory_with_boundaries(root)

            self.assertEqual(
                result.boundaries,
                (
                    ScanBoundary(
                        ScanBoundaryKind.UNAVAILABLE,
                        Path("unstable"),
                    ),
                ),
            )
            self.assertEqual(
                tuple(item.relative_path.as_posix() for item in result.metadata_items),
                (".",),
            )

    def test_boundaries_are_deterministic_and_not_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("zeta", "alpha"):
                child = root / name
                child.mkdir()
                (child / "file.txt").write_text(name, encoding="utf-8")

            result = scan_directory_with_boundaries(
                root,
                directory_device=lambda path: 1 if path == root.resolve() else 2,
            )

            self.assertEqual(
                tuple(boundary.relative_path.as_posix() for boundary in result.boundaries),
                ("alpha", "zeta"),
            )
            self.assertEqual(
                tuple(item.relative_path.as_posix() for item in result.metadata_items),
                (".",),
            )

    def test_symlink_is_not_followed_for_device_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            (target / "file.txt").write_text("target", encoding="utf-8")
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            inspected: list[Path] = []

            def device(path: Path) -> int:
                inspected.append(path)
                return 1

            result = scan_directory_with_boundaries(
                root,
                directory_device=device,
            )

            self.assertNotIn(link, inspected)
            self.assertNotIn("link", {
                item.relative_path.as_posix() for item in result.metadata_items
            })
            self.assertEqual(result.boundaries, ())

    def test_application_result_excludes_boundary_from_hashing_and_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "local.txt"
            local.write_text("same", encoding="utf-8")
            external = root / "external"
            external.mkdir()
            hidden = external / "copy.txt"
            hidden.write_text("same", encoding="utf-8")
            root_device = scanner._directory_device(root.resolve())

            def device(path: Path) -> int:
                return (
                    root_device + 1
                    if path == external.resolve()
                    else root_device
                )

            with (
                mock.patch("organizer.scanner._directory_device", side_effect=device),
                mock.patch(
                    "organizer.duplicates.hash_file",
                    wraps=duplicates.hash_file,
                ) as hasher,
            ):
                result = scan_root(root)

            hashed = {call.args[0] for call in hasher.call_args_list}
            self.assertIn(local.resolve(), hashed)
            self.assertNotIn(hidden.resolve(), hashed)
            self.assertEqual(result.summary.duplicate_group_count, 0)
            self.assertEqual(result.summary.organization_suggestion_count, 0)
            self.assertEqual(len(result.boundaries), 1)
            self.assertEqual(result.warnings[-1], DIFFERENT_VOLUME_SUMMARY)
            self.assertNotIn("external", repr(result.warnings))
            self.assertNotIn(str(root_device), repr(result.warnings))
            self.assertFalse(root.joinpath("AI_Review").exists())

            report_path = write_scan_report(result)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertNotIn("boundaries", report)
            self.assertNotIn(DIFFERENT_VOLUME_SUMMARY, report["warnings"])


if __name__ == "__main__":
    unittest.main()
