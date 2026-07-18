from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from types import ModuleType
import sys
import unittest
from unittest import mock

from organizer.macos_launcher import __main__ as native_main


class MacOSLauncherEntrypointTests(unittest.TestCase):
    def test_non_macos_returns_two_without_importing_native_modules(self) -> None:
        stderr = StringIO()
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch("builtins.__import__", wraps=__import__) as importer,
            redirect_stderr(stderr),
        ):
            result = native_main.main()

        self.assertEqual(result, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "The Loci Flow native launcher is available only on macOS.",
        )
        imported = [call.args[0] for call in importer.call_args_list]
        self.assertNotIn("organizer.macos_launcher.appkit_adapter", imported)

    def test_missing_pyobjc_is_controlled_and_has_no_traceback(self) -> None:
        class MissingAdapter:
            def __init__(self) -> None:
                raise ModuleNotFoundError(
                    "No module named 'AppKit'",
                    name="AppKit",
                )

        stderr = StringIO()
        with (
            mock.patch.object(sys, "platform", "darwin"),
            _native_modules(MissingAdapter, mock.Mock()),
            redirect_stderr(stderr),
        ):
            result = native_main.main()

        output = stderr.getvalue()
        self.assertEqual(result, 2)
        self.assertIn("optional Loci Flow macOS launcher support", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("ModuleNotFoundError", output)

    def test_macos_entrypoint_delegates_exactly_once(self) -> None:
        adapter = object()
        adapter_type = mock.Mock(return_value=adapter)
        coordinator = mock.Mock()
        coordinator.run.return_value = 7
        coordinator_type = mock.Mock(return_value=coordinator)
        diagnostic_sink = mock.Mock(return_value=None)

        with (
            mock.patch.object(sys, "platform", "darwin"),
            _native_modules(adapter_type, coordinator_type, diagnostic_sink),
        ):
            result = native_main.main([])

        self.assertEqual(result, 7)
        adapter_type.assert_called_once_with()
        diagnostic_sink.assert_called_once_with()
        coordinator_type.assert_called_once_with(adapter, diagnostic_sink=None)
        coordinator.run.assert_called_once_with()


class _native_modules:
    def __init__(
        self,
        adapter_type: object,
        coordinator_type: object,
        diagnostic_sink: object | None = None,
    ) -> None:
        self.adapter_module = ModuleType(
            "organizer.macos_launcher.appkit_adapter"
        )
        self.adapter_module.AppKitAdapter = adapter_type  # type: ignore[attr-defined]
        self.coordinator_module = ModuleType(
            "organizer.macos_launcher.coordinator"
        )
        self.coordinator_module.NativeLauncherCoordinator = (  # type: ignore[attr-defined]
            coordinator_type
        )
        self.coordinator_module.development_diagnostic_sink = (  # type: ignore[attr-defined]
            diagnostic_sink or mock.Mock(return_value=None)
        )
        self.patch = mock.patch.dict(
            sys.modules,
            {
                self.adapter_module.__name__: self.adapter_module,
                self.coordinator_module.__name__: self.coordinator_module,
            },
        )

    def __enter__(self) -> None:
        self.patch.start()

    def __exit__(self, *args: object) -> None:
        self.patch.stop()


if __name__ == "__main__":
    unittest.main()
