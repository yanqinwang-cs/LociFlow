from __future__ import annotations

from collections.abc import Sequence
import sys

_NATIVE_DEPENDENCY_MODULES = {
    "AppKit",
    "Foundation",
    "objc",
    "PyObjCTools",
}


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    if sys.platform != "darwin":
        print(
            "The Loci Flow native launcher is available only on macOS.",
            file=sys.stderr,
        )
        return 2

    try:
        from organizer.macos_launcher.appkit_adapter import AppKitAdapter
        from organizer.macos_launcher.application_paths import (
            MacOSApplicationPaths,
        )
        from organizer.macos_launcher.coordinator import (
            NativeLauncherCoordinator,
            development_diagnostic_sink,
        )

        adapter = AppKitAdapter()
    except ModuleNotFoundError as error:
        missing = (error.name or "").split(".", maxsplit=1)[0]
        if missing not in _NATIVE_DEPENDENCY_MODULES:
            raise
        print(
            "The optional Loci Flow macOS launcher support is not installed.",
            file=sys.stderr,
        )
        return 2

    return NativeLauncherCoordinator(
        adapter,
        diagnostic_sink=development_diagnostic_sink(),
        application_paths=MacOSApplicationPaths.for_home(),
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
