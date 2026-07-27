from __future__ import annotations

from collections.abc import Sequence
import importlib
from pathlib import Path
import sys

_PACKAGING_SMOKE_ARGUMENT = "--packaging-smoke-test"
_PACKAGING_FAILURE_MESSAGE = "Loci Flow packaging check failed."


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == (_PACKAGING_SMOKE_ARGUMENT,):
        try:
            return _packaging_smoke_test()
        except Exception:
            print(_PACKAGING_FAILURE_MESSAGE, file=sys.stderr)
            return 1
    try:
        from organizer.macos_launcher.__main__ import main as native_main

        return native_main(arguments)
    except Exception:
        print(_PACKAGING_FAILURE_MESSAGE, file=sys.stderr)
        return 1


def _packaging_smoke_test() -> int:
    try:
        for module_name in ("AppKit", "Foundation", "objc", "PyObjCTools"):
            importlib.import_module(module_name)

        from organizer.macos_launcher.application_paths import (
            MacOSApplicationPaths,
        )
        from organizer.macos_launcher.coordinator import (
            NativeLauncherCoordinator,
        )
        from organizer.web import app as web_app
        from organizer.web import server as web_server

        del NativeLauncherCoordinator, web_server
        package_root = Path(web_app.__file__).resolve().parent
        required_files = (
            package_root / "templates" / "loci_flow.html",
            package_root / "static" / "css" / "bootai.css",
            package_root / "static" / "js" / "bootai.js",
            package_root / "static" / "loci_flow" / "app.js",
            package_root
            / "static"
            / "loci_flow"
            / "assets"
            / "loci-flow-mark.svg",
            package_root / "static" / "vendor" / "bootstrap.min.css",
            package_root / "static" / "THIRD_PARTY_NOTICES.txt",
        )
        if any(
            not path.is_file() or not path.read_bytes()
            for path in required_files
        ):
            raise RuntimeError("required packaged resource is unavailable")

        paths = MacOSApplicationPaths.for_home()
        if not all(
            path.is_absolute()
            for path in (
                paths.application_support,
                paths.caches,
                paths.logs,
                paths.locks,
            )
        ):
            raise RuntimeError("application-owned paths are unavailable")
    except Exception:
        print(_PACKAGING_FAILURE_MESSAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
