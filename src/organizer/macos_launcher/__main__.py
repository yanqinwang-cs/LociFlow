from __future__ import annotations

from collections.abc import Callable, Sequence
import sys
from typing import Any

from organizer.macos_launcher.application_paths import MacOSApplicationPaths

_NATIVE_DEPENDENCY_MODULES = {
    "AppKit",
    "Foundation",
    "objc",
    "PyObjCTools",
}


def main(
    argv: Sequence[str] | None = None,
    *,
    diagnostic_sink: Callable[[Any], None] | None = None,
    application_paths: MacOSApplicationPaths | None = None,
) -> int:
    del argv
    if sys.platform != "darwin":
        print(
            "The Loci Flow native launcher is available only on macOS.",
            file=sys.stderr,
        )
        return 2

    try:
        from organizer.macos_launcher.coordinator import (
            NativeDiagnostic,
            NativeLauncherCoordinator,
            development_diagnostic_sink,
        )
        from organizer.macos_launcher.appkit_adapter import AppKitAdapter

        adapter = AppKitAdapter()
    except ModuleNotFoundError as error:
        missing = (error.name or "").split(".", maxsplit=1)[0]
        if missing not in _NATIVE_DEPENDENCY_MODULES:
            raise
        if diagnostic_sink is not None:
            diagnostic_sink(
                NativeDiagnostic(
                    phase="startup",
                    code="native_dependency_unavailable",
                    exception_class=type(error).__name__,
                    traceback=None,
                )
            )
        print(
            "The optional Loci Flow macOS launcher support is not installed.",
            file=sys.stderr,
        )
        return 2

    selected_sink = diagnostic_sink
    if selected_sink is None:
        selected_sink = development_diagnostic_sink()
    return NativeLauncherCoordinator(
        adapter,
        diagnostic_sink=selected_sink,
        application_paths=(
            application_paths or MacOSApplicationPaths.for_home()
        ),
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
