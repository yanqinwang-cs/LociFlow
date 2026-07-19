from __future__ import annotations

import os
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock
import uuid

import organizer.application.undo_service as undo_service
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.filesystem_operation_admission import (
    SAFE_RECOVERY_MESSAGE,
    FilesystemOperationBlockedError,
    RecoveryAdmissionState,
)
from organizer.application.organization_operation_models import (
    MOVE_MOVED,
    STATUS_VERIFIED,
    VERIFICATION_PASSED,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_operation_repository import OrganizationOperationRepository
from organizer.application.organization_undo_models import (
    UNDO_ITEM_FAILED,
    UNDO_ITEM_RESTORED,
    UNDO_PARTIAL,
    UNDO_RESTORED,
)
from organizer.application.organization_undo_repository import OrganizationUndoRepository
from organizer.application.undo_service import (
    CONFIRM_UNDO_FILE_MOVE,
    OrganizationUndoError,
    inspect_incomplete_organization_undo,
    preview_organization_undo,
    undo_organization_operation,
)
from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeOperationKind,
)
from organizer.executor import apply_move_plan
from organizer.models import MovePlanItem


class OrganizationUndoServiceTests(unittest.TestCase):
    def test_blocked_confirmation_keeps_undo_token_and_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, coordinator, record = _verified_operation(root, 1)
            preview = preview_organization_undo(
                operation_id=record.operation_id,
                browser_session_id="browser",
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            admission = mock.Mock()
            admission.root = root
            admission.inspect_and_require_clear.side_effect = (
                FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE
                )
            )

            with self.assertRaisesRegex(
                FilesystemOperationBlockedError, SAFE_RECOVERY_MESSAGE
            ):
                undo_organization_operation(
                    operation_id=record.operation_id,
                    browser_session_id="browser",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_UNDO_FILE_MOVE,
                    coordinator=coordinator,
                    operation_repository=operations,
                    undo_repository=undos,
                    filesystem_operation_admission=admission,
                )

            self.assertEqual(
                coordinator.undo_authorization_snapshot(
                    preview.confirmation_token
                ).status,
                "available",
            )
            self.assertEqual(undos.list_records(), ())
            item = record.expected_moves[0]
            self.assertFalse((root / item.source).exists())
            self.assertTrue((root / item.destination).is_file())
            self.assertEqual(coordinator.runtime_lifecycle.snapshot().active_count, 0)
            self.assertTrue(coordinator.try_acquire())
            coordinator.release()

    def test_stopping_rejects_preview_without_replacing_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, coordinator, record = _verified_operation(root, 1)
            existing = preview_organization_undo(
                operation_id=record.operation_id,
                browser_session_id="browser-one",
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            before = dict(coordinator._undo_authorizations)
            coordinator.runtime_lifecycle.begin_shutdown()

            with self.assertRaises(ApplicationStoppingError):
                preview_organization_undo(
                    operation_id=record.operation_id,
                    browser_session_id="browser-one",
                    coordinator=coordinator,
                    operation_repository=operations,
                    undo_repository=undos,
                )

            self.assertEqual(coordinator._undo_authorizations, before)
            self.assertIsNotNone(
                coordinator.undo_authorization_snapshot(existing.confirmation_token)
            )
            self.assertEqual(undos.list_records(), ())

    def test_preview_and_complete_undo_are_authorized_durable_verified_and_replay_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, coordinator, record = _verified_operation(root, 2)

            preview = preview_organization_undo(
                operation_id=record.operation_id,
                browser_session_id="browser-one",
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            self.assertEqual(preview.files_to_restore, 2)
            self.assertEqual(preview.confirmation_required, CONFIRM_UNDO_FILE_MOVE)
            self.assertEqual(undos.list_records(), ())

            real_verify = undo_service._verify_and_finish

            def checked_verify(*args, **kwargs):
                self.assertEqual(
                    coordinator.runtime_lifecycle.snapshot().active_kinds,
                    (RuntimeOperationKind.ACTIVITY_UNDO,),
                )
                return real_verify(*args, **kwargs)

            with mock.patch.object(
                undo_service,
                "_verify_and_finish",
                side_effect=checked_verify,
            ):
                outcome = undo_organization_operation(
                    operation_id=record.operation_id,
                    browser_session_id="browser-one",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_UNDO_FILE_MOVE,
                    coordinator=coordinator,
                    operation_repository=operations,
                    undo_repository=undos,
                )

            self.assertEqual(outcome.status, UNDO_RESTORED)
            self.assertEqual(outcome.files_restored, 2)
            self.assertEqual(outcome.verification_status, "passed")
            for index in range(2):
                self.assertTrue((root / "Inbox" / f"file-{index}.txt").is_file())
                self.assertFalse((root / "Documents" / f"file-{index}.txt").exists())
            persisted = OrganizationUndoRepository(root).load(outcome.undo_id)
            self.assertEqual(persisted.status, UNDO_RESTORED)
            self.assertTrue(persisted.legacy_undo_log)

            with self.assertRaisesRegex(OrganizationUndoError, "undo_already_started"):
                undo_organization_operation(
                    operation_id=record.operation_id,
                    browser_session_id="browser-one",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_UNDO_FILE_MOVE,
                    coordinator=coordinator,
                    operation_repository=operations,
                    undo_repository=undos,
                )
            self.assertEqual(
                coordinator.runtime_lifecycle.snapshot().active_count,
                0,
            )

    def test_stopping_rejects_before_undo_intent_or_filesystem_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, coordinator, record = _verified_operation(root, 1)
            preview = preview_organization_undo(
                operation_id=record.operation_id,
                browser_session_id="browser-one",
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            coordinator.runtime_lifecycle.begin_shutdown()

            with self.assertRaises(ApplicationStoppingError):
                undo_organization_operation(
                    operation_id=record.operation_id,
                    browser_session_id="browser-one",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_UNDO_FILE_MOVE,
                    coordinator=coordinator,
                    operation_repository=operations,
                    undo_repository=undos,
                )

            self.assertTrue((root / "Documents/file-0.txt").is_file())
            self.assertFalse((root / "Inbox/file-0.txt").exists())
            self.assertEqual(undos.list_records(), ())
            self.assertEqual(
                coordinator.runtime_lifecycle.snapshot().active_count,
                0,
            )

    def test_wrong_session_confirmation_and_shared_lock_restore_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, coordinator, record = _verified_operation(root, 1)
            preview = preview_organization_undo(
                operation_id=record.operation_id,
                browser_session_id="browser-one",
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            common = dict(
                operation_id=record.operation_id,
                confirmation_token=preview.confirmation_token,
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            with self.assertRaises(OrganizationUndoError):
                undo_organization_operation(
                    browser_session_id="browser-two",
                    confirmation=CONFIRM_UNDO_FILE_MOVE,
                    **common,
                )
            with self.assertRaises(OrganizationUndoError):
                undo_organization_operation(
                    browser_session_id="browser-one",
                    confirmation="undo",
                    **common,
                )
            self.assertEqual(undos.list_records(), ())

            self.assertTrue(coordinator.try_acquire())
            try:
                with self.assertRaisesRegex(OrganizationUndoError, "movement_or_undo_in_progress"):
                    undo_organization_operation(
                        browser_session_id="browser-one",
                        confirmation=CONFIRM_UNDO_FILE_MOVE,
                        **common,
                    )
            finally:
                coordinator.release()
            self.assertEqual(undos.list_records(), ())

            outcome = undo_organization_operation(
                browser_session_id="browser-one",
                confirmation=CONFIRM_UNDO_FILE_MOVE,
                **common,
            )
            self.assertEqual(outcome.status, UNDO_RESTORED)

    def test_changed_destination_and_source_collision_reject_preview_without_mutation(self) -> None:
        for change, code in (("changed", "undo_source_changed"), ("collision", "undo_destination_occupied")):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operations, undos, coordinator, record = _verified_operation(root, 1)
                if change == "changed":
                    (root / "Documents/file-0.txt").write_bytes(b"changed")
                else:
                    source = root / "Inbox/file-0.txt"
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_text("collision", encoding="utf-8")
                with self.assertRaisesRegex(OrganizationUndoError, code):
                    preview_organization_undo(
                        operation_id=record.operation_id,
                        browser_session_id="browser",
                        coordinator=coordinator,
                        operation_repository=operations,
                        undo_repository=undos,
                    )
                self.assertEqual(undos.list_records(), ())

    def test_missing_symlink_and_broken_source_are_rejected_without_restore(self) -> None:
        for change, code in (
            ("missing", "undo_source_missing"),
            ("destination_symlink", "unsafe_undo_path"),
            ("broken_source_symlink", "undo_destination_occupied"),
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_directory:
                root = Path(directory).resolve()
                operations, undos, coordinator, record = _verified_operation(root, 1)
                destination = root / "Documents/file-0.txt"
                if change == "missing":
                    destination.unlink()
                elif change == "destination_symlink":
                    destination.unlink()
                    outside = Path(outside_directory) / "outside.txt"
                    outside.write_text("data", encoding="utf-8")
                    destination.symlink_to(outside)
                else:
                    source = root / "Inbox/file-0.txt"
                    source.symlink_to(root / "missing-target")
                with self.assertRaisesRegex(OrganizationUndoError, code):
                    preview_organization_undo(
                        operation_id=record.operation_id,
                        browser_session_id="browser",
                        coordinator=coordinator,
                        operation_repository=operations,
                        undo_repository=undos,
                    )
                self.assertEqual(undos.list_records(), ())

    def test_legacy_log_is_server_resolved_strictly_typed_and_exactly_correlated(self) -> None:
        import json

        for change, code in (
            ("missing", "unsafe_undo_path"),
            ("symlink", "unsafe_undo_path"),
            ("malformed_bool", "unsafe_undo_path"),
            ("wrong_pair", "operation_not_undoable"),
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operations, undos, coordinator, record = _verified_operation(root, 1)
                log = root / str(record.legacy_operation_log)
                if change == "missing":
                    log.unlink()
                elif change == "symlink":
                    target = log.with_name("target.json")
                    log.rename(target)
                    log.symlink_to(target)
                else:
                    payload = json.loads(log.read_text(encoding="utf-8"))
                    if change == "malformed_bool":
                        payload["operations"][0]["success"] = "true"
                    else:
                        payload["operations"][0]["destination"] = str(root / "Documents/other.txt")
                    log.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(OrganizationUndoError, code):
                    preview_organization_undo(
                        operation_id=record.operation_id,
                        browser_session_id="browser",
                        coordinator=coordinator,
                        operation_repository=operations,
                        undo_repository=undos,
                    )
                self.assertEqual(undos.list_records(), ())

    def test_injected_second_restore_failure_is_durable_partial_and_not_reoffered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, coordinator, record = _verified_operation(root, 2)
            preview = preview_organization_undo(
                operation_id=record.operation_id,
                browser_session_id="browser",
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            import shutil
            real_move = shutil.move
            calls = 0

            def fail_second(source: str, destination: str):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected")
                return real_move(source, destination)

            with mock.patch("organizer.executor.shutil.move", side_effect=fail_second):
                outcome = undo_organization_operation(
                    operation_id=record.operation_id,
                    browser_session_id="browser",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_UNDO_FILE_MOVE,
                    coordinator=coordinator,
                    operation_repository=operations,
                    undo_repository=undos,
                )
            persisted = undos.load(outcome.undo_id)
            self.assertEqual(outcome.status, UNDO_PARTIAL)
            self.assertEqual(
                [item.result_state for item in persisted.expected_restores],
                [UNDO_ITEM_RESTORED, UNDO_ITEM_FAILED],
            )
            with self.assertRaises(OrganizationUndoError):
                preview_organization_undo(
                    operation_id=record.operation_id,
                    browser_session_id="browser",
                    coordinator=coordinator,
                    operation_repository=operations,
                    undo_repository=undos,
                )

    def test_incomplete_inspection_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, coordinator, record = _verified_operation(root, 1)
            preview = preview_organization_undo(
                operation_id=record.operation_id,
                browser_session_id="browser",
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            original_save = OrganizationUndoRepository.save
            save_calls = 0

            def fail_once(repository, value):
                nonlocal save_calls
                save_calls += 1
                if save_calls == 1:
                    raise OSError("before restore")
                return original_save(repository, value)

            with mock.patch(
                "organizer.application.organization_undo_repository.OrganizationUndoRepository.save",
                new=fail_once,
            ):
                outcome = undo_organization_operation(
                    operation_id=record.operation_id,
                    browser_session_id="browser",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_UNDO_FILE_MOVE,
                    coordinator=coordinator,
                    operation_repository=operations,
                    undo_repository=undos,
                )
            inspection = inspect_incomplete_organization_undo(
                undo_id=outcome.undo_id,
                undo_repository=undos,
            )
            self.assertEqual(inspection.likely_not_restored, 1)
            self.assertTrue((root / "Documents/file-0.txt").exists())


    def test_admitted_activity_undo_completes_after_shutdown_begins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, coordinator, record = _verified_operation(root, 1)
            preview = preview_organization_undo(
                operation_id=record.operation_id,
                browser_session_id="browser-one",
                coordinator=coordinator,
                operation_repository=operations,
                undo_repository=undos,
            )
            verifying = Event()
            continue_verification = Event()
            outcomes = []
            real_verify = undo_service._verify_and_finish

            def paused_verify(*args, **kwargs):
                verifying.set()
                continue_verification.wait(timeout=2)
                return real_verify(*args, **kwargs)

            worker = Thread(
                target=lambda: outcomes.append(
                    undo_organization_operation(
                        operation_id=record.operation_id,
                        browser_session_id="browser-one",
                        confirmation_token=preview.confirmation_token,
                        confirmation=CONFIRM_UNDO_FILE_MOVE,
                        coordinator=coordinator,
                        operation_repository=operations,
                        undo_repository=undos,
                    )
                )
            )
            with mock.patch.object(
                undo_service,
                "_verify_and_finish",
                side_effect=paused_verify,
            ):
                worker.start()
                self.assertTrue(verifying.wait(1))
                stopping = coordinator.runtime_lifecycle.begin_shutdown()
                self.assertEqual(stopping.active_count, 1)
                continue_verification.set()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0].status, UNDO_RESTORED)
            self.assertEqual(
                coordinator.runtime_lifecycle.wait_until_idle().active_count,
                0,
            )


def _verified_operation(
    root: Path,
    count: int,
) -> tuple[
    OrganizationOperationRepository,
    OrganizationUndoRepository,
    OrganizationExecutionCoordinator,
    OrganizationOperationRecord,
]:
    operations = OrganizationOperationRepository(root)
    undos = OrganizationUndoRepository(root)
    coordinator = OrganizationExecutionCoordinator()
    plan: list[MovePlanItem] = []
    moves: list[OrganizationExpectedMove] = []
    for index in range(count):
        source = root / "Inbox" / f"file-{index}.txt"
        destination = root / "Documents" / f"file-{index}.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"data-{index}".encode())
        modified = 100.0 + index
        os.utime(source, (modified, modified))
        plan.append(MovePlanItem(source, destination, "test", 100, "move", False))
        moves.append(OrganizationExpectedMove(
            change_id=f"O{index}", source=source.relative_to(root).as_posix(),
            destination=destination.relative_to(root).as_posix(),
            source_size_bytes=source.stat().st_size, source_modified_time=modified,
            reason="test", confidence=100, operation="move", overwrite_risk=False,
            result_state=MOVE_MOVED,
        ))
    log = apply_move_plan(plan, root)
    timestamp = "2026-07-15T08:00:00+00:00"
    record = OrganizationOperationRecord(
        operation_id=str(uuid.uuid4()), authorization_id=str(uuid.uuid4()),
        status=STATUS_VERIFIED, scan_generation="scan-1", decision_revision=1,
        approved_at=timestamp, created_at=timestamp, started_at=timestamp,
        completed_at=timestamp, configured_root_identity=operations.configured_root_identity,
        expected_moves=tuple(moves),
        verification=OrganizationOperationVerification(
            status=VERIFICATION_PASSED, verified_count=count,
        ),
        legacy_operation_log=log.log_path.relative_to(root).as_posix(),
        failure_code=None, duplicate_actions_deferred=0,
    )
    operations.create(record)
    return operations, undos, coordinator, record


if __name__ == "__main__":
    unittest.main()
