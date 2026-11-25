from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, Thread, current_thread

from organizer.application.runtime_lifecycle import (
    OperationLease,
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.application.scan_service import (
    ScanApplicationResult,
    scan_root,
    write_scan_report,
)

logger = logging.getLogger(__name__)


class ScanAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanJobSnapshot:
    status: str
    job_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: ScanApplicationResult | None = None
    report_path: Path | None = None
    error_message: str | None = None


ScanCallable = Callable[[Path], ScanApplicationResult]
ReportWriter = Callable[[ScanApplicationResult], Path]


class ScanJobController:
    """Own one root-bound, in-process scan job without web-framework concerns."""

    def __init__(
        self,
        root: Path,
        *,
        scan: ScanCallable = scan_root,
        write_report: ReportWriter = write_scan_report,
        runtime_lifecycle: RuntimeLifecycle | None = None,
    ) -> None:
        self._root = root.resolve()
        self._scan = scan
        self._write_report = write_report
        self._runtime_lifecycle = runtime_lifecycle or RuntimeLifecycle()
        self._lock = Lock()
        self._snapshot = ScanJobSnapshot(status="not_started")
        self._worker: Thread | None = None
        self._workers: list[Thread] = []

    def snapshot(self) -> ScanJobSnapshot:
        with self._lock:
            return deepcopy(self._snapshot)

    def start(self) -> ScanJobSnapshot:
        job_id = secrets.token_urlsafe(18)
        lease = self._runtime_lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        with self._lock:
            if self._snapshot.status == "scanning":
                lease.finish()
                raise ScanAlreadyRunning("a scan is already running")
            self._snapshot = ScanJobSnapshot(
                status="scanning",
                job_id=job_id,
                started_at=_now(),
            )
        worker: Thread | None = None
        try:
            worker = Thread(
                target=self._run,
                args=(job_id, lease),
                name="bootai-scan-job",
                daemon=False,
            )
            with self._lock:
                self._worker = worker
                self._workers.append(worker)
            worker.start()
        except Exception as error:
            self._fail(job_id, error)
            with self._lock:
                if self._worker is worker:
                    self._worker = None
                if worker in self._workers:
                    self._workers.remove(worker)
            lease.finish()
            raise
        return self.snapshot()

    def join_worker(self) -> None:
        with self._lock:
            workers = tuple(self._workers)
        if current_thread() in workers:
            raise RuntimeError("scan worker cannot join itself")
        for worker in workers:
            worker.join()

    def _run(self, job_id: str, lease: OperationLease) -> None:
        try:
            result = self._scan(self._root)
            report_path = self._write_report(result)
        except Exception as error:
            logger.error(
                "scan job failed for generation %s (%s)",
                job_id,
                type(error).__name__,
            )
            self._fail(job_id, error)
        else:
            self._complete(job_id, result, report_path)
        finally:
            lease.finish()

    def _complete(
        self,
        job_id: str,
        result: ScanApplicationResult,
        report_path: Path,
    ) -> None:
        with self._lock:
            if self._snapshot.job_id != job_id:
                return
            self._snapshot = ScanJobSnapshot(
                status="completed",
                job_id=job_id,
                started_at=self._snapshot.started_at,
                completed_at=_now(),
                result=result,
                report_path=report_path,
            )

    def _fail(self, job_id: str, error: Exception) -> None:
        del error
        with self._lock:
            if self._snapshot.job_id != job_id:
                return
            self._snapshot = ScanJobSnapshot(
                status="failed",
                job_id=job_id,
                started_at=self._snapshot.started_at,
                completed_at=_now(),
                error_message=(
                    "The scan could not be completed. "
                    "No files were moved. Please try again."
                ),
            )


def _now() -> datetime:
    return datetime.now(UTC)
