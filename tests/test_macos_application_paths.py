from __future__ import annotations

import stat
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest import mock

from organizer.application.organization_operation_repository import (
    DEFAULT_OPERATION_DIRECTORY,
)
from organizer.application.organization_undo_repository import (
    DEFAULT_UNDO_DIRECTORY,
)
from organizer.macos_launcher.application_paths import (
    ApplicationPathError,
    MacOSApplicationPaths,
)
from organizer.scope import DUPLICATE_BIN_RELATIVE_ROOT


class MacOSApplicationPathsTests(unittest.TestCase):
    def test_default_macos_layout_is_absolute_and_stable(self) -> None:
        home = Path("/Users/example")
        with mock.patch.object(Path, "home", return_value=home):
            paths = MacOSApplicationPaths.for_home()

        self.assertEqual(
            paths.application_support,
            home / "Library" / "Application Support" / "Loci Flow",
        )
        self.assertEqual(paths.locks, paths.application_support / "locks")
        self.assertEqual(paths.root_locks, paths.locks / "roots")
        self.assertEqual(
            paths.caches,
            home / "Library" / "Caches" / "Loci Flow",
        )
        self.assertEqual(
            paths.logs,
            home / "Library" / "Logs" / "Loci Flow",
        )
        self.assertEqual(paths.logs_directory, paths.logs)
        self.assertTrue(all(path.is_absolute() for path in paths.__dict__.values()))

    def test_injected_home_is_cwd_independent_and_relative_home_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            other = Path(directory) / "other"
            other.mkdir()
            expected = MacOSApplicationPaths.for_home(home)
            with chdir(other):
                actual = MacOSApplicationPaths.for_home(home)

            self.assertEqual(actual, expected)
            with self.assertRaisesRegex(ValueError, "home must be absolute"):
                MacOSApplicationPaths.for_home(Path("relative-home"))

    def test_construction_and_import_create_no_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "missing-home"

            paths = MacOSApplicationPaths.for_home(home)

            self.assertFalse(home.exists())
            self.assertFalse(paths.application_support.exists())
            self.assertFalse(paths.caches.exists())
            self.assertFalse(paths.logs.exists())

    def test_explicit_creation_is_idempotent_and_restrictive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")

            paths.create()
            paths.create()

            for path in (
                paths.application_support,
                paths.locks,
                paths.root_locks,
                paths.caches,
                paths.logs,
            ):
                with self.subTest(path=path):
                    self.assertTrue(path.is_dir())
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_lock_only_creation_does_not_create_cache_or_log_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")

            paths.create_lock_directories()

            self.assertTrue(paths.root_locks.is_dir())
            self.assertFalse(paths.caches.exists())
            self.assertFalse(paths.logs.exists())

    def test_log_only_creation_does_not_create_other_application_storage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")

            paths.create_logs_directory()

            self.assertTrue(paths.logs.is_dir())
            self.assertEqual(stat.S_IMODE(paths.logs.stat().st_mode), 0o700)
            self.assertFalse(paths.application_support.exists())
            self.assertFalse(paths.caches.exists())

    def test_unsafe_or_unavailable_application_path_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            paths = MacOSApplicationPaths.for_home(home)
            paths.application_support.parent.mkdir(parents=True)
            unsafe_target = Path(directory) / "outside"
            unsafe_target.mkdir()
            paths.application_support.symlink_to(
                unsafe_target,
                target_is_directory=True,
            )

            with self.assertRaises(ApplicationPathError) as unsafe:
                paths.create_lock_directories()
            self.assertEqual(unsafe.exception.code, "unsafe_path")
            self.assertNotIn(str(home), unsafe.exception.safe_message)
            self.assertNotIn(str(unsafe_target), unsafe.exception.safe_message)

        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            with mock.patch.object(
                Path,
                "mkdir",
                side_effect=PermissionError("private filesystem detail"),
            ):
                with self.assertRaises(ApplicationPathError) as unavailable:
                    paths.create()
            self.assertEqual(unavailable.exception.code, "unavailable")
            self.assertNotIn("filesystem detail", unavailable.exception.safe_message)

    def test_symlinked_standard_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            outside = Path(directory) / "outside"
            outside.mkdir()
            (home / "Library").symlink_to(outside, target_is_directory=True)
            paths = MacOSApplicationPaths.for_home(home)

            with self.assertRaises(ApplicationPathError) as raised:
                paths.create_lock_directories()

            self.assertEqual(raised.exception.code, "unsafe_path")
            self.assertFalse((outside / "Application Support").exists())

    def test_existing_broad_or_foreign_owned_storage_is_not_repermissioned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            paths.application_support.mkdir(parents=True, mode=0o755)
            paths.application_support.chmod(0o755)

            with self.assertRaises(ApplicationPathError) as raised:
                paths.create_lock_directories()

            self.assertEqual(raised.exception.code, "unsafe_path")
            self.assertEqual(
                stat.S_IMODE(paths.application_support.stat().st_mode),
                0o755,
            )

    def test_selected_root_durable_paths_remain_root_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected_root = Path(directory) / "selected"
            selected_root.mkdir()
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")

            self.assertEqual(
                selected_root / DEFAULT_OPERATION_DIRECTORY,
                selected_root / "AI_Review" / "organization_operations",
            )
            self.assertEqual(
                selected_root / DEFAULT_UNDO_DIRECTORY,
                selected_root / "AI_Review" / "organization_undos",
            )
            self.assertEqual(
                selected_root / DUPLICATE_BIN_RELATIVE_ROOT,
                selected_root / "AI_Review" / "duplicate_bin",
            )
            self.assertNotEqual(
                selected_root / "AI_Review",
                paths.application_support,
            )
            self.assertFalse((selected_root / "AI_Review").exists())


if __name__ == "__main__":
    unittest.main()
