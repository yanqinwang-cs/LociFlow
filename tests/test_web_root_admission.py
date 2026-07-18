from __future__ import annotations

from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from organizer.web.root_admission import (
    RootAdmissionError,
    admit_root,
)


class WebRootAdmissionTests(unittest.TestCase):
    def test_valid_directory_is_resolved_once_and_result_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                Path,
                "resolve",
                autospec=True,
                wraps=Path.resolve,
            ) as resolver:
                admitted = admit_root(root)

            self.assertEqual(admitted.path, root.resolve())
            self.assertEqual(resolver.call_count, 1)
            with self.assertRaises(FrozenInstanceError):
                admitted.path = root / "other"  # type: ignore[misc]

    def test_missing_path_and_regular_file_are_rejected_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RootAdmissionError) as missing:
                admit_root(root / "private-missing-name")
            self.assertEqual(missing.exception.code, "root_missing")
            self.assertNotIn(str(root), str(missing.exception))

            file_path = root / "file.txt"
            file_path.write_text("content", encoding="utf-8")
            with self.assertRaises(RootAdmissionError) as regular_file:
                admit_root(file_path)
            self.assertEqual(regular_file.exception.code, "root_not_directory")
            self.assertNotIn(str(file_path), str(regular_file.exception))

    def test_direct_symlinks_to_directory_and_file_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_directory = root / "real-directory"
            real_directory.mkdir()
            real_file = root / "real-file.txt"
            real_file.write_text("content", encoding="utf-8")
            directory_link = root / "directory-link"
            file_link = root / "file-link"
            directory_link.symlink_to(real_directory, target_is_directory=True)
            file_link.symlink_to(real_file)

            for link in (directory_link, file_link):
                with self.subTest(link=link):
                    with (
                        mock.patch.object(Path, "resolve", autospec=True) as resolver,
                        self.assertRaises(RootAdmissionError) as raised,
                    ):
                        admit_root(link)
                    self.assertEqual(raised.exception.code, "root_symlink")
                    resolver.assert_not_called()

    def test_symlink_children_do_not_make_an_ordinary_root_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            (root / "child-link").symlink_to(target, target_is_directory=True)

            self.assertEqual(admit_root(root).path, root.resolve())

    def test_filesystem_root_and_application_bundle_are_rejected(self) -> None:
        with self.assertRaises(RootAdmissionError) as filesystem_root:
            admit_root(Path("/"))
        self.assertEqual(filesystem_root.exception.code, "root_filesystem")

        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "Loci Flow.APP"
            bundle.mkdir()
            with self.assertRaises(RootAdmissionError) as app_bundle:
                admit_root(bundle)
            self.assertEqual(app_bundle.exception.code, "root_app_bundle")

    def test_inaccessible_directory_has_a_safe_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "organizer.web.root_admission.os.scandir",
                side_effect=PermissionError("private operating-system detail"),
            ):
                with self.assertRaises(RootAdmissionError) as raised:
                    admit_root(root)

            self.assertEqual(raised.exception.code, "root_inaccessible")
            self.assertNotIn(str(root), raised.exception.safe_message)
            self.assertNotIn("operating-system", raised.exception.safe_message)

    def test_user_home_is_expanded_before_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "organizer.web.root_admission.os.path.expanduser",
                return_value=os.fspath(root),
            ) as expander:
                admitted = admit_root("~/Documents")

            expander.assert_called_once_with("~/Documents")
            self.assertEqual(admitted.path, root.resolve())

    def test_relative_directory_is_not_mistaken_for_filesystem_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "organizer.web.root_admission.os.path.abspath",
                return_value=os.fspath(root),
            ):
                admitted = admit_root(".")

            self.assertEqual(admitted.path, root.resolve())


if __name__ == "__main__":
    unittest.main()
