from __future__ import annotations

import os
import signal
from threading import Event
import unittest
from unittest import mock

from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.web.launcher import LaunchRuntimeHandle, LaunchStopController
from organizer.web.signal_coordinator import (
    LifecycleShutdownCoordinator,
    ProcessSignalCoordinator,
    ShutdownSequenceState,
    SignalKind,
)


class ProcessSignalCoordinatorTests(unittest.TestCase):
    def test_main_thread_handler_intent_is_visible_before_reader_dispatch(self) -> None:
        callbacks = []
        coordinator = ProcessSignalCoordinator(
            lambda intent: callbacks.append(intent) or True
        )

        coordinator._record_pending_signal(signal.SIGTERM, None)
        coordinator._record_pending_signal(signal.SIGINT, None)
        intent = coordinator.capture_pending_intent()

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.kind, SignalKind.SIGTERM)
        self.assertEqual(intent.exit_code, 143)
        self.assertEqual(callbacks, [])

    def test_wakeup_fd_records_first_signal_and_restores_process_state(self) -> None:
        received = Event()
        intents = []
        previous_handlers = {
            signal.SIGINT: signal.getsignal(signal.SIGINT),
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        }
        prior_read, prior_write = os.pipe()
        os.set_blocking(prior_read, False)
        os.set_blocking(prior_write, False)
        original_wakeup = signal.set_wakeup_fd(
            prior_write,
            warn_on_full_buffer=False,
        )
        coordinator = ProcessSignalCoordinator(
            lambda intent: (intents.append(intent), received.set(), True)[2]
        )
        try:
            coordinator.install()
            read_fd = coordinator._read_fd
            write_fd = coordinator._write_fd

            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(received.wait(1))
            result = coordinator.close()

            self.assertEqual(result.intent.kind, SignalKind.SIGINT)
            self.assertEqual(result.intent.exit_code, 130)
            self.assertTrue(result.owns_shutdown)
            self.assertEqual(len(intents), 1)
            self.assertEqual(signal.getsignal(signal.SIGINT), previous_handlers[signal.SIGINT])
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handlers[signal.SIGTERM])
            restored_wakeup = signal.set_wakeup_fd(
                -1,
                warn_on_full_buffer=False,
            )
            self.assertEqual(restored_wakeup, prior_write)
            signal.set_wakeup_fd(
                original_wakeup,
                warn_on_full_buffer=False,
            )
            assert read_fd is not None and write_fd is not None
            with self.assertRaises(OSError):
                os.fstat(read_fd)
            with self.assertRaises(OSError):
                os.fstat(write_fd)
        finally:
            if coordinator._installed:
                coordinator.close()
            try:
                signal.set_wakeup_fd(
                    original_wakeup,
                    warn_on_full_buffer=False,
                )
            except ValueError:
                pass
            os.close(prior_read)
            os.close(prior_write)

    def test_sigterm_first_is_143_and_repeated_signals_do_not_restart(self) -> None:
        received = Event()
        calls = []
        coordinator = ProcessSignalCoordinator(
            lambda intent: (calls.append(intent), received.set(), True)[2]
        )
        coordinator.install()
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(received.wait(1))
        finally:
            result = coordinator.close()

        self.assertEqual(result.intent.kind, SignalKind.SIGTERM)
        self.assertEqual(result.intent.exit_code, 143)
        self.assertEqual(len(calls), 1)

    def test_signal_after_terminal_completion_is_ignored(self) -> None:
        called = Event()
        coordinator = ProcessSignalCoordinator(
            lambda _intent: called.set() or True,
            terminal_completed=lambda: True,
        )
        coordinator.install()
        try:
            os.kill(os.getpid(), signal.SIGINT)
        finally:
            result = coordinator.close()

        self.assertFalse(called.is_set())
        self.assertIsNone(result.intent)
        self.assertFalse(result.owns_shutdown)

    def test_installation_requires_main_thread(self) -> None:
        errors = []
        completed = Event()
        coordinator = ProcessSignalCoordinator(lambda _intent: True)

        def install() -> None:
            try:
                coordinator.install()
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()

        from threading import Thread

        worker = Thread(target=install)
        worker.start()
        self.assertTrue(completed.wait(1))
        worker.join(1)

        self.assertIsInstance(errors[0], RuntimeError)
        self.assertFalse(coordinator._installed)

    def test_pipe_creation_failure_installs_no_handlers(self) -> None:
        previous_handlers = {
            signal.SIGINT: signal.getsignal(signal.SIGINT),
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        }
        coordinator = ProcessSignalCoordinator(lambda _intent: True)

        with mock.patch(
            "organizer.web.signal_coordinator.os.pipe",
            side_effect=OSError("controlled pipe failure"),
        ):
            with self.assertRaisesRegex(OSError, "controlled pipe failure"):
                coordinator.install()

        self.assertEqual(signal.getsignal(signal.SIGINT), previous_handlers[signal.SIGINT])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handlers[signal.SIGTERM])
        self.assertFalse(coordinator._installed)

    def test_handler_restoration_failure_is_not_reported_as_success(self) -> None:
        previous_handlers = {
            signal.SIGINT: signal.getsignal(signal.SIGINT),
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        }
        coordinator = ProcessSignalCoordinator(lambda _intent: True)
        coordinator.install()
        try:
            with mock.patch.object(
                signal,
                "signal",
                side_effect=RuntimeError("controlled restore failure"),
            ):
                result = coordinator.close()
        finally:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)

        self.assertEqual(result.error, "signal_handler_restore_failed")


class LifecycleShutdownCoordinatorTests(unittest.TestCase):
    def test_every_operation_kind_finishes_before_one_stop(self) -> None:
        for kind in RuntimeOperationKind:
            with self.subTest(kind=kind):
                lifecycle = RuntimeLifecycle()
                lease = lifecycle.begin_operation(kind)
                stop_controller = LaunchStopController()
                stops = []
                stop_controller.attach(lambda: stops.append("stop"))
                runtime_handle = LaunchRuntimeHandle(stop_controller)
                runtime_handle.publish_lifecycle(lifecycle)
                shutdown = LifecycleShutdownCoordinator(runtime_handle)

                admission = shutdown.request_shutdown()

                self.assertTrue(admission.accepted)
                self.assertTrue(admission.started)
                self.assertTrue(lifecycle.snapshot().is_stopping)
                self.assertEqual(stops, [])
                with self.assertRaises(ApplicationStoppingError):
                    lifecycle.begin_operation(RuntimeOperationKind.STATE_MUTATION)

                lease.finish()
                shutdown.join()

                self.assertEqual(stops, ["stop"])
                self.assertEqual(
                    shutdown.state,
                    ShutdownSequenceState.STOP_REQUESTED,
                )
                self.assertFalse(shutdown.request_shutdown().started)
                self.assertEqual(stops, ["stop"])

    def test_prepublication_shutdown_uses_exact_lifecycle(self) -> None:
        stop_controller = LaunchStopController()
        stops = []
        stop_controller.attach(lambda: stops.append("stop"))
        runtime_handle = LaunchRuntimeHandle(stop_controller)
        shutdown = LifecycleShutdownCoordinator(runtime_handle)

        admission = shutdown.request_shutdown()
        lifecycle = RuntimeLifecycle()
        runtime_handle.publish_lifecycle(lifecycle)
        shutdown.join()

        self.assertTrue(admission.started)
        self.assertTrue(lifecycle.snapshot().is_stopping)
        self.assertEqual(stops, ["stop"])

    def test_wait_failure_does_not_request_stop_or_claim_success(self) -> None:
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        stop_controller = LaunchStopController()
        stops = []
        stop_controller.attach(lambda: stops.append("stop"))
        runtime_handle = LaunchRuntimeHandle(stop_controller)
        runtime_handle.publish_lifecycle(lifecycle)
        failures = []
        shutdown = LifecycleShutdownCoordinator(
            runtime_handle,
            on_failure=lambda code, error: failures.append((code, error)),
        )

        original_wait = lifecycle.wait_until_idle
        lifecycle.wait_until_idle = lambda: (_ for _ in ()).throw(
            RuntimeError("controlled wait failure")
        )
        try:
            shutdown.request_shutdown()
            shutdown.join()
        finally:
            lifecycle.wait_until_idle = original_wait
            lease.finish()

        self.assertEqual(shutdown.state, ShutdownSequenceState.FAILED)
        self.assertEqual(stops, [])
        self.assertEqual(failures[0][0], "shutdown_wait_failed")


if __name__ == "__main__":
    unittest.main()
