from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest import mock

import organizer.web.review_explorer as review_explorer_module
from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.application.scan_service import scan_root
from organizer.application.view_models import ReviewModule
from organizer.web.review_explorer import ReviewExplorerStore
from organizer.web.scan_jobs import ScanJobSnapshot


class ReviewExplorerStoreTests(unittest.TestCase):
    def test_store_clears_rows_for_running_and_failed_generations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = scan_root(root)
            store = ReviewExplorerStore(root)

            completed = store.snapshot(
                ScanJobSnapshot(status="completed", job_id="one", result=result)
            )
            self.assertIsNotNone(completed.session)

            running = store.snapshot(ScanJobSnapshot(status="scanning", job_id="two"))
            self.assertEqual(running.status, "scanning")
            self.assertIsNone(running.session)

            failed = store.snapshot(
                ScanJobSnapshot(
                    status="failed",
                    job_id="two",
                    error_message="safe failure",
                )
            )
            self.assertEqual(failed.status, "failed")
            self.assertIsNone(failed.session)

    def test_completed_generation_replaces_previous_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = scan_root(root)
            (root / "new.txt").write_text("new", encoding="utf-8")
            second = scan_root(root)
            store = ReviewExplorerStore(root)

            first_snapshot = store.snapshot(
                ScanJobSnapshot(status="completed", job_id="one", result=first)
            )
            second_snapshot = store.snapshot(
                ScanJobSnapshot(status="completed", job_id="two", result=second)
            )
            self.assertNotEqual(
                first_snapshot.generation_id, second_snapshot.generation_id
            )
            self.assertIsNot(first_snapshot.session, second_snapshot.session)

    def test_all_public_review_mutations_reject_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "alpha.txt").write_text("same", encoding="utf-8")
            (root / "alpha backup copy.txt").write_text("same", encoding="utf-8")
            result = scan_root(root)
            scan = ScanJobSnapshot(status="completed", job_id="one", result=result)
            lifecycle = RuntimeLifecycle()
            store = ReviewExplorerStore(root, runtime_lifecycle=lifecycle)
            pending = store.preview_page_decision(
                scan,
                "browser",
                "approved",
                project=lambda session: session,
                view_query=(),
            )
            assert pending.preview_token is not None
            before = store.snapshot(scan)
            assert before.session is not None
            lifecycle.begin_shutdown()

            mutations = (
                lambda: store.change_decision(
                    scan,
                    "untrusted-item-id",
                    "approved",
                    project=lambda session: session,
                ),
                lambda: store.preview_page_decision(
                    scan,
                    "browser",
                    "approved",
                    project=lambda session: session,
                    view_query=(),
                ),
                lambda: store.confirm_page_decision(
                    scan,
                    "browser",
                    pending.preview_token,
                    "APPROVE CURRENT PAGE",
                ),
                lambda: store.save(scan, project=lambda session: session),
                lambda: store.save_module(
                    scan,
                    ReviewModule.ORGANIZATION,
                    project=lambda session: session,
                ),
                lambda: store.revisit_skipped(scan, ReviewModule.ORGANIZATION),
            )
            for mutation in mutations:
                with self.assertRaises(ApplicationStoppingError):
                    mutation()

            after = store.snapshot(scan)
            self.assertEqual(after.session, before.session)
            self.assertEqual(after.module_queues, before.module_queues)
            self.assertIn(pending.preview_token, store._pending_page_decisions)
            self.assertEqual(lifecycle.snapshot().active_count, 0)

    def test_admitted_review_save_stays_active_through_write_and_exception(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = scan_root(root)
            scan = ScanJobSnapshot(status="completed", job_id="one", result=result)
            lifecycle = RuntimeLifecycle()
            store = ReviewExplorerStore(root, runtime_lifecycle=lifecycle)
            store.snapshot(scan)
            writing = Event()
            finish = Event()
            outcomes = []
            real_save = review_explorer_module.save_review_session

            def paused_save(session):
                self.assertEqual(
                    lifecycle.snapshot().active_kinds,
                    (RuntimeOperationKind.STATE_MUTATION,),
                )
                writing.set()
                finish.wait(timeout=2)
                return real_save(session)

            worker = Thread(
                target=lambda: outcomes.append(
                    store.save(scan, project=lambda session: session)
                )
            )
            with mock.patch.object(
                review_explorer_module,
                "save_review_session",
                side_effect=paused_save,
            ):
                worker.start()
                self.assertTrue(writing.wait(1))
                self.assertEqual(lifecycle.begin_shutdown().active_count, 1)
                finish.set()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(lifecycle.wait_until_idle().active_count, 0)

            failed_lifecycle = RuntimeLifecycle()
            failed = ReviewExplorerStore(
                root,
                runtime_lifecycle=failed_lifecycle,
            )
            failed.snapshot(scan)
            with (
                mock.patch.object(
                    review_explorer_module,
                    "save_review_session",
                    side_effect=OSError("write failed"),
                ),
                self.assertRaises(OSError),
            ):
                failed.save(scan, project=lambda session: session)
            self.assertEqual(failed_lifecycle.snapshot().active_count, 0)


if __name__ == "__main__":
    unittest.main()
