from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from enum import StrEnum
from threading import Lock, Thread, current_thread
from typing import Any, Protocol

from organizer.web.launcher import LaunchRuntimeHandle


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
        self._thread_factory = thread_factory or _shutdown_thread
        self._diagnostic = diagnostic
        self._lock = Lock()
        self._state = NativeShutdownState.IDLE
        self._waiter: Thread | None = None
        self._panel_visible = False
        self._panel_attempted = False
        self._termination_reply_expected = False
        self._reply_sent = False

    @property
    def state(self) -> NativeShutdownState:
        with self._lock:
            return self._state

    @property
    def waiter(self) -> Thread | None:
        with self._lock:
            return self._waiter

    def request_termination(self) -> bool:
        """Start or join delayed termination; False means AppKit must cancel."""

        with self._lock:
            if self._state is NativeShutdownState.FAILED:
                return False
            if self._state is not NativeShutdownState.IDLE:
                return True
            self._state = NativeShutdownState.WAITING_FOR_LIFECYCLE

        try:
            snapshot = self._runtime_handle.request_shutdown()
        except Exception as error:
            self._fail_on_main("lifecycle_shutdown_failed", error)
            return False

        if snapshot is None:
            accepted = self._start_waiter()
        elif snapshot.active_count == 0:
            self._idle_on_main()
            accepted = self.state is not NativeShutdownState.FAILED
        else:
            with self._lock:
                self._state = NativeShutdownState.WAITING_FOR_IDLE
            self._show_panel_on_main()
            accepted = self._start_waiter()
        if accepted:
            with self._lock:
                self._termination_reply_expected = True
        return accepted

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
        try:
            self._join_waiter()
            self._close_panel_on_main()
            self._adapter.set_launch_running(False)
            self._adapter.reply_to_termination(True)
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

    def _start_waiter(self) -> bool:
        with self._lock:
            if self._waiter is not None:
                return True
        try:
            waiter = self._thread_factory(
                self._wait_for_idle,
                "loci-flow-shutdown-waiter",
            )
            waiter.daemon = False
        except Exception as error:
            self._fail_on_main("shutdown_waiter_creation_failed", error)
            return False
        with self._lock:
            if self._waiter is not None:
                return True
            self._waiter = waiter
        try:
            waiter.start()
        except Exception as error:
            self._fail_on_main("shutdown_waiter_start_failed", error)
            return False
        return True

    def _wait_for_idle(self) -> None:
        try:
            lifecycle = self._runtime_handle.wait_for_lifecycle()
            snapshot = lifecycle.snapshot()
            if snapshot.active_count:
                with self._lock:
                    if self._state is NativeShutdownState.WAITING_FOR_LIFECYCLE:
                        self._state = NativeShutdownState.WAITING_FOR_IDLE
                    needs_panel = not self._panel_attempted
                if needs_panel:
                    self._adapter.dispatch_to_main(self._show_panel_on_main)
            lifecycle.wait_until_idle()
            self._adapter.dispatch_to_main(self._idle_on_main)
        except Exception as error:
            try:
                self._adapter.dispatch_to_main(
                    lambda error=error: self._fail_on_main(
                        "shutdown_wait_failed", error
                    )
                )
            except Exception as dispatch_error:
                self._record("shutdown_main_dispatch_failed", dispatch_error)
                with self._lock:
                    self._state = NativeShutdownState.FAILED

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

    def _idle_on_main(self) -> None:
        with self._lock:
            if self._state in {
                NativeShutdownState.STOPPING_SERVER,
                NativeShutdownState.WAITING_FOR_WORKER,
                NativeShutdownState.COMPLETING,
                NativeShutdownState.COMPLETED,
                NativeShutdownState.FAILED,
            }:
                return

        snapshot = self._runtime_handle.lifecycle_snapshot()
        if snapshot is None:
            self._fail_on_main("lifecycle_publication_failed", None)
            return
        if snapshot.active_count != 0:
            self._fail_on_main("lifecycle_not_idle", None)
            return
        if self._terminal_result.done():
            self._fail_on_main("worker_exited_before_stop", None)
            return

        with self._lock:
            self._state = NativeShutdownState.STOPPING_SERVER
        try:
            self._runtime_handle.request_server_stop()
        except Exception as error:
            self._fail_on_main("server_stop_failed", error)
            return
        with self._lock:
            if self._state is NativeShutdownState.STOPPING_SERVER:
                self._state = NativeShutdownState.WAITING_FOR_WORKER

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

    def _join_waiter(self) -> None:
        with self._lock:
            waiter = self._waiter
        if waiter is not None and waiter is not current_thread():
            waiter.join()

    def _record(self, code: str, error: BaseException | None) -> None:
        if self._diagnostic is None:
            return
        try:
            self._diagnostic(code, error)
        except Exception:
            pass


def _shutdown_thread(target: Callable[[], None], name: str) -> Thread:
    return Thread(target=target, name=name, daemon=False)
