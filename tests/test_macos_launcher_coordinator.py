from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread, current_thread, main_thread
from unittest import mock

from organizer.application.runtime_lifecycle import RuntimeLifecycle
from organizer.macos_launcher import coordinator as coordinator_module
from organizer.macos_launcher.application_paths import MacOSApplicationPaths
from organizer.macos_launcher.coordinator import (
    NativeLauncherCoordinator,
    NativeMessage,
    development_diagnostic_sink,
    native_message_for,
)
from organizer.macos_launcher.packaged_diagnostics import (
    PackagedDiagnosticSink,
    PackagedReleaseIdentity,
)
from organizer.macos_launcher.process_lease import (
    acquire_application_instance_lease,
    acquire_root_process_lease,
)
from organizer.web.config import WebAppConfig
from organizer.web.launcher import LauncherDependencies, LauncherError
from organizer.web.server import build_launcher_dependencies
from organizer.web.signal_coordinator import (
    SignalIntent,
    SignalKind,
    SignalShutdownResult,
)


class _Adapter:
    def __init__(
        self,
        selected: str | None,
        *,
        choose_error: BaseException | None = None,
    ) -> None:
        self.selected = selected
        self.choose_error = choose_error
        self.events: list[str] = []
        self.messages: list[NativeMessage] = []
        self.callbacks: Queue[Callable[[], None]] = Queue()
        self.dispatched_callbacks: list[str] = []
        self.loop_started = Event()
        self.loop_stopped = Event()
        self.terminal_dispatched = Event()
        self.stopped = False
        self.running_states: list[bool] = []
        self.browser_result = True
        self.browser_opened = Event()
        self.termination_handler: Callable[[], bool] | None = None
        self.finishing_shown = 0
        self.finishing_closed = 0
        self.shutdown_failures = 0
        self.termination_replies: list[bool] = []
        self.diagnostic_handler: Callable[[str], None] | None = None
        self.chooser_cancellations = 0

    def initialize(self) -> None:
        self.events.append("adapter.initialize")

    def choose_directory(self) -> str | None:
        self.events.append("adapter.choose")
        if self.choose_error is not None:
            raise self.choose_error
        return self.selected

    def cancel_active_directory_selection(self) -> bool:
        self.events.append("adapter.choose.cancel")
        self.chooser_cancellations += 1
        return True

    def open_browser(self, launch_url: str) -> bool:
        self.events.append("adapter.browser.request")
        if current_thread() is main_thread():
            self.events.append("adapter.browser.open")
            self.browser_opened.set()
            return bool(launch_url) and self.browser_result
        completed = Event()
        outcome = {"opened": False}

        def open_on_main() -> None:
            self.events.append("adapter.browser.open")
            outcome["opened"] = bool(launch_url) and self.browser_result
            self.browser_opened.set()
            completed.set()

        self.dispatch_to_main(open_on_main)
        if not completed.wait(2):
            raise AssertionError("main-thread browser dispatch did not complete")
        return outcome["opened"]

    def show_error(self, message: NativeMessage) -> None:
        self.events.append("adapter.error")
        self.messages.append(message)

    def dispatch_to_main(self, callback: Callable[[], None]) -> None:
        self.events.append("adapter.dispatch")
        self.dispatched_callbacks.append(callback.__name__)
        if callback.__name__ == "_complete_on_main":
            self.terminal_dispatched.set()
        self.callbacks.put(callback)

    def run_event_loop(self) -> None:
        self.events.append("adapter.run_loop")
        self.loop_started.set()
        while not self.loop_stopped.is_set():
            try:
                callback = self.callbacks.get(timeout=2)
            except Empty as error:
                raise AssertionError("native event loop received no callback") from error
            callback()

    def stop_event_loop(self) -> None:
        self.events.append("adapter.stop_loop")
        self.stopped = True
        self.loop_stopped.set()

    def set_launch_running(self, running: bool) -> None:
        self.events.append(f"adapter.running:{running}")
        self.running_states.append(running)

    def set_termination_handler(self, handler: Callable[[], bool]) -> None:
        self.termination_handler = handler

    def set_diagnostic_handler(self, handler: Callable[[str], None]) -> None:
        self.diagnostic_handler = handler

    def show_finishing_status(self) -> None:
        self.finishing_shown += 1

    def close_finishing_status(self) -> None:
        self.finishing_closed += 1

    def show_shutdown_failure(self) -> None:
        self.shutdown_failures += 1

    def reply_to_termination(self, allow: bool) -> None:
        self.termination_replies.append(allow)
        if allow:
            self.stop_event_loop()


class _Socket:
    def __init__(self) -> None:
        self.closed = False

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 48191)

    def close(self) -> None:
        self.closed = True


class _RecordingLease:
    def __init__(
        self,
        events: list[str],
        name: str,
        *,
        before_release: Callable[[], None] | None = None,
    ) -> None:
        self._events = events
        self._name = name
        self._before_release = before_release
        self.released = False

    def release(self) -> bool:
        if self.released:
            return False
        if self._before_release is not None:
            self._before_release()
        self._events.append(f"{self._name}.release")
        self.released = True
        return True


class _Server:
    def __init__(self) -> None:
        self.runs = 0
        self.exit_requests = 0

    def run(self, *, sockets: list[_Socket]) -> None:
        self.runs += 1

    def request_exit(self) -> None:
        self.exit_requests += 1


class _Observer:
    error = None

    def start(self) -> None:
        pass

    def join(self) -> None:
        pass


class _BlockingServer(_Server):
    def __init__(self) -> None:
        super().__init__()
        self.exit_requested = Event()

    def run(self, *, sockets: list[_Socket]) -> None:
        self.runs += 1
        if not self.exit_requested.wait(2):
            raise AssertionError("test server did not receive its exit request")

    def request_exit(self) -> None:
        super().request_exit()
        self.exit_requested.set()


class _InterruptingAdapter(_Adapter):
    def __init__(
        self,
        selected: str | None,
        *,
        interrupt_before_callbacks: bool = False,
        interrupt_after: Event | None = None,
    ) -> None:
        super().__init__(selected)
        self.interrupt_before_callbacks = interrupt_before_callbacks
        self.interrupt_after = interrupt_after

    def run_event_loop(self) -> None:
        self.events.append("adapter.run_loop")
        self.loop_started.set()
        if self.interrupt_before_callbacks:
            raise KeyboardInterrupt
        while not self.loop_stopped.is_set():
            try:
                callback = self.callbacks.get(timeout=2)
            except Empty as error:
                raise AssertionError("native event loop received no callback") from error
            callback()
            if self.interrupt_after is not None and self.interrupt_after.is_set():
                raise KeyboardInterrupt


class _TriggeredInterruptAdapter(_Adapter):
    def __init__(self, selected: str, trigger: Event) -> None:
        super().__init__(selected)
        self.trigger = trigger

    def run_event_loop(self) -> None:
        self.events.append("adapter.run_loop")
        self.loop_started.set()
        try:
            callback = self.callbacks.get(timeout=2)
        except Empty as error:
            raise AssertionError("worker-start callback was not delivered") from error
        callback()
        if not self.trigger.wait(2):
            raise AssertionError("interrupt lifecycle trigger was not reached")
        raise KeyboardInterrupt


class _CompletedWorkerInterruptAdapter(_Adapter):
    def run_event_loop(self) -> None:
        self.events.append("adapter.run_loop")
        self.loop_started.set()
        try:
            callback = self.callbacks.get(timeout=2)
        except Empty as error:
            raise AssertionError("worker-start callback was not delivered") from error
        callback()
        if not self.terminal_dispatched.wait(2):
            raise AssertionError("terminal worker result was not delivered")
        raise KeyboardInterrupt


class _NativeQuitAdapter(_Adapter):
    def __init__(self, selected: str, server_running: Event) -> None:
        super().__init__(selected)
        self.server_running = server_running

    def run_event_loop(self) -> None:
        self.events.append("adapter.run_loop")
        self.loop_started.set()
        try:
            start_worker = self.callbacks.get(timeout=2)
        except Empty as error:
            raise AssertionError("worker-start callback was not delivered") from error
        start_worker()
        if not self.server_running.wait(2):
            raise AssertionError("server did not start")
        if self.termination_handler is None:
            raise AssertionError("termination handler was not installed")
        self.events.append("adapter.quit")
        if not self.termination_handler():
            raise AssertionError("native termination was not delayed")
        while not self.loop_stopped.is_set():
            try:
                callback = self.callbacks.get(timeout=2)
            except Empty as error:
                raise AssertionError("terminal callback was not delivered") from error
            callback()


class _SignalAdapter(_Adapter):
    def __init__(
        self,
        selected: str,
        signal_number: int,
        server_running: Event,
    ) -> None:
        super().__init__(selected)
        self.signal_number = signal_number
        self.server_running = server_running

    def run_event_loop(self) -> None:
        self.events.append("adapter.run_loop")
        self.loop_started.set()
        try:
            start_worker = self.callbacks.get(timeout=2)
        except Empty as error:
            raise AssertionError("worker-start callback was not delivered") from error
        start_worker()
        if not self.server_running.wait(2):
            raise AssertionError("server did not start")
        os.kill(os.getpid(), self.signal_number)
        while not self.loop_stopped.is_set():
            try:
                callback = self.callbacks.get(timeout=2)
            except Empty as error:
                raise AssertionError("signal callback was not delivered") from error
            callback()


class _PreRootSignalAdapter(_Adapter):
    def __init__(self, selected: str, signal_number: int) -> None:
        super().__init__(selected)
        self.signal_number = signal_number
        self.chooser_active = False

    def choose_directory(self) -> str | None:
        self.events.append("adapter.choose")
        self.chooser_active = True
        os.kill(os.getpid(), self.signal_number)
        try:
            callback = self.callbacks.get(timeout=2)
        except Empty as error:
            raise AssertionError("pre-root signal callback was not delivered") from error
        callback()
        self.chooser_active = False
        return None if self.chooser_cancellations else self.selected

    def cancel_active_directory_selection(self) -> bool:
        if not self.chooser_active or self.chooser_cancellations:
            return False
        return super().cancel_active_directory_selection()


class _SignalThenQuitAdapter(_PreRootSignalAdapter):
    def choose_directory(self) -> str | None:
        self.events.append("adapter.choose")
        self.chooser_active = True
        os.kill(os.getpid(), self.signal_number)
        try:
            callback = self.callbacks.get(timeout=2)
        except Empty as error:
            raise AssertionError("pre-root signal callback was not delivered") from error
        callback()
        if self.termination_handler is None:
            raise AssertionError("pre-root termination handler was not installed")
        self.termination_handler()
        self.chooser_active = False
        return None


class _QuitThenSignalAdapter(_Adapter):
    def __init__(self, selected: str, signal_number: int) -> None:
        super().__init__(selected)
        self.signal_number = signal_number

    def choose_directory(self) -> str | None:
        self.events.append("adapter.choose")
        if self.termination_handler is None:
            raise AssertionError("pre-root termination handler was not installed")
        self.termination_handler()
        os.kill(os.getpid(), self.signal_number)
        return None


class _FailingChooserCancellationAdapter(_PreRootSignalAdapter):
    def cancel_active_directory_selection(self) -> bool:
        raise RuntimeError("controlled chooser cancellation failure")


class _PreRootQuitAdapter(_Adapter):
    def choose_directory(self) -> str | None:
        self.events.append("adapter.choose")
        if self.termination_handler is None:
            raise AssertionError("pre-root termination handler was not installed")
        self.events.append("adapter.quit")
        self.termination_handler()
        return None


class _ControlledSignalCoordinator:
    instances: list[_ControlledSignalCoordinator] = []
    capture_results: list[SignalIntent | None] = []

    def __init__(self, on_signal, **_kwargs: object) -> None:
        self.on_signal = on_signal
        self.installs = 0
        self.closes = 0
        self.captures = 0
        self.intent: SignalIntent | None = None
        self.__class__.instances.append(self)

    def install(self) -> None:
        self.installs += 1

    def capture_pending_intent(self) -> SignalIntent | None:
        self.captures += 1
        if self.__class__.capture_results:
            self.intent = self.__class__.capture_results.pop(0)
        return self.intent

    def close(self) -> SignalShutdownResult:
        self.closes += 1
        return SignalShutdownResult(self.intent, False, None)


class _AsyncObserver:
    def __init__(
        self,
        observe: Callable[[], LauncherError | None],
        request_exit: Callable[[], None],
    ) -> None:
        self.error: LauncherError | None = None
        self._observe = observe
        self._request_exit = request_exit
        self._thread = Thread(target=self._run)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        self.error = self._observe()
        self._request_exit()


class _PassiveBrowserObserver:
    def __init__(self, browser_opener: Callable[[str], bool]) -> None:
        self.error = None
        self._browser_opener = browser_opener
        self._thread = Thread(target=self._run)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        if not self._browser_opener("opaque-private-url"):
            self.error = LauncherError("browser_open_failed", "safe")


class _CountingFuture(Future[object]):
    def __init__(self) -> None:
        super().__init__()
        self.set_calls = 0

    def set_result(self, result: object) -> None:
        self.set_calls += 1
        super().set_result(result)


class MacOSLauncherCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.socket = _Socket()
        self.server = _Server()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def dependencies(self) -> LauncherDependencies:
        return LauncherDependencies(
            config_factory=WebAppConfig,
            socket_binder=lambda _port: self.socket,
            app_factory=lambda _config: object(),
            server_factory=lambda _app, _port: self.server,
            readiness_factory=lambda _health, _launch, _stop, _exit: _Observer(),
            manual_browser_bootstrap=lambda _url: None,
        )

    def application_paths(self) -> MacOSApplicationPaths:
        return MacOSApplicationPaths.for_home(self.root / "application-home")

    def test_global_conflict_stops_before_chooser_root_or_server(self) -> None:
        paths = self.application_paths()
        adapter = _Adapter(str(self.root))
        config_factory = mock.Mock()
        dependencies_factory = mock.Mock()
        diagnostics = []
        with acquire_application_instance_lease(paths):
            result = NativeLauncherCoordinator(
                adapter,
                application_paths=paths,
                config_factory=config_factory,
                dependencies_factory=dependencies_factory,
                diagnostic_sink=diagnostics.append,
            ).run()

        self.assertEqual(result, 1)
        self.assertEqual(
            adapter.messages[-1].message,
            "Loci Flow is already open.",
        )
        self.assertNotIn("adapter.choose", adapter.events)
        config_factory.assert_not_called()
        dependencies_factory.assert_not_called()
        self.assertNotIn("adapter.browser.request", adapter.events)
        application_diagnostic = next(
            item for item in diagnostics if item.phase == "application_lease"
        )
        self.assertIsNone(application_diagnostic.traceback)

    def test_root_conflict_stops_before_dependencies_config_socket_or_browser(
        self,
    ) -> None:
        paths = self.application_paths()
        adapter = _Adapter(str(self.root))
        config_factory = mock.Mock()
        dependencies_factory = mock.Mock()
        diagnostics = []
        development_dependencies = build_launcher_dependencies(
            application_paths=paths
        )
        assert development_dependencies.root_lease_factory is not None
        with development_dependencies.root_lease_factory(self.root):
            result = NativeLauncherCoordinator(
                adapter,
                application_paths=paths,
                config_factory=config_factory,
                dependencies_factory=dependencies_factory,
                diagnostic_sink=diagnostics.append,
            ).run()

        self.assertEqual(result, 1)
        self.assertEqual(
            adapter.messages[-1].message,
            "This folder is already open in Loci Flow.",
        )
        self.assertIn("adapter.choose", adapter.events)
        config_factory.assert_not_called()
        dependencies_factory.assert_not_called()
        self.assertNotIn("adapter.browser.request", adapter.events)
        root_diagnostic = next(
            item for item in diagnostics if item.phase == "root_lease"
        )
        self.assertIsNone(root_diagnostic.traceback)
        self.assertNotIn(str(self.root), repr(root_diagnostic))
        with acquire_application_instance_lease(paths):
            pass

    def test_native_lease_order_and_release_follow_complete_socket_cleanup(
        self,
    ) -> None:
        paths = self.application_paths()
        adapter = _Adapter(str(self.root))
        events: list[str] = []
        adapter.events = events
        application_lease = _RecordingLease(events, "application")
        root_lease = _RecordingLease(
            events,
            "root",
            before_release=lambda: self.assertTrue(self.socket.closed),
        )

        def acquire_application(_paths: MacOSApplicationPaths):
            events.append("application.acquire")
            return application_lease

        def acquire_root(_paths: MacOSApplicationPaths, root: Path):
            self.assertEqual(root, self.root.resolve())
            events.append("root.acquire")
            return root_lease

        def dependencies_factory(_browser):
            events.append("dependencies")
            return self.dependencies()

        def config_factory(root: Path) -> WebAppConfig:
            events.append("config")
            return WebAppConfig(
                root,
                session_secret="s" * 32,
                launch_token="t" * 32,
                testing=True,
            )

        result = NativeLauncherCoordinator(
            adapter,
            application_paths=paths,
            application_lease_factory=acquire_application,
            root_lease_factory=acquire_root,
            dependencies_factory=dependencies_factory,
            config_factory=config_factory,
        ).run()

        self.assertEqual(result, 0)
        self.assertLess(
            events.index("application.acquire"),
            events.index("adapter.choose"),
        )
        self.assertLess(events.index("root.acquire"), events.index("dependencies"))
        self.assertLess(events.index("root.acquire"), events.index("config"))
        self.assertEqual(events[-2:], ["root.release", "application.release"])

    def test_native_startup_failure_releases_both_real_leases(self) -> None:
        paths = self.application_paths()
        adapter = _Adapter(str(self.root))

        result = NativeLauncherCoordinator(
            adapter,
            application_paths=paths,
            dependencies_factory=mock.Mock(
                side_effect=RuntimeError("controlled dependency failure")
            ),
        ).run()

        self.assertEqual(result, 1)
        with acquire_application_instance_lease(paths):
            with acquire_root_process_lease(paths, self.root):
                pass

    def test_admission_completes_before_worker_creation(self) -> None:
        adapter = _Adapter(str(self.root))
        events = adapter.events

        def config_factory(path: Path) -> WebAppConfig:
            events.append("config.admitted")
            return WebAppConfig(
                path,
                session_secret="s" * 32,
                launch_token="t" * 32,
                testing=True,
            )

        def thread_factory(target: Callable[[], None], name: str) -> Thread:
            events.append("worker.created")

            def run_target() -> None:
                events.append("worker.started")
                target()

            return Thread(target=run_target, name=name)

        coordinator = NativeLauncherCoordinator(
            adapter,
            config_factory=config_factory,
            dependencies_factory=lambda _browser: self.dependencies(),
            thread_factory=thread_factory,
        )

        result = coordinator.run()

        self.assertEqual(result, 0)
        self.assertLess(events.index("config.admitted"), events.index("worker.created"))
        self.assertEqual(adapter.running_states, [True, False])
        self.assertTrue(adapter.stopped)
        self.assertTrue(self.socket.closed)
        self.assertEqual(self.server.runs, 1)
        self.assertEqual(events.count("adapter.dispatch"), 2)
        self.assertEqual(adapter.dispatched_callbacks.count("_complete_on_main"), 1)
        self.assertLess(events.index("adapter.run_loop"), events.index("worker.started"))

    def test_native_idle_quit_uses_exact_app_lifecycle_and_worker_result_owner(
        self,
    ) -> None:
        lifecycle = RuntimeLifecycle()
        server_running = Event()
        server = _BlockingServer()
        original_run = server.run

        def run(*, sockets: list[_Socket]) -> None:
            server_running.set()
            original_run(sockets=sockets)

        server.run = run  # type: ignore[method-assign]
        adapter = _NativeQuitAdapter(str(self.root), server_running)
        dependencies = LauncherDependencies(
            config_factory=WebAppConfig,
            socket_binder=lambda _port: self.socket,
            app_factory=lambda _config: lifecycle,
            server_factory=lambda _app, _port: server,
            readiness_factory=lambda _health, _launch, _stop, _exit: _Observer(),
            manual_browser_bootstrap=lambda _url: None,
            runtime_lifecycle_accessor=lambda app: app,
        )

        coordinator = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=lambda _browser: dependencies,
        )
        result = coordinator.run()

        self.assertEqual(result, 0)
        self.assertIs(coordinator._runtime_handle.wait_for_lifecycle(), lifecycle)
        self.assertTrue(lifecycle.snapshot().is_stopping)
        self.assertEqual(server.exit_requests, 1)
        self.assertTrue(self.socket.closed)
        self.assertEqual(adapter.termination_replies, [True])
        self.assertIsNotNone(coordinator._worker)
        self.assertFalse(coordinator._worker.is_alive())

    def test_native_post_selection_signals_wait_and_return_signal_code(self) -> None:
        for signal_number, expected_code in (
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal_number=signal_number):
                lifecycle = RuntimeLifecycle()
                server_running = Event()
                server = _BlockingServer()
                original_run = server.run

                def run(*, sockets: list[_Socket]) -> None:
                    server_running.set()
                    original_run(sockets=sockets)

                server.run = run  # type: ignore[method-assign]
                adapter = _SignalAdapter(
                    str(self.root),
                    signal_number,
                    server_running,
                )
                socket = _Socket()
                dependencies = LauncherDependencies(
                    config_factory=WebAppConfig,
                    socket_binder=lambda _port, socket=socket: socket,
                    app_factory=lambda _config: lifecycle,
                    server_factory=lambda _app, _port, server=server: server,
                    readiness_factory=(
                        lambda _health, _launch, _stop, _exit: _Observer()
                    ),
                    manual_browser_bootstrap=lambda _url: None,
                    runtime_lifecycle_accessor=lambda app: app,
                )

                result = NativeLauncherCoordinator(
                    adapter,
                    dependencies_factory=lambda _browser, deps=dependencies: deps,
                ).run()

                self.assertEqual(result, expected_code)
                self.assertTrue(lifecycle.snapshot().is_stopping)
                self.assertEqual(server.exit_requests, 1)
                self.assertTrue(socket.closed)
                self.assertEqual(adapter.termination_replies, [])
                self.assertTrue(adapter.stopped)

    def test_cancellation_creates_no_config_worker_or_browser_side_effect(self) -> None:
        adapter = _Adapter(None)
        config_factory = mock.Mock()
        dependencies_factory = mock.Mock()
        thread_factory = mock.Mock()

        result = NativeLauncherCoordinator(
            adapter,
            config_factory=config_factory,
            dependencies_factory=dependencies_factory,
            thread_factory=thread_factory,
        ).run()

        self.assertEqual(result, 0)
        config_factory.assert_not_called()
        dependencies_factory.assert_not_called()
        thread_factory.assert_not_called()
        self.assertEqual(adapter.messages, [])
        self.assertNotIn("adapter.browser", adapter.events)
        self.assertNotIn("adapter.run_loop", adapter.events)

    def test_pre_root_signals_abort_chooser_and_create_no_root_side_effects(self) -> None:
        for signal_number, expected_code in (
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal_number=signal_number):
                adapter = _PreRootSignalAdapter(str(self.root), signal_number)
                config_factory = mock.Mock()
                dependencies_factory = mock.Mock()
                thread_factory = mock.Mock()

                result = NativeLauncherCoordinator(
                    adapter,
                    config_factory=config_factory,
                    dependencies_factory=dependencies_factory,
                    thread_factory=thread_factory,
                ).run()

                self.assertEqual(result, expected_code)
                self.assertEqual(adapter.chooser_cancellations, 1)
                config_factory.assert_not_called()
                dependencies_factory.assert_not_called()
                thread_factory.assert_not_called()
                self.assertNotIn("adapter.run_loop", adapter.events)
                self.assertEqual(adapter.messages, [])

    def test_native_quit_during_chooser_wins_without_fake_runtime_shutdown(self) -> None:
        adapter = _PreRootQuitAdapter(str(self.root))
        config_factory = mock.Mock()
        dependencies_factory = mock.Mock()
        thread_factory = mock.Mock()

        result = NativeLauncherCoordinator(
            adapter,
            config_factory=config_factory,
            dependencies_factory=dependencies_factory,
            thread_factory=thread_factory,
        ).run()

        self.assertEqual(result, 0)
        self.assertEqual(adapter.chooser_cancellations, 1)
        config_factory.assert_not_called()
        dependencies_factory.assert_not_called()
        thread_factory.assert_not_called()
        self.assertFalse(adapter.finishing_shown)
        self.assertEqual(adapter.termination_replies, [])
        self.assertNotIn("adapter.run_loop", adapter.events)

    def test_signal_then_native_quit_preserves_signal_exit_owner(self) -> None:
        adapter = _SignalThenQuitAdapter(str(self.root), signal.SIGTERM)
        config_factory = mock.Mock()

        result = NativeLauncherCoordinator(
            adapter,
            config_factory=config_factory,
            dependencies_factory=mock.Mock(),
            thread_factory=mock.Mock(),
        ).run()

        self.assertEqual(result, 143)
        self.assertEqual(adapter.chooser_cancellations, 1)
        config_factory.assert_not_called()

    def test_native_quit_then_signal_preserves_clean_quit_owner(self) -> None:
        adapter = _QuitThenSignalAdapter(str(self.root), signal.SIGINT)
        config_factory = mock.Mock()

        result = NativeLauncherCoordinator(
            adapter,
            config_factory=config_factory,
            dependencies_factory=mock.Mock(),
            thread_factory=mock.Mock(),
        ).run()

        self.assertEqual(result, 0)
        self.assertEqual(adapter.chooser_cancellations, 1)
        config_factory.assert_not_called()

    def test_signal_cancellation_failure_is_safe_and_starts_no_server(self) -> None:
        adapter = _FailingChooserCancellationAdapter(
            str(self.root),
            signal.SIGINT,
        )
        config_factory = mock.Mock()
        diagnostics = []

        result = NativeLauncherCoordinator(
            adapter,
            config_factory=config_factory,
            dependencies_factory=mock.Mock(),
            thread_factory=mock.Mock(),
            diagnostic_sink=diagnostics.append,
        ).run()

        self.assertEqual(result, 1)
        config_factory.assert_not_called()
        self.assertEqual(adapter.messages, [])
        self.assertIn(
            "chooser_cancellation_failed",
            [diagnostic.code for diagnostic in diagnostics],
        )

    def test_signal_observed_before_panel_presentation_skips_chooser(self) -> None:
        intent = SignalIntent(SignalKind.SIGINT, signal.SIGINT, 130)
        _ControlledSignalCoordinator.instances = []
        _ControlledSignalCoordinator.capture_results = [intent]
        adapter = _Adapter(str(self.root))
        config_factory = mock.Mock()

        with mock.patch.object(
            coordinator_module,
            "ProcessSignalCoordinator",
            _ControlledSignalCoordinator,
        ):
            result = NativeLauncherCoordinator(
                adapter,
                config_factory=config_factory,
                dependencies_factory=mock.Mock(),
                thread_factory=mock.Mock(),
            ).run()

        self.assertEqual(result, 130)
        self.assertNotIn("adapter.choose", adapter.events)
        config_factory.assert_not_called()
        self.assertEqual(len(_ControlledSignalCoordinator.instances), 1)
        signals = _ControlledSignalCoordinator.instances[0]
        self.assertEqual((signals.installs, signals.closes), (1, 1))

    def test_signal_observed_as_selection_returns_discards_selected_root(self) -> None:
        intent = SignalIntent(SignalKind.SIGTERM, signal.SIGTERM, 143)
        _ControlledSignalCoordinator.instances = []
        _ControlledSignalCoordinator.capture_results = [None, intent]
        adapter = _Adapter(str(self.root))
        config_factory = mock.Mock()

        with mock.patch.object(
            coordinator_module,
            "ProcessSignalCoordinator",
            _ControlledSignalCoordinator,
        ):
            result = NativeLauncherCoordinator(
                adapter,
                config_factory=config_factory,
                dependencies_factory=mock.Mock(),
                thread_factory=mock.Mock(),
            ).run()

        self.assertEqual(result, 143)
        self.assertEqual(adapter.events.count("adapter.choose"), 1)
        config_factory.assert_not_called()
        self.assertEqual(len(_ControlledSignalCoordinator.instances), 1)
        signals = _ControlledSignalCoordinator.instances[0]
        self.assertEqual((signals.installs, signals.closes), (1, 1))

    def test_keyboard_interrupt_before_worker_creation_returns_130(self) -> None:
        adapter = _Adapter(None, choose_error=KeyboardInterrupt())
        dependencies_factory = mock.Mock()
        thread_factory = mock.Mock()
        stderr = StringIO()

        with redirect_stderr(stderr):
            result = NativeLauncherCoordinator(
                adapter,
                dependencies_factory=dependencies_factory,
                thread_factory=thread_factory,
            ).run()

        self.assertEqual(result, 130)
        dependencies_factory.assert_not_called()
        thread_factory.assert_not_called()
        self.assertEqual(stderr.getvalue(), "")

    def test_keyboard_interrupt_before_worker_start_leaves_no_worker(self) -> None:
        adapter = _InterruptingAdapter(
            str(self.root),
            interrupt_before_callbacks=True,
        )
        coordinator = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=lambda _browser: self.dependencies(),
        )

        result = coordinator.run()

        self.assertEqual(result, 130)
        self.assertIsNotNone(coordinator._worker)
        self.assertIsNone(coordinator._worker.ident)
        self.assertFalse(coordinator._worker.is_alive())
        self.assertEqual(self.server.exit_requests, 0)
        self.assertTrue(coordinator._terminal_result.done())
        self.assertEqual(coordinator._terminal_result.result().exit_code, 130)

    def test_interrupt_before_readiness_uses_pending_graceful_stop(self) -> None:
        server_created = Event()
        server = _BlockingServer()
        adapter = _TriggeredInterruptAdapter(str(self.root), server_created)

        def server_factory(_app: object, _port: int) -> _BlockingServer:
            server_created.set()
            return server

        def readiness_factory(_health, _launch, _stop, _request_exit):
            if not server.exit_requested.wait(2):
                raise AssertionError("pending interrupt stop was not dispatched")
            return _Observer()

        dependencies = LauncherDependencies(
            config_factory=WebAppConfig,
            socket_binder=lambda _port: self.socket,
            app_factory=lambda _config: object(),
            server_factory=server_factory,
            readiness_factory=readiness_factory,
            manual_browser_bootstrap=lambda _url: None,
        )
        coordinator = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=lambda _browser: dependencies,
        )

        result = coordinator.run()

        self.assertEqual(result, 130)
        self.assertEqual(server.exit_requests, 1)
        self.assertTrue(self.socket.closed)
        self.assertIsNotNone(coordinator._worker)
        self.assertFalse(coordinator._worker.is_alive())

    def test_interrupt_after_worker_completed_preserves_terminal_result(self) -> None:
        adapter = _CompletedWorkerInterruptAdapter(str(self.root))
        coordinator = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=lambda _browser: self.dependencies(),
        )

        result = coordinator.run()

        self.assertEqual(result, 0)
        self.assertTrue(coordinator._terminal_result.done())
        self.assertEqual(self.server.exit_requests, 0)
        self.assertIsNotNone(coordinator._worker)
        self.assertFalse(coordinator._worker.is_alive())

    def test_admission_failure_creates_no_worker(self) -> None:
        adapter = _Adapter(str(self.root / "missing-private-folder"))
        thread_factory = mock.Mock()
        dependencies_factory = mock.Mock()

        result = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=dependencies_factory,
            thread_factory=thread_factory,
        ).run()

        self.assertEqual(result, 1)
        thread_factory.assert_not_called()
        dependencies_factory.assert_not_called()
        self.assertEqual(adapter.messages[-1].message, "That folder can’t be used.")
        self.assertNotIn(str(self.root), adapter.messages[-1].message)

    def test_startup_construction_failure_is_safe_and_creates_no_worker(self) -> None:
        adapter = _Adapter(str(self.root))
        thread_factory = mock.Mock()

        result = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=mock.Mock(
                side_effect=RuntimeError("private dependency detail")
            ),
            thread_factory=thread_factory,
        ).run()

        self.assertEqual(result, 1)
        thread_factory.assert_not_called()
        self.assertEqual(adapter.messages[-1].message, "Loci Flow couldn’t start.")
        self.assertNotIn("private dependency", adapter.messages[-1].message)

    def test_worker_receives_already_admitted_frozen_configuration(self) -> None:
        adapter = _Adapter(str(self.root))
        captured: list[WebAppConfig] = []
        dependencies = self.dependencies()
        original_app_factory = dependencies.app_factory
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=dependencies.socket_binder,
            app_factory=lambda config: (
                captured.append(config),
                original_app_factory(config),
            )[1],
            server_factory=dependencies.server_factory,
            readiness_factory=dependencies.readiness_factory,
            manual_browser_bootstrap=dependencies.manual_browser_bootstrap,
        )

        result = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=lambda _browser: dependencies,
        ).run()

        self.assertEqual(result, 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].root, self.root.resolve())

    def test_worker_exception_becomes_one_safe_typed_result(self) -> None:
        adapter = _Adapter(str(self.root))
        dependencies = self.dependencies()
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=mock.Mock(side_effect=RuntimeError(
                "private traceback token-should-not-appear"
            )),
            app_factory=dependencies.app_factory,
            server_factory=dependencies.server_factory,
            readiness_factory=dependencies.readiness_factory,
            manual_browser_bootstrap=dependencies.manual_browser_bootstrap,
        )

        result = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=lambda _browser: dependencies,
        ).run()

        self.assertEqual(result, 1)
        self.assertEqual(len(adapter.messages), 1)
        self.assertEqual(adapter.messages[0].message, "Loci Flow couldn’t start.")
        self.assertNotIn("private traceback", adapter.messages[0].message)
        self.assertEqual(adapter.events.count("adapter.dispatch"), 2)

    def test_event_loop_services_browser_dispatch_before_worker_completion(self) -> None:
        adapter = _Adapter(str(self.root))
        server = _BlockingServer()

        def dependencies_factory(browser_opener: Callable[[str], bool]):
            def readiness_factory(_health, _launch, _stop, request_exit):
                def observe() -> LauncherError | None:
                    self.assertTrue(adapter.loop_started.is_set())
                    self.assertTrue(browser_opener("opaque-private-url"))
                    return None

                return _AsyncObserver(observe, request_exit)

            return LauncherDependencies(
                config_factory=WebAppConfig,
                socket_binder=lambda _port: self.socket,
                app_factory=lambda _config: object(),
                server_factory=lambda _app, _port: server,
                readiness_factory=readiness_factory,
                manual_browser_bootstrap=lambda _url: None,
            )

        coordinator = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=dependencies_factory,
        )
        result = coordinator.run()

        self.assertEqual(result, 0)
        self.assertLess(
            adapter.events.index("adapter.run_loop"),
            adapter.events.index("adapter.browser.request"),
        )
        self.assertLess(
            adapter.events.index("adapter.browser.open"),
            adapter.events.index("adapter.stop_loop"),
        )
        self.assertTrue(self.socket.closed)
        self.assertTrue(adapter.loop_stopped.is_set())

    def test_running_keyboard_interrupt_stops_once_waits_and_joins(self) -> None:
        adapter = _InterruptingAdapter(str(self.root))
        adapter.interrupt_after = adapter.browser_opened
        server = _BlockingServer()
        terminal = _CountingFuture()

        def dependencies_factory(browser_opener: Callable[[str], bool]):
            return LauncherDependencies(
                config_factory=WebAppConfig,
                socket_binder=lambda _port: self.socket,
                app_factory=lambda _config: object(),
                server_factory=lambda _app, _port: server,
                readiness_factory=(
                    lambda _health, _launch, _stop, _request_exit: (
                        _PassiveBrowserObserver(browser_opener)
                    )
                ),
                manual_browser_bootstrap=lambda _url: None,
            )

        with mock.patch.object(coordinator_module, "Future", return_value=terminal):
            coordinator = NativeLauncherCoordinator(
                adapter,
                dependencies_factory=dependencies_factory,
            )

        stderr = StringIO()
        with redirect_stderr(stderr):
            result = coordinator.run()

        self.assertEqual(result, 130)
        self.assertEqual(server.exit_requests, 1)
        self.assertTrue(self.socket.closed)
        self.assertTrue(terminal.done())
        self.assertEqual(terminal.set_calls, 1)
        self.assertIsNotNone(coordinator._worker)
        self.assertFalse(coordinator._worker.is_alive())
        self.assertFalse(coordinator._stop_controller.request_stop())
        self.assertEqual(server.exit_requests, 1)
        self.assertFalse(adapter.stopped)
        self.assertEqual(
            adapter.dispatched_callbacks.count("_complete_on_main"),
            1,
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_interrupt_debug_diagnostic_is_concise_and_safe(self) -> None:
        adapter = _Adapter(None, choose_error=KeyboardInterrupt())
        output = StringIO()
        sink = development_diagnostic_sink(
            environ={"LOCI_FLOW_NATIVE_DEBUG": "1"},
            stream=output,
        )

        result = NativeLauncherCoordinator(
            adapter,
            diagnostic_sink=sink,
        ).run()

        diagnostic = output.getvalue()
        self.assertEqual(result, 130)
        self.assertIn("phase: native_event_loop", diagnostic)
        self.assertIn("code: interrupted", diagnostic)
        self.assertNotIn("Traceback", diagnostic)
        self.assertNotIn("KeyboardInterrupt", diagnostic)
        self.assertNotIn(str(self.root), diagnostic)
        self.assertNotIn("/launch/", diagnostic)

    def test_browser_failure_preserves_code_and_cleans_up(self) -> None:
        adapter = _Adapter(str(self.root))
        adapter.browser_result = False
        server = _BlockingServer()

        def dependencies_factory(browser_opener: Callable[[str], bool]):
            def readiness_factory(_health, _launch, _stop, request_exit):
                return _AsyncObserver(
                    lambda: (
                        None
                        if browser_opener("opaque-private-url")
                        else LauncherError("browser_open_failed", "safe")
                    ),
                    request_exit,
                )

            return LauncherDependencies(
                config_factory=WebAppConfig,
                socket_binder=lambda _port: self.socket,
                app_factory=lambda _config: object(),
                server_factory=lambda _app, _port: server,
                readiness_factory=readiness_factory,
                manual_browser_bootstrap=lambda _url: None,
            )

        coordinator = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=dependencies_factory,
        )
        result = coordinator.run()

        self.assertEqual(result, 1)
        self.assertEqual(adapter.messages[-1].message, "Loci Flow couldn’t open your browser.")
        self.assertTrue(self.socket.closed)
        self.assertEqual(server.exit_requests, 1)
        self.assertIsNotNone(coordinator._worker)
        self.assertFalse(coordinator._worker.is_alive())
        self.assertEqual(adapter.dispatched_callbacks.count("_complete_on_main"), 1)

    def test_readiness_failure_preserves_code_and_cleans_up(self) -> None:
        adapter = _Adapter(str(self.root))
        server = _BlockingServer()

        def dependencies_factory(_browser_opener: Callable[[str], bool]):
            def readiness_factory(_health, _launch, _stop, request_exit):
                return _AsyncObserver(
                    lambda: LauncherError("readiness_failed", "safe"),
                    request_exit,
                )

            return LauncherDependencies(
                config_factory=WebAppConfig,
                socket_binder=lambda _port: self.socket,
                app_factory=lambda _config: object(),
                server_factory=lambda _app, _port: server,
                readiness_factory=readiness_factory,
                manual_browser_bootstrap=lambda _url: None,
            )

        coordinator = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=dependencies_factory,
        )
        result = coordinator.run()

        self.assertEqual(result, 1)
        self.assertIn("confirm", adapter.messages[-1].message)
        self.assertTrue(self.socket.closed)
        self.assertIsNotNone(coordinator._worker)
        self.assertFalse(coordinator._worker.is_alive())

    def test_typed_bind_failure_is_not_remapped_to_app_creation(self) -> None:
        adapter = _Adapter(str(self.root))
        dependencies = self.dependencies()
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=mock.Mock(side_effect=OSError("private bind detail")),
            app_factory=dependencies.app_factory,
            server_factory=dependencies.server_factory,
            readiness_factory=dependencies.readiness_factory,
            manual_browser_bootstrap=dependencies.manual_browser_bootstrap,
        )

        result = NativeLauncherCoordinator(
            adapter,
            dependencies_factory=lambda _browser: dependencies,
        ).run()

        self.assertEqual(result, 1)
        self.assertIn("private local server", adapter.messages[-1].message)

    def test_debug_diagnostics_are_disabled_by_default(self) -> None:
        self.assertIsNone(development_diagnostic_sink(environ={}))
        adapter = _Adapter(str(self.root))
        dependencies = self.dependencies()
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=mock.Mock(side_effect=RuntimeError("private detail")),
            app_factory=dependencies.app_factory,
            server_factory=dependencies.server_factory,
            readiness_factory=dependencies.readiness_factory,
            manual_browser_bootstrap=dependencies.manual_browser_bootstrap,
        )
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = NativeLauncherCoordinator(
                adapter,
                dependencies_factory=lambda _browser: dependencies,
            ).run()

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "")

    def test_packaged_sink_projects_launch_and_shutdown_failures_boundedly(
        self,
    ) -> None:
        paths = self.application_paths()
        sink = PackagedDiagnosticSink(
            paths,
            identity=PackagedReleaseIdentity("0.1.0-beta.1", 1),
        )
        coordinator = NativeLauncherCoordinator(
            _Adapter(None),
            diagnostic_sink=sink,
        )

        coordinator._record_launch_diagnostic(
            coordinator_module.LaunchPhase.WAITING_FOR_READY,
            LauncherError("readiness_failed", "private safe message"),
            RuntimeError(f"private path {self.root}"),
        )
        coordinator._record_launch_diagnostic(
            coordinator_module.LaunchPhase.BROWSER_BOOTSTRAPPED,
            LauncherError("browser_open_failed", "private URL"),
            RuntimeError("http://127.0.0.1:1234/launch/private-token"),
        )
        coordinator._record_shutdown_diagnostic(
            "worker_failed",
            RuntimeError("private shutdown detail"),
        )

        content = (paths.logs_directory / "launcher.log").read_text(
            encoding="ascii"
        )
        payloads = [json.loads(line) for line in content.splitlines()]
        self.assertEqual(
            [payload["code"] for payload in payloads],
            [
                "readiness_failed",
                "browser_launch_failed",
                "coordinated_shutdown_failed",
            ],
        )
        self.assertNotIn(str(self.root), content)
        self.assertNotIn("private-token", content)
        self.assertNotIn("private shutdown", content)
        self.assertNotIn("Traceback", content)

    def test_native_appkit_event_trace_is_safe_and_debug_only(self) -> None:
        output = StringIO()
        sink = development_diagnostic_sink(
            environ={"LOCI_FLOW_NATIVE_DEBUG": "1"},
            stream=output,
        )
        coordinator = NativeLauncherCoordinator(
            _Adapter(None),
            diagnostic_sink=sink,
        )

        coordinator._record_native_event("application_should_terminate")

        trace = output.getvalue()
        self.assertIn("phase: native_appkit", trace)
        self.assertIn("code: application_should_terminate", trace)
        self.assertNotIn("Traceback", trace)
        self.assertNotIn("/Users/", trace)
        self.assertNotIn("/launch/", trace)
        self.assertNotIn("0x", trace)

    def test_debug_diagnostics_report_safe_details_and_redact_secrets(self) -> None:
        adapter = _Adapter(str(self.root))
        output = StringIO()
        sink = development_diagnostic_sink(
            environ={"LOCI_FLOW_NATIVE_DEBUG": "1"},
            stream=output,
        )
        self.assertIsNotNone(sink)
        token = "t" * 32
        secret = "s" * 32
        private_url = f"http://127.0.0.1:48191/launch/{token}"

        def config_factory(path: Path) -> WebAppConfig:
            return WebAppConfig(
                path,
                session_secret=secret,
                launch_token=token,
                testing=True,
            )

        dependencies = self.dependencies()
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=mock.Mock(
                side_effect=RuntimeError(
                    f"root={self.root} token={token} secret={secret} url={private_url}"
                )
            ),
            app_factory=dependencies.app_factory,
            server_factory=dependencies.server_factory,
            readiness_factory=dependencies.readiness_factory,
            manual_browser_bootstrap=dependencies.manual_browser_bootstrap,
        )

        result = NativeLauncherCoordinator(
            adapter,
            config_factory=config_factory,
            dependencies_factory=lambda _browser: dependencies,
            diagnostic_sink=sink,
        ).run()

        diagnostic = output.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("phase: worker", diagnostic)
        self.assertIn("code: app_creation_failed", diagnostic)
        self.assertIn("exception: RuntimeError", diagnostic)
        self.assertIn("Traceback", diagnostic)
        self.assertNotIn(str(self.root), diagnostic)
        self.assertNotIn(token, diagnostic)
        self.assertNotIn(secret, diagnostic)
        self.assertNotIn(private_url, diagnostic)

    def test_every_public_launch_error_maps_to_elementary_safe_text(self) -> None:
        expected = {
            "root_symlink": "That folder can’t be used.",
            "root_inaccessible": "Loci Flow can’t access that folder.",
            "app_creation_failed": "Loci Flow couldn’t start.",
            "bind_failed": "Loci Flow couldn’t start its private local server.",
            "readiness_failed": (
                "Loci Flow couldn’t confirm that its local server was ready."
            ),
            "browser_open_failed": "Loci Flow couldn’t open your browser.",
        }
        forbidden = [str(self.root), "t" * 32, "s" * 32, "Traceback", "repr("]
        for code, message in expected.items():
            with self.subTest(code=code):
                projection = native_message_for(code)
                self.assertEqual(projection.message, message)
                for private in forbidden:
                    self.assertNotIn(private, projection.message)

    def test_coordinator_source_has_no_child_process_or_product_owner_import(self) -> None:
        source = Path(__import__(
            "organizer.macos_launcher.coordinator",
            fromlist=["__file__"],
        ).__file__).read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "organizer.scanner",
            "organizer.executor",
            "execution_service",
            "movement_service",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
