from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import uuid

from organizer.application.duplicate_bin_models import (
    BATCH_MOVED,
    ITEM_AVAILABLE,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_ATTEMPTING as DUPLICATE_MOVE_ATTEMPTING,
    MOVE_MOVED as DUPLICATE_MOVE_MOVED,
    MOVE_PENDING as DUPLICATE_MOVE_PENDING,
    OP_INTERRUPTED,
    OP_MOVED_UNVERIFIED,
    OP_MOVING,
    OP_PREPARED,
    DuplicateBinVerification,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_ATTEMPTING as RESTORE_ITEM_ATTEMPTING,
    ITEM_PENDING as RESTORE_ITEM_PENDING,
    ITEM_RESTORED as RESTORE_ITEM_RESTORED,
    RESTORE_INTERRUPTED,
    RESTORE_PREPARED,
    RESTORE_RESTORED_UNVERIFIED,
    RESTORE_RESTORING,
    DuplicateBinRestoreVerification,
)
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
    RecoveryAdmissionState,
)
from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_MOVED,
    MOVE_PENDING,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVING,
    STATUS_PREPARED,
    VERIFICATION_NOT_RUN,
    OrganizationOperationVerification,
)
from organizer.application.organization_undo_models import (
    UNDO_INTERRUPTED,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_PREPARED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
    UndoVerificationResult,
)
from organizer.application.recovery_evidence import (
    FilesystemEvidence,
    RecoveryActionKind,
    RecoveryActionability,
    RecoveryEvidenceInspector,
    RecoveryEvidenceState,
    RecoveryOperationFamily,
)
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from tests.test_application_duplicate_bin_movement_service import ISSUED
from tests.test_application_duplicate_bin_restore_service import (
    _moved as _duplicate_moved,
    _restore,
    _restore_preview,
)
from tests.test_application_filesystem_operation_admission import (
    _durable_bytes_and_mtimes,
    _ordinary_operation,
    _ordinary_undo,
)


class RecoveryEvidenceTests(unittest.TestCase):
    def test_clear_root_has_no_detail_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            result = _inspect(root)

            self.assertEqual(result.state, RecoveryEvidenceState.CLEAR)
            self.assertIsNone(result.case)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_one_record_is_detailed_but_multiple_are_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first, _ = _ordinary_movement(root, STATUS_PREPARED, "pending")

            one = _inspect(root)

            self.assertEqual(one.unresolved_count, 1)
            self.assertEqual(
                one.case.family, RecoveryOperationFamily.ORGANIZATION_MOVEMENT
            )
            self.assertEqual(
                one.actionability, RecoveryActionability.INSPECT_ONLY
            )

            second, second_record = _ordinary_operation(root, status=STATUS_PREPARED)
            second.save(
                replace(
                    second_record,
                    expected_moves=(
                        replace(second_record.expected_moves[0], result_state=MOVE_PENDING),
                    ),
                    verification=OrganizationOperationVerification(
                        status=VERIFICATION_NOT_RUN
                    ),
                )
            )
            aggregate = _inspect(root)
            self.assertEqual(aggregate.state, RecoveryEvidenceState.NON_ACTIONABLE)
            self.assertEqual(aggregate.unresolved_count, 2)
            self.assertIsNone(aggregate.case)
            self.assertEqual(
                aggregate.actionability,
                RecoveryActionability.MULTIPLE_UNRESOLVED_OPERATIONS,
            )
            self.assertEqual(
                aggregate.safe_projection().title,
                "More than one file operation needs attention.",
            )
            self.assertEqual(aggregate.safe_projection().affected_items, 2)
            self.assertEqual(len(first.list_records()), 2)

    def test_unfinished_plus_malformed_never_hides_malformed_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _ordinary_movement(root, STATUS_PREPARED, "pending")
            malformed = (
                root
                / "AI_Review"
                / "duplicate_bin"
                / "restores"
                / "operations"
                / f"{uuid.uuid4()}.json"
            )
            malformed.parent.mkdir(parents=True)
            malformed.write_text("{bad", encoding="utf-8")

            result = _inspect(root)

            self.assertEqual(result.state, RecoveryEvidenceState.NON_ACTIONABLE)
            self.assertEqual(
                result.actionability,
                RecoveryActionability.NON_ACTIONABLE_MALFORMED,
            )
            self.assertIsNone(result.case)

    def test_evidence_digest_is_deterministic_and_changes_with_both_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, record = _ordinary_movement(root, STATUS_MOVING, "pending")
            first = _inspect(root).case.digest.value
            second = _inspect(root).case.digest.value
            self.assertEqual(first, second)

            repository.save(replace(record, status=STATUS_INTERRUPTED))
            durable_changed = _inspect(root).case.digest.value
            self.assertNotEqual(durable_changed, first)

            source = root / record.expected_moves[0].source
            destination = root / record.expected_moves[0].destination
            destination.parent.mkdir(parents=True)
            shutil.move(source, destination)
            filesystem_changed = _inspect(root).case.digest.value
            self.assertNotEqual(filesystem_changed, durable_changed)

    def test_same_size_same_mtime_replacement_never_proves_ordinary_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, record = _ordinary_movement(root, STATUS_PREPARED, "pending")
            before = _inspect(root)
            source = root / record.expected_moves[0].source
            source.write_bytes(b"evil")
            os.utime(source, (100.0, 100.0))

            replaced = _inspect(root)

            self.assertEqual(
                replaced.actionability, RecoveryActionability.INSPECT_ONLY
            )
            self.assertNotIn(
                RecoveryActionKind.CONTINUE,
                replaced.case.available_action_kinds,
            )
            self.assertNotEqual(
                replaced.case.digest.value,
                before.case.digest.value,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, undo = _ordinary_undo_state(root, UNDO_PREPARED, "pending")
            before = _inspect(root)
            moved = root / undo.expected_restores[0].moved_from
            moved.write_bytes(b"evil")
            os.utime(moved, (100.0, 100.0))

            replaced = _inspect(root)

            self.assertEqual(
                replaced.actionability, RecoveryActionability.INSPECT_ONLY
            )
            self.assertNotIn(
                RecoveryActionKind.CONTINUE,
                replaced.case.available_action_kinds,
            )
            self.assertNotEqual(
                replaced.case.digest.value,
                before.case.digest.value,
            )

    def test_roots_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root = Path(first).resolve()
            second_root = Path(second).resolve()
            _ordinary_movement(first_root, STATUS_PREPARED, "pending")
            _ordinary_movement(second_root, STATUS_PREPARED, "pending")
            first_digest = _inspect(first_root).case.digest.value
            second_digest = _inspect(second_root).case.digest.value
            self.assertNotEqual(first_digest, second_digest)

    def test_organization_movement_all_states_and_evidence(self) -> None:
        cases = (
            (STATUS_PREPARED, "pending", RecoveryActionability.INSPECT_ONLY),
            (STATUS_MOVING, "pending", RecoveryActionability.INSPECT_ONLY),
            (
                STATUS_MOVED_UNVERIFIED,
                "complete",
                RecoveryActionability.CHECK_RESULT_POSSIBLE,
            ),
            (
                STATUS_INTERRUPTED,
                "complete",
                RecoveryActionability.INSPECT_ONLY,
            ),
        )
        for status, placement, expected in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository, record = _ordinary_movement(root, status, placement)
                before = _durable_bytes_and_mtimes(root)

                result = _inspect(root)

                self.assertEqual(result.actionability, expected)
                self.assertEqual(result.case.durable_status, status)
                self.assertEqual(_durable_bytes_and_mtimes(root), before)
                self.assertEqual(repository.load(record.operation_id), record)

    def test_organization_movement_mixed_conflicting_missing_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, record = _ordinary_movement(
                root, STATUS_MOVING, "pending", count=2
            )
            completed = record.expected_moves[0]
            (root / completed.destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(root / completed.source, root / completed.destination)
            record = replace(
                record,
                expected_moves=(
                    replace(completed, result_state=MOVE_MOVED),
                    record.expected_moves[1],
                ),
            )
            repository.save(record)
            mixed = _inspect(root)
            self.assertEqual(
                mixed.actionability,
                RecoveryActionability.INSPECT_ONLY,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, record = _ordinary_movement(root, STATUS_MOVING, "pending")
            source = root / record.expected_moves[0].source
            destination = root / record.expected_moves[0].destination
            destination.parent.mkdir(parents=True)
            _write_expected(destination)
            both = _inspect(root)
            self.assertEqual(
                both.actionability, RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS
            )
            self.assertIn(FilesystemEvidence.BOTH_PRESENT, both.case.items[0].observations)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, record = _ordinary_movement(root, STATUS_MOVING, "pending")
            (root / record.expected_moves[0].source).unlink()
            neither = _inspect(root)
            self.assertIn(
                FilesystemEvidence.NEITHER_PRESENT,
                neither.case.items[0].observations,
            )
            self.assertFalse(neither.safe_projection().is_actionable)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, record = _ordinary_movement(root, STATUS_MOVING, "pending")
            source = root / record.expected_moves[0].source
            source.unlink()
            source.symlink_to(root / "outside")
            symlink = _inspect(root)
            self.assertIn(
                FilesystemEvidence.UNSAFE_SYMLINK,
                symlink.case.items[0].observations,
            )
            self.assertFalse(symlink.safe_projection().is_actionable)

    def test_ordinary_undo_all_states_parent_and_identity(self) -> None:
        cases = (
            (UNDO_PREPARED, "pending", RecoveryActionability.INSPECT_ONLY),
            (UNDO_UNDOING, "pending", RecoveryActionability.INSPECT_ONLY),
            (
                UNDO_RESTORED_UNVERIFIED,
                "complete",
                RecoveryActionability.CHECK_RESULT_POSSIBLE,
            ),
            (
                UNDO_INTERRUPTED,
                "complete",
                RecoveryActionability.INSPECT_ONLY,
            ),
        )
        for status, placement, expected in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                undo_repository, undo = _ordinary_undo_state(root, status, placement)
                before = _durable_bytes_and_mtimes(root)
                result = _inspect(root)
                self.assertEqual(result.actionability, expected)
                self.assertEqual(_durable_bytes_and_mtimes(root), before)
                self.assertEqual(undo_repository.load(undo.undo_id), undo)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, undo = _ordinary_undo_state(root, UNDO_PREPARED, "pending")
            parent = root / "AI_Review/organization_operations" / f"{undo.operation_id}.json"
            parent.unlink()
            result = _inspect(root)
            self.assertEqual(
                result.actionability,
                RecoveryActionability.NON_ACTIONABLE_INCONSISTENT,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, undo = _ordinary_undo_state(root, UNDO_PREPARED, "pending")
            moved = root / undo.expected_restores[0].moved_from
            moved.write_bytes(b"changed")
            result = _inspect(root)
            self.assertEqual(
                result.actionability, RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, undo = _ordinary_undo_state(
                root, UNDO_UNDOING, "pending", count=2
            )
            completed = undo.expected_restores[0]
            (root / completed.restore_target).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(root / completed.moved_from, root / completed.restore_target)
            undo = replace(
                undo,
                expected_restores=(
                    replace(completed, result_state=UNDO_ITEM_RESTORED),
                    undo.expected_restores[1],
                ),
            )
            repository.save(undo)
            mixed = _inspect(root)
            self.assertEqual(
                mixed.actionability,
                RecoveryActionability.INSPECT_ONLY,
            )

    def test_duplicate_bin_movement_all_states_hashes_and_conflicts(self) -> None:
        cases = (
            (OP_PREPARED, "pending", RecoveryActionability.CONTINUE_POSSIBLE),
            (OP_MOVING, "pending", RecoveryActionability.CONTINUE_POSSIBLE),
            (
                OP_MOVED_UNVERIFIED,
                "complete",
                RecoveryActionability.CHECK_RESULT_POSSIBLE,
            ),
            (
                OP_INTERRUPTED,
                "complete",
                RecoveryActionability.CHECK_RESULT_POSSIBLE,
            ),
        )
        for status, placement, expected in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                owners, record = _duplicate_move_state(Path(directory).resolve(), status, placement)
                before = _durable_bytes_and_mtimes(owners.root)
                result = _inspect(owners.root)
                self.assertEqual(result.actionability, expected)
                self.assertEqual(_durable_bytes_and_mtimes(owners.root), before)

        with tempfile.TemporaryDirectory() as directory:
            owners, record = _duplicate_move_state(
                Path(directory).resolve(), OP_PREPARED, "pending"
            )
            source = owners.root / record.expected_moves[0].source
            source.write_bytes(b"wrong")
            mismatch = _inspect(owners.root)
            self.assertEqual(
                mismatch.actionability, RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS
            )
            self.assertIn(
                FilesystemEvidence.EXPECTED_IDENTITY_MISMATCH,
                mismatch.case.items[0].observations,
            )

        with tempfile.TemporaryDirectory() as directory:
            owners, record = _duplicate_move_state(
                Path(directory).resolve(), OP_PREPARED, "pending"
            )
            destination = owners.root / record.expected_moves[0].destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(owners.root / record.expected_moves[0].source, destination)
            conflict = _inspect(owners.root)
            self.assertIn(
                FilesystemEvidence.BOTH_PRESENT,
                conflict.case.items[0].observations,
            )
            self.assertFalse(conflict.safe_projection().is_actionable)

        with tempfile.TemporaryDirectory() as directory:
            owners, record = _duplicate_move_state(
                Path(directory).resolve(), OP_MOVING, "complete", selected=2
            )
            pending = record.expected_moves[0]
            shutil.move(
                owners.root / pending.destination,
                owners.root / pending.source,
            )
            mixed_items = (
                replace(
                    pending,
                    result_state=DUPLICATE_MOVE_ATTEMPTING,
                    completed_at=None,
                ),
                record.expected_moves[1],
            )
            record = replace(record, expected_moves=mixed_items)
            owners.operations.save(record)
            mixed = _inspect(owners.root)
            self.assertEqual(
                mixed.actionability,
                RecoveryActionability.CHECK_OR_CONTINUE_POSSIBLE,
            )

    def test_duplicate_bin_restore_all_states_hashes_and_no_repair(self) -> None:
        cases = (
            (RESTORE_PREPARED, "pending", RecoveryActionability.CONTINUE_POSSIBLE),
            (RESTORE_RESTORING, "pending", RecoveryActionability.CONTINUE_POSSIBLE),
            (
                RESTORE_RESTORED_UNVERIFIED,
                "complete",
                RecoveryActionability.CHECK_RESULT_POSSIBLE,
            ),
            (
                RESTORE_INTERRUPTED,
                "complete",
                RecoveryActionability.CHECK_RESULT_POSSIBLE,
            ),
        )
        for status, placement, expected in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                owners, restores, record = _duplicate_restore_state(
                    Path(directory).resolve(), status, placement
                )
                before = _durable_bytes_and_mtimes(owners.root)
                result = _inspect(owners.root)
                self.assertEqual(result.actionability, expected)
                self.assertEqual(_durable_bytes_and_mtimes(owners.root), before)
                self.assertEqual(
                    restores.load_operation(record.restore_operation_id), record
                )

        with tempfile.TemporaryDirectory() as directory:
            owners, _, record = _duplicate_restore_state(
                Path(directory).resolve(), RESTORE_PREPARED, "pending"
            )
            source = owners.root / record.items[0].protected_source
            source.write_bytes(b"wrong")
            mismatch = _inspect(owners.root)
            self.assertEqual(
                mismatch.actionability, RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS
            )

        with tempfile.TemporaryDirectory() as directory:
            owners, restores, record = _duplicate_restore_state(
                Path(directory).resolve(),
                RESTORE_RESTORING,
                "complete",
                selected=2,
            )
            pending = record.items[0]
            shutil.move(
                owners.root / pending.previous_destination,
                owners.root / pending.protected_source,
            )
            record = replace(
                record,
                items=(
                    replace(
                        pending,
                        state=RESTORE_ITEM_ATTEMPTING,
                        completed_at=None,
                    ),
                    record.items[1],
                ),
            )
            restores.save_operation(record)
            mixed = _inspect(owners.root)
            self.assertEqual(
                mixed.actionability,
                RecoveryActionability.CHECK_OR_CONTINUE_POSSIBLE,
            )

    def test_safe_projection_contains_no_internal_or_filesystem_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, record = _ordinary_movement(root, STATUS_PREPARED, "pending")
            projection = _inspect(root).safe_projection().to_dict()
            serialized = json.dumps(projection)
            self.assertEqual(
                set(projection),
                {
                    "state",
                    "title",
                    "message",
                    "affected_items",
                    "is_actionable",
                    "available_action_kinds",
                },
            )
            self.assertEqual(projection["available_action_kinds"], [])
            self.assertFalse(projection["is_actionable"])
            for forbidden in (
                str(root),
                record.operation_id,
                record.status,
                record.expected_moves[0].source,
                "organization_movement",
                "sha256",
                "digest",
                "token",
            ):
                self.assertNotIn(forbidden, serialized)

    def test_explicit_inspection_is_non_mutating_and_startup_does_not_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners, _ = _duplicate_move_state(root, OP_PREPARED, "pending")
            before = _durable_bytes_and_mtimes(root)
            with (
                mock.patch("organizer.executor.apply_move_plan") as executor,
                mock.patch("organizer.application.activity_service.list_activity_entries") as activity,
            ):
                _inspect(root)
            executor.assert_not_called()
            activity.assert_not_called()
            self.assertEqual(_durable_bytes_and_mtimes(root), before)
            self.assertEqual(
                FilesystemOperationInspector(root).inspect().state,
                RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION,
            )

            with mock.patch(
                "organizer.application.recovery_evidence.hash_file",
                side_effect=AssertionError("startup hashed a file"),
            ):
                app = create_app(
                    WebAppConfig(
                        root,
                        session_secret="s" * 32,
                        launch_token="t" * 32,
                        testing=True,
                    )
                )
            self.assertEqual(
                app.state.recovery_admission_snapshot.state,
                RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION,
            )


def _inspect(root: Path):
    return RecoveryEvidenceInspector(FilesystemOperationInspector(root)).inspect()


def _write_expected(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    os.utime(path, (100.0, 100.0))


def _ordinary_movement(
    root: Path, status: str, placement: str, *, count: int = 1
):
    repository, record = _ordinary_operation(root, status=status)
    item_state = (
        MOVE_PENDING
        if status == STATUS_PREPARED
        else MOVE_ATTEMPTING
        if status == STATUS_MOVING
        else MOVE_MOVED
    )
    expected_moves = tuple(
        replace(
            record.expected_moves[0],
            change_id=f"O{index + 1}",
            source=f"Inbox/file-{index}.txt",
            destination=f"Documents/file-{index}.txt",
            result_state=item_state,
        )
        for index in range(count)
    )
    record = replace(
        record,
        status=status,
        expected_moves=expected_moves,
        verification=OrganizationOperationVerification(status=VERIFICATION_NOT_RUN),
    )
    repository.save(record)
    for item in record.expected_moves:
        relative = item.source if placement == "pending" else item.destination
        _write_expected(root / relative)
    return repository, record


def _ordinary_undo_state(
    root: Path, status: str, placement: str, *, count: int = 1
):
    operation_repository, operation = _ordinary_operation(root)
    if count > 1:
        moves = tuple(
            replace(
                operation.expected_moves[0],
                change_id=f"O{index + 1}",
                source=f"Inbox/file-{index}.txt",
                destination=f"Documents/file-{index}.txt",
            )
            for index in range(count)
        )
        operation = replace(
            operation,
            expected_moves=moves,
            verification=replace(operation.verification, verified_count=count),
        )
        operation_repository.save(operation)
    repository, undo = _ordinary_undo(root, operation)
    item_state = (
        UNDO_ITEM_PENDING
        if status == UNDO_PREPARED
        else UNDO_ITEM_ATTEMPTING
        if status == UNDO_UNDOING
        else UNDO_ITEM_RESTORED
    )
    restores = tuple(
        replace(
            undo.expected_restores[0],
            change_id=move.change_id,
            restore_target=move.source,
            moved_from=move.destination,
            result_state=item_state,
        )
        for move in operation.expected_moves
    )
    undo = replace(
        undo,
        status=status,
        expected_restores=restores,
        verification=UndoVerificationResult(status="not_run"),
    )
    repository.save(undo)
    for item in undo.expected_restores:
        relative = item.moved_from if placement == "pending" else item.restore_target
        _write_expected(root / relative)
    return repository, undo


def _duplicate_move_state(
    root: Path, status: str, placement: str, *, selected: int = 1
):
    owners = _duplicate_moved(root, selected=selected)
    record = owners.operations.list_records()[0]
    item_state = (
        DUPLICATE_MOVE_PENDING
        if status == OP_PREPARED
        else DUPLICATE_MOVE_ATTEMPTING
        if status == OP_MOVING
        else DUPLICATE_MOVE_MOVED
    )
    attempted_at = None if status == OP_PREPARED else record.started_at
    completed_at = (
        record.completed_at
        if status in {OP_MOVED_UNVERIFIED, OP_INTERRUPTED}
        else None
    )
    record = replace(
        record,
        status=status,
        expected_moves=tuple(
            replace(
                item,
                result_state=item_state,
                attempted_at=attempted_at,
                completed_at=completed_at,
            )
            for item in record.expected_moves
        ),
        verification=DuplicateBinVerification(status="not_run"),
        completed_at=None if status != OP_INTERRUPTED else record.completed_at,
    )
    owners.operations.save(record)
    if placement == "pending":
        for item in record.expected_moves:
            source = owners.root / item.source
            destination = owners.root / item.destination
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(destination, source)
    return owners, record


def _duplicate_restore_state(
    root: Path, status: str, placement: str, *, selected: int = 1
):
    owners = _duplicate_moved(root, selected=selected)
    restores = DuplicateBinRestoreRepository(root)
    item_ids = tuple(item.bin_item_id for item in owners.batch.items)
    preview = _restore_preview(owners, restores, item_ids)
    _restore(owners, restores, preview, now=ISSUED + timedelta(minutes=1))
    record = restores.list_operations()[0]
    current_batch = owners.batches.load(owners.batch.batch_id)
    owners.batches.save_execution_state(
        current_batch.batch_id,
        expected_version=current_batch.batch_version,
        status=BATCH_MOVED,
        items=tuple(replace(item, state=ITEM_AVAILABLE) for item in current_batch.items),
        updated_at=(ISSUED + timedelta(minutes=2)).isoformat(),
    )
    item_state = (
        RESTORE_ITEM_PENDING
        if status == RESTORE_PREPARED
        else RESTORE_ITEM_ATTEMPTING
        if status == RESTORE_RESTORING
        else RESTORE_ITEM_RESTORED
    )
    attempted_at = None if status == RESTORE_PREPARED else record.started_at
    completed_at = (
        record.completed_at
        if status in {RESTORE_RESTORED_UNVERIFIED, RESTORE_INTERRUPTED}
        else None
    )
    record = replace(
        record,
        status=status,
        items=tuple(
            replace(
                item,
                state=item_state,
                attempted_at=attempted_at,
                completed_at=completed_at,
            )
            for item in record.items
        ),
        verification=DuplicateBinRestoreVerification(status="not_run"),
        completed_at=None if status != RESTORE_INTERRUPTED else record.completed_at,
    )
    restores.save_operation(record)
    if placement == "pending":
        for item in record.items:
            source = root / item.protected_source
            destination = root / item.previous_destination
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(destination, source)
    return owners, restores, record


if __name__ == "__main__":
    unittest.main()
