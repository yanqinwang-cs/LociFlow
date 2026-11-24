from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Condition, Event, Lock
from typing import Any, Protocol

from organizer.application.runtime_lifecycle import (
    RuntimeLifecycle,
    RuntimeSnapshot,
)
from organizer.macos_launcher.process_lease import ProcessLeaseError
from organizer.web.config import WebAppConfig
from organizer.web.root_admission import RootAdmissionError, admit_root


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


LaunchDiagnostic = Callable[
    [LaunchPhase, LauncherError, BaseException | None],
    None,
]


class BoundSocket(Protocol):
    def getsockname(self) -> tuple[str, int]: ...

    def close(self) -> None: ...


class ServerRunner(Protocol):
    def run(self, *, sockets: list[BoundSocket]) -> None: ...

    def request_exit(self) -> None: ...


class RootProcessLease(Protocol):
    def release(self) -> bool: ...


class LaunchStopController:
    def __init__(self) -> None:
        self._lock = Lock()
        self._request_exit: Callable[[], None] | None = None
        self._requested = False
        self._dispatched = False

    def attach(self, request_exit: Callable[[], None]) -> None:
        should_dispatch = False
        with self._lock:
            if self._request_exit is not None:
                raise RuntimeError("launch stop controller is already attached")
            self._request_exit = request_exit
            if self._requested and not self._dispatched:
                self._dispatched = True
                should_dispatch = True
        if should_dispatch:
            request_exit()

    def request_stop(self) -> bool:
        request_exit: Callable[[], None] | None = None
        with self._lock:
            if self._requested:
                return False
            self._requested = True
            if self._request_exit is not None and not self._dispatched:
                self._dispatched = True
                request_exit = self._request_exit
        if request_exit is not None:
            request_exit()
        return True


class LaunchRuntimeHandle:
    """Publishes one app lifecycle and coordinates its monotonic shutdown."""

    def __init__(self, stop_controller: LaunchStopController) -> None:
        self._condition = Condition()
        self._stop_controller = stop_controller
        self._lifecycle: RuntimeLifecycle | None = None
        self._publication_finished = False
        self._shutdown_requested = False
        self._shutdown_started = False

    def publish_lifecycle(self, lifecycle: RuntimeLifecycle) -> RuntimeSnapshot:
        if not isinstance(lifecycle, RuntimeLifecycle):
            raise TypeError("runtime lifecycle is required")
        with self._condition:
            if self._publication_finished and self._lifecycle is None:
                raise RuntimeError("runtime lifecycle publication already failed")
            if self._lifecycle is not None:
                if self._lifecycle is lifecycle:
                    return lifecycle.snapshot()
                raise RuntimeError("a different runtime lifecycle is already published")
            self._lifecycle = lifecycle
            self._publication_finished = True
            if self._shutdown_requested:
                self._begin_shutdown_locked()
            snapshot = lifecycle.snapshot()
            self._condition.notify_all()
            return snapshot

    def request_shutdown(self) -> RuntimeSnapshot | None:
        with self._condition:
            self._shutdown_requested = True
            if self._lifecycle is None:
                return None
            self._begin_shutdown_locked()
            return self._lifecycle.snapshot()

    def wait_for_lifecycle(self) -> RuntimeLifecycle:
        with self._condition:
            while self._lifecycle is None and not self._publication_finished:
                self._condition.wait()
            if self._lifecycle is None:
                raise RuntimeError(
                    "the application runtime lifecycle was not published"
                )
            return self._lifecycle

    def finish_lifecycle_publication(self) -> None:
        """Wake publication waiters when application construction is terminal."""

        with self._condition:
            self._publication_finished = True
            self._condition.notify_all()

    def lifecycle_snapshot(self) -> RuntimeSnapshot | None:
        with self._condition:
            lifecycle = self._lifecycle
        return None if lifecycle is None else lifecycle.snapshot()

    def request_server_stop(self) -> bool:
        return self._stop_controller.request_stop()

    def _begin_shutdown_locked(self) -> None:
        if self._shutdown_started:
            return
        assert self._lifecycle is not None
        self._lifecycle.begin_shutdown()
        self._shutdown_started = True


class ReadinessObserver(Protocol):
    def start(self) -> None: ...

    def join(self) -> None: ...


@dataclass(frozen=True)
class LauncherDependencies:
    config_factory: Callable[[Path], WebAppConfig]
    socket_binder: Callable[[int | None], BoundSocket]
    app_factory: Callable[[WebAppConfig], Any]
    server_factory: Callable[[Any, int], ServerRunner]
    readiness_factory: Callable[
        [str, str, Event, Callable[[], None]],
        ReadinessObserver,
    ]
    manual_browser_bootstrap: Callable[[str], None]
    runtime_lifecycle_accessor: Callable[[Any], RuntimeLifecycle] | None = None
    root_lease_factory: Callable[[Path], RootProcessLease] | None = None


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
    stop_controller: LaunchStopController | None = None,
    runtime_handle: LaunchRuntimeHandle | None = None,
    root_lease: RootProcessLease | None = None,
) -> LaunchResult:
    recorder = lifecycle or LifecycleRecorder()
    recorder.record(LaunchPhase.SELECTING_ROOT)
    try:
        raw_root = root_provider()
    except Exception:
        if runtime_handle is not None:
            runtime_handle.finish_lifecycle_publication()
        recorder.record(LaunchPhase.FAILED)
        return _result(
            recorder,
            "selection_failed",
            "The folder selection could not be completed.",
        )

    if raw_root is None:
        if runtime_handle is not None:
            runtime_handle.finish_lifecycle_publication()
        recorder.record(LaunchPhase.STOPPED)
        return LaunchResult(exit_code=0, phases=recorder.snapshot())

    try:
        admitted_root = admit_root(raw_root).path
    except RootAdmissionError as error:
        if runtime_handle is not None:
            runtime_handle.finish_lifecycle_publication()
        recorder.record(LaunchPhase.FAILED)
        return _result(recorder, error.code, error.safe_message)
    except (OSError, TypeError, ValueError):
        if runtime_handle is not None:
            runtime_handle.finish_lifecycle_publication()
        recorder.record(LaunchPhase.FAILED)
        return _result(
            recorder,
            "root_not_allowed",
            "The selected folder cannot be used.",
        )

    recorder.record(LaunchPhase.ROOT_ADMITTED)
    owns_lease = root_lease is None
    lease = root_lease
    if lease is None:
        lease, lease_error = _acquire_root_lease(
            admitted_root,
            dependencies,
            recorder,
            runtime_handle,
        )
        if lease_error is not None:
            return lease_error

    result: LaunchResult
    try:
        try:
            config = dependencies.config_factory(admitted_root)
        except RootAdmissionError as error:
            if runtime_handle is not None:
                runtime_handle.finish_lifecycle_publication()
            recorder.record(LaunchPhase.FAILED)
            result = _result(recorder, error.code, error.safe_message)
        except (OSError, TypeError, ValueError):
            if runtime_handle is not None:
                runtime_handle.finish_lifecycle_publication()
            recorder.record(LaunchPhase.FAILED)
            result = _result(
                recorder,
                "root_not_allowed",
                "The selected folder cannot be used.",
            )
        else:
            result = _launch_configured(
                config,
                dependencies=dependencies,
                port=port,
                open_browser=open_browser,
                lifecycle=recorder,
                stop_controller=stop_controller,
                runtime_handle=runtime_handle,
            )
    finally:
        release_error = (
            _release_root_lease(lease) if owns_lease else None
        )
    return _apply_release_error(result, release_error)


def launch_configured(
    config: WebAppConfig,
    *,
    dependencies: LauncherDependencies,
    port: int | None = None,
    open_browser: bool = True,
    lifecycle: LifecycleRecorder | None = None,
    propagate_unexpected: bool = False,
    diagnostic: LaunchDiagnostic | None = None,
    stop_controller: LaunchStopController | None = None,
    runtime_handle: LaunchRuntimeHandle | None = None,
) -> LaunchResult:
    recorder = lifecycle or LifecycleRecorder()
    lease, lease_error = _acquire_root_lease(
        config.root,
        dependencies,
        recorder,
        runtime_handle,
    )
    if lease_error is not None:
        return lease_error
    result: LaunchResult
    try:
        result = _launch_configured(
            config,
            dependencies=dependencies,
            port=port,
            open_browser=open_browser,
            lifecycle=recorder,
            propagate_unexpected=propagate_unexpected,
            diagnostic=diagnostic,
            stop_controller=stop_controller,
            runtime_handle=runtime_handle,
        )
    finally:
        release_error = _release_root_lease(lease)
    return _apply_release_error(result, release_error)


def _launch_configured(
    config: WebAppConfig,
    *,
    dependencies: LauncherDependencies,
    port: int | None = None,
    open_browser: bool = True,
    lifecycle: LifecycleRecorder | None = None,
    propagate_unexpected: bool = False,
    diagnostic: LaunchDiagnostic | None = None,
    stop_controller: LaunchStopController | None = None,
    runtime_handle: LaunchRuntimeHandle | None = None,
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
        except (OSError, ValueError) as raised:
            error = LauncherError(
                code="bind_failed",
                safe_message="The local server could not reserve a private port.",
            )
            _diagnose(diagnostic, LaunchPhase.SOCKET_BOUND, error, raised)
            return _finalize_without_socket(recorder, error)

        recorder.record(LaunchPhase.SOCKET_BOUND)
        actual_port = int(bound_socket.getsockname()[1])
        base_url = f"http://127.0.0.1:{actual_port}"
        health_url = f"{base_url}/healthz"
        launch_url = f"{base_url}/launch/{config.launch_token}"

        try:
            app = dependencies.app_factory(config)
            if runtime_handle is not None:
                accessor = dependencies.runtime_lifecycle_accessor
                if accessor is None:
                    raise RuntimeError(
                        "native launch requires a runtime lifecycle accessor"
                    )
                runtime_handle.publish_lifecycle(accessor(app))
            server = dependencies.server_factory(app, actual_port)
            if stop_controller is not None:
                stop_controller.attach(server.request_exit)
        except Exception as raised:
            error = LauncherError(
                code="app_creation_failed",
                safe_message="The local application could not be started.",
            )
            _diagnose(diagnostic, LaunchPhase.APP_CREATED, error, raised)
        else:
            recorder.record(LaunchPhase.APP_CREATED)
            try:
                if open_browser:
                    recorder.record(LaunchPhase.WAITING_FOR_READY)
                    readiness_observer = dependencies.readiness_factory(
                        health_url,
                        launch_url,
                        stop_event,
                        (
                            server.request_exit
                            if stop_controller is None
                            else lambda: stop_controller.request_stop()
                        ),
                    )
                    readiness_observer.start()
                else:
                    dependencies.manual_browser_bootstrap(launch_url)
                recorder.record(LaunchPhase.BROWSER_BOOTSTRAPPED)
            except Exception as raised:
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
                _diagnose(
                    diagnostic,
                    LaunchPhase.WAITING_FOR_READY,
                    error,
                    raised,
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
                    _diagnose(diagnostic, LaunchPhase.RUNNING, error, raised)
    finally:
        if runtime_handle is not None:
            runtime_handle.finish_lifecycle_publication()
        if bound_socket is not None:
            recorder.record(LaunchPhase.STOPPING)
            stop_event.set()
            if readiness_observer is not None:
                try:
                    readiness_observer.join()
                except Exception as raised:
                    if error is None:
                        error = LauncherError(
                            code="readiness_failed",
                            safe_message="The local server readiness check did not stop cleanly.",
                        )
                        _diagnose(
                            diagnostic,
                            LaunchPhase.STOPPING,
                            error,
                            raised,
                        )
                readiness_error = getattr(readiness_observer, "error", None)
                if error is None and isinstance(readiness_error, LauncherError):
                    error = readiness_error
                    _diagnose(
                        diagnostic,
                        LaunchPhase.WAITING_FOR_READY,
                        error,
                        None,
                    )
            try:
                bound_socket.close()
            except OSError as raised:
                if error is None:
                    error = LauncherError(
                        code="cleanup_failed",
                        safe_message="The local server did not stop cleanly.",
                    )
                    _diagnose(
                        diagnostic,
                        LaunchPhase.STOPPING,
                        error,
                        raised,
                    )

    recorder.record(LaunchPhase.FAILED if error is not None else LaunchPhase.STOPPED)
    if unexpected is not None and propagate_unexpected:
        raise unexpected
    return LaunchResult(
        exit_code=1 if error is not None else 0,
        phases=recorder.snapshot(),
        error=error,
    )


def _acquire_root_lease(
    root: Path,
    dependencies: LauncherDependencies,
    recorder: LifecycleRecorder,
    runtime_handle: LaunchRuntimeHandle | None,
) -> tuple[RootProcessLease | None, LaunchResult | None]:
    factory = dependencies.root_lease_factory
    if factory is None:
        return None, None
    try:
        return factory(root), None
    except ProcessLeaseError as error:
        if runtime_handle is not None:
            runtime_handle.finish_lifecycle_publication()
        recorder.record(LaunchPhase.FAILED)
        if error.code == "already_held":
            return None, _result(
                recorder,
                "root_already_open",
                "This folder is already open in Loci Flow.",
            )
        return None, _result(
            recorder,
            "lease_unavailable",
            "Loci Flow could not start safely.",
        )


def _release_root_lease(
    lease: RootProcessLease | None,
) -> ProcessLeaseError | None:
    if lease is None:
        return None
    try:
        lease.release()
    except ProcessLeaseError as error:
        return error
    return None


def _apply_release_error(
    result: LaunchResult,
    release_error: ProcessLeaseError | None,
) -> LaunchResult:
    if release_error is None or result.error is not None:
        return result
    phases = result.phases
    if phases and phases[-1] is LaunchPhase.STOPPED:
        phases = (*phases[:-1], LaunchPhase.FAILED)
    return LaunchResult(
        exit_code=1,
        phases=phases,
        error=LauncherError(
            code="cleanup_failed",
            safe_message="The local server did not stop cleanly.",
        ),
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


def _diagnose(
    diagnostic: LaunchDiagnostic | None,
    phase: LaunchPhase,
    error: LauncherError,
    exception: BaseException | None,
) -> None:
    if diagnostic is not None:
        diagnostic(phase, error, exception)
