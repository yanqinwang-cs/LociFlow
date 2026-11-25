from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from organizer.application.activity_service import list_activity_entries
from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.identity_verification_publication import (
    IdentityVerificationPublicationState,
    OrganizationIdentityVerificationPublicationService,
)
from organizer.application.organization_identity_verification import (
    IdentityMismatchCategory,
    IdentityOrganizationVerificationResult,
    IdentityVerificationState,
)
from organizer.application.organization_operation_models import (
    MOVE_MOVED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_NOT_RUN,
    VERIFICATION_PASSED,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)

_COMPLETED = datetime(2026, 7, 22, 3, 4, 5, tzinfo=UTC)


class IdentityVerificationPublicationTests(unittest.TestCase):
    def test_passed_verification_publishes_existing_terminal_record_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, original = _operation(root, (b"first", b"second"))
            service = _service(repository)

            with mock.patch.object(
                repository,
                "save",
                wraps=repository.save,
            ) as save:
                result = service.publish(original.operation_id)

            durable = repository.load(original.operation_id)
            self.assertEqual(
                result.state,
                IdentityVerificationPublicationState.PUBLISHED_VERIFIED,
            )
            self.assertTrue(result.published)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(durable.status, STATUS_VERIFIED)
            self.assertEqual(durable.verification.status, VERIFICATION_PASSED)
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

    def test_mismatch_publishes_bounded_failure_and_preserves_verified_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, original = _operation(root, (b"first", b"second"))
            verifier = mock.Mock(
                return_value=IdentityOrganizationVerificationResult(
                    state=IdentityVerificationState.MISMATCH,
                    affected_items=2,
                    verified_count=1,
                    mismatch_categories=(
                        IdentityMismatchCategory.SOURCE_PRESENT,
                        IdentityMismatchCategory.DESTINATION_IDENTITY_MISMATCH,
                    ),
                )
            )
            service = _service(repository, verifier=verifier)

            with mock.patch.object(
                repository,
                "save",
                wraps=repository.save,
            ) as save:
                result = service.publish(original.operation_id)

            durable = repository.load(original.operation_id)
            self.assertEqual(
                result.state,
                IdentityVerificationPublicationState.PUBLISHED_VERIFICATION_FAILED,
            )
            self.assertEqual(verifier.call_count, 1)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(durable.status, STATUS_VERIFICATION_FAILED)
            self.assertEqual(durable.verification.status, VERIFICATION_FAILED)
            self.assertEqual(durable.verification.verified_count, 1)
            self.assertEqual(
                durable.verification.mismatch_codes,
                ("identity_source_present", "identity_destination_mismatch"),
            )
            self.assertEqual(durable.failure_code, "verification_failed")
            rendered = repr(durable.verification)
            for secret in (
                str(root),
                "item-1",
                original.operation_id,
                original.expected_moves[0].content_identity.digest,
            ):
                self.assertNotIn(secret, rendered)

    def test_indeterminate_invalid_pass_and_unexpected_error_never_save(self) -> None:
        outcomes = (
            IdentityOrganizationVerificationResult(
                IdentityVerificationState.INDETERMINATE,
                affected_items=1,
                verified_count=0,
                mismatch_categories=(),
            ),
            IdentityOrganizationVerificationResult(
                IdentityVerificationState.PASSED,
                affected_items=1,
                verified_count=0,
                mismatch_categories=(),
            ),
            RuntimeError("private/path/item.txt"),
        )
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository, original = _operation(root, (b"content",))
                verifier = mock.Mock(
                    side_effect=outcome
                    if isinstance(outcome, Exception)
                    else None,
                    return_value=None
                    if isinstance(outcome, Exception)
                    else outcome,
                )
                with mock.patch.object(repository, "save") as save:
                    result = _service(repository, verifier=verifier).publish(
                        original.operation_id
                    )
                self.assertEqual(
                    result.state,
                    IdentityVerificationPublicationState.VERIFICATION_INDETERMINATE,
                )
                self.assertFalse(result.published)
                save.assert_not_called()
                self.assertEqual(repository.load(original.operation_id), original)

    def test_process_control_propagates_and_leaves_record_unchanged(self) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit()):
            with self.subTest(kind=type(interruption).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository, original = _operation(root, (b"content",))
                with self.assertRaises(type(interruption)):
                    _service(
                        repository,
                        verifier=mock.Mock(side_effect=interruption),
                    ).publish(original.operation_id)
                self.assertEqual(repository.load(original.operation_id), original)

    def test_record_changed_during_verification_is_stale_without_second_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, original = _operation(root, (b"content",))
            changed = replace(original, failure_code="concurrent_change")

            def change_durable_record(*_args):
                repository.save(changed)
                return IdentityOrganizationVerificationResult(
                    IdentityVerificationState.PASSED,
                    affected_items=1,
                    verified_count=1,
                    mismatch_categories=(),
                )

            with mock.patch.object(
                repository,
                "save",
                wraps=repository.save,
            ) as save:
                result = _service(
                    repository,
                    verifier=change_durable_record,
                ).publish(original.operation_id)

            self.assertEqual(result.state, IdentityVerificationPublicationState.STALE)
            self.assertFalse(result.published)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(repository.load(original.operation_id), changed)

    def test_root_identity_item_and_terminal_changes_before_save_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _repository, original = _operation(root, (b"content",))
            changed_identity = RegularFileContentIdentity(
                "regular_file", "sha256", "b" * 64
            )
            changes = (
                replace(original, configured_root_identity="c" * 64),
                replace(
                    original,
                    expected_moves=(
                        replace(
                            original.expected_moves[0],
                            content_identity=changed_identity,
                        ),
                    ),
                ),
                replace(
                    original,
                    verification=OrganizationOperationVerification(
                        status=VERIFICATION_FAILED,
                        mismatch_codes=("changed",),
                    ),
                ),
                replace(
                    original,
                    status=STATUS_VERIFIED,
                    completed_at=_COMPLETED.isoformat(),
                    verification=OrganizationOperationVerification(
                        status=VERIFICATION_PASSED,
                        verified_count=1,
                    ),
                ),
            )
            for changed in changes:
                with self.subTest(change=changed.status):
                    repository = mock.Mock(root=root)
                    repository.load.side_effect = (original, changed)
                    result = _service(repository).publish(original.operation_id)
                    self.assertEqual(
                        result.state,
                        IdentityVerificationPublicationState.STALE,
                    )
                    repository.save.assert_not_called()

    def test_terminal_and_ineligible_records_reject_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, original = _operation(root, (b"content",))
            terminal = replace(
                original,
                status=STATUS_VERIFIED,
                completed_at=_COMPLETED.isoformat(),
                verification=OrganizationOperationVerification(
                    status=VERIFICATION_PASSED,
                    verified_count=1,
                ),
            )
            repository.save(terminal)
            verifier = mock.Mock()
            result = _service(repository, verifier=verifier).publish(
                original.operation_id
            )
            self.assertEqual(result.state, IdentityVerificationPublicationState.STALE)
            verifier.assert_not_called()
            self.assertEqual(repository.load(original.operation_id), terminal)

    def test_missing_operation_log_does_not_block_real_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, original = _operation(root, (b"content",))
            self.assertIsNone(original.legacy_operation_log)
            result = _service(repository).publish(original.operation_id)
            self.assertEqual(
                result.state,
                IdentityVerificationPublicationState.PUBLISHED_VERIFIED,
            )

    def test_publication_updates_one_repository_derived_activity_row_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, original = _operation(root, (b"content",))
            undos = OrganizationUndoRepository(root)

            before = list_activity_entries(
                operation_repository=repository,
                undo_repository=undos,
            )
            result = _service(repository).publish(original.operation_id)
            after = list_activity_entries(
                operation_repository=repository,
                undo_repository=undos,
            )

            self.assertEqual(len(before.activity), 1)
            self.assertEqual(len(after.activity), 1)
            self.assertEqual(after.activity[0].id, before.activity[0].id)
            self.assertEqual(after.activity[0].status, STATUS_VERIFIED)
            self.assertTrue(after.activity[0].undoable)
            self.assertEqual(
                result.state,
                IdentityVerificationPublicationState.PUBLISHED_VERIFIED,
            )

    def test_repository_failure_before_replacement_returns_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, original = _operation(root, (b"content",))
            with mock.patch.object(
                repository,
                "save",
                side_effect=OSError("private durable detail"),
            ) as save:
                result = _service(repository).publish(original.operation_id)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(
                result.state,
                IdentityVerificationPublicationState.VERIFICATION_INDETERMINATE,
            )
            self.assertEqual(repository.load(original.operation_id), original)

    def test_publisher_is_disconnected_from_live_execution_routes_and_activity(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "organizer"
        execution = (
            source_root / "application" / "execution_service.py"
        ).read_text(encoding="utf-8")
        publisher = (
            source_root
            / "application"
            / "identity_verification_publication.py"
        ).read_text(encoding="utf-8")
        route_references = [
            path
            for path in (source_root / "web" / "routes").glob("*.py")
            if "identity_verification_publication" in path.read_text(
                encoding="utf-8"
            )
        ]
        self.assertNotIn("identity_verification_publication", execution)
        self.assertNotIn("OrganizationIdentityVerificationPublicationService", execution)
        self.assertEqual(route_references, [])
        for forbidden in (
            "activity_service",
            "Activity",
            "OperationLog",
            "executor",
        ):
            self.assertNotIn(forbidden, publisher)


def _service(repository, *, verifier=None):
    arguments = {"now": lambda: _COMPLETED}
    if verifier is not None:
        arguments["verifier"] = verifier
    return OrganizationIdentityVerificationPublicationService(
        repository,
        **arguments,
    )


def _operation(
    root: Path,
    payloads: tuple[bytes, ...],
) -> tuple[OrganizationOperationRepository, OrganizationOperationRecord]:
    repository = OrganizationOperationRepository(root)
    moves: list[OrganizationExpectedMove] = []
    for index, payload in enumerate(payloads, start=1):
        destination = root / "Documents" / f"item-{index}.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        moves.append(
            OrganizationExpectedMove(
                change_id=f"O{index}",
                source=f"Inbox/item-{index}.txt",
                destination=f"Documents/item-{index}.txt",
                source_size_bytes=len(payload),
                source_modified_time=100.0,
                reason="test",
                confidence=100,
                operation="move",
                overwrite_risk=False,
                result_state=MOVE_MOVED,
                content_identity=RegularFileContentIdentity(
                    "regular_file",
                    "sha256",
                    hashlib.sha256(payload).hexdigest(),
                ),
            )
        )
    record = OrganizationOperationRecord(
        operation_id=str(uuid.uuid4()),
        authorization_id=str(uuid.uuid4()),
        status=STATUS_MOVED_UNVERIFIED,
        scan_generation="scan-one",
        decision_revision=1,
        approved_at="2026-07-22T03:00:00+00:00",
        created_at="2026-07-22T03:00:00+00:00",
        started_at="2026-07-22T03:01:00+00:00",
        completed_at=None,
        configured_root_identity=repository.configured_root_identity,
        expected_moves=tuple(moves),
        verification=OrganizationOperationVerification(
            status=VERIFICATION_NOT_RUN
        ),
        legacy_operation_log=None,
        failure_code=None,
        duplicate_actions_deferred=0,
        schema_version=2,
    )
    repository.create(record)
    return repository, record


if __name__ == "__main__":
    unittest.main()
