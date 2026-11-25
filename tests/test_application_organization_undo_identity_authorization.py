from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread

from organizer.application.organization_undo_identity_authorization import (
    IDENTITY_V2_UNDO_MODE,
    V2_UNDO_AUTHORIZATION_AVAILABLE,
    V2_UNDO_AUTHORIZATION_CONSUMED,
    V2_UNDO_AUTHORIZATION_EXPIRED,
    V2_UNDO_AUTHORIZATION_INVALIDATED,
    V2UndoAuthorizationError,
    V2UndoAuthorizationStore,
    canonical_parent_record_digest,
    canonical_reverse_evidence_digest,
)
from organizer.application.organization_undo_identity_eligibility import (
    OrganizationUndoIdentityEligibilityInspector,
)
from tests.test_application_undo_identity_eligibility import _stored_parent

_TOKEN = "11111111-1111-4111-8111-111111111111"
_NOW = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


class V2UndoAuthorizationTests(unittest.TestCase):
    def test_binding_digests_are_deterministic_and_evidence_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            operations, undos, parent = _stored_parent(
                Path(directory).resolve(), (b"alpha", b"beta")
            )
            eligibility = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id)

            self.assertEqual(
                canonical_parent_record_digest(parent),
                canonical_parent_record_digest(operations.load(parent.operation_id)),
            )
            self.assertEqual(
                canonical_reverse_evidence_digest(eligibility),
                canonical_reverse_evidence_digest(eligibility),
            )
            changed = replace(
                eligibility,
                captures=tuple(reversed(eligibility.captures)),
            )
            self.assertNotEqual(
                canonical_reverse_evidence_digest(eligibility),
                canonical_reverse_evidence_digest(changed),
            )

    def test_session_root_parent_mode_expiry_and_one_use_are_enforced(self) -> None:
        current = [_NOW]
        root_identity = "a" * 64
        store = V2UndoAuthorizationStore(
            root_identity,
            now=lambda: current[0],
            id_source=lambda: _TOKEN,
            lifetime=timedelta(minutes=1),
        )
        parent_id = "22222222-2222-4222-8222-222222222222"
        authorization = store.issue(
            browser_session_id="session-one",
            parent_operation_id=parent_id,
            parent_record_digest="b" * 64,
            reverse_evidence_digest="c" * 64,
        )
        self.assertEqual(authorization.state, V2_UNDO_AUTHORIZATION_AVAILABLE)
        for kwargs in (
            {"browser_session_id": "wrong", "parent_operation_id": parent_id},
            {
                "browser_session_id": "session-one",
                "parent_operation_id": "33333333-3333-4333-8333-333333333333",
            },
            {
                "browser_session_id": "session-one",
                "parent_operation_id": parent_id,
                "mode": "legacy",
            },
        ):
            with self.assertRaises(V2UndoAuthorizationError) as raised:
                store.require_available(_TOKEN, **kwargs)
            self.assertEqual(raised.exception.code, "binding_mismatch")
        self.assertEqual(store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_AVAILABLE)

        self.assertTrue(store.consume(_TOKEN))
        self.assertFalse(store.consume(_TOKEN))
        self.assertEqual(store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_CONSUMED)
        with self.assertRaises(V2UndoAuthorizationError):
            store.require_available(
                _TOKEN,
                browser_session_id="session-one",
                parent_operation_id=parent_id,
                mode=IDENTITY_V2_UNDO_MODE,
            )

        expiring = V2UndoAuthorizationStore(
            root_identity,
            now=lambda: current[0],
            id_source=lambda: "44444444-4444-4444-8444-444444444444",
            lifetime=timedelta(seconds=1),
        )
        token = expiring.issue(
            browser_session_id="session",
            parent_operation_id=parent_id,
            parent_record_digest="d" * 64,
            reverse_evidence_digest="e" * 64,
        )
        current[0] += timedelta(seconds=2)
        with self.assertRaises(V2UndoAuthorizationError) as raised:
            expiring.require_available(
                token.authorization_id,
                browser_session_id="session",
                parent_operation_id=parent_id,
            )
        self.assertEqual(raised.exception.code, "expired")
        self.assertEqual(
            expiring.snapshot(token.authorization_id).state,
            V2_UNDO_AUTHORIZATION_EXPIRED,
        )

    def test_invalidation_and_concurrent_consumption_are_atomic(self) -> None:
        store = V2UndoAuthorizationStore(
            "a" * 64, now=lambda: _NOW, id_source=lambda: _TOKEN
        )
        parent_id = "22222222-2222-4222-8222-222222222222"
        store.issue(
            browser_session_id="session",
            parent_operation_id=parent_id,
            parent_record_digest="b" * 64,
            reverse_evidence_digest="c" * 64,
        )
        barrier = Barrier(3)
        outcomes: list[bool] = []

        def consume() -> None:
            barrier.wait()
            outcomes.append(store.consume(_TOKEN))

        threads = (Thread(target=consume), Thread(target=consume))
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertFalse(store.invalidate(_TOKEN))

        other = V2UndoAuthorizationStore(
            "a" * 64,
            now=lambda: _NOW,
            id_source=lambda: "55555555-5555-4555-8555-555555555555",
        )
        token = other.issue(
            browser_session_id="session",
            parent_operation_id=parent_id,
            parent_record_digest="b" * 64,
            reverse_evidence_digest="c" * 64,
        )
        self.assertTrue(other.invalidate(token.authorization_id))
        self.assertEqual(
            other.snapshot(token.authorization_id).state,
            V2_UNDO_AUTHORIZATION_INVALIDATED,
        )

    def test_repr_and_errors_do_not_disclose_bindings(self) -> None:
        store = V2UndoAuthorizationStore(
            "a" * 64, now=lambda: _NOW, id_source=lambda: _TOKEN
        )
        authorization = store.issue(
            browser_session_id="private-session",
            parent_operation_id="22222222-2222-4222-8222-222222222222",
            parent_record_digest="b" * 64,
            reverse_evidence_digest="c" * 64,
        )
        rendered = repr(authorization)
        for secret in (
            _TOKEN,
            "private-session",
            "a" * 64,
            "b" * 64,
            "c" * 64,
        ):
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
