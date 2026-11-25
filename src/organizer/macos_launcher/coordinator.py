from __future__ import annotations

import os
import re
import sys
import traceback
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Protocol, TextIO

from organizer.macos_launcher.application_paths import MacOSApplicationPaths
from organizer.macos_launcher.process_lease import (
    ProcessLease,
    ProcessLeaseError,
    acquire_application_instance_lease,
    acquire_root_process_lease,
)
from organizer.macos_launcher.shutdown_coordinator import (
    NativeShutdownCoordinator,
)
from organizer.web.config import WebAppConfig
from organizer.web.launcher import (
    LauncherDependencies,
    LauncherError,
    LaunchPhase,
    LaunchResult,
    LaunchRuntimeHandle,
    LaunchStopController,
    launch_configured,
)
from organizer.web.root_admission import RootAdmissionError, admit_root
from organizer.web.server import build_launcher_dependencies
from organizer.web.signal_coordinator import (
    ProcessSignalCoordinator,
    SignalIntent,
)


@dataclass(frozen=True)
class NativeMessage:
    title: str
    message: str


@dataclass(frozen=True)
class NativeDiagnostic:
    phase: str
    code: str
    exception_class: str | None
    traceback: str | None


class NativeAdapterError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _PreRootPhase(StrEnum):
    INITIALIZING = "initializing"
    CHOOSING = "choosing"
    SIGNAL_WON = "signal_won"
    QUIT_WON = "quit_won"
    CANCEL_WON = "cancel_won"
    SELECTION_WON = "selection_won"
    RUNNING = "running"


class NativeAdapter(Protocol):
    def initialize(self) -> None: ...

    def choose_directory(self) -> str | None: ...

    def cancel_active_directory_selection(self) -> bool: ...

    def open_browser(self, launch_url: str) -> bool: ...

    def show_error(self, message: NativeMessage) -> None: ...

    def dispatch_to_main(self, callback: Callable[[], None]) -> None: ...

    def run_event_loop(self) -> None: ...

    def stop_event_loop(self) -> None: ...

    def set_launch_running(self, running: bool) -> None: ...

    def set_termination_handler(self, handler: Callable[[], bool]) -> None: ...

    def set_diagnostic_handler(self, handler: Callable[[str], None]) -> None: ...

    def show_finishing_status(self) -> None: ...

    def close_finishing_status(self) -> None: ...

    def show_shutdown_failure(self) -> None: ...

    def reply_to_termination(self, allow: bool) -> None: ...


ThreadFactory = Callable[[Callable[[], None], str], Thread]
DependenciesFactory = Callable[[Callable[[str], bool]], LauncherDependencies]
DiagnosticSink = Callable[[NativeDiagnostic], None]
ApplicationLeaseFactory = Callable[[MacOSApplicationPaths], ProcessLease]
RootLeaseFactory = Callable[[MacOSApplicationPaths, Path], ProcessLease]

_PRIVATE_LAUNCH_URL = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost):\d+/launch/[^\s]+"
)


class NativeLauncherCoordinator:
    def __init__(
        self,
        adapter: NativeAdapter,
        *,
        config_factory: Callable[[Path], WebAppConfig] = WebAppConfig,
        dependencies_factory: DependenciesFactory | None = None,
        thread_factory: ThreadFactory | None = None,
        diagnostic_sink: DiagnosticSink | None = None,
        application_paths: MacOSApplicationPaths | None = None,
        application_lease_factory: ApplicationLeaseFactory = (
            acquire_application_instance_lease
        ),
        root_lease_factory: RootLeaseFactory = acquire_root_process_lease,
    ) -> None:
        self._adapter = adapter
        self._config_factory = config_factory
        self._dependencies_factory = (
            dependencies_factory or _native_launcher_dependencies
        )
        self._thread_factory = thread_factory or _worker_thread
        self._diagnostic_sink = diagnostic_sink
        self._application_paths = application_paths
        self._application_lease_factory = application_lease_factory
        self._root_lease_factory = root_lease_factory
        self._application_lease: ProcessLease | None = None
        self._root_lease: ProcessLease | None = None
        self._sensitive_values: set[str] = set()
        self._terminal_result: Future[LaunchResult] = Future()
        self._stop_controller = LaunchStopController()
        self._runtime_handle = LaunchRuntimeHandle(self._stop_controller)
        self._shutdown = NativeShutdownCoordinator(
            adapter,
            self._runtime_handle,
            self._terminal_result,
            thread_factory=self._thread_factory,
            diagnostic=self._record_shutdown_diagnostic,
        )
        self._worker: Thread | None = None
        self._worker_started = False
        self._exit_code = 1
        self._signals: ProcessSignalCoordinator | None = None
        self._signal_intent: SignalIntent | None = None
        self._signal_owned_shutdown = False
        self._signal_cleanup_succeeded = False
        self._pre_root_lock = Lock()
        self._pre_root_phase = _PreRootPhase.INITIALIZING
        self._pre_root_terminal = Event()
        self._pre_root_cancellation_failed = False

    def run(self) -> int:
        try:
            try:
                result = self._run()
            except KeyboardInterrupt:
                result = self._handle_keyboard_interrupt()
        finally:
            leases_released = self._release_process_leases()
        if leases_released:
            return result
        self._present_code("lease_unavailable")
        return 1

    def _run(self) -> int:
        try:
            self._adapter.set_diagnostic_handler(self._record_native_event)
            self._adapter.initialize()
            if self._application_paths is not None:
                self._application_lease = self._application_lease_factory(
                    self._application_paths
                )
            self._signals = ProcessSignalCoordinator(
                self._receive_signal,
                terminal_completed=lambda: (
                    self._terminal_result.done()
                    or self._pre_root_terminal.is_set()
                ),
                diagnostic=self._record_signal_diagnostic,
            )
            self._signals.install()
            self._record_native_event("pre_root_signal_owner_installed")
            self._adapter.set_termination_handler(self._request_pre_root_quit)
            with self._pre_root_lock:
                self._pre_root_phase = _PreRootPhase.CHOOSING
            pending = self._signals.capture_pending_intent()
            if pending is not None:
                self._claim_pre_root_signal(pending)
                return self._finish_pre_root(pending.exit_code)
            raw_root = self._adapter.choose_directory()
            outcome, intent = self._resolve_directory_selection(raw_root)
        except ProcessLeaseError as error:
            self._pre_root_terminal.set()
            self._finish_signal_coordination(1)
            code = (
                "application_already_open"
                if error.code == "already_held"
                else "lease_unavailable"
            )
            self._record_lease_diagnostic("application_lease", code, error)
            self._present_code(code)
            return 1
        except NativeAdapterError as error:
            self._pre_root_terminal.set()
            self._finish_signal_coordination(1)
            self._record_diagnostic("native_shell", error.code, error)
            self._present_code(error.code)
            return 1
        except KeyboardInterrupt:
            self._pre_root_terminal.set()
            self._finish_signal_coordination(130)
            raise
        except Exception as error:
            self._pre_root_terminal.set()
            self._finish_signal_coordination(1)
            self._record_diagnostic(
                "native_shell",
                "app_creation_failed",
                error,
            )
            self._present_code("app_creation_failed")
            return 1

        if outcome is _PreRootPhase.SIGNAL_WON:
            assert intent is not None
            return self._finish_pre_root(intent.exit_code)
        if outcome in (_PreRootPhase.QUIT_WON, _PreRootPhase.CANCEL_WON):
            return self._finish_pre_root(0)
        self._record_native_event("selection_committed")
        self._adapter.set_termination_handler(self._shutdown.request_termination)
        with self._pre_root_lock:
            self._pre_root_phase = _PreRootPhase.RUNNING
        self._record_native_event("signal_handoff_post_selection")
        assert raw_root is not None
        self._remember_sensitive(raw_root)

        try:
            admitted_root = admit_root(Path(raw_root)).path
        except RootAdmissionError as error:
            self._pre_root_terminal.set()
            self._finish_signal_coordination(1)
            self._record_diagnostic("root_admission", error.code, error)
            self._present_code(error.code)
            return 1
        except (OSError, TypeError, ValueError) as error:
            self._pre_root_terminal.set()
            self._finish_signal_coordination(1)
            self._record_diagnostic("root_admission", "root_not_allowed", error)
            self._present_code("root_not_allowed")
            return 1
        self._remember_sensitive(str(admitted_root))

        try:
            if self._application_paths is not None:
                self._root_lease = self._root_lease_factory(
                    self._application_paths,
                    admitted_root,
                )
            dependencies = self._dependencies_factory(self._adapter.open_browser)
            config = self._config_factory(admitted_root)
            self._remember_sensitive(config.launch_token)
            self._remember_sensitive(config.session_secret)
            self._worker = self._thread_factory(
                lambda: self._run_worker(config, dependencies),
                "loci-flow-server",
            )
            self._adapter.set_launch_running(True)
            self._adapter.dispatch_to_main(self._start_worker_on_main)
        except ProcessLeaseError as error:
            self._adapter.set_launch_running(False)
            code = (
                "root_already_open"
                if error.code == "already_held"
                else "lease_unavailable"
            )
            self._record_lease_diagnostic("root_lease", code, error)
            self._present_code(code)
            return self._finish_signal_coordination(1)
        except Exception as error:
            self._adapter.set_launch_running(False)
            self._record_diagnostic(
                "worker_start",
                "app_creation_failed",
                error,
            )
            self._present_code("app_creation_failed")
            return self._finish_signal_coordination(1)

        try:
            self._adapter.run_event_loop()
        finally:
            exit_code = self._finish_signal_coordination(self._exit_code)
        return exit_code

    def _start_worker_on_main(self) -> None:
        try:
            if self._worker is None:
                raise RuntimeError("native launcher worker is unavailable")
            self._worker.start()
            self._worker_started = True
        except Exception as error:
            result = LaunchResult(
                exit_code=1,
                phases=(LaunchPhase.FAILED,),
                error=LauncherError(
                    code="app_creation_failed",
                    safe_message="The local application could not be started.",
                ),
            )
            self._terminal_result.set_result(result)
            self._record_diagnostic(
                "worker_start",
                "app_creation_failed",
                error,
            )
            self._complete_on_main()

    def _run_worker(
        self,
        config: WebAppConfig,
        dependencies: LauncherDependencies,
    ) -> None:
        try:
            result = launch_configured(
                config,
                dependencies=dependencies,
                diagnostic=self._record_launch_diagnostic,
                stop_controller=self._stop_controller,
                runtime_handle=(
                    self._runtime_handle
                    if dependencies.runtime_lifecycle_accessor is not None
                    else None
                ),
            )
        except Exception as error:
            result = LaunchResult(
                exit_code=1,
                phases=(LaunchPhase.FAILED,),
                error=LauncherError(
                    code="app_creation_failed",
                    safe_message="The local application could not be started.",
                ),
            )
            self._record_diagnostic(
                "worker",
                "app_creation_failed",
                error,
            )
        self._terminal_result.set_result(result)
        try:
            self._adapter.dispatch_to_main(self._complete_on_main)
        except Exception as error:
            self._record_diagnostic(
                "worker_completion",
                "native_dispatch_failed",
                error,
            )
            if self._shutdown.owns_worker_completion():
                self._shutdown.main_dispatch_failed(error)

    def _complete_on_main(self) -> None:
        result = self._terminal_result.result()
        if self._shutdown.owns_worker_completion():
            try:
                if self._worker_has_started():
                    assert self._worker is not None
                    self._worker.join()
            except Exception as error:
                self._shutdown.worker_join_failed(error)
                return
            self._exit_code = self._signal_exit_code(result)
            self._shutdown.worker_completed(succeeded=result.succeeded)
            return
        if self._worker_has_started():
            assert self._worker is not None
            self._worker.join()
        self._adapter.set_launch_running(False)
        if result.error is not None:
            self._adapter.show_error(native_message_for(result.error.code))
        self._exit_code = result.exit_code
        self._adapter.stop_event_loop()

    def _receive_signal(self, intent: SignalIntent) -> bool:
        self._adapter.dispatch_to_main(
            lambda intent=intent: self._receive_signal_on_main(intent)
        )
        return True

    def _receive_signal_on_main(self, intent: SignalIntent) -> None:
        with self._pre_root_lock:
            phase = self._pre_root_phase
            if phase in (_PreRootPhase.INITIALIZING, _PreRootPhase.CHOOSING):
                self._pre_root_phase = _PreRootPhase.SIGNAL_WON
                self._signal_intent = intent
                pre_root = True
            elif phase is _PreRootPhase.SIGNAL_WON:
                pre_root = True
            elif phase in (_PreRootPhase.QUIT_WON, _PreRootPhase.CANCEL_WON):
                return
            else:
                pre_root = False
        if pre_root:
            self._record_native_event("pre_root_signal_won")
            try:
                self._adapter.cancel_active_directory_selection()
            except Exception as error:
                with self._pre_root_lock:
                    self._pre_root_cancellation_failed = True
                self._record_signal_diagnostic(
                    "chooser_cancellation_failed",
                    error,
                )
            return
        if self._terminal_result.done():
            return
        if self._shutdown.request_signal_shutdown():
            self._signal_intent = intent
            self._signal_owned_shutdown = True

    def _request_pre_root_quit(self) -> bool:
        signals = self._signals
        pending = None if signals is None else signals.capture_pending_intent()
        with self._pre_root_lock:
            if self._pre_root_phase is _PreRootPhase.SIGNAL_WON or pending is not None:
                self._pre_root_phase = _PreRootPhase.SIGNAL_WON
                if pending is not None:
                    self._signal_intent = pending
                event = "pre_root_signal_won"
            elif self._pre_root_phase is _PreRootPhase.CHOOSING:
                self._pre_root_phase = _PreRootPhase.QUIT_WON
                event = "pre_root_quit_won"
            else:
                return True
        self._record_native_event(event)
        try:
            self._adapter.cancel_active_directory_selection()
        except Exception as error:
            with self._pre_root_lock:
                self._pre_root_cancellation_failed = True
            self._record_signal_diagnostic("chooser_cancellation_failed", error)
            return False
        return True

    def _claim_pre_root_signal(self, intent: SignalIntent) -> None:
        with self._pre_root_lock:
            if self._pre_root_phase not in (
                _PreRootPhase.QUIT_WON,
                _PreRootPhase.CANCEL_WON,
                _PreRootPhase.SELECTION_WON,
            ):
                self._pre_root_phase = _PreRootPhase.SIGNAL_WON
                self._signal_intent = intent
        self._record_native_event("pre_root_signal_won")

    def _resolve_directory_selection(
        self,
        raw_root: str | None,
    ) -> tuple[_PreRootPhase, SignalIntent | None]:
        signals = self._signals
        pending = None if signals is None else signals.capture_pending_intent()
        with self._pre_root_lock:
            phase = self._pre_root_phase
            if phase is _PreRootPhase.SIGNAL_WON:
                intent = self._signal_intent or pending
                if intent is not None:
                    self._signal_intent = intent
                return phase, intent
            if phase is _PreRootPhase.QUIT_WON:
                return phase, None
            if pending is not None:
                self._pre_root_phase = _PreRootPhase.SIGNAL_WON
                self._signal_intent = pending
                return _PreRootPhase.SIGNAL_WON, pending
            if raw_root is None:
                self._pre_root_phase = _PreRootPhase.CANCEL_WON
                return _PreRootPhase.CANCEL_WON, None
            self._pre_root_phase = _PreRootPhase.SELECTION_WON
            return _PreRootPhase.SELECTION_WON, None

    def _finish_pre_root(self, exit_code: int) -> int:
        self._pre_root_terminal.set()
        self._record_native_event("pre_root_exit_code_selected")
        with self._pre_root_lock:
            cancellation_failed = self._pre_root_cancellation_failed
        result = self._finish_signal_coordination(
            1 if cancellation_failed else exit_code
        )
        if self._signal_cleanup_succeeded:
            self._record_native_event("pre_root_signal_state_restored")
        return result

    def _signal_exit_code(self, result: LaunchResult) -> int:
        if (
            result.succeeded
            and self._signal_owned_shutdown
            and self._signal_intent is not None
        ):
            return self._signal_intent.exit_code
        return result.exit_code

    def _finish_signal_coordination(self, exit_code: int) -> int:
        signals = self._signals
        if signals is None:
            return exit_code
        try:
            result = signals.close()
        except Exception as error:
            self._record_signal_diagnostic("signal_restore_failed", error)
            self._present_code("cleanup_failed")
            return 1
        if result.error is not None:
            self._present_code("cleanup_failed")
            return 1
        self._signal_cleanup_succeeded = True
        return exit_code

    def _handle_keyboard_interrupt(self) -> int:
        self._record_interrupt_diagnostic()
        terminal_completed_before_interrupt = self._terminal_result.done()
        if not self._worker_has_started():
            if not terminal_completed_before_interrupt:
                self._terminal_result.set_result(_interrupted_result())
            self._finish_interrupted_native_loop()
            return (
                self._terminal_result.result().exit_code
                if terminal_completed_before_interrupt
                else 130
            )

        if not terminal_completed_before_interrupt:
            self._stop_controller.request_stop()
        result = self._terminal_result.result()
        assert self._worker is not None
        self._worker.join()
        self._finish_interrupted_native_loop()
        return result.exit_code if terminal_completed_before_interrupt else 130

    def _worker_has_started(self) -> bool:
        return self._worker is not None and self._worker.ident is not None

    def _finish_interrupted_native_loop(self) -> None:
        try:
            self._adapter.set_launch_running(False)
        except Exception:
            pass

    def _release_process_leases(self) -> bool:
        succeeded = True
        for attribute in ("_root_lease", "_application_lease"):
            lease = getattr(self, attribute)
            setattr(self, attribute, None)
            if lease is None:
                continue
            try:
                lease.release()
            except ProcessLeaseError as error:
                succeeded = False
                self._record_lease_diagnostic(
                    "process_lease_cleanup",
                    "lease_unavailable",
                    error,
                )
        return succeeded

    def _record_interrupt_diagnostic(self) -> None:
        if self._diagnostic_sink is None:
            return
        try:
            self._diagnostic_sink(
                NativeDiagnostic(
                    phase="native_event_loop",
                    code="interrupted",
                    exception_class=None,
                    traceback=None,
                )
            )
        except Exception:
            pass

    def _present_code(self, code: str) -> None:
        try:
            self._adapter.show_error(native_message_for(code))
        except Exception:
            pass

    def _record_launch_diagnostic(
        self,
        phase: LaunchPhase,
        error: LauncherError,
        exception: BaseException | None,
    ) -> None:
        self._record_diagnostic(phase.value, error.code, exception, error)

    def _record_shutdown_diagnostic(
        self,
        code: str,
        exception: BaseException | None,
    ) -> None:
        if self._diagnostic_sink is None:
            return
        diagnostic = NativeDiagnostic(
            phase="native_shutdown",
            code=code,
            exception_class=(
                None if exception is None else type(exception).__name__
            ),
            traceback=None,
        )
        try:
            self._diagnostic_sink(diagnostic)
        except Exception:
            pass

    def _record_signal_diagnostic(
        self,
        code: str,
        exception: BaseException | None,
    ) -> None:
        if self._diagnostic_sink is None:
            return
        try:
            self._diagnostic_sink(
                NativeDiagnostic(
                    phase="native_signal",
                    code=code,
                    exception_class=(
                        None if exception is None else type(exception).__name__
                    ),
                    traceback=None,
                )
            )
        except Exception:
            pass

    def _record_native_event(self, event: str) -> None:
        if self._diagnostic_sink is None:
            return
        try:
            self._diagnostic_sink(
                NativeDiagnostic(
                    phase="native_appkit",
                    code=event,
                    exception_class=None,
                    traceback=None,
                )
            )
        except Exception:
            pass

    def _record_lease_diagnostic(
        self,
        phase: str,
        code: str,
        error: ProcessLeaseError,
    ) -> None:
        if self._diagnostic_sink is None:
            return
        try:
            self._diagnostic_sink(
                NativeDiagnostic(
                    phase=phase,
                    code=code,
                    exception_class=type(error).__name__,
                    traceback=None,
                )
            )
        except Exception:
            pass

    def _record_diagnostic(
        self,
        phase: str,
        code: str,
        exception: BaseException | None,
        typed_error: LauncherError | None = None,
    ) -> None:
        if self._diagnostic_sink is None:
            return
        error_for_class: object = exception if exception is not None else typed_error
        exception_class = type(error_for_class).__name__
        if exception is None:
            formatted_traceback = (
                "Traceback unavailable: the typed launch result contained no exception."
            )
        else:
            formatted_traceback = "".join(
                traceback.format_exception(
                    type(exception),
                    exception,
                    exception.__traceback__,
                )
            )
        diagnostic = NativeDiagnostic(
            phase=phase,
            code=code,
            exception_class=exception_class,
            traceback=self._redact(formatted_traceback),
        )
        try:
            self._diagnostic_sink(diagnostic)
        except Exception:
            pass

    def _remember_sensitive(self, value: object) -> None:
        if isinstance(value, str) and value:
            self._sensitive_values.add(value)

    def _redact(self, value: str) -> str:
        redacted = _PRIVATE_LAUNCH_URL.sub("<redacted-private-url>", value)
        for sensitive in sorted(self._sensitive_values, key=len, reverse=True):
            redacted = redacted.replace(sensitive, "<redacted>")
        return redacted


def native_message_for(code: str) -> NativeMessage:
    messages = {
        "application_already_open": "Loci Flow is already open.",
        "root_already_open": "This folder is already open in Loci Flow.",
        "lease_unavailable": "Loci Flow could not start safely.",
        "root_inaccessible": "Loci Flow can’t access that folder.",
        "app_creation_failed": "Loci Flow couldn’t start.",
        "server_failed": "Loci Flow couldn’t start.",
        "cleanup_failed": "Loci Flow couldn’t start.",
        "bind_failed": "Loci Flow couldn’t start its private local server.",
        "readiness_failed": (
            "Loci Flow couldn’t confirm that its local server was ready."
        ),
        "browser_open_failed": "Loci Flow couldn’t open your browser.",
    }
    return NativeMessage(
        title="Loci Flow",
        message=messages.get(code, "That folder can’t be used."),
    )


def development_diagnostic_sink(
    *,
    environ: dict[str, str] | None = None,
    stream: TextIO | None = None,
) -> DiagnosticSink | None:
    environment = os.environ if environ is None else environ
    if environment.get("LOCI_FLOW_NATIVE_DEBUG") != "1":
        return None
    output = sys.stderr if stream is None else stream

    def emit(diagnostic: NativeDiagnostic) -> None:
        print("Loci Flow native development diagnostic", file=output)
        print(f"phase: {diagnostic.phase}", file=output)
        print(f"code: {diagnostic.code}", file=output)
        if diagnostic.exception_class is not None:
            print(f"exception: {diagnostic.exception_class}", file=output)
        if diagnostic.traceback is not None:
            print(diagnostic.traceback, file=output)

    return emit


def _native_launcher_dependencies(
    browser_opener: Callable[[str], bool],
) -> LauncherDependencies:
    return build_launcher_dependencies(
        browser_opener=browser_opener,
        fail_closed_readiness=True,
        coordinated_signals=True,
        root_lease_factory=None,
    )


def _worker_thread(target: Callable[[], None], name: str) -> Thread:
    return Thread(target=target, name=name)


def _interrupted_result() -> LaunchResult:
    return LaunchResult(
        exit_code=130,
        phases=(LaunchPhase.STOPPED,),
    )
