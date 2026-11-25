import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from organizer.web.config import WebAppConfig


class WebConfigTests(unittest.TestCase):
    def test_valid_root_is_resolved_and_configuration_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WebAppConfig(root / ".")

            self.assertEqual(config.root, root.resolve())
            with self.assertRaises(FrozenInstanceError):
                config.root = root / "other"  # type: ignore[misc]

    def test_root_validation_reuses_shared_admission_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch(
                "organizer.web.config.admit_root",
                wraps=__import__(
                    "organizer.web.config",
                    fromlist=["admit_root"],
                ).admit_root,
            ) as admission:
                config = WebAppConfig(root)

            self.assertEqual(config.root, root)
            admission.assert_called_once_with(root)

    def test_missing_and_non_directory_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "does not exist"):
                WebAppConfig(root / "missing")

            file_path = root / "file.txt"
            file_path.write_text("content", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not a folder"):
                WebAppConfig(file_path)

    def test_direct_configuration_cannot_bypass_symlink_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            real_root = Path(directory) / "real"
            real_root.mkdir()
            symlink_root = Path(directory) / "link"
            symlink_root.symlink_to(real_root, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                WebAppConfig(symlink_root)

    def test_rejected_root_creates_no_session_secret_or_launch_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with (
                mock.patch("organizer.web.config._session_secret") as secret,
                mock.patch("organizer.web.config._launch_token") as token,
                self.assertRaisesRegex(ValueError, "does not exist"),
            ):
                WebAppConfig(missing)

            secret.assert_not_called()
            token.assert_not_called()

    def test_secrets_are_random_and_independent_per_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = WebAppConfig(root)
            second = WebAppConfig(root)

            self.assertGreaterEqual(len(first.session_secret), 32)
            self.assertGreaterEqual(len(first.launch_token), 32)
            self.assertNotEqual(first.session_secret, second.session_secret)
            self.assertNotEqual(first.launch_token, second.launch_token)

    def test_weak_or_non_url_safe_secrets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "session secret"):
                WebAppConfig(root, session_secret="short")
            with self.assertRaisesRegex(ValueError, "launch token"):
                WebAppConfig(root, launch_token="not/a/safe/token" * 3)

    def test_testserver_is_allowed_only_for_testing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production = WebAppConfig(root)
            testing = WebAppConfig(root, testing=True)

            self.assertEqual(production.allowed_hosts, ("127.0.0.1", "localhost"))
            self.assertEqual(
                testing.allowed_hosts,
                ("127.0.0.1", "localhost", "testserver"),
            )


if __name__ == "__main__":
    unittest.main()
