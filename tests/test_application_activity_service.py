from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from organizer.application.activity_service import list_activity_entries
from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.organization_operation_models import (
    MOVE_FAILED,
    MOVE_MOVED,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_INTERRUPTED,
    STATUS_MOVEMENT_FAILED,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_models import (
    UNDO_ITEM_RESTORED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED,
    UNDO_VERIFICATION_PASSED,
    OrganizationUndoItem,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)


class ActivityServiceTests(unittest.TestCase):
    def test_empty_repositories_produce_empty_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = list_activity_entries(
                operation_repository=OrganizationOperationRepository(root),
                undo_repository=OrganizationUndoRepository(root),
            )
            self.assertEqual(result.to_dict(), {"activity": []})

    def test_verified_movement_is_safe_restart_stable_and_live_undoable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations = OrganizationOperationRepository(root)
            undos = OrganizationUndoRepository(root)
            record = _operation(operations, root, status=STATUS_VERIFIED, live=True)
            operations.create(record)

            first = list_activity_entries(
                operation_repository=operations, undo_repository=undos
            )
            second = list_activity_entries(
                operation_repository=OrganizationOperationRepository(root),
                undo_repository=OrganizationUndoRepository(root),
            )
            entry = first.activity[0]
            serialized = json.dumps(first.to_dict())

            self.assertEqual(first, second)
            self.assertEqual(entry.title, "Organized 1 file")
            self.assertTrue(entry.undoable)
            self.assertEqual(entry.details[0].source, "Inbox/a.txt")
            self.assertNotIn(str(root), serialized)
            for forbidden in (
                "legacy_operation_log",
                "authorization_id",
                "baseline",
                "sha256",
            ):
                self.assertNotIn(forbidden, serialized.lower())

    def test_status_language_and_linked_restore_are_projected_separately(self) -> None:
        cases = (
            (STATUS_PARTIAL_MOVEMENT, "Move partly completed"),
            (STATUS_MOVEMENT_FAILED, "Move failed"),
            (STATUS_VERIFICATION_FAILED, "Move could not be confirmed"),
            (STATUS_INTERRUPTED, "Move needs attention"),
        )
        for status, title in cases:
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                operations = OrganizationOperationRepository(root)
                undos = OrganizationUndoRepository(root)
                operation = _operation(operations, root, status=status)
                operations.create(operation)
                result = list_activity_entries(
                    operation_repository=operations, undo_repository=undos
                )
                self.assertEqual(result.activity[0].title, title)
                self.assertFalse(result.activity[0].undoable)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations = OrganizationOperationRepository(root)
            undos = OrganizationUndoRepository(root)
            operation = _operation(operations, root, status=STATUS_VERIFIED, live=True)
            operations.create(operation)
            undos.create(_undo(undos, operation, status=UNDO_RESTORED))
            entries = list_activity_entries(
                operation_repository=operations, undo_repository=undos
            ).activity
            self.assertEqual(
                {entry.type for entry in entries}, {"organization", "restore"}
            )
            self.assertEqual(entries[0].title, "Restored 1 file")
            self.assertFalse(
                next(
                    entry for entry in entries if entry.type == "organization"
                ).undoable
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations = OrganizationOperationRepository(root)
            undos = OrganizationUndoRepository(root)
            operation = _operation(operations, root, status=STATUS_VERIFIED, live=True)
            operations.create(operation)
            undos.create(_undo(undos, operation, status=UNDO_PREPARED))
            entries = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            ).activity
            self.assertEqual(entries[0].title, "Restore needs attention")
            self.assertFalse(any(entry.undoable for entry in entries))

    def test_ordering_uses_parsed_timestamps_then_id_not_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations = OrganizationOperationRepository(root)
            undos = OrganizationUndoRepository(root)
            old = _operation(
                operations,
                root,
                status=STATUS_MOVEMENT_FAILED,
                operation_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
                created_at="2026-07-14T12:00:00+00:00",
            )
            new = _operation(
                operations,
                root,
                status=STATUS_MOVEMENT_FAILED,
                operation_id="00000000-0000-4000-8000-000000000001",
                created_at="2026-07-15T12:00:00+00:00",
            )
            operations.create(old)
            operations.create(new)
            entries = list_activity_entries(
                operation_repository=operations, undo_repository=undos
            ).activity
            self.assertEqual(
                [entry.id for entry in entries], [new.operation_id, old.operation_id]
            )

    def test_linked_partial_undo_blocks_normal_undo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations = OrganizationOperationRepository(root)
            undos = OrganizationUndoRepository(root)
            operation = _operation(operations, root, status=STATUS_VERIFIED, live=True)
            operations.create(operation)
            undos.create(_undo(undos, operation, status=UNDO_PARTIAL))
            entries = list_activity_entries(
                operation_repository=operations, undo_repository=undos
            ).activity
            self.assertFalse(any(entry.undoable for entry in entries))
            self.assertEqual(entries[0].title, "Restore partly completed")

    def test_later_clean_zero_move_failure_is_ignored_but_partial_move_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations = OrganizationOperationRepository(root)
            undos = OrganizationUndoRepository(root)
            verified = _operation(
                operations,
                root,
                status=STATUS_VERIFIED,
                live=True,
                created_at="2026-07-15T08:00:00+00:00",
            )
            failed = _operation(
                operations,
                root,
                status=STATUS_MOVEMENT_FAILED,
                created_at="2026-07-15T09:00:00+00:00",
            )
            operations.create(verified)
            operations.create(failed)
            entries = list_activity_entries(
                operation_repository=operations, undo_repository=undos
            ).activity
            self.assertEqual(
                [entry.id for entry in entries if entry.undoable],
                [verified.operation_id],
            )

            partial = _operation(
                operations,
                root,
                status=STATUS_PARTIAL_MOVEMENT,
                created_at="2026-07-15T10:00:00+00:00",
            )
            operations.create(partial)
            entries = list_activity_entries(
                operation_repository=operations, undo_repository=undos
            ).activity
            self.assertFalse(any(entry.undoable for entry in entries))

    def test_verified_record_with_zero_moves_is_not_undoable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations = OrganizationOperationRepository(root)
            undos = OrganizationUndoRepository(root)
            record = replace(
                _operation(operations, root, status=STATUS_VERIFIED),
                expected_moves=(),
                verification=OrganizationOperationVerification(
                    status=VERIFICATION_PASSED,
                    verified_count=0,
                ),
            )
            operations.create(record)
            result = list_activity_entries(
                operation_repository=operations, undo_repository=undos
            )
            self.assertFalse(result.activity[0].undoable)

    def test_verified_v2_record_remains_non_undoable_with_valid_legacy_log(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations = OrganizationOperationRepository(root)
            undos = OrganizationUndoRepository(root)
            v1 = _operation(operations, root, status=STATUS_VERIFIED, live=True)
            v2 = replace(
                v1,
                schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
                expected_moves=tuple(
                    replace(
                        item,
                        content_identity=RegularFileContentIdentity(
                            "regular_file", "sha256", "a" * 64
                        ),
                    )
                    for item in v1.expected_moves
                ),
            )
            operations.create(v2)

            result = list_activity_entries(
                operation_repository=operations,
                undo_repository=undos,
            )

            self.assertEqual(len(result.activity), 1)
            self.assertFalse(result.activity[0].undoable)


def _operation(
    repository: OrganizationOperationRepository,
    root: Path,
    *,
    status: str,
    live: bool = False,
    operation_id: str | None = None,
    created_at: str = "2026-07-15T08:00:00+00:00",
) -> OrganizationOperationRecord:
    source = root / "Inbox" / "a.txt"
    destination = root / "Documents" / "a.txt"
    size = 4
    modified = 100.0
    result_state = MOVE_MOVED
    verification_status = VERIFICATION_PASSED
    verified_count = 1
    legacy: str | None = None
    if status == STATUS_MOVEMENT_FAILED:
        result_state = MOVE_FAILED
        verification_status = "not_run"
        verified_count = 0
    elif status == STATUS_PARTIAL_MOVEMENT:
        verification_status = VERIFICATION_FAILED
        verified_count = 0
    elif status in {STATUS_VERIFICATION_FAILED, STATUS_INTERRUPTED}:
        verification_status = VERIFICATION_FAILED
        verified_count = 0
    if live:
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"data")
        os.utime(destination, (modified, modified))
        log = (
            root / "AI_Review" / "operation_logs" / f"operation_log_{uuid.uuid4()}.json"
        )
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            json.dumps(
                {
                    "operations": [
                        {
                            "source": str(source),
                            "destination": str(destination),
                            "success": True,
                            "message": "moved",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        legacy = log.relative_to(root).as_posix()
    return OrganizationOperationRecord(
        operation_id=operation_id or str(uuid.uuid4()),
        authorization_id=str(uuid.uuid4()),
        status=status,
        scan_generation="scan-1",
        decision_revision=1,
        approved_at=created_at,
        created_at=created_at,
        started_at=created_at,
        completed_at=created_at,
        configured_root_identity=repository.configured_root_identity,
        expected_moves=(
            OrganizationExpectedMove(
                change_id="O1",
                source="Inbox/a.txt",
                destination="Documents/a.txt",
                source_size_bytes=size,
                source_modified_time=modified,
                reason="test",
                confidence=100,
                operation="move",
                overwrite_risk=False,
                result_state=result_state,
            ),
        ),
        verification=OrganizationOperationVerification(
            status=verification_status,
            verified_count=verified_count,
        ),
        legacy_operation_log=legacy,
        failure_code=None,
        duplicate_actions_deferred=0,
    )


def _undo(
    repository: OrganizationUndoRepository,
    operation: OrganizationOperationRecord,
    *,
    status: str,
) -> OrganizationUndoRecord:
    item_state = UNDO_ITEM_RESTORED
    verification = UndoVerificationResult(
        status=UNDO_VERIFICATION_PASSED, verified_count=1
    )
    return OrganizationUndoRecord(
        undo_id=str(uuid.uuid4()),
        operation_id=operation.operation_id,
        authorization_id=str(uuid.uuid4()),
        configured_root_identity=repository.configured_root_identity,
        status=status,
        created_at="2026-07-15T09:00:00+00:00",
        started_at="2026-07-15T09:00:00+00:00",
        completed_at="2026-07-15T09:00:01+00:00",
        expected_restores=(
            OrganizationUndoItem(
                change_id="O1",
                restore_target="Inbox/a.txt",
                moved_from="Documents/a.txt",
                baseline_size_bytes=4,
                baseline_modified_time=100.0,
                result_state=item_state,
            ),
        ),
        verification=verification,
        legacy_undo_log=None,
        failure_code=None,
    )


if __name__ == "__main__":
    unittest.main()
