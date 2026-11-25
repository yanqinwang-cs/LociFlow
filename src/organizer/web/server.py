from __future__ import annotations

import argparse
import errno
import socket
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from organizer.macos_launcher.application_paths import MacOSApplicationPaths
from organizer.macos_launcher.process_lease import (
    ProcessLease,
    ProcessLeaseError,
    acquire_root_process_lease,
)
from organizer.web.app import app_runtime_lifecycle, create_app
from organizer.web.config import WebAppConfig
from organizer.web.launcher import (
    LauncherDependencies,
    LauncherError,
    LaunchPhase,
    LaunchResult,
    LaunchRuntimeHandle,
    LaunchStopController,
    launch,
    launch_configured,
)
from organizer.web.root_admission import RootAdmissionError, admit_root
from organizer.web.signal_coordinator import (
    LifecycleShutdownCoordinator,
    ProcessSignalCoordinator,
    ShutdownSequenceState,
)

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_READY_TIMEOUT_SECONDS = 5.0
_SOCKET_BACKLOG = 2048
_MAX_DYNAMIC_BIND_ATTEMPTS = 3
_SAFE_SIGNAL_FAILURE_MESSAGE = "The local server could not close safely."
_DEFAULT_ROOT_LEASE_FACTORY = object()


class WebServerError(ValueError):
    pass


class _UvicornServerRunner:
    def __init__(self, server: uvicorn.Server) -> None:
        self._server = server

    def run(self, *, sockets: list[socket.socket]) -> None:
        self._server.run(sockets=sockets)

    def request_exit(self) -> None:
        self._server.should_exit = True


class _CoordinatedUvicornServer(uvicorn.Server):
    """Delegate SIGINT/SIGTERM ownership to ProcessSignalCoordinator."""

    @contextmanager
    def capture_signals(self):
        yield


class _FailClosedReadinessObserver:
    def __init__(
        self,
        health_url: str,
        launch_url: str,
        stop_event: Event,
        request_exit: Callable[[], None],
        browser_opener: Callable[[str], bool],
    ) -> None:
        self.error: LauncherError | None = None
        self._request_exit = request_exit
        self._thread = Thread(
            target=self._observe,
            args=(health_url, launch_url, stop_event, browser_opener),
            name="bootai-native-browser-launch",
        )

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join()

    def _observe(
        self,
        health_url: str,
        launch_url: str,
        stop_event: Event,
        browser_opener: Callable[[str], bool],
    ) -> None:
        self.error = _open_browser_when_ready(
            health_url,
            launch_url,
            stop_event,
            browser_opener=browser_opener,
            fail_closed=True,
        )
        if self.error is not None:
            self._request_exit()


def main(
    argv: Sequence[str] | None = None,
    *,
    application_paths: MacOSApplicationPaths | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    default_root = args.root is None
    root = args.root if args.root is not None else Path.home() / "Downloads"
    stop_controller = LaunchStopController()
    runtime_handle = LaunchRuntimeHandle(stop_controller)
    shutdown = LifecycleShutdownCoordinator(runtime_handle)
    launch_completed = Event()
    signals = ProcessSignalCoordinator(
        lambda _intent: shutdown.request_shutdown().started,
        terminal_completed=launch_completed.is_set,
    )
    try:
        signals.install()
    except Exception:
        print(_SAFE_SIGNAL_FAILURE_MESSAGE, file=sys.stderr)
        return 1
    root_lease: ProcessLease | None = None
    lease_release_failed = False
    try:
        try:
            admitted_root = admit_root(root).path
        except RootAdmissionError as error:
            runtime_handle.finish_lifecycle_publication()
            result = LaunchResult(
                exit_code=1,
                phases=(LaunchPhase.SELECTING_ROOT, LaunchPhase.FAILED),
                error=LauncherError(error.code, error.safe_message),
            )
        else:
            paths = application_paths or MacOSApplicationPaths.for_home()
            try:
                root_lease = acquire_root_process_lease(paths, admitted_root)
            except ProcessLeaseError as error:
                runtime_handle.finish_lifecycle_publication()
                code = (
                    "root_already_open"
                    if error.code == "already_held"
                    else "lease_unavailable"
                )
                message = (
                    "This folder is already open in Loci Flow."
                    if error.code == "already_held"
                    else "Loci Flow could not start safely."
                )
                result = LaunchResult(
                    exit_code=1,
                    phases=(
                        LaunchPhase.SELECTING_ROOT,
                        LaunchPhase.ROOT_ADMITTED,
                        LaunchPhase.FAILED,
                    ),
                    error=LauncherError(code, message),
                )
            else:
                result = launch(
                    lambda: admitted_root,
                    dependencies=build_launcher_dependencies(
                        coordinated_signals=True,
                        root_lease_factory=None,
                    ),
                    port=args.port,
                    open_browser=not args.no_browser,
                    stop_controller=stop_controller,
                    runtime_handle=runtime_handle,
                    root_lease=root_lease,
                )
    finally:
        launch_completed.set()
        try:
            shutdown.join()
        finally:
            try:
                signal_result = signals.close()
            finally:
                if root_lease is not None:
                    try:
                        root_lease.release()
                    except ProcessLeaseError:
                        lease_release_failed = True
    if result.error is not None:
        message = result.error.safe_message
        if default_root and result.error.code == "root_missing":
            message = "The Downloads folder does not exist."
        parser.error(message)
    if (
        shutdown.state is ShutdownSequenceState.FAILED
        or signal_result.error is not None
        or lease_release_failed
    ):
        print(_SAFE_SIGNAL_FAILURE_MESSAGE, file=sys.stderr)
        return 1
    if signal_result.intent is not None and signal_result.owns_shutdown:
        return signal_result.intent.exit_code
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch bootAI's root-locked local web interface.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="folder locked to this web-server process (default: ~/Downloads)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print a private launch URL instead of opening a browser",
    )
    parser.add_argument(
        "--port",
        type=_validated_port,
        default=None,
        help="fixed loopback port for development (default: dynamic)",
    )
    return parser


def run_server(
    config: WebAppConfig,
    *,
    port: int | None = None,
    open_browser: bool = True,
    application_paths: MacOSApplicationPaths | None = None,
) -> int:
    result = launch_configured(
        config,
        dependencies=_launcher_dependencies(application_paths=application_paths),
        port=port,
        open_browser=open_browser,
        propagate_unexpected=True,
    )
    if result.error is not None:
        raise WebServerError(result.error.safe_message)
    return result.exit_code


def bind_loopback_socket(port: int | None = None) -> socket.socket:
    requested_port = 0 if port is None else _validated_port(str(port))
    attempts = _MAX_DYNAMIC_BIND_ATTEMPTS if port is None else 1
    last_error: OSError | None = None
    for attempt in range(attempts):
        bound_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            bound_socket.bind((LOOPBACK_HOST, requested_port))
            bound_socket.listen(_SOCKET_BACKLOG)
            bound_socket.set_inheritable(True)
            return bound_socket
        except OSError as error:
            bound_socket.close()
            last_error = error
            if (
                port is not None
                or error.errno != errno.EADDRINUSE
                or attempt == attempts - 1
            ):
                break

    label = "a dynamic port" if port is None else f"port {port}"
    raise WebServerError(
        f"could not bind {label} on {LOOPBACK_HOST}: {last_error}"
    ) from last_error


def build_launcher_dependencies(
    *,
    browser_opener: Callable[[str], bool] | None = None,
    fail_closed_readiness: bool = False,
    coordinated_signals: bool = False,
    application_paths: MacOSApplicationPaths | None = None,
    root_lease_factory: Callable[[Path], ProcessLease] | None | object = (
        _DEFAULT_ROOT_LEASE_FACTORY
    ),
) -> LauncherDependencies:
    if root_lease_factory is _DEFAULT_ROOT_LEASE_FACTORY:
        paths = application_paths or MacOSApplicationPaths.for_home()

        def effective_root_lease_factory(root):
            return acquire_root_process_lease(paths, root)

    else:
        effective_root_lease_factory = root_lease_factory
    if fail_closed_readiness:
        if browser_opener is None:
            raise ValueError("fail-closed readiness requires a browser opener")

        def readiness_factory(health, launch, stop, request_exit):
            return _FailClosedReadinessObserver(
                health,
                launch,
                stop,
                request_exit,
                browser_opener,
            )

    else:
        readiness_factory = _create_readiness_thread
    return LauncherDependencies(
        config_factory=lambda root: WebAppConfig(root=root),
        socket_binder=bind_loopback_socket,
        app_factory=create_app,
        server_factory=lambda app, port: _create_uvicorn_server(
            app,
            port,
            coordinated_signals=coordinated_signals,
        ),
        readiness_factory=readiness_factory,
        manual_browser_bootstrap=_print_manual_launch_url,
        runtime_lifecycle_accessor=app_runtime_lifecycle,
        root_lease_factory=effective_root_lease_factory,  # type: ignore[arg-type]
    )


def _launcher_dependencies(
    *,
    application_paths: MacOSApplicationPaths | None = None,
) -> LauncherDependencies:
    return build_launcher_dependencies(application_paths=application_paths)


def _create_uvicorn_server(
    app: Any,
    port: int,
    *,
    coordinated_signals: bool = False,
) -> _UvicornServerRunner:
    uvicorn_config = uvicorn.Config(
        app=app,
        host=LOOPBACK_HOST,
        port=port,
        workers=1,
        reload=False,
        access_log=False,
        proxy_headers=False,
    )
    server_type = _CoordinatedUvicornServer if coordinated_signals else uvicorn.Server
    return _UvicornServerRunner(server_type(uvicorn_config))


def _create_readiness_thread(
    health_url: str,
    launch_url: str,
    stop_event: Event,
    _request_exit: Callable[[], None],
) -> Thread:
    return Thread(
        target=_open_browser_when_ready,
        args=(health_url, launch_url, stop_event),
        name="bootai-browser-launch",
    )


def _validated_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _open_browser_when_ready(
    health_url: str,
    launch_url: str,
    stop_event: Event,
    *,
    timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
    health_opener: Callable[..., Any] = urlopen,
    browser_opener: Callable[[str], bool] = webbrowser.open,
    fail_closed: bool = False,
) -> LauncherError | None:
    deadline = time.monotonic() + timeout_seconds
    while not stop_event.is_set() and time.monotonic() < deadline:
        try:
            with closing(health_opener(health_url, timeout=0.25)) as response:
                ready = response.status == 200
        except (OSError, URLError):
            ready = False
        if ready:
            try:
                opened = bool(browser_opener(launch_url))
            except Exception:
                opened = False
            if opened:
                return None
            if fail_closed:
                return LauncherError(
                    code="browser_open_failed",
                    safe_message="The browser could not be opened.",
                )
            _print_manual_launch_url(launch_url)
            return None
        stop_event.wait(0.05)

    if not stop_event.is_set():
        if fail_closed:
            return LauncherError(
                code="readiness_failed",
                safe_message="The local server did not become ready.",
            )
        print(
            "bootAI could not confirm that the local web server was ready.",
            file=sys.stderr,
        )
    return None


def _print_manual_launch_url(launch_url: str) -> None:
    print("Open this temporary private URL in your browser:")
    print(launch_url)
    print("The URL works once and should not be shared.")
