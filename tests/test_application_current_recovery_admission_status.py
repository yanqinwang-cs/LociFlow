from __future__ import annotations

from pathlib import Path
import tempfile
from threading import Barrier, Thread
import unittest
from unittest import mock

from organizer.application.current_recovery_admission_status import (
    CurrentRecoveryAdmissionStatus,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
    RecoveryAdmissionSnapshot,
    RecoveryAdmissionState,
)


class CurrentRecoveryAdmissionStatusTests(unittest.TestCase):
    def test_initializes_and_refreshes_from_coarse_inspector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inspector = FilesystemOperationInspector(Path(directory))
            blocked = RecoveryAdmissionSnapshot(
                RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
            )
            with mock.patch.object(
                inspector,
                "inspect",
                side_effect=[
                    RecoveryAdmissionSnapshot(RecoveryAdmissionState.CLEAR),
                    blocked,
                ],
            ) as inspect:
                status = CurrentRecoveryAdmissionStatus(inspector)
                self.assertFalse(status.snapshot().is_blocked)
                self.assertEqual(status.refresh(), blocked)
                self.assertTrue(status.snapshot().is_blocked)
            self.assertEqual(inspect.call_count, 2)

    def test_inspection_failure_is_fail_closed_and_never_clears(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inspector = FilesystemOperationInspector(Path(directory))
            with mock.patch.object(
                inspector,
                "inspect",
                side_effect=OSError("private path"),
            ):
                status = CurrentRecoveryAdmissionStatus(inspector)
                self.assertTrue(status.snapshot().is_blocked)
                self.assertTrue(status.refresh().is_blocked)
                self.assertEqual(
                    status.snapshot().state,
                    RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE,
                )

    def test_snapshot_and_refresh_are_thread_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inspector = FilesystemOperationInspector(Path(directory))
            status = CurrentRecoveryAdmissionStatus(inspector)
            barrier = Barrier(3)
            results = []

            def read() -> None:
                barrier.wait()
                results.append(status.snapshot())

            threads = (Thread(target=read), Thread(target=read))
            for thread in threads:
                thread.start()
            barrier.wait()
            status.refresh()
            for thread in threads:
                thread.join()
            self.assertEqual(len(results), 2)
            self.assertTrue(
                all(isinstance(item, RecoveryAdmissionSnapshot) for item in results)
            )


if __name__ == "__main__":
    unittest.main()
