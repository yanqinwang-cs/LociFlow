from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from organizer.application.activity_service import list_activity_entries
from organizer.application.organization_undo_identity_verification import (
    IdentityUndoMismatchCategory,
    IdentityUndoVerificationResult,
    IdentityUndoVerificationState,
)
from organizer.application.organization_undo_models import (
    UNDO_RESTORED,
    UNDO_VERIFICATION_FAILED,
    UNDO_VERIFICATION_FAILED_STATE,
    UNDO_VERIFICATION_NOT_RUN,
    UNDO_VERIFICATION_PASSED,
)
from organizer.application.undo_identity_verification_publication import (
    IdentityUndoVerificationPublicationState,
    OrganizationUndoIdentityVerificationPublicationService,
)
from tests.test_application_organization_undo_identity_verification import (
    _restored_pair,
)

_COMPLETED = datetime(2026, 7, 23, 9, 10, 11, tzinfo=UTC)


class IdentityUndoVerificationPublicationTests(unittest.TestCase):
    def test_passed_verification_publishes_restored_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent, original = _stored_pair(
                root,
                (b"first", b"second"),
            )
            service = _service(operations, undos)

            with mock.patch.object(undos, "save", wraps=undos.save) as save:
                result = service.publish(original.undo_id)

            durable = undos.load(original.undo_id)
            self.assertEqual(
                result.state,
                IdentityUndoVerificationPublicationState.PUBLISHED_RESTORED,
            )
            self.assertTrue(result.published)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(durable.status, UNDO_RESTORED)
            self.assertEqual(durable.verification.status, UNDO_VERIFICATION_PASSED)
            self.assertEqual(durable.verification.verified_count, 2)
            self.assertEqual(durable.verification.mismatch_codes, ())
            self.assertEqual(durable.completed_at, _COMPLETED.isoformat())
            self.assertEqual(
                replace(
                    durable,
                    status=original.status,
                    completed_at=original.completed_at,
                    verification=original.verification,
                ),
                original,
            )
            self.assertEqual(operations.load(parent.operation_id), parent)

    def test_mismatch_publishes_bounded_terminal_failure_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, original = _stored_pair(
                root,
                (b"first", b"second"),
            )
            verifier = mock.Mock(
                return_value=IdentityUndoVerificationResult(
                    state=IdentityUndoVerificationState.MISMATCH,
                    affected_items=2,
                    verified_count=1,
                    mismatch_categories=(
                        IdentityUndoMismatchCategory.MOVED_FROM_PRESENT,
                        IdentityUndoMismatchCategory.RESTORED_IDENTITY_MISMATCH,
                    ),
                )
            )

            with mock.patch.object(undos, "save", wraps=undos.save) as save:
                result = _service(
                    operations,
                    undos,
                    verifier=verifier,
                ).publish(original.undo_id)

            durable = undos.load(original.undo_id)
            self.assertEqual(
                result.state,
                IdentityUndoVerificationPublicationState.PUBLISHED_VERIFICATION_FAILED,
            )
            self.assertEqual(verifier.call_count, 1)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(durable.status, UNDO_VERIFICATION_FAILED)
            self.assertEqual(
                durable.verification.status,
                UNDO_VERIFICATION_FAILED_STATE,
            )
            self.assertEqual(durable.verification.verified_count, 1)
            self.assertEqual(
                durable.verification.mismatch_codes,
                (
                    "undo_identity_moved_from_present",
                    "undo_identity_restored_mismatch",
                ),
            )
            self.assertEqual(durable.failure_code, UNDO_VERIFICATION_FAILED)
            rendered = repr(result)
            for secret in (
                str(root),
                "item-1",
                original.undo_id,
                original.operation_id,
                original.expected_restores[0].content_identity.digest,
            ):
                self.assertNotIn(secret, rendered)

    def test_indeterminate_invalid_result_and_unexpected_error_save_nothing(
        self,
    ) -> None:
        outcomes = (
            IdentityUndoVerificationResult(
                IdentityUndoVerificationState.INDETERMINATE,
                affected_items=1,
                verified_count=0,
                mismatch_categories=(),
            ),
            IdentityUndoVerificationResult(
                IdentityUndoVerificationState.PASSED,
                affected_items=1,
                verified_count=0,
                mismatch_categories=(),
            ),
            RuntimeError("private/path/item.txt"),
        )
        for outcome in outcomes:
            with (
                self.subTest(outcome=type(outcome).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                operations, undos, _parent, original = _stored_pair(
                    root,
                    (b"content",),
                )
                verifier = mock.Mock(
                    side_effect=outcome if isinstance(outcome, Exception) else None,
                    return_value=None if isinstance(outcome, Exception) else outcome,
                )
                with mock.patch.object(undos, "save") as save:
                    result = _service(
                        operations,
                        undos,
                        verifier=verifier,
                    ).publish(original.undo_id)

                self.assertEqual(
                    result.state,
                    IdentityUndoVerificationPublicationState.VERIFICATION_INDETERMINATE,
                )
                self.assertFalse(result.published)
                save.assert_not_called()
                self.assertEqual(undos.load(original.undo_id), original)
                self.assertEqual(
                    original.verification.status,
                    UNDO_VERIFICATION_NOT_RUN,
                )

    def test_stale_undo_and_parent_are_never_overwritten(self) -> None:
        for owner in ("undo", "parent"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operations, undos, parent, original = _stored_pair(
                    root,
                    (b"content",),
                )
                changed_undo = replace(original, failure_code="concurrent_change")
                changed_parent = replace(parent, failure_code="concurrent_change")

                def mutate_during_verification(*_args):
                    if owner == "undo":
                        undos.save(changed_undo)
                    else:
                        operations.save(changed_parent)
                    return IdentityUndoVerificationResult(
                        IdentityUndoVerificationState.PASSED,
                        affected_items=1,
                        verified_count=1,
                        mismatch_categories=(),
                    )

                result = _service(
                    operations,
                    undos,
                    verifier=mutate_during_verification,
                ).publish(original.undo_id)

                self.assertEqual(
                    result.state,
                    IdentityUndoVerificationPublicationState.STALE,
                )
                self.assertFalse(result.published)
                self.assertEqual(
                    undos.load(original.undo_id),
                    changed_undo if owner == "undo" else original,
                )
                self.assertEqual(
                    operations.load(parent.operation_id),
                    changed_parent if owner == "parent" else parent,
                )

    def test_terminal_and_ineligible_records_reject_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, original = _stored_pair(
                root,
                (b"content",),
            )
            terminal = replace(
                original,
                status=UNDO_RESTORED,
                completed_at=_COMPLETED.isoformat(),
                verification=replace(
                    original.verification,
                    status=UNDO_VERIFICATION_PASSED,
                    verified_count=1,
                ),
            )
            undos.save(terminal)
            verifier = mock.Mock()

            result = _service(
                operations,
                undos,
                verifier=verifier,
            ).publish(original.undo_id)

            self.assertEqual(
                result.state,
                IdentityUndoVerificationPublicationState.STALE,
            )
            verifier.assert_not_called()
            self.assertEqual(undos.load(original.undo_id), terminal)

    def test_process_control_propagates_and_save_failure_is_indeterminate(self) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit()):
            with (
                self.subTest(kind=type(interruption).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                operations, undos, _parent, original = _stored_pair(
                    root,
                    (b"content",),
                )
                with self.assertRaises(type(interruption)):
                    _service(
                        operations,
                        undos,
                        verifier=mock.Mock(side_effect=interruption),
                    ).publish(original.undo_id)
                self.assertEqual(undos.load(original.undo_id), original)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, original = _stored_pair(
                root,
                (b"content",),
            )
            with mock.patch.object(
                undos,
                "save",
                side_effect=OSError("private durable detail"),
            ) as save:
                result = _service(operations, undos).publish(original.undo_id)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(
                result.state,
                IdentityUndoVerificationPublicationState.VERIFICATION_INDETERMINATE,
            )
            self.assertEqual(undos.load(original.undo_id), original)

    def test_logs_are_not_required_and_activity_remains_repository_derived(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent, original = _stored_pair(
                root,
                (b"content",),
            )
            self.assertIsNone(parent.legacy_operation_log)
            self.assertIsNone(original.legacy_undo_log)
            before = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )

            result = _service(operations, undos).publish(original.undo_id)

            after = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )
            before_restore = next(
                item for item in before.activity if item.id == original.undo_id
            )
            after_restore = next(
                item for item in after.activity if item.id == original.undo_id
            )
            parent_entry = next(
                item for item in after.activity if item.id == parent.operation_id
            )
            self.assertEqual(
                result.state,
                IdentityUndoVerificationPublicationState.PUBLISHED_RESTORED,
            )
            self.assertEqual(len(after.activity), len(before.activity))
            self.assertEqual(after_restore.id, before_restore.id)
            self.assertEqual(after_restore.status, UNDO_RESTORED)
            self.assertEqual(
                after_restore.verification_status,
                UNDO_VERIFICATION_PASSED,
            )
            self.assertFalse(parent_entry.undoable)

    def test_mismatch_and_indeterminate_update_no_extra_activity_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, original = _stored_pair(
                root,
                (b"content",),
            )
            target = root / original.expected_restores[0].restore_target
            target.write_bytes(b"changed")
            before = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )

            result = _service(operations, undos).publish(original.undo_id)

            after = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )
            restore = next(
                item for item in after.activity if item.id == original.undo_id
            )
            self.assertEqual(
                result.state,
                IdentityUndoVerificationPublicationState.PUBLISHED_VERIFICATION_FAILED,
            )
            self.assertEqual(len(after.activity), len(before.activity))
            self.assertEqual(restore.status, UNDO_VERIFICATION_FAILED)
            self.assertEqual(
                restore.verification_status,
                UNDO_VERIFICATION_FAILED_STATE,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, original = _stored_pair(
                root,
                (b"content",),
            )
            before = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )
            result = _service(
                operations,
                undos,
                verifier=mock.Mock(
                    return_value=IdentityUndoVerificationResult(
                        IdentityUndoVerificationState.INDETERMINATE,
                        affected_items=1,
                        verified_count=0,
                        mismatch_categories=(),
                    )
                ),
            ).publish(original.undo_id)
            after = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )
            self.assertEqual(
                result.state,
                IdentityUndoVerificationPublicationState.VERIFICATION_INDETERMINATE,
            )
            self.assertEqual(after, before)
            self.assertEqual(undos.load(original.undo_id), original)

    def test_publisher_is_disconnected_from_routes_activity_and_production_undo(
        self,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        source_root = repository_root / "src/organizer"
        publisher = (
            source_root / "application/undo_identity_verification_publication.py"
        ).read_text(encoding="utf-8")
        protected = (
            source_root / "application/undo_service.py",
            source_root / "application/activity_service.py",
        )
        route_references = [
            path
            for path in (source_root / "web/routes").glob("*.py")
            if "undo_identity_verification_publication"
            in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(route_references, [])
        for path in protected:
            self.assertNotIn(
                "undo_identity_verification_publication",
                path.read_text(encoding="utf-8"),
            )
        for forbidden in (
            "activity_service",
            "Activity",
            "OperationLog",
            "executor",
        ):
            self.assertNotIn(forbidden, publisher)


def _service(operations, undos, *, verifier=None):
    arguments = {"now": lambda: _COMPLETED}
    if verifier is not None:
        arguments["verifier"] = verifier
    return OrganizationUndoIdentityVerificationPublicationService(
        operations,
        undos,
        **arguments,
    )


def _stored_pair(root: Path, payloads: tuple[bytes, ...]):
    operations, undos, parent, undo = _restored_pair(root, payloads)
    undos.create(undo)
    return operations, undos, parent, undo


if __name__ == "__main__":
    unittest.main()
