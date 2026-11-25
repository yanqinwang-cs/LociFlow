from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from organizer.macos_launcher import frozen_main
from organizer.macos_launcher.packaged_diagnostics import (
    PackagedDiagnosticInput,
)


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

    def test_frozen_packaging_smoke_still_creates_no_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "disposable-home"
            with (
                mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(
                    frozen_main,
                    "_verify_packaged_resources",
                ),
                mock.patch(
                    "organizer.macos_launcher.packaged_diagnostics."
                    "create_packaged_diagnostic_sink",
                ) as create_sink,
            ):
                result = frozen_main.main(["--packaging-smoke-test"])

            self.assertEqual(result, 0)
            create_sink.assert_not_called()
            self.assertFalse(home.exists())

    def test_workflow_acceptance_is_frozen_only(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = frozen_main.main(["--packaging-workflow-acceptance"])

        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue().strip(),
            "Loci Flow frozen workflow acceptance failed.",
        )

    def test_frozen_workflow_uses_disposable_paths_sink_and_preflight(
        self,
    ) -> None:
        sink = mock.Mock()
        report = object()
        stdout = StringIO()
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch(
                "organizer.macos_launcher.packaged_diagnostics."
                "create_packaged_diagnostic_sink",
                return_value=sink,
            ),
            mock.patch.object(
                frozen_main,
                "_verify_packaged_resources",
            ) as preflight,
            mock.patch(
                "organizer.macos_launcher.frozen_acceptance."
                "run_frozen_workflow_acceptance",
                return_value=report,
            ) as run_workflow,
            mock.patch(
                "organizer.macos_launcher.frozen_acceptance."
                "format_frozen_workflow_report",
                return_value='{"acceptance":"passed"}',
            ),
            redirect_stdout(stdout),
        ):
            result = frozen_main.main(
                ["--packaging-workflow-acceptance"]
            )

        self.assertEqual(result, 0)
        preflight.assert_called_once_with(include_runtime_identity=True)
        self.assertEqual(sink.call_args_list[0].args[0].code, "launcher_started")
        run_workflow.assert_called_once()
        call = run_workflow.call_args.kwargs
        self.assertIs(call["diagnostic_sink"], sink)
        self.assertNotEqual(call["selected_root"], call["different_root"])
        self.assertFalse(call["selected_root"].exists())
        self.assertFalse(call["application_paths"].home.exists())
        self.assertEqual(stdout.getvalue().strip(), '{"acceptance":"passed"}')

    def test_frozen_launch_activates_sink_before_preflight_and_injects_it(
        self,
    ) -> None:
        paths = object()
        sink = mock.Mock()
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch(
                "organizer.macos_launcher.application_paths."
                "MacOSApplicationPaths.for_home",
                return_value=paths,
            ),
            mock.patch(
                "organizer.macos_launcher.packaged_diagnostics."
                "create_packaged_diagnostic_sink",
                return_value=sink,
            ),
            mock.patch.object(
                frozen_main,
                "_verify_packaged_resources",
            ) as preflight,
            mock.patch(
                "organizer.macos_launcher.__main__.main",
                return_value=7,
            ) as native_main,
        ):
            result = frozen_main.main([])

        self.assertEqual(result, 7)
        preflight.assert_called_once_with(include_runtime_identity=True)
        first = sink.call_args_list[0].args[0]
        self.assertEqual(
            first,
            PackagedDiagnosticInput("startup", "launcher_started"),
        )
        native_main.assert_called_once_with(
            (),
            diagnostic_sink=sink,
            application_paths=paths,
        )

    def test_frozen_resource_failure_is_logged_and_presented_before_launch(
        self,
    ) -> None:
        sink = mock.Mock()
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch(
                "organizer.macos_launcher.application_paths."
                "MacOSApplicationPaths.for_home",
                return_value=object(),
            ),
            mock.patch(
                "organizer.macos_launcher.packaged_diagnostics."
                "create_packaged_diagnostic_sink",
                return_value=sink,
            ),
            mock.patch.object(
                frozen_main,
                "_verify_packaged_resources",
                side_effect=frozen_main._PackagedResourceError,
            ),
            mock.patch.object(
                frozen_main,
                "_present_packaged_startup_failure",
            ) as present,
            mock.patch(
                "organizer.macos_launcher.__main__.main",
            ) as native_main,
        ):
            result = frozen_main.main([])

        self.assertEqual(result, 1)
        native_main.assert_not_called()
        present.assert_called_once_with()
        self.assertEqual(sink.call_count, 2)
        failure = sink.call_args_list[-1].args[0]
        self.assertEqual(failure.phase, "startup")
        self.assertEqual(failure.code, "packaged_resource_missing")
        self.assertEqual(failure.exception_class, "_PackagedResourceError")

    def test_resource_preflight_checks_identity_without_creating_runtime_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resource = Path(directory) / "resource"
            identity = Path(directory) / "identity"
            resource.write_bytes(b"resource")
            identity.write_text(
                '{"application_version":"1.0.0","build_number":1}',
                encoding="ascii",
            )
            with (
                mock.patch.object(
                    frozen_main,
                    "_required_packaged_files",
                    return_value=(resource,),
                ),
                mock.patch(
                    "organizer.macos_launcher.packaged_diagnostics."
                    "runtime_identity_path",
                    return_value=identity,
                ),
            ):
                frozen_main._verify_packaged_resources(
                    include_runtime_identity=True
                )

    def test_packaged_failure_alert_uses_only_bounded_native_copy(self) -> None:
        adapter = mock.Mock()
        with mock.patch(
            "organizer.macos_launcher.appkit_adapter.AppKitAdapter",
            return_value=adapter,
        ):
            frozen_main._present_packaged_startup_failure()

        adapter.initialize.assert_called_once_with()
        adapter.show_error.assert_called_once()
        message = adapter.show_error.call_args.args[0]
        self.assertEqual(message.title, "Loci Flow")
        self.assertEqual(
            message.message,
            "Loci Flow could not start safely.",
        )

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
