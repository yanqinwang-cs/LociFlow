from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock
from typing import Any, Protocol

from organizer.web.config import WebAppConfig
from organizer.web.root_admission import RootAdmissionError


class LaunchPhase(StrEnum):
    SELECTING_ROOT = "selecting_root"
    ROOT_ADMITTED = "root_admitted"
    SOCKET_BOUND = "socket_bound"
    APP_CREATED = "app_created"
    WAITING_FOR_READY = "waiting_for_ready"
    BROWSER_BOOTSTRAPPED = "browser_bootstrapped"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class LauncherError:
    code: str
    safe_message: str


@dataclass(frozen=True)
class LaunchResult:
    exit_code: int
    phases: tuple[LaunchPhase, ...]
    error: LauncherError | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and self.error is None


class BoundSocket(Protocol):
    def getsockname(self) -> tuple[str, int]: ...

    def close(self) -> None: ...


class ServerRunner(Protocol):
    def run(self, *, sockets: list[BoundSocket]) -> None: ...


class ReadinessObserver(Protocol):
    def start(self) -> None: ...

    def join(self) -> None: ...


@dataclass(frozen=True)
class LauncherDependencies:
    config_factory: Callable[[Path], WebAppConfig]
    socket_binder: Callable[[int | None], BoundSocket]
    app_factory: Callable[[WebAppConfig], Any]
    server_factory: Callable[[Any, int], ServerRunner]
    readiness_factory: Callable[[str, str, Event], ReadinessObserver]
    manual_browser_bootstrap: Callable[[str], None]


@dataclass
class LifecycleRecorder:
    _phases: list[LaunchPhase] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock)

    def record(self, phase: LaunchPhase) -> None:
        with self._lock:
            if self._phases and self._phases[-1] in {
                LaunchPhase.STOPPED,
                LaunchPhase.FAILED,
            }:
                return
            self._phases.append(phase)

    def snapshot(self) -> tuple[LaunchPhase, ...]:
        with self._lock:
            return tuple(self._phases)


def launch(
    root_provider: Callable[[], str | Path | None],
    *,
    dependencies: LauncherDependencies,
    port: int | None = None,
    open_browser: bool = True,
    lifecycle: LifecycleRecorder | None = None,
) -> LaunchResult:
    recorder = lifecycle or LifecycleRecorder()
    recorder.record(LaunchPhase.SELECTING_ROOT)
    try:
        raw_root = root_provider()
    except Exception:
        recorder.record(LaunchPhase.FAILED)
        return _result(
            recorder,
            "selection_failed",
            "The folder selection could not be completed.",
        )

    if raw_root is None:
        recorder.record(LaunchPhase.STOPPED)
        return LaunchResult(exit_code=0, phases=recorder.snapshot())

    try:
        config = dependencies.config_factory(Path(raw_root))
    except RootAdmissionError as error:
        recorder.record(LaunchPhase.FAILED)
        return _result(recorder, error.code, error.safe_message)
    except (OSError, TypeError, ValueError):
        recorder.record(LaunchPhase.FAILED)
        return _result(
            recorder,
            "root_not_allowed",
            "The selected folder cannot be used.",
        )

    recorder.record(LaunchPhase.ROOT_ADMITTED)
    return launch_configured(
        config,
        dependencies=dependencies,
        port=port,
        open_browser=open_browser,
        lifecycle=recorder,
    )


def launch_configured(
    config: WebAppConfig,
    *,
    dependencies: LauncherDependencies,
    port: int | None = None,
    open_browser: bool = True,
    lifecycle: LifecycleRecorder | None = None,
    propagate_unexpected: bool = False,
) -> LaunchResult:
    recorder = lifecycle or LifecycleRecorder()
    bound_socket: BoundSocket | None = None
    readiness_observer: ReadinessObserver | None = None
    stop_event = Event()
    error: LauncherError | None = None
    unexpected: Exception | None = None

    try:
        try:
            bound_socket = dependencies.socket_binder(port)
        except (OSError, ValueError):
            error = LauncherError(
                code="bind_failed",
                safe_message="The local server could not reserve a private port.",
            )
            return _finalize_without_socket(recorder, error)

        recorder.record(LaunchPhase.SOCKET_BOUND)
        actual_port = int(bound_socket.getsockname()[1])
        base_url = f"http://127.0.0.1:{actual_port}"
        health_url = f"{base_url}/healthz"
        launch_url = f"{base_url}/launch/{config.launch_token}"

        try:
            app = dependencies.app_factory(config)
            server = dependencies.server_factory(app, actual_port)
        except Exception:
            error = LauncherError(
                code="app_creation_failed",
                safe_message="The local application could not be started.",
            )
        else:
            recorder.record(LaunchPhase.APP_CREATED)
            try:
                if open_browser:
                    recorder.record(LaunchPhase.WAITING_FOR_READY)
                    readiness_observer = dependencies.readiness_factory(
                        health_url,
                        launch_url,
                        stop_event,
                    )
                    readiness_observer.start()
                else:
                    dependencies.manual_browser_bootstrap(launch_url)
                recorder.record(LaunchPhase.BROWSER_BOOTSTRAPPED)
            except Exception:
                error = LauncherError(
                    code=(
                        "readiness_failed" if open_browser else "browser_open_failed"
                    ),
                    safe_message=(
                        "The local server readiness check could not start."
                        if open_browser
                        else "The private browser link could not be prepared."
                    ),
                )

            if error is None:
                recorder.record(LaunchPhase.RUNNING)
                try:
                    server.run(sockets=[bound_socket])
                except KeyboardInterrupt:
                    pass
                except Exception as raised:
                    error = LauncherError(
                        code="server_failed",
                        safe_message="The local server stopped unexpectedly.",
                    )
                    unexpected = raised
    finally:
        if bound_socket is not None:
            recorder.record(LaunchPhase.STOPPING)
            stop_event.set()
            if readiness_observer is not None:
                try:
                    readiness_observer.join()
                except Exception:
                    if error is None:
                        error = LauncherError(
                            code="readiness_failed",
                            safe_message="The local server readiness check did not stop cleanly.",
                        )
            try:
                bound_socket.close()
            except OSError:
                if error is None:
                    error = LauncherError(
                        code="cleanup_failed",
                        safe_message="The local server did not stop cleanly.",
                    )

    recorder.record(LaunchPhase.FAILED if error is not None else LaunchPhase.STOPPED)
    if unexpected is not None and propagate_unexpected:
        raise unexpected
    return LaunchResult(
        exit_code=1 if error is not None else 0,
        phases=recorder.snapshot(),
        error=error,
    )


def _finalize_without_socket(
    recorder: LifecycleRecorder,
    error: LauncherError,
) -> LaunchResult:
    recorder.record(LaunchPhase.FAILED)
    return LaunchResult(exit_code=1, phases=recorder.snapshot(), error=error)


def _result(
    recorder: LifecycleRecorder,
    code: str,
    safe_message: str,
) -> LaunchResult:
    return LaunchResult(
        exit_code=1,
        phases=recorder.snapshot(),
        error=LauncherError(code=code, safe_message=safe_message),
    )
