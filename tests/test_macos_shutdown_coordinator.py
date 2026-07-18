from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from threading import Event, Lock
import unittest
from unittest import mock

from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.macos_launcher.shutdown_coordinator import (
    NativeShutdownCoordinator,
    NativeShutdownState,
)
from organizer.web.launcher import LaunchRuntimeHandle, LaunchStopController


class _Adapter:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.callbacks: list[Callable[[], None]] = []
        self.callback_ready = Event()
        self._callback_lock = Lock()
        self.replies: list[bool] = []
        self.panel_error: BaseException | None = None
        self.dispatch_error: BaseException | None = None
        self.reply_error: BaseException | None = None

    def dispatch_to_main(self, callback: Callable[[], None]) -> None:
        if self.dispatch_error is not None:
            raise self.dispatch_error
        with self._callback_lock:
            self.callbacks.append(callback)
            self.callback_ready.set()

    def run_next_callback(self) -> None:
        self.assert_callback_ready()
        with self._callback_lock:
            callback = self.callbacks.pop(0)
            if not self.callbacks:
                self.callback_ready.clear()
        callback()

    def assert_callback_ready(self) -> None:
        if not self.callback_ready.wait(1):
            raise AssertionError("shutdown callback was not dispatched")

    def show_finishing_status(self) -> None:
        self.events.append("panel.show")
        if self.panel_error is not None:
            raise self.panel_error

    def close_finishing_status(self) -> None:
        self.events.append("panel.close")

    def show_shutdown_failure(self) -> None:
        self.events.append("failure.show")

    def reply_to_termination(self, allow: bool) -> None:
        self.events.append(f"reply:{allow}")
        self.replies.append(allow)
        if self.reply_error is not None:
            raise self.reply_error

    def set_launch_running(self, running: bool) -> None:
        self.events.append(f"running:{running}")


class NativeShutdownCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = _Adapter()
        self.stop_controller = LaunchStopController()
        self.stop_calls: list[str] = []
        self.stop_controller.attach(lambda: self.stop_calls.append("stop"))
        self.runtime_handle = LaunchRuntimeHandle(self.stop_controller)
        self.terminal: Future[object] = Future()
        self.diagnostics: list[tuple[str, BaseException | None]] = []
        self.shutdown = NativeShutdownCoordinator(
            self.adapter,
            self.runtime_handle,
            self.terminal,
            diagnostic=lambda code, error: self.diagnostics.append((code, error)),
        )

    def test_idle_quit_closes_admission_and_replies_after_terminal(self) -> None:
        lifecycle = RuntimeLifecycle()
        self.runtime_handle.publish_lifecycle(lifecycle)

        self.assertTrue(self.shutdown.request_termination())
        self.assertTrue(self.shutdown.request_termination())

        self.assertTrue(lifecycle.snapshot().is_stopping)
        self.assertEqual(self.stop_calls, ["stop"])
        self.assertIsNone(self.shutdown.waiter)
        self.assertNotIn("panel.show", self.adapter.events)
        self.assertFalse(self.terminal.done())

        self.terminal.set_result(object())
        self.shutdown.worker_completed(succeeded=True)

        self.assertEqual(self.shutdown.state, NativeShutdownState.COMPLETED)
        self.assertEqual(self.adapter.replies, [True])
        self.assertEqual(self.adapter.events[-1], "reply:True")

    def test_active_quit_waits_without_polling_then_stops_and_replies(self) -> None:
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        self.runtime_handle.publish_lifecycle(lifecycle)

        self.assertTrue(self.shutdown.request_termination())

        self.assertEqual(self.shutdown.state, NativeShutdownState.WAITING_FOR_IDLE)
        self.assertEqual(self.adapter.events, ["panel.show"])
        self.assertEqual(self.stop_calls, [])
        with self.assertRaises(ApplicationStoppingError):
            lifecycle.begin_operation(RuntimeOperationKind.STATE_MUTATION)
        waiter = self.shutdown.waiter
        self.assertIsNotNone(waiter)
        assert waiter is not None
        self.assertFalse(waiter.daemon)
        self.assertTrue(waiter.is_alive())

        lease.finish()
        self.adapter.run_next_callback()

        self.assertEqual(self.stop_calls, ["stop"])
        self.assertEqual(self.shutdown.state, NativeShutdownState.WAITING_FOR_WORKER)
        self.terminal.set_result(object())
        self.shutdown.worker_completed(succeeded=True)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(
            self.adapter.events,
            ["panel.show", "panel.close", "running:False", "reply:True"],
        )

    def test_quit_before_publication_uses_real_lifecycle(self) -> None:
        self.assertTrue(self.shutdown.request_termination())
        waiter = self.shutdown.waiter
        self.assertIsNotNone(waiter)
        lifecycle = RuntimeLifecycle()

        self.runtime_handle.publish_lifecycle(lifecycle)
        self.adapter.run_next_callback()

        self.assertTrue(lifecycle.snapshot().is_stopping)
        self.assertEqual(self.stop_calls, ["stop"])
        self.assertNotIn("panel.show", self.adapter.events)
        self.terminal.set_result(object())
        self.shutdown.worker_completed(succeeded=True)
        assert waiter is not None
        self.assertFalse(waiter.is_alive())

    def test_every_safe_operation_kind_finishes_before_stop(self) -> None:
        kinds = (
            RuntimeOperationKind.SCAN,
            RuntimeOperationKind.ORGANIZATION_MOVEMENT,
            RuntimeOperationKind.DUPLICATE_BIN_MOVEMENT,
            RuntimeOperationKind.DUPLICATE_BIN_RESTORE,
            RuntimeOperationKind.ACTIVITY_UNDO,
            RuntimeOperationKind.STATE_MUTATION,
        )
        for kind in kinds:
            with self.subTest(kind=kind):
                self.setUp()
                lifecycle = RuntimeLifecycle()
                lease = lifecycle.begin_operation(kind)
                self.runtime_handle.publish_lifecycle(lifecycle)

                self.shutdown.request_termination()
                self.assertEqual(self.stop_calls, [])
                lease.finish()
                self.adapter.run_next_callback()

                self.assertEqual(self.stop_calls, ["stop"])
                self.terminal.set_result(object())
                self.shutdown.worker_completed(succeeded=True)
                self.assertEqual(self.adapter.replies, [True])

    def test_repeated_active_quit_reuses_waiter_panel_stop_and_reply(self) -> None:
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.STATE_MUTATION)
        self.runtime_handle.publish_lifecycle(lifecycle)

        for _ in range(5):
            self.assertTrue(self.shutdown.request_termination())
        waiter = self.shutdown.waiter

        self.assertEqual(self.adapter.events.count("panel.show"), 1)
        lease.finish()
        self.adapter.run_next_callback()
        self.terminal.set_result(object())
        self.shutdown.worker_completed(succeeded=True)

        self.assertIs(self.shutdown.waiter, waiter)
        self.assertEqual(self.stop_calls, ["stop"])
        self.assertEqual(self.adapter.replies, [True])

    def test_panel_failure_does_not_weaken_safe_shutdown(self) -> None:
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        self.runtime_handle.publish_lifecycle(lifecycle)
        self.adapter.panel_error = RuntimeError("private panel detail")

        self.assertTrue(self.shutdown.request_termination())
        lease.finish()
        self.adapter.run_next_callback()
        self.terminal.set_result(object())
        self.shutdown.worker_completed(succeeded=True)

        self.assertEqual(self.adapter.replies, [True])
        self.assertEqual(self.diagnostics[0][0], "finishing_panel_failed")
        self.assertNotIn("panel.close", self.adapter.events)

    def test_stop_failure_cancels_termination_without_false_success(self) -> None:
        controller = LaunchStopController()
        controller.attach(lambda: (_ for _ in ()).throw(RuntimeError("private")))
        handle = LaunchRuntimeHandle(controller)
        lifecycle = RuntimeLifecycle()
        handle.publish_lifecycle(lifecycle)
        shutdown = NativeShutdownCoordinator(self.adapter, handle, self.terminal)

        self.assertFalse(shutdown.request_termination())

        self.assertEqual(shutdown.state, NativeShutdownState.FAILED)
        self.assertEqual(self.adapter.replies, [])
        self.assertIn("failure.show", self.adapter.events)

    def test_worker_exit_while_active_is_not_reported_as_success(self) -> None:
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        self.runtime_handle.publish_lifecycle(lifecycle)
        self.shutdown.request_termination()
        self.terminal.set_result(object())

        self.shutdown.worker_completed(succeeded=True)

        self.assertEqual(self.shutdown.state, NativeShutdownState.FAILED)
        self.assertEqual(self.adapter.replies, [False])
        self.assertEqual(self.stop_calls, [])
        lease.finish()

    def test_publication_and_worker_failures_cancel_delayed_termination(self) -> None:
        self.shutdown.request_termination()
        self.runtime_handle.finish_lifecycle_publication()
        self.adapter.run_next_callback()

        self.assertEqual(self.shutdown.state, NativeShutdownState.FAILED)
        self.assertEqual(self.adapter.replies, [False])
        waiter = self.shutdown.waiter
        assert waiter is not None
        waiter.join(1)
        self.assertFalse(waiter.is_alive())

        self.setUp()
        lifecycle = RuntimeLifecycle()
        self.runtime_handle.publish_lifecycle(lifecycle)
        self.shutdown.request_termination()
        self.terminal.set_result(object())
        self.shutdown.worker_completed(succeeded=False)

        self.assertEqual(self.shutdown.state, NativeShutdownState.FAILED)
        self.assertEqual(self.adapter.replies, [False])

    def test_join_and_final_reply_failures_do_not_issue_success_twice(self) -> None:
        lifecycle = RuntimeLifecycle()
        self.runtime_handle.publish_lifecycle(lifecycle)
        self.shutdown.request_termination()

        self.shutdown.worker_join_failed(RuntimeError("private join detail"))

        self.assertEqual(self.shutdown.state, NativeShutdownState.FAILED)
        self.assertEqual(self.adapter.replies, [False])

        self.setUp()
        lifecycle = RuntimeLifecycle()
        self.runtime_handle.publish_lifecycle(lifecycle)
        self.adapter.reply_error = RuntimeError("private reply detail")
        self.shutdown.request_termination()
        self.terminal.set_result(object())
        self.shutdown.worker_completed(succeeded=True)

        self.assertEqual(self.shutdown.state, NativeShutdownState.FAILED)
        self.assertEqual(self.adapter.replies, [True])
        self.assertIn("failure.show", self.adapter.events)

    def test_wait_exception_and_dispatch_failure_never_fabricate_success(self) -> None:
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        self.runtime_handle.publish_lifecycle(lifecycle)
        with mock.patch.object(
            lifecycle,
            "wait_until_idle",
            side_effect=RuntimeError("private wait detail"),
        ):
            self.shutdown.request_termination()
            self.adapter.run_next_callback()

        self.assertEqual(self.shutdown.state, NativeShutdownState.FAILED)
        self.assertEqual(self.adapter.replies, [False])
        lease.finish()

        adapter = _Adapter()
        adapter.dispatch_error = RuntimeError("private dispatch detail")
        handle = LaunchRuntimeHandle(LaunchStopController())
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        handle.publish_lifecycle(lifecycle)
        diagnostics: list[tuple[str, BaseException | None]] = []
        shutdown = NativeShutdownCoordinator(
            adapter,
            handle,
            Future(),
            diagnostic=lambda code, error: diagnostics.append((code, error)),
        )
        shutdown.request_termination()
        lease.finish()
        waiter = shutdown.waiter
        assert waiter is not None
        waiter.join(1)

        self.assertEqual(shutdown.state, NativeShutdownState.FAILED)
        self.assertEqual(adapter.replies, [])
        self.assertEqual(diagnostics[-1][0], "shutdown_main_dispatch_failed")

    def test_shutdown_never_completes_the_worker_future(self) -> None:
        lifecycle = RuntimeLifecycle()
        self.runtime_handle.publish_lifecycle(lifecycle)

        self.shutdown.request_termination()

        self.assertFalse(self.terminal.done())

    def test_repeated_worker_completion_after_reply_is_harmless(self) -> None:
        lifecycle = RuntimeLifecycle()
        self.runtime_handle.publish_lifecycle(lifecycle)
        self.shutdown.request_termination()
        self.terminal.set_result(object())

        self.shutdown.worker_completed(succeeded=True)
        self.shutdown.worker_completed(succeeded=True)

        self.assertEqual(self.adapter.replies, [True])
        self.assertEqual(self.adapter.events.count("running:False"), 1)


if __name__ == "__main__":
    unittest.main()
