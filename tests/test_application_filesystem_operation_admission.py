from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from organizer.application.duplicate_bin_models import BATCH_MOVED, ITEM_RESTORED
from organizer.application.duplicate_bin_movement_models import (
    MOVE_ATTEMPTING as DUPLICATE_MOVE_ATTEMPTING,
    MOVE_MOVED as DUPLICATE_MOVE_MOVED,
    MOVE_PENDING as DUPLICATE_MOVE_PENDING,
    OP_INTERRUPTED,
    OP_MOVED_UNVERIFIED,
    OP_MOVING,
    OP_PREPARED,
    OP_TERMINAL_STATUSES,
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
    RESTORE_TERMINAL,
    DuplicateBinRestoreVerification,
)
from organizer.application.duplicate_bin_restore_repository import (
    DuplicateBinRestoreRepository,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
    RecoveryAdmissionSnapshot,
    RecoveryAdmissionState,
)
from organizer.application.organization_operation_models import (
    MOVE_MOVED,
    REPLAYABLE_TERMINAL_STATUSES,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVING,
    STATUS_PREPARED,
    STATUS_VERIFIED,
    VERIFICATION_NOT_RUN,
    VERIFICATION_PASSED,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_models import (
    UNDO_FAILED,
    UNDO_INTERRUPTED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
    UNDO_VERIFICATION_FAILED,
    OrganizationUndoItem,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from tests.test_application_duplicate_bin_movement_service import (
    ISSUED,
    _execute,
    _owners,
    _preview,
)
from tests.test_application_duplicate_bin_restore_service import (
    _restore,
    _restore_preview,
)


class FilesystemOperationInspectorTests(unittest.TestCase):
    def test_empty_root_is_clear_without_creating_operation_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            snapshot = FilesystemOperationInspector(root).inspect()

            self.assertEqual(snapshot, _snapshot(RecoveryAdmissionState.CLEAR))
            self.assertFalse(snapshot.is_blocked)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_ordinary_movement_terminal_states_are_clear(self) -> None:
        for status in sorted(REPLAYABLE_TERMINAL_STATUSES):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository, record = _ordinary_operation(root)
                repository.save(replace(record, status=status))

                self.assertEqual(
                    FilesystemOperationInspector(root).inspect().state,
                    RecoveryAdmissionState.CLEAR,
                )

    def test_every_unfinished_ordinary_movement_status_blocks(self) -> None:
        for status in (
            STATUS_PREPARED,
            STATUS_MOVING,
            STATUS_MOVED_UNVERIFIED,
            STATUS_INTERRUPTED,
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository, record = _ordinary_operation(root)
                repository.save(
                    replace(
                        record,
                        status=status,
                        verification=OrganizationOperationVerification(
                            status=VERIFICATION_NOT_RUN
                        ),
                    )
                )

                self.assertEqual(
                    FilesystemOperationInspector(root).inspect().state,
                    RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION,
                )

    def test_ordinary_undo_terminal_and_unfinished_states(self) -> None:
        terminal = (UNDO_RESTORED, UNDO_PARTIAL, UNDO_FAILED, UNDO_VERIFICATION_FAILED)
        unfinished = (
            UNDO_PREPARED,
            UNDO_UNDOING,
            UNDO_RESTORED_UNVERIFIED,
            UNDO_INTERRUPTED,
        )
        for status in terminal + unfinished:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operation_repository, operation = _ordinary_operation(root)
                undo_repository, undo = _ordinary_undo(root, operation)
                undo_repository.save(replace(undo, status=status))

                expected = (
                    RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                    if status in unfinished
                    else RecoveryAdmissionState.CLEAR
                )
                self.assertEqual(
                    FilesystemOperationInspector(root).inspect().state,
                    expected,
                )
                self.assertEqual(operation_repository.load(operation.operation_id), operation)

    def test_missing_or_incompatible_undo_parent_is_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, operation = _ordinary_operation(root)
            undo_repository, undo = _ordinary_undo(root, operation)
            operation_path = (
                root
                / "AI_Review"
                / "organization_operations"
                / f"{operation.operation_id}.json"
            )
            operation_path.unlink()

            self.assertEqual(
                FilesystemOperationInspector(root).inspect().state,
                RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE,
            )

            incompatible_id = str(uuid.uuid4())
            undo_repository.save(replace(undo, operation_id=incompatible_id))
            self.assertEqual(
                FilesystemOperationInspector(root).inspect().state,
                RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE,
            )

    def test_duplicate_bin_movement_terminal_and_all_unfinished_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _moved(root)
            record = owners.operations.list_records()[0]
            self.assertEqual(
                FilesystemOperationInspector(root).inspect().state,
                RecoveryAdmissionState.CLEAR,
            )
            for status in sorted(OP_TERMINAL_STATUSES):
                terminal = (
                    record
                    if status == record.status
                    else replace(
                        record,
                        status=status,
                        verification=replace(record.verification, status="failed"),
                    )
                )
                owners.operations.save(terminal)
                self.assertEqual(
                    FilesystemOperationInspector(root).inspect().state,
                    RecoveryAdmissionState.CLEAR,
                )
            for status in (OP_PREPARED, OP_MOVING, OP_MOVED_UNVERIFIED, OP_INTERRUPTED):
                with self.subTest(status=status):
                    owners.operations.save(_unfinished_duplicate_move(record, status))
                    self.assertEqual(
                        FilesystemOperationInspector(root).inspect().state,
                        RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION,
                    )
            owners.operations.save(record)

    def test_duplicate_bin_restore_terminal_and_all_unfinished_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _moved(root)
            restores = DuplicateBinRestoreRepository(root)
            item_id = owners.batch.items[0].bin_item_id
            preview = _restore_preview(owners, restores, (item_id,))
            _restore(owners, restores, preview)
            record = restores.list_operations()[0]
            self.assertEqual(
                FilesystemOperationInspector(root).inspect().state,
                RecoveryAdmissionState.CLEAR,
            )
            for status in sorted(RESTORE_TERMINAL):
                terminal = (
                    record
                    if status == record.status
                    else replace(
                        record,
                        status=status,
                        verification=replace(record.verification, status="failed"),
                    )
                )
                restores.save_operation(terminal)
                self.assertEqual(
                    FilesystemOperationInspector(root).inspect().state,
                    RecoveryAdmissionState.CLEAR,
                )
            for status in (
                RESTORE_PREPARED,
                RESTORE_RESTORING,
                RESTORE_RESTORED_UNVERIFIED,
                RESTORE_INTERRUPTED,
            ):
                with self.subTest(status=status):
                    restores.save_operation(_unfinished_restore(record, status))
                    self.assertEqual(
                        FilesystemOperationInspector(root).inspect().state,
                        RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION,
                    )

    def test_activity_undo_reuses_restore_family_without_another_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _moved(root)
            restores = DuplicateBinRestoreRepository(root)
            preview = _restore_preview(
                owners,
                restores,
                (owners.batch.items[0].bin_item_id,),
            )
            _restore(owners, restores, preview)

            durable_directories = {
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir() and "operations" in path.parts
            }
            self.assertIn("AI_Review/duplicate_bin/restores/operations", durable_directories)
            self.assertNotIn("AI_Review/duplicate_bin/activity_undo", durable_directories)

    def test_malformed_record_in_each_family_fails_closed(self) -> None:
        locations = (
            Path("AI_Review/organization_operations"),
            Path("AI_Review/organization_undos"),
            Path("AI_Review/duplicate_bin/operations"),
            Path("AI_Review/duplicate_bin/restores/operations"),
        )
        for relative in locations:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                target = root / relative / f"{uuid.uuid4()}.json"
                target.parent.mkdir(parents=True)
                target.write_text("{not-json", encoding="utf-8")

                self.assertEqual(
                    FilesystemOperationInspector(root).inspect().state,
                    RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE,
                )

    def test_unknown_status_and_duplicate_ownership_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, operation = _ordinary_operation(root)
            path = repository.directory / f"{operation.operation_id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["status"] = "unknown"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(
                FilesystemOperationInspector(root).inspect().state,
                RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, operation = _ordinary_operation(root)
            source = repository.directory / f"{operation.operation_id}.json"
            duplicate = json.loads(source.read_text(encoding="utf-8"))
            duplicate_id = str(uuid.uuid4())
            duplicate["operation_id"] = duplicate_id
            (repository.directory / f"{duplicate_id}.json").write_text(
                json.dumps(duplicate),
                encoding="utf-8",
            )
            self.assertEqual(
                FilesystemOperationInspector(root).inspect().state,
                RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE,
            )

    def test_multiple_unfinished_families_publish_one_safe_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _ordinary_operation(root, status=STATUS_INTERRUPTED)
            owners = _moved(root)
            operation = owners.operations.list_records()[0]
            owners.operations.save(
                _unfinished_duplicate_move(operation, OP_INTERRUPTED)
            )

            snapshot = FilesystemOperationInspector(root).inspect()

            self.assertEqual(
                snapshot,
                RecoveryAdmissionSnapshot(
                    RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                ),
            )
            self.assertEqual(tuple(snapshot.__dataclass_fields__), ("state",))

    def test_wrong_root_and_symlinked_records_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root = Path(first).resolve()
            second_root = Path(second).resolve()
            repository, operation = _ordinary_operation(first_root)
            source = repository.directory / f"{operation.operation_id}.json"
            target = second_root / "AI_Review/organization_operations" / source.name
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            self.assertEqual(
                FilesystemOperationInspector(second_root).inspect().state,
                RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operation_directory = root / "AI_Review/organization_operations"
            operation_directory.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            (operation_directory / f"{uuid.uuid4()}.json").symlink_to(outside)
            self.assertEqual(
                FilesystemOperationInspector(root).inspect().state,
                RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE,
            )

    def test_duplicate_operation_batch_and_authorization_mismatches_are_inconsistent(self) -> None:
        for field in ("batch_id", "authorization_operation"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                owners = _moved(root)
                operation = owners.operations.list_records()[0]
                if field == "batch_id":
                    path = owners.operations.directory / f"{operation.operation_id}.json"
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["batch_id"] = str(uuid.uuid4())
                else:
                    path = owners.authorizations.path
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["authorizations"][0]["operation_id"] = str(uuid.uuid4())
                path.write_text(json.dumps(data), encoding="utf-8")

                self.assertEqual(
                    FilesystemOperationInspector(root).inspect().state,
                    RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE,
                )

    def test_inspection_is_deterministic_refreshable_and_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _moved(root)
            inspector = FilesystemOperationInspector(root)
            before = _durable_bytes_and_mtimes(root)

            with mock.patch("organizer.executor.apply_move_plan") as executor:
                first = inspector.inspect()
                second = inspector.inspect()

            self.assertEqual(first, second)
            self.assertEqual(first.state, RecoveryAdmissionState.CLEAR)
            self.assertEqual(_durable_bytes_and_mtimes(root), before)
            executor.assert_not_called()

            operation = owners.operations.list_records()[0]
            owners.operations.save(
                _unfinished_duplicate_move(operation, OP_INTERRUPTED)
            )
            self.assertEqual(
                inspector.inspect().state,
                RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION,
            )

    def test_ordinary_unfinished_inspection_does_not_mark_or_rewrite_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, operation = _ordinary_operation(root, status=STATUS_MOVING)
            path = repository.directory / f"{operation.operation_id}.json"
            before = (path.read_bytes(), path.stat().st_mtime_ns)

            snapshot = FilesystemOperationInspector(root).inspect()

            self.assertEqual(
                snapshot.state,
                RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION,
            )
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
            self.assertEqual(repository.load(operation.operation_id).status, STATUS_MOVING)

    def test_inspection_does_not_repair_fully_restored_batch_or_change_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            owners = _moved(root)
            current = owners.batches.load(owners.batch.batch_id)
            owners.batches.save_execution_state(
                current.batch_id,
                expected_version=current.batch_version,
                status=BATCH_MOVED,
                items=tuple(replace(item, state=ITEM_RESTORED) for item in current.items),
                updated_at="2026-07-16T11:00:00+00:00",
            )
            catalog_before = owners.batches.catalog_path.read_bytes()
            authorization_before = owners.authorizations.path.read_bytes()

            FilesystemOperationInspector(root).inspect()
            app = create_app(
                WebAppConfig(
                    root,
                    session_secret="s" * 32,
                    launch_token="t" * 32,
                    testing=True,
                )
            )

            self.assertEqual(owners.batches.catalog_path.read_bytes(), catalog_before)
            self.assertEqual(owners.authorizations.path.read_bytes(), authorization_before)
            self.assertEqual(
                app.state.recovery_admission_snapshot.state,
                RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE,
            )

    def test_multiple_roots_are_isolated_and_app_construction_publishes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root = Path(first).resolve()
            second_root = Path(second).resolve()
            _ordinary_operation(first_root, status=STATUS_INTERRUPTED)

            self.assertTrue(FilesystemOperationInspector(first_root).inspect().is_blocked)
            self.assertFalse(FilesystemOperationInspector(second_root).inspect().is_blocked)

            malformed = second_root / "AI_Review/duplicate_bin/catalog.json"
            malformed.parent.mkdir(parents=True)
            malformed.write_text("{bad", encoding="utf-8")
            before = malformed.read_bytes()
            app = create_app(
                WebAppConfig(
                    second_root,
                    session_secret="s" * 32,
                    launch_token="t" * 32,
                    testing=True,
                )
            )
            self.assertEqual(
                app.state.recovery_admission_snapshot.state,
                RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE,
            )
            self.assertIs(
                app.state.filesystem_operation_inspector.duplicate_bin_batches,
                app.state.duplicate_bin_batches,
            )
            self.assertEqual(malformed.read_bytes(), before)


def _snapshot(state: RecoveryAdmissionState) -> RecoveryAdmissionSnapshot:
    return RecoveryAdmissionSnapshot(state)


def _ordinary_operation(root: Path, *, status: str = STATUS_VERIFIED):
    repository = OrganizationOperationRepository(root)
    move = OrganizationExpectedMove(
        change_id="O1",
        source="Inbox/file.txt",
        destination="Documents/file.txt",
        source_size_bytes=4,
        source_modified_time=100.0,
        reason="test",
        confidence=100,
        operation="move",
        overwrite_risk=False,
        result_state=MOVE_MOVED,
    )
    verification = OrganizationOperationVerification(
        status=VERIFICATION_PASSED,
        verified_count=1,
    )
    record = OrganizationOperationRecord(
        operation_id=str(uuid.uuid4()),
        authorization_id=str(uuid.uuid4()),
        status=status,
        scan_generation="scan-one",
        decision_revision=1,
        approved_at="2026-07-16T10:00:00+00:00",
        created_at="2026-07-16T10:00:00+00:00",
        started_at="2026-07-16T10:00:01+00:00",
        completed_at="2026-07-16T10:00:02+00:00",
        configured_root_identity=repository.configured_root_identity,
        expected_moves=(move,),
        verification=verification,
        legacy_operation_log="AI_Review/operation_logs/operation.json",
        failure_code=None,
        duplicate_actions_deferred=0,
    )
    repository.create(record)
    return repository, record


def _ordinary_undo(root: Path, operation: OrganizationOperationRecord):
    repository = OrganizationUndoRepository(root)
    move = operation.expected_moves[0]
    record = OrganizationUndoRecord(
        undo_id=str(uuid.uuid4()),
        operation_id=operation.operation_id,
        authorization_id=str(uuid.uuid4()),
        configured_root_identity=repository.configured_root_identity,
        status=UNDO_RESTORED,
        created_at="2026-07-16T10:05:00+00:00",
        started_at="2026-07-16T10:05:01+00:00",
        completed_at="2026-07-16T10:05:02+00:00",
        expected_restores=(
            OrganizationUndoItem(
                change_id=move.change_id,
                restore_target=move.source,
                moved_from=move.destination,
                baseline_size_bytes=move.source_size_bytes,
                baseline_modified_time=move.source_modified_time,
            ),
        ),
        verification=UndoVerificationResult(status="passed", verified_count=1),
        legacy_undo_log="AI_Review/undo_logs/undo.json",
        failure_code=None,
    )
    repository.create(record)
    return repository, record


def _moved(root: Path):
    owners = _owners(root, selected=1)
    preview = _preview(owners, issued=ISSUED)
    _execute(owners, preview, now=ISSUED.replace(minute=1))
    return owners


def _unfinished_duplicate_move(record, status):
    if status == OP_PREPARED:
        state = DUPLICATE_MOVE_PENDING
        attempted_at = None
        completed_at = None
    elif status == OP_MOVING:
        state = DUPLICATE_MOVE_ATTEMPTING
        attempted_at = record.started_at or record.created_at
        completed_at = None
    else:
        state = DUPLICATE_MOVE_MOVED
        attempted_at = record.started_at or record.created_at
        completed_at = record.completed_at or record.created_at
    return replace(
        record,
        status=status,
        expected_moves=tuple(
            replace(
                item,
                result_state=state,
                attempted_at=attempted_at,
                completed_at=completed_at,
            )
            for item in record.expected_moves
        ),
        verification=DuplicateBinVerification(status="not_run", verified_count=0),
    )


def _unfinished_restore(record, status):
    if status == RESTORE_PREPARED:
        state = RESTORE_ITEM_PENDING
        attempted_at = None
        completed_at = None
    elif status == RESTORE_RESTORING:
        state = RESTORE_ITEM_ATTEMPTING
        attempted_at = record.started_at or record.created_at
        completed_at = None
    else:
        state = RESTORE_ITEM_RESTORED
        attempted_at = record.started_at or record.created_at
        completed_at = record.completed_at or record.created_at
    return replace(
        record,
        status=status,
        items=tuple(
            replace(
                item,
                state=state,
                attempted_at=attempted_at,
                completed_at=completed_at,
            )
            for item in record.items
        ),
        verification=DuplicateBinRestoreVerification(
            status="not_run",
            verified_count=0,
        ),
    )


def _durable_bytes_and_mtimes(root: Path):
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
