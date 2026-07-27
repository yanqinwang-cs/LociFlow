from __future__ import annotations

from collections.abc import Sequence
import importlib
from pathlib import Path
import sys

_PACKAGING_SMOKE_ARGUMENT = "--packaging-smoke-test"
_PACKAGING_FAILURE_MESSAGE = "Loci Flow packaging check failed."
_STARTUP_FAILURE_MESSAGE = "Loci Flow could not start safely."


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == (_PACKAGING_SMOKE_ARGUMENT,):
        try:
            return _packaging_smoke_test()
        except Exception:
            print(_PACKAGING_FAILURE_MESSAGE, file=sys.stderr)
            return 1
    if not getattr(sys, "frozen", False):
        try:
            from organizer.macos_launcher.__main__ import main as native_main

            return native_main(arguments)
        except Exception:
            print(_PACKAGING_FAILURE_MESSAGE, file=sys.stderr)
            return 1

    diagnostic_sink = None
    try:
        from organizer.macos_launcher.application_paths import (
            MacOSApplicationPaths,
        )
        from organizer.macos_launcher.packaged_diagnostics import (
            PackagedDiagnosticInput,
            create_packaged_diagnostic_sink,
        )

        paths = MacOSApplicationPaths.for_home()
        diagnostic_sink = create_packaged_diagnostic_sink(paths)
        diagnostic_sink(
            PackagedDiagnosticInput(
                phase="startup",
                code="launcher_started",
            )
        )
        _verify_packaged_resources(include_runtime_identity=True)

        from organizer.macos_launcher.__main__ import main as native_main

        return native_main(
            arguments,
            diagnostic_sink=diagnostic_sink,
            application_paths=paths,
        )
    except Exception as error:
        if diagnostic_sink is not None:
            diagnostic_sink(
                PackagedDiagnosticInput(
                    phase="startup",
                    code=(
                        "packaged_resource_missing"
                        if isinstance(error, _PackagedResourceError)
                        else "unknown_launcher_failure"
                    ),
                    exception_class=type(error).__name__,
                )
            )
        _present_packaged_startup_failure()
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
        from organizer.web import server as web_server

        del NativeLauncherCoordinator, web_server
        _verify_packaged_resources(
            include_runtime_identity=getattr(sys, "frozen", False)
        )

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


class _PackagedResourceError(RuntimeError):
    pass


def _verify_packaged_resources(*, include_runtime_identity: bool) -> None:
    required_files = list(_required_packaged_files())
    if include_runtime_identity:
        from organizer.macos_launcher.packaged_diagnostics import (
            read_runtime_identity,
            runtime_identity_path,
        )

        identity_path = runtime_identity_path()
        required_files.append(identity_path)
        if read_runtime_identity(identity_path) is None:
            raise _PackagedResourceError
    try:
        if any(
            not path.is_file() or not path.read_bytes()
            for path in required_files
        ):
            raise _PackagedResourceError
    except OSError as error:
        raise _PackagedResourceError from error


def _required_packaged_files() -> tuple[Path, ...]:
    from organizer.web import app as web_app

    package_root = Path(web_app.__file__).resolve().parent
    return (
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


def _present_packaged_startup_failure() -> None:
    try:
        from organizer.macos_launcher.appkit_adapter import AppKitAdapter
        from organizer.macos_launcher.coordinator import NativeMessage

        adapter = AppKitAdapter()
        adapter.initialize()
        adapter.show_error(
            NativeMessage(
                title="Loci Flow",
                message=_STARTUP_FAILURE_MESSAGE,
            )
        )
    except Exception:
        print(_STARTUP_FAILURE_MESSAGE, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
