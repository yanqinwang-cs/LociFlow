from __future__ import annotations

import os
import select
import signal
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock, Thread, current_thread, main_thread
from types import FrameType

from organizer.web.launcher import LaunchRuntimeHandle


class SignalKind(StrEnum):
    SIGINT = "sigint"
    SIGTERM = "sigterm"


@dataclass(frozen=True)
class SignalIntent:
    kind: SignalKind
    signal_number: int
    exit_code: int


@dataclass(frozen=True)
class SignalShutdownResult:
    intent: SignalIntent | None
    owns_shutdown: bool
    error: str | None


class ShutdownSequenceState(StrEnum):
    IDLE = "idle"
    WAITING_FOR_LIFECYCLE = "waiting_for_lifecycle"
    WAITING_FOR_IDLE = "waiting_for_idle"
    STOP_REQUESTED = "stop_requested"
    FAILED = "failed"


@dataclass(frozen=True)
class ShutdownAdmission:
    accepted: bool
    started: bool


ShutdownThreadFactory = Callable[[Callable[[], None], str], Thread]
ShutdownWaitingCallback = Callable[[bool], None]
ShutdownStoppedCallback = Callable[[], None]
ShutdownFailureCallback = Callable[[str, BaseException | None], None]
ShutdownStopDispatcher = Callable[[Callable[[], None]], None]


class LifecycleShutdownCoordinator:
    """Own one lifecycle wait and one request through LaunchStopController."""

    def __init__(
        self,
        runtime_handle: LaunchRuntimeHandle,
        *,
        thread_factory: ShutdownThreadFactory | None = None,
        on_waiting: ShutdownWaitingCallback | None = None,
        on_stop_requested: ShutdownStoppedCallback | None = None,
        on_failure: ShutdownFailureCallback | None = None,
        stop_dispatch: ShutdownStopDispatcher | None = None,
    ) -> None:
        self._runtime_handle = runtime_handle
        self._thread_factory = thread_factory or _shutdown_thread
        self._on_waiting = on_waiting
        self._on_stop_requested = on_stop_requested
        self._on_failure = on_failure
        self._stop_dispatch = stop_dispatch
        self._lock = Lock()
        self._state = ShutdownSequenceState.IDLE
        self._waiter: Thread | None = None

    @property
    def state(self) -> ShutdownSequenceState:
        with self._lock:
            return self._state

    @property
    def waiter(self) -> Thread | None:
        with self._lock:
            return self._waiter

    def request_shutdown(self) -> ShutdownAdmission:
        with self._lock:
            if self._state is ShutdownSequenceState.FAILED:
                return ShutdownAdmission(False, False)
            if self._state is not ShutdownSequenceState.IDLE:
                return ShutdownAdmission(True, False)
            self._state = ShutdownSequenceState.WAITING_FOR_LIFECYCLE

        try:
            snapshot = self._runtime_handle.request_shutdown()
        except Exception as error:
            self._fail("lifecycle_shutdown_failed", error)
            return ShutdownAdmission(False, True)

        if snapshot is None:
            accepted = self._start_waiter()
        elif snapshot.active_count:
            with self._lock:
                self._state = ShutdownSequenceState.WAITING_FOR_IDLE
            self._notify_waiting(True)
            accepted = self._start_waiter()
        else:
            self._notify_waiting(False)
            accepted = self._request_stop()
        return ShutdownAdmission(accepted, True)

    def join(self) -> None:
        with self._lock:
            waiter = self._waiter
        if waiter is not None and waiter is not current_thread():
            waiter.join()

    def _start_waiter(self) -> bool:
        try:
            waiter = self._thread_factory(
                self._wait_for_idle,
                "loci-flow-shutdown-waiter",
            )
            waiter.daemon = False
        except Exception as error:
            self._fail("shutdown_waiter_creation_failed", error)
            return False
        with self._lock:
            if self._waiter is not None:
                return True
            self._waiter = waiter
        try:
            waiter.start()
        except Exception as error:
            self._fail("shutdown_waiter_start_failed", error)
            return False
        return True

    def _wait_for_idle(self) -> None:
        try:
            lifecycle = self._runtime_handle.wait_for_lifecycle()
            snapshot = lifecycle.snapshot()
            if snapshot.active_count:
                with self._lock:
                    self._state = ShutdownSequenceState.WAITING_FOR_IDLE
                self._notify_waiting(True)
            else:
                self._notify_waiting(False)
            lifecycle.wait_until_idle()
            if self._stop_dispatch is None:
                self._request_stop()
            else:
                self._stop_dispatch(self._request_stop)
        except Exception as error:
            self._fail("shutdown_wait_failed", error)

    def _request_stop(self) -> bool:
        snapshot = self._runtime_handle.lifecycle_snapshot()
        if snapshot is None:
            self._fail("lifecycle_publication_failed", None)
            return False
        if snapshot.active_count:
            self._fail("lifecycle_not_idle", None)
            return False
        try:
            self._runtime_handle.request_server_stop()
        except Exception as error:
            self._fail("server_stop_failed", error)
            return False
        with self._lock:
            if self._state is not ShutdownSequenceState.FAILED:
                self._state = ShutdownSequenceState.STOP_REQUESTED
        if self._on_stop_requested is not None:
            self._on_stop_requested()
        return True

    def _notify_waiting(self, active: bool) -> None:
        if self._on_waiting is not None:
            self._on_waiting(active)

    def _fail(self, code: str, error: BaseException | None) -> None:
        with self._lock:
            if self._state is ShutdownSequenceState.FAILED:
                return
            self._state = ShutdownSequenceState.FAILED
        if self._on_failure is not None:
            self._on_failure(code, error)


SignalCallback = Callable[[SignalIntent], bool]
SignalDiagnostic = Callable[[str, BaseException | None], None]
SignalThreadFactory = Callable[[Callable[[], None], str], Thread]


class ProcessSignalCoordinator:
    """Own process signal handlers and the first post-install signal intent."""

    def __init__(
        self,
        on_signal: SignalCallback,
        *,
        terminal_completed: Callable[[], bool] | None = None,
        thread_factory: SignalThreadFactory | None = None,
        diagnostic: SignalDiagnostic | None = None,
    ) -> None:
        self._on_signal = on_signal
        self._terminal_completed = terminal_completed or (lambda: False)
        self._thread_factory = thread_factory or _signal_thread
        self._diagnostic = diagnostic
        self._lock = Lock()
        self._intent: SignalIntent | None = None
        self._callback_started = False
        self._owns_shutdown = False
        self._error: str | None = None
        self._read_fd: int | None = None
        self._write_fd: int | None = None
        self._reader: Thread | None = None
        self._previous_handlers: dict[int, object] = {}
        self._previous_wakeup_fd: int | None = None
        self._installed = False
        self._closed = False

    @property
    def intent(self) -> SignalIntent | None:
        with self._lock:
            return self._intent

    @property
    def owns_shutdown(self) -> bool:
        with self._lock:
            return self._owns_shutdown

    def capture_pending_intent(self) -> SignalIntent | None:
        """Atomically expose an accepted or handler-observed first signal."""
        with self._lock:
            return self._intent

    def install(self) -> None:
        if current_thread() is not main_thread():
            raise RuntimeError("process signal handlers require the main thread")
        with self._lock:
            if self._installed:
                return
            if self._closed:
                raise RuntimeError("process signal coordinator is closed")

        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        previous_wakeup_fd: int | None = None
        previous_handlers: dict[int, object] = {}
        reader: Thread | None = None
        try:
            previous_wakeup_fd = signal.set_wakeup_fd(
                write_fd,
                warn_on_full_buffer=False,
            )
            for number in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[number] = signal.getsignal(number)
                signal.signal(number, self._record_pending_signal)
            reader = self._thread_factory(
                self._read_signals,
                "loci-flow-signal-coordinator",
            )
            reader.daemon = False
            with self._lock:
                self._read_fd = read_fd
                self._write_fd = write_fd
                self._reader = reader
                self._previous_handlers = previous_handlers
                self._previous_wakeup_fd = previous_wakeup_fd
                self._installed = True
            reader.start()
        except Exception:
            self._restore_partial(previous_handlers, previous_wakeup_fd)
            os.close(read_fd)
            os.close(write_fd)
            with self._lock:
                self._read_fd = None
                self._write_fd = None
                self._reader = None
                self._previous_handlers = {}
                self._previous_wakeup_fd = None
                self._installed = False
            raise

    def close(self) -> SignalShutdownResult:
        if current_thread() is not main_thread():
            raise RuntimeError("process signal handlers require the main thread")
        terminal_completed = self._terminal_completed()
        with self._lock:
            if self._closed:
                return self._result_locked()
            if terminal_completed and not self._callback_started:
                self._intent = None
            self._closed = True
            installed = self._installed
            handlers = dict(self._previous_handlers)
            previous_wakeup_fd = self._previous_wakeup_fd
            write_fd = self._write_fd
            reader = self._reader

        if installed:
            for number, previous in handlers.items():
                try:
                    signal.signal(number, previous)
                except Exception as error:
                    self._record_failure("signal_handler_restore_failed", error)
            try:
                signal.set_wakeup_fd(
                    -1 if previous_wakeup_fd is None else previous_wakeup_fd,
                    warn_on_full_buffer=False,
                )
            except Exception as error:
                self._record_failure("signal_wakeup_restore_failed", error)

        if write_fd is not None:
            try:
                os.write(write_fd, b"\x00")
            except (BlockingIOError, OSError):
                pass
        if reader is not None and reader is not current_thread():
            reader.join()

        with self._lock:
            read_fd = self._read_fd
            write_fd = self._write_fd
            self._read_fd = None
            self._write_fd = None
            self._reader = None
            self._installed = False
        for descriptor in (read_fd, write_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        with self._lock:
            return self._result_locked()

    def _read_signals(self) -> None:
        try:
            while True:
                with self._lock:
                    read_fd = self._read_fd
                if read_fd is None:
                    return
                ready, _, _ = select.select([read_fd], [], [])
                if not ready:
                    continue
                try:
                    payload = os.read(read_fd, 1024)
                except BlockingIOError:
                    continue
                if not payload:
                    return
                for value in payload:
                    if value == 0:
                        return
                    intent = _intent_for(value)
                    if intent is not None:
                        self._accept(intent)
        except Exception as error:
            self._record_failure("signal_coordinator_failed", error)

    def _accept(self, intent: SignalIntent) -> None:
        with self._lock:
            if self._callback_started or self._closed:
                return
        if self._terminal_completed():
            with self._lock:
                if not self._callback_started:
                    self._intent = None
            return
        with self._lock:
            if self._callback_started or self._closed:
                return
            if self._intent is None:
                self._intent = intent
            accepted_intent = self._intent
            self._callback_started = True
        try:
            owns_shutdown = bool(self._on_signal(accepted_intent))
        except Exception as error:
            self._record_failure("signal_shutdown_failed", error)
            return
        with self._lock:
            self._owns_shutdown = owns_shutdown

    def _record_pending_signal(
        self,
        number: int,
        _frame: FrameType | None,
    ) -> None:
        # Python invokes this handler on the main thread. Keep it lock-free and
        # callback-free; the wakeup-fd reader performs the callback work.
        if self._intent is None:
            self._intent = _intent_for(number)

    def _restore_partial(
        self,
        handlers: dict[int, object],
        previous_wakeup_fd: int | None,
    ) -> None:
        for number, previous in handlers.items():
            try:
                signal.signal(number, previous)
            except Exception:
                pass
        if previous_wakeup_fd is not None:
            try:
                signal.set_wakeup_fd(
                    previous_wakeup_fd,
                    warn_on_full_buffer=False,
                )
            except Exception:
                pass

    def _record_failure(self, code: str, error: BaseException | None) -> None:
        with self._lock:
            if self._error is None:
                self._error = code
        if self._diagnostic is not None:
            try:
                self._diagnostic(code, error)
            except Exception:
                pass

    def _result_locked(self) -> SignalShutdownResult:
        return SignalShutdownResult(
            intent=self._intent,
            owns_shutdown=self._owns_shutdown,
            error=self._error,
        )


def _intent_for(number: int) -> SignalIntent | None:
    if number == signal.SIGINT:
        return SignalIntent(SignalKind.SIGINT, number, 130)
    if number == signal.SIGTERM:
        return SignalIntent(SignalKind.SIGTERM, number, 143)
    return None


def _signal_thread(target: Callable[[], None], name: str) -> Thread:
    return Thread(target=target, name=name, daemon=False)


def _shutdown_thread(target: Callable[[], None], name: str) -> Thread:
    return Thread(target=target, name=name, daemon=False)
