from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from enum import StrEnum
from threading import Lock, Thread, current_thread, main_thread
from typing import Any, Protocol

from organizer.web.launcher import LaunchRuntimeHandle
from organizer.web.signal_coordinator import (
    LifecycleShutdownCoordinator,
    ShutdownSequenceState,
)

SAFE_SHUTDOWN_FAILURE_MESSAGE = (
    "Loci Flow couldn’t close safely. Keep the application open."
)


class NativeShutdownState(StrEnum):
    IDLE = "idle"
    WAITING_FOR_LIFECYCLE = "waiting_for_lifecycle"
    WAITING_FOR_IDLE = "waiting_for_idle"
    STOPPING_SERVER = "stopping_server"
    WAITING_FOR_WORKER = "waiting_for_worker"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"


class ShutdownAdapter(Protocol):
    def dispatch_to_main(self, callback: Callable[[], None]) -> None: ...

    def show_finishing_status(self) -> None: ...

    def close_finishing_status(self) -> None: ...

    def show_shutdown_failure(self) -> None: ...

    def reply_to_termination(self, allow: bool) -> None: ...

    def set_launch_running(self, running: bool) -> None: ...

    def stop_event_loop(self) -> None: ...


ShutdownThreadFactory = Callable[[Callable[[], None], str], Thread]
ShutdownDiagnostic = Callable[[str, BaseException | None], None]


class NativeShutdownCoordinator:
    """Owns one native delayed-termination flow without owning worker results."""

    def __init__(
        self,
        adapter: ShutdownAdapter,
        runtime_handle: LaunchRuntimeHandle,
        terminal_result: Future[Any],
        *,
        thread_factory: ShutdownThreadFactory | None = None,
        diagnostic: ShutdownDiagnostic | None = None,
    ) -> None:
        self._adapter = adapter
        self._runtime_handle = runtime_handle
        self._terminal_result = terminal_result
        self._thread_factory = thread_factory
        self._diagnostic = diagnostic
        self._lock = Lock()
        self._state = NativeShutdownState.IDLE
        self._panel_visible = False
        self._panel_attempted = False
        self._termination_reply_expected = False
        self._reply_sent = False
        self._sequence = LifecycleShutdownCoordinator(
            runtime_handle,
            thread_factory=self._thread_factory,
            on_waiting=self._sequence_waiting,
            on_stop_requested=self._sequence_stopped,
            on_failure=self._sequence_failed,
            stop_dispatch=self._adapter.dispatch_to_main,
        )

    @property
    def state(self) -> NativeShutdownState:
        with self._lock:
            return self._state

    @property
    def waiter(self) -> Thread | None:
        return self._sequence.waiter

    def request_termination(self) -> bool:
        """Start or join delayed termination; False means AppKit must cancel."""

        with self._lock:
            if self._state is NativeShutdownState.FAILED:
                return False
            already_started = self._state is not NativeShutdownState.IDLE
            if already_started:
                self._termination_reply_expected = True
        accepted = self._request_shutdown()
        if accepted and not already_started:
            with self._lock:
                self._termination_reply_expected = True
        return accepted

    def request_signal_shutdown(self) -> bool:
        """Join or start shutdown without fabricating an AppKit termination reply."""

        with self._lock:
            if self._state is NativeShutdownState.FAILED:
                return False
            started = self._state is NativeShutdownState.IDLE
        return self._request_shutdown() and started

    def _request_shutdown(self) -> bool:

        with self._lock:
            if self._state is NativeShutdownState.FAILED:
                return False
            if self._state is NativeShutdownState.IDLE:
                self._state = NativeShutdownState.WAITING_FOR_LIFECYCLE
        admission = self._sequence.request_shutdown()
        if not admission.accepted:
            return False
        if admission.started:
            sequence_state = self._sequence.state
            with self._lock:
                if sequence_state is ShutdownSequenceState.WAITING_FOR_IDLE:
                    self._state = NativeShutdownState.WAITING_FOR_IDLE
                elif sequence_state is ShutdownSequenceState.STOP_REQUESTED:
                    self._state = NativeShutdownState.WAITING_FOR_WORKER
        return True

    def _sequence_waiting(self, active: bool) -> None:
        if not active:
            return
        with self._lock:
            if self._state not in {
                NativeShutdownState.COMPLETED,
                NativeShutdownState.FAILED,
            }:
                self._state = NativeShutdownState.WAITING_FOR_IDLE
            needs_panel = not self._panel_attempted
        if needs_panel:
            if current_thread() is main_thread():
                self._show_panel_on_main()
                return
            try:
                self._adapter.dispatch_to_main(self._show_panel_on_main)
            except Exception as error:
                self._sequence_failed("shutdown_main_dispatch_failed", error)

    def _sequence_stopped(self) -> None:
        with self._lock:
            if self._state not in {
                NativeShutdownState.COMPLETED,
                NativeShutdownState.FAILED,
            }:
                self._state = NativeShutdownState.WAITING_FOR_WORKER

    def _sequence_failed(
        self,
        code: str,
        error: BaseException | None,
    ) -> None:
        if current_thread() is main_thread():
            self._fail_on_main(code, error)
            return
        try:
            self._adapter.dispatch_to_main(
                lambda code=code, error=error: self._fail_on_main(code, error)
            )
        except Exception as dispatch_error:
            self._record("shutdown_main_dispatch_failed", dispatch_error)
            with self._lock:
                self._state = NativeShutdownState.FAILED

    def owns_worker_completion(self) -> bool:
        with self._lock:
            return self._state is not NativeShutdownState.IDLE

    def worker_join_failed(self, error: BaseException) -> None:
        self._fail_on_main("worker_join_failed", error)

    def main_dispatch_failed(self, error: BaseException) -> None:
        self._record("shutdown_main_dispatch_failed", error)
        with self._lock:
            if self._state is not NativeShutdownState.COMPLETED:
                self._state = NativeShutdownState.FAILED

    def worker_completed(self, *, succeeded: bool) -> None:
        """Consume an already-completed, already-joined worker outcome."""

        with self._lock:
            state = self._state
        if state is NativeShutdownState.IDLE:
            raise RuntimeError("shutdown does not own this worker completion")
        if state in {NativeShutdownState.COMPLETED, NativeShutdownState.FAILED}:
            return

        snapshot = self._runtime_handle.lifecycle_snapshot()
        if snapshot is None:
            self._fail_on_main("lifecycle_publication_failed", None)
            return
        if snapshot.active_count != 0:
            self._fail_on_main("worker_exited_while_active", None)
            return
        if state is not NativeShutdownState.WAITING_FOR_WORKER:
            self._fail_on_main("worker_exited_before_stop", None)
            return
        if not succeeded:
            self._fail_on_main("worker_shutdown_failed", None)
            return

        with self._lock:
            self._state = NativeShutdownState.COMPLETING
            self._reply_sent = True
            termination_reply_expected = self._termination_reply_expected
        try:
            self._sequence.join()
            self._close_panel_on_main()
            self._adapter.set_launch_running(False)
            if termination_reply_expected:
                self._adapter.reply_to_termination(True)
            else:
                self._adapter.stop_event_loop()
        except Exception as error:
            self._record("native_termination_reply_failed", error)
            with self._lock:
                self._state = NativeShutdownState.FAILED
            try:
                self._adapter.show_shutdown_failure()
            except Exception as presentation_error:
                self._record(
                    "shutdown_failure_presentation_failed",
                    presentation_error,
                )
            return

        with self._lock:
            self._state = NativeShutdownState.COMPLETED

    def _show_panel_on_main(self) -> None:
        with self._lock:
            if self._panel_attempted or self._state not in {
                NativeShutdownState.WAITING_FOR_LIFECYCLE,
                NativeShutdownState.WAITING_FOR_IDLE,
            }:
                return
            self._panel_attempted = True
        try:
            self._adapter.show_finishing_status()
        except Exception as error:
            self._record("finishing_panel_failed", error)
            return
        with self._lock:
            self._panel_visible = True

    def _fail_on_main(self, code: str, error: BaseException | None) -> None:
        self._record(code, error)
        with self._lock:
            if self._state in {
                NativeShutdownState.COMPLETED,
                NativeShutdownState.FAILED,
            }:
                return
            self._state = NativeShutdownState.FAILED
            should_reply = (
                self._termination_reply_expected and not self._reply_sent
            )
            if should_reply:
                self._reply_sent = True
        try:
            self._close_panel_on_main()
        except Exception as close_error:
            self._record("finishing_panel_close_failed", close_error)
        if should_reply:
            try:
                self._adapter.reply_to_termination(False)
            except Exception as reply_error:
                self._record("native_termination_cancel_failed", reply_error)
        try:
            self._adapter.show_shutdown_failure()
        except Exception as presentation_error:
            self._record("shutdown_failure_presentation_failed", presentation_error)

    def _close_panel_on_main(self) -> None:
        with self._lock:
            visible = self._panel_visible
            self._panel_visible = False
        if visible:
            self._adapter.close_finishing_status()

    def _record(self, code: str, error: BaseException | None) -> None:
        if self._diagnostic is None:
            return
        try:
            self._diagnostic(code, error)
        except Exception:
            pass
