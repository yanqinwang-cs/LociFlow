from __future__ import annotations

from dataclasses import asdict
from threading import Barrier, Event, Thread
import unittest

from organizer.application.runtime_lifecycle import (
    APPLICATION_STOPPING_MESSAGE,
    ApplicationStoppingError,
    RuntimeLifecycle,
    RuntimeOperationKind,
    RuntimeSnapshot,
)
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator


class RuntimeLifecycleTests(unittest.TestCase):
    def test_initial_snapshot_is_immutable_running_and_idle(self) -> None:
        lifecycle = RuntimeLifecycle()

        snapshot = lifecycle.snapshot()

        self.assertEqual(snapshot, RuntimeSnapshot(False, 0, ()))
        with self.assertRaises(AttributeError):
            snapshot.active_count = 1  # type: ignore[misc]

    def test_operation_kinds_and_simultaneous_registrations_are_counted(self) -> None:
        lifecycle = RuntimeLifecycle()
        scan = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        movement = lifecycle.begin_operation(
            RuntimeOperationKind.ORGANIZATION_MOVEMENT
        )
        second_movement = lifecycle.begin_operation(
            RuntimeOperationKind.ORGANIZATION_MOVEMENT
        )

        self.assertEqual(
            lifecycle.snapshot(),
            RuntimeSnapshot(
                False,
                3,
                (
                    RuntimeOperationKind.ORGANIZATION_MOVEMENT,
                    RuntimeOperationKind.SCAN,
                ),
            ),
        )

        self.assertTrue(scan.finish())
        self.assertTrue(movement.finish())
        self.assertTrue(second_movement.finish())
        self.assertEqual(lifecycle.snapshot().active_count, 0)

    def test_shutdown_is_monotonic_idempotent_and_rejects_new_work(self) -> None:
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)

        first = lifecycle.begin_shutdown()
        second = lifecycle.begin_shutdown()

        self.assertEqual(first, second)
        self.assertTrue(first.is_stopping)
        self.assertEqual(first.active_count, 1)
        with self.assertRaisesRegex(
            ApplicationStoppingError,
            APPLICATION_STOPPING_MESSAGE,
        ):
            lifecycle.begin_operation(RuntimeOperationKind.ACTIVITY_UNDO)
        self.assertEqual(lifecycle.snapshot().active_count, 1)
        self.assertTrue(lease.finish())
        self.assertEqual(lifecycle.snapshot(), RuntimeSnapshot(True, 0, ()))

    def test_context_manager_releases_on_success_and_exception(self) -> None:
        lifecycle = RuntimeLifecycle()

        with lifecycle.begin_operation(RuntimeOperationKind.DUPLICATE_BIN_MOVEMENT):
            self.assertEqual(lifecycle.snapshot().active_count, 1)
        self.assertEqual(lifecycle.snapshot().active_count, 0)

        with self.assertRaisesRegex(RuntimeError, "operation failed"):
            with lifecycle.begin_operation(RuntimeOperationKind.DUPLICATE_BIN_RESTORE):
                raise RuntimeError("operation failed")
        self.assertEqual(lifecycle.snapshot().active_count, 0)

    def test_duplicate_completion_is_harmless_and_never_decrements_twice(self) -> None:
        lifecycle = RuntimeLifecycle()
        first = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        second = lifecycle.begin_operation(RuntimeOperationKind.ACTIVITY_UNDO)

        self.assertTrue(first.finish())
        self.assertFalse(first.finish())
        self.assertEqual(lifecycle.snapshot().active_count, 1)
        self.assertTrue(second.finish())

    def test_wait_until_idle_returns_immediately_when_idle_or_already_finished(self) -> None:
        lifecycle = RuntimeLifecycle()

        self.assertEqual(lifecycle.wait_until_idle().active_count, 0)
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        lease.finish()
        self.assertEqual(lifecycle.wait_until_idle().active_count, 0)

    def test_completion_wakes_all_waiters_without_lost_wakeup(self) -> None:
        lifecycle = RuntimeLifecycle()
        lease = lifecycle.begin_operation(RuntimeOperationKind.SCAN)
        ready = Barrier(3)
        completed = [Event(), Event()]
        snapshots: list[RuntimeSnapshot] = []

        def wait(index: int) -> None:
            ready.wait()
            snapshots.append(lifecycle.wait_until_idle())
            completed[index].set()

        waiters = [Thread(target=wait, args=(index,)) for index in range(2)]
        for waiter in waiters:
            waiter.start()
        ready.wait()
        self.assertFalse(completed[0].is_set())
        self.assertFalse(completed[1].is_set())

        lifecycle.begin_shutdown()
        lease.finish()

        for event in completed:
            self.assertTrue(event.wait(1))
        for waiter in waiters:
            waiter.join(1)
            self.assertFalse(waiter.is_alive())
        self.assertEqual(snapshots, [RuntimeSnapshot(True, 0, ())] * 2)

    def test_shutdown_and_admission_have_one_atomic_winner(self) -> None:
        for _ in range(50):
            lifecycle = RuntimeLifecycle()
            start = Barrier(3)
            outcome: list[str] = []
            lease_holder = []

            def admit() -> None:
                start.wait()
                try:
                    lease_holder.append(
                        lifecycle.begin_operation(RuntimeOperationKind.SCAN)
                    )
                except ApplicationStoppingError:
                    outcome.append("rejected")
                else:
                    outcome.append("admitted")

            def stop() -> None:
                start.wait()
                lifecycle.begin_shutdown()

            threads = [Thread(target=admit), Thread(target=stop)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(1)
                self.assertFalse(thread.is_alive())

            self.assertEqual(len(outcome), 1)
            snapshot = lifecycle.snapshot()
            self.assertTrue(snapshot.is_stopping)
            if outcome == ["admitted"]:
                self.assertEqual(snapshot.active_count, 1)
                lease_holder[0].finish()
            else:
                self.assertEqual(snapshot.active_count, 0)

    def test_snapshots_are_safe_and_lifecycle_instances_are_independent(self) -> None:
        first = RuntimeLifecycle()
        second = RuntimeLifecycle()
        first.begin_operation(RuntimeOperationKind.ACTIVITY_UNDO)
        first.begin_shutdown()

        values = asdict(first.snapshot())
        serialized = repr(values)
        for forbidden in ("/Users/", "token", "sha256", "operation_id"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(second.snapshot(), RuntimeSnapshot(False, 0, ()))

    def test_invalid_kind_is_rejected_without_registration(self) -> None:
        lifecycle = RuntimeLifecycle()

        with self.assertRaisesRegex(TypeError, "operation kind"):
            lifecycle.begin_operation("scan")  # type: ignore[arg-type]

        self.assertEqual(lifecycle.snapshot().active_count, 0)

    def test_execution_coordinator_admits_before_lock_and_releases_after_unlock(self) -> None:
        lifecycle = RuntimeLifecycle()
        coordinator = OrganizationExecutionCoordinator(lifecycle)

        self.assertTrue(
            coordinator.try_acquire(RuntimeOperationKind.ORGANIZATION_MOVEMENT)
        )
        self.assertEqual(lifecycle.snapshot().active_count, 1)
        self.assertFalse(
            coordinator.try_acquire(RuntimeOperationKind.DUPLICATE_BIN_MOVEMENT)
        )
        self.assertEqual(lifecycle.snapshot().active_count, 1)

        lifecycle.begin_shutdown()
        coordinator.release()

        self.assertEqual(lifecycle.wait_until_idle(), RuntimeSnapshot(True, 0, ()))
        with self.assertRaises(ApplicationStoppingError):
            coordinator.try_acquire(RuntimeOperationKind.ACTIVITY_UNDO)


if __name__ == "__main__":
    unittest.main()
