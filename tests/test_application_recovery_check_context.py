from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread
import unittest
import uuid

from organizer.application.recovery_check_context import (
    RECOVERY_CHECK_ACTION,
    RecoveryCheckContextError,
    RecoveryCheckContextState,
    RecoveryCheckContextStore,
    RecoveryCheckFamily,
)


_ROOT = "a" * 64
_OPERATION = "11111111-1111-4111-8111-111111111111"
_PARENT = "22222222-2222-4222-8222-222222222222"


class RecoveryCheckContextStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current = [datetime(2026, 7, 27, tzinfo=timezone.utc)]
        self.ids = iter(str(uuid.uuid4()) for _ in range(40))
        self.store = RecoveryCheckContextStore(
            _ROOT,
            now=lambda: self.current[0],
            id_source=lambda: next(self.ids),
        )

    def test_issue_is_opaque_bounded_and_redacted(self) -> None:
        context = self._issue()

        self.assertEqual(context.state, RecoveryCheckContextState.AVAILABLE)
        self.assertEqual(context.action_kind, RECOVERY_CHECK_ACTION)
        self.assertEqual(
            datetime.fromisoformat(context.expires_at)
            - datetime.fromisoformat(context.issued_at),
            timedelta(minutes=5),
        )
        uuid.UUID(context.case_id)
        uuid.UUID(context.action_id)
        rendered = repr(context)
        for secret in (
            _ROOT,
            _OPERATION,
            _PARENT,
            "b" * 64,
            "c" * 64,
        ):
            self.assertNotIn(secret, rendered)

    def test_wrong_binding_does_not_consume_then_correct_binding_does(self) -> None:
        context = self._issue()
        for changes in (
            {"browser_session_id": "other"},
            {"configured_root_identity": "d" * 64},
            {"action_id": str(uuid.uuid4())},
            {"action_kind": "other"},
        ):
            arguments = self._consume_arguments(context)
            arguments.update(changes)
            with self.assertRaises(RecoveryCheckContextError) as raised:
                self.store.consume_bound(**arguments)
            self.assertEqual(raised.exception.code, "binding_mismatch")
            self.assertEqual(
                self.store.snapshot(context.case_id).state,
                RecoveryCheckContextState.AVAILABLE,
            )

        consumed = self.store.consume_bound(
            **self._consume_arguments(context)
        )
        self.assertEqual(consumed.state, RecoveryCheckContextState.CONSUMED)
        with self.assertRaises(RecoveryCheckContextError) as raised:
            self.store.consume_bound(**self._consume_arguments(context))
        self.assertEqual(raised.exception.code, "consumed")

    def test_expired_context_is_consumed_and_cannot_be_reused(self) -> None:
        context = self._issue()
        self.current[0] += timedelta(minutes=5)

        with self.assertRaises(RecoveryCheckContextError) as raised:
            self.store.consume_bound(**self._consume_arguments(context))

        self.assertEqual(raised.exception.code, "expired")
        self.assertEqual(
            self.store.snapshot(context.case_id).state,
            RecoveryCheckContextState.CONSUMED,
        )

    def test_concurrent_consumption_has_exactly_one_winner(self) -> None:
        context = self._issue()
        barrier = Barrier(3)
        outcomes: list[str] = []

        def consume() -> None:
            barrier.wait()
            try:
                self.store.consume_bound(
                    **self._consume_arguments(context)
                )
            except RecoveryCheckContextError as error:
                outcomes.append(error.code)
            else:
                outcomes.append("success")

        threads = [Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertCountEqual(outcomes, ["success", "consumed"])
        self.assertEqual(
            self.store.snapshot(context.case_id).state,
            RecoveryCheckContextState.CONSUMED,
        )

    def test_capacity_never_evicts_available_contexts_and_prunes_consumed(self) -> None:
        store = RecoveryCheckContextStore(
            _ROOT,
            now=lambda: self.current[0],
            id_source=lambda: next(self.ids),
            capacity=1,
        )
        first = self._issue(store)
        with self.assertRaises(RecoveryCheckContextError) as raised:
            self._issue(store)
        self.assertEqual(raised.exception.code, "capacity_unavailable")
        self.assertEqual(store.snapshot(first.case_id), first)

        store.consume_bound(**self._consume_arguments(first))
        second = self._issue(store)
        self.assertIsNone(store.snapshot(first.case_id))
        self.assertEqual(
            store.snapshot(second.case_id).state,
            RecoveryCheckContextState.AVAILABLE,
        )

    def _issue(
        self,
        store: RecoveryCheckContextStore | None = None,
    ):
        return (store or self.store).issue(
            browser_session_id="session",
            target_family=RecoveryCheckFamily.ORGANIZATION_UNDO,
            operation_id=_OPERATION,
            parent_operation_id=_PARENT,
            durable_record_digest="b" * 64,
            filesystem_observation_digest="c" * 64,
        )

    @staticmethod
    def _consume_arguments(context) -> dict[str, str]:
        return {
            "case_id": context.case_id,
            "action_id": context.action_id,
            "browser_session_id": "session",
            "configured_root_identity": _ROOT,
            "action_kind": RECOVERY_CHECK_ACTION,
        }


if __name__ == "__main__":
    unittest.main()
