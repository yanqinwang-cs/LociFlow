from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest import mock

from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.application.view_models import ScanApplicationResult, ScanSummary
from organizer.web.scan_jobs import ScanAlreadyRunning, ScanJobController


def _result(root: Path) -> ScanApplicationResult:
    return ScanApplicationResult(
        root=root,
        report={"summary": {}, "warnings": []},
        summary=ScanSummary(1, 10, 0, 0, 0, 0),
        warnings=(),
    )


class ScanJobControllerTests(unittest.TestCase):
    def test_scan_lease_is_active_until_completed_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered = Event()
            release = Event()
            lifecycle = RuntimeLifecycle()

            def scan(path: Path) -> ScanApplicationResult:
                entered.set()
                release.wait(timeout=2)
                return _result(path)

            controller = ScanJobController(
                root,
                scan=scan,
                write_report=lambda _: root / "report.json",
                runtime_lifecycle=lifecycle,
            )

            controller.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(lifecycle.snapshot().active_count, 1)
            self.assertEqual(
                lifecycle.snapshot().active_kinds,
                (RuntimeOperationKind.SCAN,),
            )
            stopping = lifecycle.begin_shutdown()
            self.assertEqual(stopping.active_count, 1)
            release.set()
            lifecycle.wait_until_idle()

            self.assertEqual(controller.snapshot().status, "completed")
            self.assertTrue(lifecycle.snapshot().is_stopping)
            controller.join_worker()

    def test_scan_failure_is_published_before_lease_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = RuntimeLifecycle()
            controller = ScanJobController(
                root,
                scan=mock.Mock(side_effect=RuntimeError("private")),
                write_report=lambda _: root / "report.json",
                runtime_lifecycle=lifecycle,
            )

            controller.start()
            lifecycle.wait_until_idle()

            self.assertEqual(controller.snapshot().status, "failed")
            self.assertEqual(lifecycle.snapshot().active_count, 0)
            controller.join_worker()

    def test_stopping_rejects_before_snapshot_mutation_or_thread_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = RuntimeLifecycle()
            lifecycle.begin_shutdown()
            controller = ScanJobController(
                Path(directory),
                runtime_lifecycle=lifecycle,
            )

            with mock.patch("organizer.web.scan_jobs.Thread") as thread_type:
                with self.assertRaises(ApplicationStoppingError):
                    controller.start()

            thread_type.assert_not_called()
            self.assertEqual(controller.snapshot().status, "not_started")
            self.assertEqual(lifecycle.snapshot().active_count, 0)

    def test_worker_is_retained_non_daemon_and_joinable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = Event()
            controller = ScanJobController(
                root,
                scan=lambda path: (gate.wait(timeout=2), _result(path))[1],
                write_report=lambda _: root / "report.json",
            )

            controller.start()
            worker = controller._worker

            self.assertIsNotNone(worker)
            assert worker is not None
            self.assertFalse(worker.daemon)
            self.assertTrue(worker.is_alive())
            gate.set()
            controller.join_worker()
            self.assertFalse(worker.is_alive())

    def test_thread_start_failure_publishes_failure_and_releases_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = RuntimeLifecycle()
            controller = ScanJobController(
                Path(directory),
                runtime_lifecycle=lifecycle,
            )
            worker = mock.Mock()
            worker.start.side_effect = RuntimeError("thread unavailable")

            with mock.patch("organizer.web.scan_jobs.Thread", return_value=worker):
                with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                    controller.start()

            self.assertEqual(controller.snapshot().status, "failed")
            self.assertEqual(lifecycle.snapshot().active_count, 0)
            self.assertIsNone(controller._worker)
            self.assertEqual(controller._workers, [])

    def test_join_retains_ownership_of_every_started_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = ScanJobController(
                root,
                scan=lambda path: _result(path),
                write_report=lambda _: root / "report.json",
            )

            controller.start()
            self._wait_for_status(controller, "completed")
            first = controller._worker
            assert first is not None
            first.join()

            controller.start()
            self._wait_for_status(controller, "completed")
            second = controller._worker
            assert second is not None
            controller.join_worker()

            self.assertEqual(controller._workers, [first, second])
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())

    def test_successful_job_writes_before_completed_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            written = root / "AI_Review" / "reports" / "report.json"
            gate = Event()

            def scan(path: Path) -> ScanApplicationResult:
                gate.wait(timeout=2)
                return _result(path)

            controller = ScanJobController(
                root,
                scan=scan,
                write_report=lambda result: written,
            )

            started = controller.start()
            self.assertEqual(started.status, "scanning")
            self.assertIsNotNone(started.job_id)
            gate.set()
            self._wait_for_status(controller, "completed")
            self.assertEqual(controller.snapshot().status, "completed")
            self.assertEqual(controller.snapshot().report_path, written)

    def test_duplicate_start_is_rejected_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = Event()
            root = Path(directory)

            def scan(path: Path) -> ScanApplicationResult:
                gate.wait(timeout=2)
                return _result(path)

            controller = ScanJobController(root, scan=scan, write_report=lambda _: root / "report.json")
            controller.start()
            with self.assertRaises(ScanAlreadyRunning):
                controller.start()
            gate.set()
            self._wait_for_status(controller, "completed")

    def test_scan_and_report_failures_are_user_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = ScanJobController(
                root,
                scan=lambda _: (_ for _ in ()).throw(RuntimeError("secret detail")),
                write_report=lambda _: root / "report.json",
            )
            controller.start()
            self._wait_for_status(controller, "failed")
            snapshot = controller.snapshot()
            self.assertNotIn("secret detail", snapshot.error_message or "")
            self.assertNotIn("Traceback", snapshot.error_message or "")

            controller = ScanJobController(
                root,
                scan=lambda path: _result(path),
                write_report=lambda _: (_ for _ in ()).throw(OSError("secret detail")),
            )
            controller.start()
            self._wait_for_status(controller, "failed")
            self.assertIsNone(controller.snapshot().result)

    def test_stale_generation_cannot_overwrite_newer_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = ScanJobController(
                root,
                scan=lambda path: _result(path),
                write_report=lambda _: root / "report.json",
            )
            first = controller.start()
            self._wait_for_status(controller, "completed")
            second = controller.start()
            controller._fail(first.job_id or "", RuntimeError("stale"))
            self.assertEqual(controller.snapshot().job_id, second.job_id)
            self._wait_for_status(controller, "completed")

    @staticmethod
    def _wait_for_status(controller: ScanJobController, status: str) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if controller.snapshot().status == status:
                return
            time.sleep(0.01)
        raise AssertionError(f"job did not reach {status}")


if __name__ == "__main__":
    unittest.main()
