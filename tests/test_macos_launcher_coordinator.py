from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from queue import Empty, Queue
import tempfile
from threading import Event, Thread, current_thread, main_thread
import unittest
from unittest import mock

from organizer.macos_launcher.coordinator import (
    NativeLauncherCoordinator,
    NativeMessage,
    development_diagnostic_sink,
    native_message_for,
)
from organizer.macos_launcher import coordinator as coordinator_module
from organizer.web.config import WebAppConfig
from organizer.web.launcher import LauncherDependencies, LauncherError


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

    def initialize(self) -> None:
        self.events.append("adapter.initialize")

    def choose_directory(self) -> str | None:
        self.events.append("adapter.choose")
        if self.choose_error is not None:
            raise self.choose_error
        return self.selected

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


class _Socket:
    def __init__(self) -> None:
        self.closed = False

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 48191)

    def close(self) -> None:
        self.closed = True


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
