from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.cookiejar import CookieJar
import json
from pathlib import Path
from queue import Empty, Queue
import re
from threading import Event
import time
from typing import Final
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)

from organizer.macos_launcher.application_paths import MacOSApplicationPaths
from organizer.macos_launcher.coordinator import (
    DiagnosticSink,
    NativeLauncherCoordinator,
    NativeMessage,
)
from organizer.macos_launcher.packaged_diagnostics import (
    ACTIVE_LOG_NAME,
    ALLOWED_CODES,
    ALLOWED_EXCEPTION_CLASSES,
    ALLOWED_PHASES,
)
from organizer.macos_launcher.process_lease import (
    acquire_application_instance_lease,
    acquire_root_process_lease,
)
from organizer.web.launcher import launch
from organizer.web.server import build_launcher_dependencies


_CSRF_PATTERN: Final = re.compile(
    r'<meta name="csrf-token" content="([^"]+)">'
)
_DIAGNOSTIC_KEYS: Final = frozenset(
    {
        "application_version",
        "build_number",
        "code",
        "exception_class",
        "phase",
        "timestamp_utc",
    }
)
_REQUIRED_ASSETS: Final = (
    "/static/css/bootai.css",
    "/static/js/bootai.js",
    "/static/loci_flow/app.js",
    "/static/loci_flow/assets/loci-flow-mark.svg",
    "/static/vendor/bootstrap.min.css",
    "/static/THIRD_PARTY_NOTICES.txt",
)


class FrozenWorkflowAcceptanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrozenWorkflowAcceptanceReport:
    authenticated_home: str
    check_request: str
    required_assets: str
    browser_url_count: int
    global_conflict: str
    same_root_conflict: str
    different_root_coexistence: str
    coordinated_shutdown: str
    post_shutdown_reacquisition: str
    packaged_diagnostics: str

    def as_safe_dict(self) -> dict[str, object]:
        return asdict(self)


def run_frozen_workflow_acceptance(
    *,
    selected_root: Path,
    different_root: Path,
    application_paths: MacOSApplicationPaths,
    diagnostic_sink: DiagnosticSink,
) -> FrozenWorkflowAcceptanceReport:
    adapter = _AcceptanceAdapter(
        selected_root=selected_root,
        different_root=different_root,
        application_paths=application_paths,
        diagnostic_sink=diagnostic_sink,
    )
    result = NativeLauncherCoordinator(
        adapter,
        diagnostic_sink=diagnostic_sink,
        application_paths=application_paths,
    ).run()
    if result != 0 or adapter.workflow_error is not None:
        raise FrozenWorkflowAcceptanceError
    if adapter.messages or adapter.shutdown_failures:
        raise FrozenWorkflowAcceptanceError
    if adapter.browser_url_count != 1:
        raise FrozenWorkflowAcceptanceError
    if adapter.termination_replies != [True]:
        raise FrozenWorkflowAcceptanceError

    application_lease = acquire_application_instance_lease(application_paths)
    try:
        root_lease = acquire_root_process_lease(
            application_paths,
            selected_root,
        )
        root_lease.release()
    finally:
        application_lease.release()

    _validate_bounded_diagnostics(
        application_paths.logs_directory / ACTIVE_LOG_NAME,
        forbidden=(
            str(selected_root),
            str(different_root),
            "http://",
            "/launch/",
        ),
    )
    return FrozenWorkflowAcceptanceReport(
        authenticated_home="passed",
        check_request="passed",
        required_assets="passed",
        browser_url_count=adapter.browser_url_count,
        global_conflict="passed",
        same_root_conflict="passed",
        different_root_coexistence="passed",
        coordinated_shutdown="passed",
        post_shutdown_reacquisition="passed",
        packaged_diagnostics="passed",
    )


def format_frozen_workflow_report(
    report: FrozenWorkflowAcceptanceReport,
) -> str:
    return json.dumps(
        report.as_safe_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


class _AcceptanceAdapter:
    def __init__(
        self,
        *,
        selected_root: Path,
        different_root: Path,
        application_paths: MacOSApplicationPaths,
        diagnostic_sink: DiagnosticSink,
    ) -> None:
        self._selected_root = selected_root
        self._different_root = different_root
        self._application_paths = application_paths
        self._diagnostic_sink = diagnostic_sink
        self._callbacks: Queue[Callable[[], None]] = Queue()
        self._stopped = Event()
        self._termination_handler: Callable[[], bool] | None = None
        self.workflow_error: Exception | None = None
        self.browser_url_count = 0
        self.messages: list[NativeMessage] = []
        self.shutdown_failures = 0
        self.termination_replies: list[bool] = []

    def initialize(self) -> None:
        return None

    def choose_directory(self) -> str:
        return str(self._selected_root)

    def cancel_active_directory_selection(self) -> bool:
        return True

    def open_browser(self, launch_url: str) -> bool:
        self.browser_url_count += 1
        try:
            _exercise_authenticated_workflow(launch_url)
            self._prove_live_lease_contracts()
        except Exception as error:
            self.workflow_error = error
        finally:
            self.dispatch_to_main(self._request_shutdown)
        return self.workflow_error is None

    def show_error(self, message: NativeMessage) -> None:
        self.messages.append(message)

    def dispatch_to_main(self, callback: Callable[[], None]) -> None:
        self._callbacks.put(callback)

    def run_event_loop(self) -> None:
        deadline = time.monotonic() + 45
        while not self._stopped.is_set():
            if time.monotonic() >= deadline:
                raise FrozenWorkflowAcceptanceError
            try:
                callback = self._callbacks.get(timeout=0.1)
            except Empty:
                continue
            callback()

    def stop_event_loop(self) -> None:
        self._stopped.set()

    def set_launch_running(self, _running: bool) -> None:
        return None

    def set_termination_handler(
        self,
        handler: Callable[[], bool],
    ) -> None:
        self._termination_handler = handler

    def set_diagnostic_handler(
        self,
        _handler: Callable[[str], None],
    ) -> None:
        return None

    def show_finishing_status(self) -> None:
        return None

    def close_finishing_status(self) -> None:
        return None

    def show_shutdown_failure(self) -> None:
        self.shutdown_failures += 1

    def reply_to_termination(self, allow: bool) -> None:
        self.termination_replies.append(allow)
        if allow:
            self._stopped.set()

    def _request_shutdown(self) -> None:
        if self._termination_handler is None:
            self.workflow_error = FrozenWorkflowAcceptanceError()
            self._stopped.set()
            return
        if not self._termination_handler():
            self.workflow_error = FrozenWorkflowAcceptanceError()

    def _prove_live_lease_contracts(self) -> None:
        global_conflict = _GlobalConflictAdapter()
        global_result = NativeLauncherCoordinator(
            global_conflict,
            diagnostic_sink=self._diagnostic_sink,
            application_paths=self._application_paths,
        ).run()
        if (
            global_result != 1
            or global_conflict.choose_called
            or len(global_conflict.messages) != 1
            or global_conflict.messages[0].message
            != "Loci Flow is already open."
        ):
            raise FrozenWorkflowAcceptanceError

        same_root = launch(
            lambda: self._selected_root,
            dependencies=build_launcher_dependencies(
                application_paths=self._application_paths,
            ),
            open_browser=False,
        )
        if (
            same_root.error is None
            or same_root.error.code != "root_already_open"
            or same_root.error.safe_message
            != "This folder is already open in Loci Flow."
        ):
            raise FrozenWorkflowAcceptanceError

        different = acquire_root_process_lease(
            self._application_paths,
            self._different_root,
        )
        different.release()


class _GlobalConflictAdapter:
    def __init__(self) -> None:
        self.choose_called = False
        self.messages: list[NativeMessage] = []

    def initialize(self) -> None:
        return None

    def choose_directory(self) -> None:
        self.choose_called = True
        raise FrozenWorkflowAcceptanceError

    def show_error(self, message: NativeMessage) -> None:
        self.messages.append(message)

    def set_diagnostic_handler(
        self,
        _handler: Callable[[str], None],
    ) -> None:
        return None

    def dispatch_to_main(self, _callback: Callable[[], None]) -> None:
        raise FrozenWorkflowAcceptanceError

    def cancel_active_directory_selection(self) -> bool:
        raise FrozenWorkflowAcceptanceError

    def open_browser(self, _launch_url: str) -> bool:
        raise FrozenWorkflowAcceptanceError

    def run_event_loop(self) -> None:
        raise FrozenWorkflowAcceptanceError

    def stop_event_loop(self) -> None:
        raise FrozenWorkflowAcceptanceError

    def set_launch_running(self, _running: bool) -> None:
        raise FrozenWorkflowAcceptanceError

    def set_termination_handler(
        self,
        _handler: Callable[[], bool],
    ) -> None:
        raise FrozenWorkflowAcceptanceError

    def show_finishing_status(self) -> None:
        raise FrozenWorkflowAcceptanceError

    def close_finishing_status(self) -> None:
        raise FrozenWorkflowAcceptanceError

    def show_shutdown_failure(self) -> None:
        raise FrozenWorkflowAcceptanceError

    def reply_to_termination(self, _allow: bool) -> None:
        raise FrozenWorkflowAcceptanceError


def _exercise_authenticated_workflow(launch_url: str) -> None:
    split = urlsplit(launch_url)
    if split.scheme != "http" or split.hostname != "127.0.0.1":
        raise FrozenWorkflowAcceptanceError
    base_url = f"http://127.0.0.1:{split.port}"
    opener = build_opener(HTTPCookieProcessor(CookieJar()))

    home = _read(opener, launch_url)
    if "Loci Flow" not in home:
        raise FrozenWorkflowAcceptanceError
    match = _CSRF_PATTERN.search(home)
    if match is None:
        raise FrozenWorkflowAcceptanceError
    csrf = match.group(1)

    for relative in _REQUIRED_ASSETS:
        if not _read(opener, urljoin(base_url, relative)):
            raise FrozenWorkflowAcceptanceError

    encoded = urlencode({"csrf_token": csrf}).encode("ascii")
    request = Request(
        f"{base_url}/scan?surface=home",
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": base_url,
        },
    )
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise FrozenWorkflowAcceptanceError
        response.read()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = _read(opener, f"{base_url}/scan/status?surface=home")
        if "Scan completed" in status:
            return
        if "Scan failed" in status:
            raise FrozenWorkflowAcceptanceError
        time.sleep(0.05)
    raise FrozenWorkflowAcceptanceError


def _read(opener, url: str) -> str:
    with opener.open(url, timeout=10) as response:
        if response.status != 200:
            raise FrozenWorkflowAcceptanceError
        return response.read().decode("utf-8")


def _validate_bounded_diagnostics(
    log_path: Path,
    *,
    forbidden: tuple[str, ...],
) -> None:
    try:
        content = log_path.read_text(encoding="ascii")
    except OSError as error:
        raise FrozenWorkflowAcceptanceError from error
    if any(value in content for value in forbidden):
        raise FrozenWorkflowAcceptanceError
    for line in content.splitlines():
        payload = json.loads(line)
        if not isinstance(payload, dict) or not set(payload) <= _DIAGNOSTIC_KEYS:
            raise FrozenWorkflowAcceptanceError
        if payload.get("phase") not in ALLOWED_PHASES:
            raise FrozenWorkflowAcceptanceError
        if payload.get("code") not in ALLOWED_CODES:
            raise FrozenWorkflowAcceptanceError
        exception_class = payload.get("exception_class")
        if (
            exception_class is not None
            and exception_class not in ALLOWED_EXCEPTION_CLASSES
        ):
            raise FrozenWorkflowAcceptanceError
