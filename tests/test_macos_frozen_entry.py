from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from organizer.macos_launcher import frozen_main


class MacOSFrozenEntryTests(unittest.TestCase):
    def test_normal_entry_delegates_to_the_existing_native_main(self) -> None:
        with mock.patch(
            "organizer.macos_launcher.__main__.main",
            return_value=7,
        ) as native_main:
            result = frozen_main.main(["normal-private-argument"])

        self.assertEqual(result, 7)
        native_main.assert_called_once_with(("normal-private-argument",))

    def test_packaging_smoke_imports_resources_without_native_launch(self) -> None:
        with (
            mock.patch(
                "organizer.macos_launcher.__main__.main"
            ) as native_main,
            mock.patch(
                "organizer.web.config._launch_token",
                side_effect=AssertionError("token creation is forbidden"),
            ),
            mock.patch(
                "organizer.web.config._session_secret",
                side_effect=AssertionError("secret creation is forbidden"),
            ),
            mock.patch(
                "organizer.macos_launcher.appkit_adapter.AppKitAdapter"
            ) as adapter,
            mock.patch(
                "organizer.web.server.bind_loopback_socket"
            ) as socket_binder,
        ):
            result = frozen_main.main(["--packaging-smoke-test"])

        self.assertEqual(result, 0)
        native_main.assert_not_called()
        adapter.assert_not_called()
        socket_binder.assert_not_called()

    def test_packaging_smoke_does_not_create_application_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "disposable-home"
            with mock.patch.object(Path, "home", return_value=home):
                result = frozen_main.main(["--packaging-smoke-test"])

            self.assertEqual(result, 0)
            self.assertFalse(home.exists())

    def test_packaging_failure_is_bounded_and_has_no_traceback(self) -> None:
        stderr = StringIO()
        with (
            mock.patch.object(
                frozen_main,
                "_packaging_smoke_test",
                side_effect=RuntimeError("private packaging detail"),
            ),
            redirect_stderr(stderr),
        ):
            result = frozen_main.main(["--packaging-smoke-test"])

        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue().strip(),
            "Loci Flow packaging check failed.",
        )
        self.assertNotIn("private packaging", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

        stderr = StringIO()
        with (
            mock.patch(
                "organizer.macos_launcher.__main__.main",
                side_effect=RuntimeError("private startup detail"),
            ),
            redirect_stderr(stderr),
        ):
            result = frozen_main.main([])

        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue().strip(),
            "Loci Flow packaging check failed.",
        )
        self.assertNotIn("private startup", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
