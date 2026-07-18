from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from organizer.web.config import WebAppConfig
from organizer.web.launcher import (
    LaunchPhase,
    LauncherDependencies,
    launch,
)


class _Socket:
    def __init__(self, events: list[str], port: int = 48123) -> None:
        self.events = events
        self.port = port
        self.closed = False

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    def close(self) -> None:
        self.events.append("socket.close")
        self.closed = True


class _Server:
    def __init__(
        self,
        events: list[str],
        side_effect: BaseException | None = None,
    ) -> None:
        self.events = events
        self.side_effect = side_effect
        self.sockets: list[_Socket] | None = None

    def run(self, *, sockets: list[_Socket]) -> None:
        self.events.append("server.run")
        self.sockets = sockets
        if self.side_effect is not None:
            raise self.side_effect


class _Observer:
    def __init__(
        self,
        events: list[str],
        start_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.joined = False

    def start(self) -> None:
        self.events.append("readiness.start")
        if self.start_error is not None:
            raise self.start_error
        self.events.append("health.ready")
        self.events.append("browser.open")

    def join(self) -> None:
        self.events.append("readiness.join")
        self.joined = True


class WebLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.events: list[str] = []
        self.socket = _Socket(self.events)
        self.server = _Server(self.events)
        self.observer = _Observer(self.events)
        self.seen_launch_url: str | None = None

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def dependencies(self) -> LauncherDependencies:
        def config_factory(root: Path) -> WebAppConfig:
            self.events.append("config")
            return WebAppConfig(
                root,
                session_secret="s" * 32,
                launch_token="t" * 32,
                testing=True,
            )

        def bind(port: int | None) -> _Socket:
            self.events.append(f"bind:{port}")
            return self.socket

        def app_factory(config: WebAppConfig) -> object:
            self.events.append("app")
            return {"root": config.root}

        def server_factory(app: object, port: int) -> _Server:
            self.events.append(f"server:{port}")
            return self.server

        def readiness_factory(
            health_url: str,
            launch_url: str,
            stop_event: object,
        ) -> _Observer:
            self.events.append("readiness.create")
            self.assertNotIn("t" * 32, health_url)
            self.seen_launch_url = launch_url
            return self.observer

        def manual_browser_bootstrap(launch_url: str) -> None:
            self.events.append("manual.browser")
            self.seen_launch_url = launch_url

        return LauncherDependencies(
            config_factory=config_factory,
            socket_binder=bind,
            app_factory=app_factory,
            server_factory=server_factory,
            readiness_factory=readiness_factory,
            manual_browser_bootstrap=manual_browser_bootstrap,
        )

    def test_normal_launch_has_explicit_ordered_phases_and_cleanup(self) -> None:
        result = launch(lambda: self.root, dependencies=self.dependencies())

        self.assertTrue(result.succeeded)
        self.assertEqual(
            result.phases,
            (
                LaunchPhase.SELECTING_ROOT,
                LaunchPhase.ROOT_ADMITTED,
                LaunchPhase.SOCKET_BOUND,
                LaunchPhase.APP_CREATED,
                LaunchPhase.WAITING_FOR_READY,
                LaunchPhase.BROWSER_BOOTSTRAPPED,
                LaunchPhase.RUNNING,
                LaunchPhase.STOPPING,
                LaunchPhase.STOPPED,
            ),
        )
        self.assertLess(
            self.events.index("health.ready"),
            self.events.index("browser.open"),
        )
        self.assertEqual(self.server.sockets, [self.socket])
        self.assertTrue(self.socket.closed)
        self.assertTrue(self.observer.joined)
        self.assertEqual(
            self.seen_launch_url,
            "http://127.0.0.1:48123/launch/" + "t" * 32,
        )

    def test_cancelled_selection_is_a_successful_side_effect_free_noop(self) -> None:
        dependencies = self.dependencies()
        dependencies = LauncherDependencies(
            config_factory=mock.Mock(wraps=dependencies.config_factory),
            socket_binder=mock.Mock(wraps=dependencies.socket_binder),
            app_factory=mock.Mock(wraps=dependencies.app_factory),
            server_factory=mock.Mock(wraps=dependencies.server_factory),
            readiness_factory=mock.Mock(wraps=dependencies.readiness_factory),
            manual_browser_bootstrap=mock.Mock(
                wraps=dependencies.manual_browser_bootstrap
            ),
        )

        result = launch(lambda: None, dependencies=dependencies)

        self.assertTrue(result.succeeded)
        self.assertEqual(
            result.phases,
            (LaunchPhase.SELECTING_ROOT, LaunchPhase.STOPPED),
        )
        dependencies.config_factory.assert_not_called()
        dependencies.socket_binder.assert_not_called()
        dependencies.app_factory.assert_not_called()
        dependencies.server_factory.assert_not_called()
        dependencies.readiness_factory.assert_not_called()
        dependencies.manual_browser_bootstrap.assert_not_called()
        self.assertEqual(self.events, [])

    def test_root_rejection_prevents_every_later_phase(self) -> None:
        dependencies = self.dependencies()
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=mock.Mock(wraps=dependencies.socket_binder),
            app_factory=mock.Mock(wraps=dependencies.app_factory),
            server_factory=mock.Mock(wraps=dependencies.server_factory),
            readiness_factory=mock.Mock(wraps=dependencies.readiness_factory),
            manual_browser_bootstrap=mock.Mock(
                wraps=dependencies.manual_browser_bootstrap
            ),
        )

        result = launch(
            lambda: self.root / "personal-missing-name",
            dependencies=dependencies,
        )

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error.code, "root_missing")
        self.assertNotIn(str(self.root), result.error.safe_message)
        self.assertEqual(
            result.phases,
            (LaunchPhase.SELECTING_ROOT, LaunchPhase.FAILED),
        )
        dependencies.socket_binder.assert_not_called()
        dependencies.app_factory.assert_not_called()
        dependencies.server_factory.assert_not_called()
        dependencies.readiness_factory.assert_not_called()

    def test_bind_failure_prevents_app_browser_and_server(self) -> None:
        dependencies = self.dependencies()
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=mock.Mock(side_effect=OSError("private bind detail")),
            app_factory=mock.Mock(wraps=dependencies.app_factory),
            server_factory=mock.Mock(wraps=dependencies.server_factory),
            readiness_factory=mock.Mock(wraps=dependencies.readiness_factory),
            manual_browser_bootstrap=dependencies.manual_browser_bootstrap,
        )

        result = launch(lambda: self.root, dependencies=dependencies)

        self.assertEqual(result.error.code, "bind_failed")
        self.assertNotIn("private bind detail", result.error.safe_message)
        dependencies.app_factory.assert_not_called()
        dependencies.server_factory.assert_not_called()
        dependencies.readiness_factory.assert_not_called()

    def test_app_creation_failure_closes_socket_and_is_terminal(self) -> None:
        dependencies = self.dependencies()
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=dependencies.socket_binder,
            app_factory=mock.Mock(side_effect=RuntimeError("private app detail")),
            server_factory=mock.Mock(wraps=dependencies.server_factory),
            readiness_factory=mock.Mock(wraps=dependencies.readiness_factory),
            manual_browser_bootstrap=dependencies.manual_browser_bootstrap,
        )

        result = launch(lambda: self.root, dependencies=dependencies)

        self.assertEqual(result.error.code, "app_creation_failed")
        self.assertEqual(result.phases[-2:], (LaunchPhase.STOPPING, LaunchPhase.FAILED))
        self.assertTrue(self.socket.closed)
        dependencies.server_factory.assert_not_called()
        dependencies.readiness_factory.assert_not_called()

    def test_readiness_start_failure_closes_socket_and_joins_observer(self) -> None:
        self.observer = _Observer(
            self.events,
            start_error=RuntimeError("private readiness detail"),
        )

        result = launch(lambda: self.root, dependencies=self.dependencies())

        self.assertEqual(result.error.code, "readiness_failed")
        self.assertNotIn("private readiness detail", result.error.safe_message)
        self.assertNotIn("server.run", self.events)
        self.assertTrue(self.observer.joined)
        self.assertTrue(self.socket.closed)

    def test_no_browser_bootstraps_the_manual_url_without_readiness(self) -> None:
        dependencies = self.dependencies()
        readiness = mock.Mock(wraps=dependencies.readiness_factory)
        dependencies = LauncherDependencies(
            config_factory=dependencies.config_factory,
            socket_binder=dependencies.socket_binder,
            app_factory=dependencies.app_factory,
            server_factory=dependencies.server_factory,
            readiness_factory=readiness,
            manual_browser_bootstrap=dependencies.manual_browser_bootstrap,
        )

        result = launch(
            lambda: self.root,
            dependencies=dependencies,
            open_browser=False,
        )

        self.assertTrue(result.succeeded)
        readiness.assert_not_called()
        self.assertIn("manual.browser", self.events)
        self.assertNotIn(LaunchPhase.WAITING_FOR_READY, result.phases)

    def test_keyboard_interrupt_still_performs_clean_shutdown(self) -> None:
        self.server = _Server(self.events, KeyboardInterrupt())

        result = launch(lambda: self.root, dependencies=self.dependencies())

        self.assertTrue(result.succeeded)
        self.assertEqual(result.phases[-1], LaunchPhase.STOPPED)
        self.assertTrue(self.observer.joined)
        self.assertTrue(self.socket.closed)

    def test_server_failure_is_safe_terminal_and_leaks_no_launch_secret(self) -> None:
        self.server = _Server(
            self.events,
            RuntimeError(
                "private detail " + "t" * 32 + " " + "s" * 32
            ),
        )

        result = launch(lambda: self.root, dependencies=self.dependencies())

        self.assertEqual(result.error.code, "server_failed")
        self.assertEqual(result.phases[-1], LaunchPhase.FAILED)
        self.assertNotIn("t" * 32, result.error.safe_message)
        self.assertNotIn("s" * 32, result.error.safe_message)
        self.assertNotIn(str(self.root), result.error.safe_message)
        self.assertTrue(self.observer.joined)
        self.assertTrue(self.socket.closed)

    def test_launcher_does_not_create_or_manage_child_processes(self) -> None:
        source = Path(__import__(
            "organizer.web.launcher",
            fromlist=["__file__"],
        ).__file__).read_text(encoding="utf-8")

        self.assertNotIn("subprocess", source)
        self.assertNotIn("multiprocessing", source)


if __name__ == "__main__":
    unittest.main()
