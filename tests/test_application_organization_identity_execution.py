from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from organizer.application.execution_coordinator import approved_plan_identity
from organizer.application.file_identity_capture import (
    FileIdentityCaptureError,
    capture_regular_file_identity,
)
from organizer.application.identity_verification_publication import (
    IdentityVerificationPublicationResult,
    IdentityVerificationPublicationState,
    OrganizationIdentityVerificationPublicationService,
)
from organizer.application.organization_decision_models import (
    ApprovedOrganizationPlan,
    OrganizationApprovalSummary,
    SourceFingerprint,
)
from organizer.application.organization_identity_execution import (
    IdentityOrganizationExecutionState,
    OrganizationIdentityExecutionError,
    OrganizationIdentityExecutionService,
)
from organizer.application.organization_identity_verification import (
    IdentityMismatchCategory,
    IdentityOrganizationVerificationResult,
    IdentityVerificationState,
)
from organizer.application.organization_operation_models import (
    AUTHORIZATION_AVAILABLE,
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_INTERRUPTED,
    STATUS_MOVEMENT_FAILED,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_PREPARED,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    MovementAuthorization,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.executor import (
    MovementRecordingError,
    apply_same_volume_identity_move_plan,
)
from organizer.executor_identity import (
    IdentityMovePlanItem,
    StrictExecutorError,
    StrictExecutorInterrupted,
)
from organizer.models import MovePlanItem, MoveResult, OperationLog, ReviewedPlanItem

_NOW = datetime(2026, 7, 27, 3, 4, 5, tzinfo=UTC)
_OPERATION_ID = "11111111-1111-4111-8111-111111111111"
_AUTHORIZATION_ID = "22222222-2222-4222-8222-222222222222"


class OrganizationIdentityExecutionTests(unittest.TestCase):
    def test_complete_capture_intent_consumption_execution_and_publication_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt", "beta.txt"))
            captures: list[str] = []
            consumed = False
            strict_plan_validated = False
            real_create = repository.create

            def capture(path: Path, capture_root: Path):
                self.assertEqual(capture_root, root)
                self.assertEqual(repository.list_records(), ())
                captures.append(path.name)
                return capture_regular_file_identity(path, capture_root)

            def consume(authorization_id: str) -> bool:
                nonlocal consumed
                self.assertEqual(authorization_id, _AUTHORIZATION_ID)
                durable = repository.load(_OPERATION_ID)
                self.assertEqual(durable.status, STATUS_PREPARED)
                self.assertEqual(
                    durable.schema_version,
                    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
                )
                self.assertTrue(
                    all(item.content_identity is not None for item in durable.expected_moves)
                )
                consumed = True
                return True

            def validate_strict(plan_items, validate_root):
                nonlocal strict_plan_validated
                from organizer.executor_identity import (
                    validate_same_volume_identity_move_plan,
                )

                strict_plan_validated = True
                return validate_same_volume_identity_move_plan(
                    plan_items,
                    validate_root,
                )

            def create(record) -> None:
                self.assertEqual(captures, ["alpha.txt", "beta.txt"])
                self.assertTrue(strict_plan_validated)
                real_create(record)

            def execute(plan_items, execute_root, *, recorder):
                self.assertTrue(consumed)
                self.assertEqual(execute_root, root)
                self.assertEqual(
                    tuple(item.change_id for item in plan_items),
                    ("O1", "O2"),
                )
                self.assertTrue(
                    all(isinstance(item, IdentityMovePlanItem) for item in plan_items)
                )
                self.assertEqual(repository.load(_OPERATION_ID).status, STATUS_PREPARED)
                return apply_same_volume_identity_move_plan(
                    plan_items,
                    execute_root,
                    recorder=recorder,
                )

            with (
                mock.patch(
                    "organizer.application.organization_identity_execution.validate_same_volume_identity_move_plan",
                    side_effect=validate_strict,
                ),
                mock.patch.object(repository, "create", side_effect=create),
                mock.patch(
                    "organizer.application.execution_service.verify_organization_operation"
                ) as legacy_verify,
            ):
                result = _service(
                    repository,
                    consume=consume,
                    capture=capture,
                    executor=execute,
                ).execute(approved_plan=plan, authorization=authorization)
            legacy_verify.assert_not_called()

            durable = repository.load(_OPERATION_ID)
            self.assertEqual(captures, ["alpha.txt", "beta.txt"])
            self.assertEqual(result.state, IdentityOrganizationExecutionState.VERIFIED)
            self.assertEqual(durable.status, STATUS_VERIFIED)
            self.assertEqual([item.result_state for item in durable.expected_moves], [MOVE_MOVED, MOVE_MOVED])
            self.assertEqual(result.files_moved, 2)
            self.assertTrue((root / "Documents/alpha.txt").is_file())
            self.assertTrue((root / "Documents/beta.txt").is_file())
            rendered = repr(result)
            for secret in (
                str(root),
                _OPERATION_ID,
                durable.expected_moves[0].content_identity.digest,
            ):
                self.assertNotIn(secret, rendered)

    def test_later_capture_failure_creates_nothing_and_calls_no_consumption_or_executor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt", "beta.txt"))
            real_capture = capture_regular_file_identity
            capture = mock.Mock(
                side_effect=(
                    real_capture(root / "Inbox/alpha.txt", root),
                    FileIdentityCaptureError("source_changed_during_hash"),
                )
            )
            consume = mock.Mock()
            executor = mock.Mock()

            with self.assertRaises(OrganizationIdentityExecutionError) as raised:
                _service(
                    repository,
                    consume=consume,
                    capture=capture,
                    executor=executor,
                ).execute(approved_plan=plan, authorization=authorization)

            self.assertEqual(raised.exception.code, "identity_capture_failed")
            self.assertEqual(capture.call_count, 2)
            consume.assert_not_called()
            executor.assert_not_called()
            self.assertEqual(repository.list_records(), ())
            self.assertFalse(repository.directory.exists())
            self.assertFalse((root / "Documents").exists())

    def test_same_size_same_mtime_replacement_becomes_confirmation_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))
            source = root / "Inbox/alpha.txt"
            original = source.read_bytes()
            original_stat = source.stat()
            replacement = b"x" * len(original)
            source.write_bytes(replacement)
            os.utime(
                source,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            self.assertEqual(source.stat().st_size, len(original))
            self.assertEqual(
                source.stat().st_mtime,
                plan.source_fingerprints[0].modified_time,
            )

            result = _service(repository).execute(
                approved_plan=plan,
                authorization=authorization,
            )

            durable = repository.load(_OPERATION_ID)
            self.assertEqual(result.state, IdentityOrganizationExecutionState.VERIFIED)
            self.assertEqual((root / "Documents/alpha.txt").read_bytes(), replacement)
            self.assertNotEqual(
                durable.expected_moves[0].content_identity.digest,
                hashlib.sha256(original).hexdigest(),
            )

    def test_preflight_and_strict_plan_rejection_precede_durable_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))
            consume = mock.Mock()
            executor = mock.Mock()
            capture = mock.Mock()
            occupied = root / "Documents/alpha.txt"
            occupied.parent.mkdir()
            occupied.write_bytes(b"occupied")

            with self.assertRaises(OrganizationIdentityExecutionError) as raised:
                _service(
                    repository,
                    consume=consume,
                    capture=capture,
                    executor=executor,
                ).execute(approved_plan=plan, authorization=authorization)
            self.assertEqual(raised.exception.code, "overwrite_detected")
            self.assertEqual(repository.list_records(), ())
            capture.assert_not_called()
            consume.assert_not_called()
            executor.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))
            with (
                mock.patch(
                    "organizer.application.organization_identity_execution.validate_same_volume_identity_move_plan",
                    side_effect=StrictExecutorError("invalid_strict_plan"),
                ),
                self.assertRaises(OrganizationIdentityExecutionError) as raised,
            ):
                _service(repository).execute(
                    approved_plan=plan,
                    authorization=authorization,
                )
            self.assertEqual(raised.exception.code, "strict_plan_invalid")
            self.assertEqual(repository.list_records(), ())
            self.assertFalse(repository.directory.exists())

    def test_create_and_consumption_failures_preserve_required_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))
            consume = mock.Mock()
            executor = mock.Mock()
            with (
                mock.patch.object(
                    repository,
                    "create",
                    side_effect=OSError("private durable failure"),
                ),
                self.assertRaises(OrganizationIdentityExecutionError) as raised,
            ):
                _service(
                    repository,
                    consume=consume,
                    executor=executor,
                ).execute(approved_plan=plan, authorization=authorization)
            self.assertEqual(raised.exception.code, "durable_intent_failed")
            consume.assert_not_called()
            executor.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))
            executor = mock.Mock()
            result = _service(
                repository,
                consume=mock.Mock(return_value=False),
                executor=executor,
            ).execute(approved_plan=plan, authorization=authorization)
            durable = repository.load(_OPERATION_ID)
            self.assertEqual(result.state, IdentityOrganizationExecutionState.INTERRUPTED)
            self.assertEqual(durable.status, STATUS_INTERRUPTED)
            self.assertEqual(durable.expected_moves[0].result_state, MOVE_PENDING)
            self.assertEqual(durable.failure_code, "movement_recovery_required")
            executor.assert_not_called()

    def test_consumption_failure_remains_recovery_required_when_interrupted_save_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))

            with mock.patch.object(
                repository,
                "save",
                side_effect=OSError("private publication failure"),
            ):
                result = _service(
                    repository,
                    consume=mock.Mock(return_value=False),
                ).execute(approved_plan=plan, authorization=authorization)

            self.assertEqual(result.state, IdentityOrganizationExecutionState.INTERRUPTED)
            durable = repository.load(_OPERATION_ID)
            self.assertEqual(durable.status, STATUS_PREPARED)
            self.assertEqual(durable.expected_moves[0].result_state, MOVE_PENDING)

    def test_recorder_maps_ordinary_failure_partial_and_ambiguity_truthfully(
        self,
    ) -> None:
        scenarios = (
            ("failed", STATUS_MOVEMENT_FAILED, (MOVE_FAILED, MOVE_UNATTEMPTED)),
            ("partial", STATUS_PARTIAL_MOVEMENT, (MOVE_MOVED, MOVE_FAILED)),
            ("ambiguous", STATUS_INTERRUPTED, (MOVE_ATTEMPTING, MOVE_PENDING)),
        )
        for name, expected_status, expected_items in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                plan, authorization, repository = _inputs(
                    root,
                    ("alpha.txt", "beta.txt"),
                )

                def executor(plan_items, execute_root, *, recorder):
                    ordinary = tuple(item.plan_item for item in plan_items)
                    recorder.before_batch(ordinary)
                    recorder.before_move(0, ordinary[0])
                    if name == "ambiguous":
                        raise MovementRecordingError(
                            "after_success",
                            filesystem_changed=True,
                        )
                    first_success = name == "partial"
                    first_result = MoveResult(
                        ordinary[0].source,
                        ordinary[0].destination,
                        first_success,
                        "moved" if first_success else "failed",
                    )
                    (
                        recorder.after_success
                        if first_success
                        else recorder.after_failure
                    )(0, ordinary[0], first_result)
                    operations = [first_result]
                    if name == "partial":
                        recorder.before_move(1, ordinary[1])
                        second_result = MoveResult(
                            ordinary[1].source,
                            ordinary[1].destination,
                            False,
                            "failed",
                        )
                        recorder.after_failure(1, ordinary[1], second_result)
                        operations.append(second_result)
                    recorder.after_batch(
                        OperationLog(root / "operation_logs/test.json", operations)
                    )
                    return OperationLog(
                        root / "operation_logs/test.json",
                        operations,
                    )

                result = _service(
                    repository,
                    executor=executor,
                    publish_verification=mock.Mock(),
                ).execute(approved_plan=plan, authorization=authorization)
                durable = repository.load(_OPERATION_ID)
                self.assertEqual(durable.status, expected_status)
                self.assertEqual(
                    tuple(item.result_state for item in durable.expected_moves),
                    expected_items,
                )
                self.assertEqual(result.state.value, expected_status)

    def test_executor_revalidation_and_process_interruption_never_claim_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))

            def changed_executor(plan_items, execute_root, *, recorder):
                (root / "Inbox/alpha.txt").write_bytes(b"changed")
                return apply_same_volume_identity_move_plan(
                    plan_items,
                    execute_root,
                    recorder=recorder,
                )

            result = _service(
                repository,
                executor=changed_executor,
            ).execute(approved_plan=plan, authorization=authorization)
            durable = repository.load(_OPERATION_ID)
            self.assertEqual(result.state, IdentityOrganizationExecutionState.FAILED)
            self.assertEqual(durable.status, STATUS_MOVEMENT_FAILED)
            self.assertEqual(durable.expected_moves[0].result_state, MOVE_UNATTEMPTED)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))

            def interrupted(plan_items, execute_root, *, recorder):
                ordinary = tuple(item.plan_item for item in plan_items)
                recorder.before_batch(ordinary)
                recorder.before_move(0, ordinary[0])
                raise StrictExecutorInterrupted()

            with self.assertRaises(StrictExecutorInterrupted):
                _service(repository, executor=interrupted).execute(
                    approved_plan=plan,
                    authorization=authorization,
                )
            durable = repository.load(_OPERATION_ID)
            self.assertEqual(durable.status, STATUS_INTERRUPTED)
            self.assertEqual(
                durable.expected_moves[0].result_state,
                MOVE_ATTEMPTING,
            )

        for interruption in (KeyboardInterrupt(), SystemExit(2)):
            with (
                self.subTest(interruption=type(interruption).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                plan, authorization, repository = _inputs(root, ("alpha.txt",))
                with self.assertRaises(type(interruption)):
                    _service(
                        repository,
                        executor=mock.Mock(side_effect=interruption),
                    ).execute(
                        approved_plan=plan,
                        authorization=authorization,
                    )
                durable = repository.load(_OPERATION_ID)
                self.assertEqual(durable.status, STATUS_PREPARED)
                self.assertEqual(
                    durable.expected_moves[0].result_state,
                    MOVE_PENDING,
                )

    def test_verification_mismatch_indeterminate_and_stale_preserve_policy(
        self,
    ) -> None:
        scenarios = (
            (
                IdentityOrganizationVerificationResult(
                    IdentityVerificationState.MISMATCH,
                    affected_items=1,
                    verified_count=0,
                    mismatch_categories=(
                        IdentityMismatchCategory.DESTINATION_IDENTITY_MISMATCH,
                    ),
                ),
                STATUS_VERIFICATION_FAILED,
            ),
            (
                IdentityOrganizationVerificationResult(
                    IdentityVerificationState.INDETERMINATE,
                    affected_items=1,
                    verified_count=0,
                    mismatch_categories=(),
                ),
                "moved_unverified",
            ),
        )
        for verification, expected_status in scenarios:
            with (
                self.subTest(expected_status=expected_status),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                plan, authorization, repository = _inputs(root, ("alpha.txt",))
                publisher = OrganizationIdentityVerificationPublicationService(
                    repository,
                    verifier=mock.Mock(return_value=verification),
                    now=lambda: _NOW,
                )
                result = _service(
                    repository,
                    publish_verification=publisher.publish,
                ).execute(approved_plan=plan, authorization=authorization)
                self.assertEqual(result.state.value, expected_status)
                self.assertEqual(repository.load(_OPERATION_ID).status, expected_status)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))
            stale = mock.Mock(
                return_value=IdentityVerificationPublicationResult(
                    IdentityVerificationPublicationState.STALE,
                    published=False,
                )
            )
            result = _service(
                repository,
                publish_verification=stale,
            ).execute(approved_plan=plan, authorization=authorization)
            self.assertEqual(
                result.state,
                IdentityOrganizationExecutionState.MOVED_UNVERIFIED,
            )
            stale.assert_called_once_with(_OPERATION_ID)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, authorization, repository = _inputs(root, ("alpha.txt",))
            result = _service(
                repository,
                publish_verification=mock.Mock(
                    side_effect=OSError("private verification failure")
                ),
            ).execute(approved_plan=plan, authorization=authorization)
            self.assertEqual(
                result.state,
                IdentityOrganizationExecutionState.MOVED_UNVERIFIED,
            )
            self.assertEqual(
                repository.load(_OPERATION_ID).status,
                "moved_unverified",
            )

    def test_production_service_connects_owner_without_legacy_execution(
        self,
    ) -> None:
        source_root = Path(__file__).parents[1] / "src" / "organizer"
        connected = (
            source_root / "application" / "organization_movement_service.py",
            source_root / "web" / "app.py",
        )
        for path in connected:
            source = path.read_text(encoding="utf-8")
            self.assertIn("organization_identity_execution", source)
        for path in (source_root / "web" / "routes").glob("*.py"):
            self.assertNotIn(
                "from organizer.application.organization_identity_execution",
                path.read_text(encoding="utf-8"),
            )

        owner = (
            source_root
            / "application"
            / "organization_identity_execution.py"
        ).read_text(encoding="utf-8")
        self.assertIn("apply_same_volume_identity_move_plan", owner)
        for forbidden in (
            "apply_move_plan(",
            "verify_organization_operation",
            "activity_service",
            "RuntimeLifecycle",
            "OrganizationExecutionCoordinator",
            "shutil.move",
        ):
            self.assertNotIn(forbidden, owner)


def _service(
    repository: OrganizationOperationRepository,
    *,
    consume=None,
    capture=capture_regular_file_identity,
    executor=apply_same_volume_identity_move_plan,
    publish_verification=None,
) -> OrganizationIdentityExecutionService:
    return OrganizationIdentityExecutionService(
        repository,
        consume_authorization=consume or mock.Mock(return_value=True),
        capture=capture,
        executor=executor,
        publish_verification=publish_verification,
        operation_id_factory=lambda: _OPERATION_ID,
        clock=lambda: _NOW,
    )


def _inputs(
    root: Path,
    names: tuple[str, ...],
) -> tuple[
    ApprovedOrganizationPlan,
    MovementAuthorization,
    OrganizationOperationRepository,
]:
    reviewed: list[ReviewedPlanItem] = []
    fingerprints: list[SourceFingerprint] = []
    for index, name in enumerate(names, start=1):
        source = root / "Inbox" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"{name}-content".encode())
        stat = source.stat()
        reviewed.append(
            ReviewedPlanItem(
                id=f"O{index}",
                category="organization",
                plan_item=MovePlanItem(
                    source=source,
                    destination=root / "Documents" / name,
                    reason=f"approved move {index}",
                    confidence=95,
                    operation="move",
                    overwrite_risk=False,
                ),
                decision="approved",
            )
        )
        fingerprints.append(
            SourceFingerprint(
                item_id=f"scan-one:item-{index}",
                relative_path=Path("Inbox") / name,
                size_bytes=stat.st_size,
                modified_time=stat.st_mtime,
            )
        )
    plan = ApprovedOrganizationPlan(
        status="ready_to_move",
        scan_generation="scan-one",
        decision_revision=3,
        approved_at="2026-07-27T02:00:00+00:00",
        reviewed_move_items=tuple(reviewed),
        duplicate_decisions=(),
        source_fingerprints=tuple(fingerprints),
        summary=OrganizationApprovalSummary(
            pending_recommendations=0,
            pending_choices=0,
            accepted_moves=len(reviewed),
            kept_recommendations=0,
            duplicate_copies_for_future_bin=0,
            choices_remaining=0,
            ready_for_approval=True,
            files_to_move=len(reviewed),
        ),
    )
    repository = OrganizationOperationRepository(root)
    authorization = MovementAuthorization(
        authorization_id=_AUTHORIZATION_ID,
        scan_generation=plan.scan_generation,
        decision_revision=plan.decision_revision,
        approved_at=plan.approved_at,
        approved_plan_identity=approved_plan_identity(plan),
        issued_at="2026-07-27T02:30:00+00:00",
        consumed_at=None,
        status=AUTHORIZATION_AVAILABLE,
        browser_session_id="session",
        configured_root_identity=repository.configured_root_identity,
    )
    return plan, authorization, repository
