from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from threading import Thread
import traceback
from typing import Protocol, TextIO

from organizer.macos_launcher.shutdown_coordinator import (
    NativeShutdownCoordinator,
)
from organizer.web.config import WebAppConfig
from organizer.web.launcher import (
    LaunchPhase,
    LauncherDependencies,
    LauncherError,
    LaunchResult,
    LaunchRuntimeHandle,
    LaunchStopController,
    launch_configured,
)
from organizer.web.root_admission import RootAdmissionError
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


class NativeAdapter(Protocol):
    def initialize(self) -> None: ...

    def choose_directory(self) -> str | None: ...

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
    ) -> None:
        self._adapter = adapter
        self._config_factory = config_factory
        self._dependencies_factory = (
            dependencies_factory or _native_launcher_dependencies
        )
        self._thread_factory = thread_factory or _worker_thread
        self._diagnostic_sink = diagnostic_sink
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

    def run(self) -> int:
        try:
            return self._run()
        except KeyboardInterrupt:
            return self._handle_keyboard_interrupt()

    def _run(self) -> int:
        try:
            self._adapter.set_diagnostic_handler(self._record_native_event)
            self._adapter.initialize()
            self._adapter.set_termination_handler(
                self._shutdown.request_termination
            )
            raw_root = self._adapter.choose_directory()
        except NativeAdapterError as error:
            self._record_diagnostic("native_shell", error.code, error)
            self._present_code(error.code)
            return 1
        except Exception as error:
            self._record_diagnostic(
                "native_shell",
                "app_creation_failed",
                error,
            )
            self._present_code("app_creation_failed")
            return 1

        if raw_root is None:
            return 0
        self._remember_sensitive(raw_root)

        try:
            config = self._config_factory(Path(raw_root))
        except RootAdmissionError as error:
            self._record_diagnostic("root_admission", error.code, error)
            self._present_code(error.code)
            return 1
        except (OSError, TypeError, ValueError) as error:
            self._record_diagnostic("root_admission", "root_not_allowed", error)
            self._present_code("root_not_allowed")
            return 1
        self._remember_sensitive(str(config.root))
        self._remember_sensitive(config.launch_token)
        self._remember_sensitive(config.session_secret)

        try:
            self._signals = ProcessSignalCoordinator(
                self._receive_signal,
                terminal_completed=self._terminal_result.done,
                diagnostic=self._record_signal_diagnostic,
            )
            self._signals.install()
        except Exception as error:
            self._record_diagnostic(
                "signal_installation",
                "signal_installation_failed",
                error,
            )
            self._present_code("app_creation_failed")
            return 1

        try:
            dependencies = self._dependencies_factory(self._adapter.open_browser)
            self._worker = self._thread_factory(
                lambda: self._run_worker(config, dependencies),
                "loci-flow-server",
            )
            self._adapter.set_launch_running(True)
            self._adapter.dispatch_to_main(self._start_worker_on_main)
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
        if self._terminal_result.done():
            return
        if self._shutdown.request_signal_shutdown():
            self._signal_intent = intent
            self._signal_owned_shutdown = True

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
    )


def _worker_thread(target: Callable[[], None], name: str) -> Thread:
    return Thread(target=target, name=name)


def _interrupted_result() -> LaunchResult:
    return LaunchResult(
        exit_code=130,
        phases=(LaunchPhase.STOPPED,),
    )
