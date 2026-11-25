from __future__ import annotations

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread

from organizer.application.recovery_continue_context import (
    RECOVERY_CONTINUE_ACTION,
    RecoveryContinueContextError,
    RecoveryContinueContextState,
    RecoveryContinueContextStore,
)
from organizer.application.recovery_continue_eligibility import (
    RecoveryContinueFamily,
)

_ROOT = "a" * 64
_OPERATION = "11111111-1111-4111-8111-111111111111"
_PARENT = "22222222-2222-4222-8222-222222222222"


class RecoveryContinueContextStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current = [datetime(2026, 7, 27, tzinfo=UTC)]
        self.ids = iter(str(uuid.uuid4()) for _ in range(40))
        self.store = RecoveryContinueContextStore(
            _ROOT,
            now=lambda: self.current[0],
            id_source=lambda: next(self.ids),
        )

    def test_issue_is_opaque_five_minute_and_redacted(self) -> None:
        context = self._issue()
        self.assertEqual(context.state, RecoveryContinueContextState.AVAILABLE)
        self.assertEqual(context.action_kind, RECOVERY_CONTINUE_ACTION)
        self.assertEqual(
            datetime.fromisoformat(context.expires_at)
            - datetime.fromisoformat(context.issued_at),
            timedelta(minutes=5),
        )
        uuid.UUID(context.case_id)
        uuid.UUID(context.action_id)
        for secret in (_ROOT, _OPERATION, _PARENT, "b" * 64, "e" * 64):
            self.assertNotIn(secret, repr(context))

    def test_wrong_binding_does_not_consume_and_correct_binding_is_one_use(self) -> None:
        context = self._issue()
        for changes in (
            {"browser_session_id": "other"},
            {"configured_root_identity": "f" * 64},
            {"action_id": str(uuid.uuid4())},
            {"action_kind": "other"},
        ):
            arguments = self._consume(context)
            arguments.update(changes)
            with self.assertRaises(RecoveryContinueContextError) as raised:
                self.store.consume_bound(**arguments)
            self.assertEqual(raised.exception.code, "binding_mismatch")
            self.assertEqual(
                self.store.snapshot(context.case_id).state,
                RecoveryContinueContextState.AVAILABLE,
            )
        consumed = self.store.consume_bound(**self._consume(context))
        self.assertEqual(consumed.state, RecoveryContinueContextState.CONSUMED)
        with self.assertRaises(RecoveryContinueContextError) as raised:
            self.store.consume_bound(**self._consume(context))
        self.assertEqual(raised.exception.code, "consumed")

    def test_expiry_and_concurrent_consumption_never_restore_availability(self) -> None:
        expired = self._issue()
        self.current[0] += timedelta(minutes=5)
        with self.assertRaises(RecoveryContinueContextError) as raised:
            self.store.consume_bound(**self._consume(expired))
        self.assertEqual(raised.exception.code, "expired")
        self.assertEqual(
            self.store.snapshot(expired.case_id).state,
            RecoveryContinueContextState.CONSUMED,
        )

        self.current[0] -= timedelta(minutes=5)
        context = self._issue()
        barrier = Barrier(3)
        outcomes: list[str] = []

        def consume() -> None:
            barrier.wait()
            try:
                self.store.consume_bound(**self._consume(context))
            except RecoveryContinueContextError as error:
                outcomes.append(error.code)
            else:
                outcomes.append("success")

        threads = (Thread(target=consume), Thread(target=consume))
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["success", "consumed"])

    def test_full_store_rejects_without_evicting_valid_context(self) -> None:
        store = RecoveryContinueContextStore(
            _ROOT,
            now=lambda: self.current[0],
            id_source=lambda: next(self.ids),
            capacity=1,
        )
        first = self._issue(store)
        with self.assertRaises(RecoveryContinueContextError) as raised:
            self._issue(store)
        self.assertEqual(raised.exception.code, "capacity_unavailable")
        self.assertEqual(store.snapshot(first.case_id), first)

    def _issue(self, store=None):
        return (store or self.store).issue(
            browser_session_id="session",
            target_family=RecoveryContinueFamily.ORGANIZATION_UNDO,
            operation_id=_OPERATION,
            parent_operation_id=_PARENT,
            durable_record_digest="b" * 64,
            completed_subset_digest="c" * 64,
            pending_subset_digest="d" * 64,
            filesystem_observation_digest="e" * 64,
        )

    @staticmethod
    def _consume(context) -> dict[str, str]:
        return {
            "case_id": context.case_id,
            "action_id": context.action_id,
            "browser_session_id": "session",
            "configured_root_identity": _ROOT,
            "action_kind": RECOVERY_CONTINUE_ACTION,
        }


if __name__ == "__main__":
    unittest.main()
