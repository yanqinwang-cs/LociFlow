from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.file_identity_capture import FileIdentityCaptureError
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationBlockedError,
    RecoveryAdmissionState,
)
from organizer.application.organization_operation_models import (
    MOVE_MOVED,
    ORGANIZATION_OPERATION_SCHEMA_VERSION,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_VERIFIED,
    VERIFICATION_PASSED,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_identity_eligibility import (
    IdentityUndoEligibilityReason,
    IdentityUndoEligibilityState,
    OrganizationUndoIdentityEligibilityInspector,
)
from organizer.application.organization_undo_identity_intent import (
    build_v2_organization_undo_intent,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.volume_admission import OrganizationVolumeAdmission


class OrganizationUndoIdentityEligibilityTests(unittest.TestCase):
    def test_verified_v2_parent_is_eligible_without_legacy_log_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha", b"beta"))
            restore_parent = root / "Inbox"
            self.assertFalse(restore_parent.exists())

            with (
                mock.patch.object(undos, "create") as create,
                mock.patch("organizer.executor.apply_move_plan") as legacy_executor,
                mock.patch(
                    "organizer.executor.apply_same_volume_identity_move_plan"
                ) as strict_executor,
            ):
                result = OrganizationUndoIdentityEligibilityInspector(
                    operations, undos
                ).inspect(parent.operation_id)

            self.assertTrue(result.eligible)
            self.assertEqual(result.state, IdentityUndoEligibilityState.ELIGIBLE)
            self.assertEqual(result.parent, parent)
            self.assertEqual(
                tuple(item.change_id for item in result.captures), ("O1", "O2")
            )
            self.assertEqual(
                tuple(
                    (item.source_relative_path, item.destination_relative_path)
                    for item in result.reverse_path_pairs
                ),
                (
                    ("Documents/item-1.txt", "Inbox/item-1.txt"),
                    ("Documents/item-2.txt", "Inbox/item-2.txt"),
                ),
            )
            self.assertFalse(restore_parent.exists())
            create.assert_not_called()
            legacy_executor.assert_not_called()
            strict_executor.assert_not_called()

    def test_missing_or_malformed_legacy_log_does_not_supply_identity(self) -> None:
        for log_contents in (None, b"not-json"):
            with self.subTest(log_contents=log_contents), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operations, undos, parent = _stored_parent(root, (b"content",))
                log_relative = "AI_Review/operation_logs/legacy.json"
                if log_contents is not None:
                    log_path = root / log_relative
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_bytes(log_contents)
                parent = replace(parent, legacy_operation_log=log_relative)
                operations.save(parent)

                result = OrganizationUndoIdentityEligibilityInspector(
                    operations, undos
                ).inspect(parent.operation_id)

                self.assertTrue(result.eligible)

    def test_v1_nonterminal_invalid_wrong_root_and_superseded_parents_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"content",))
            inspector = OrganizationUndoIdentityEligibilityInspector(operations, undos)

            v1 = replace(parent, schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION)
            with mock.patch.object(operations, "load", return_value=v1):
                result = inspector.inspect(parent.operation_id)
            self.assertEqual(result.reason, IdentityUndoEligibilityReason.PARENT_NOT_V2)

            for status in (
                STATUS_MOVED_UNVERIFIED,
                STATUS_PARTIAL_MOVEMENT,
                STATUS_INTERRUPTED,
            ):
                with self.subTest(status=status), mock.patch.object(
                    operations, "load", return_value=replace(parent, status=status)
                ):
                    result = inspector.inspect(parent.operation_id)
                self.assertEqual(
                    result.reason, IdentityUndoEligibilityReason.PARENT_NOT_VERIFIED
                )

            wrong_root = replace(parent, configured_root_identity="f" * 64)
            with mock.patch.object(operations, "load", return_value=wrong_root):
                result = inspector.inspect(parent.operation_id)
            self.assertEqual(result.reason, IdentityUndoEligibilityReason.WRONG_ROOT)

            invalid = replace(
                parent,
                expected_moves=(
                    replace(parent.expected_moves[0], content_identity=None),
                ),
            )
            with (
                mock.patch.object(operations, "load", return_value=invalid),
                mock.patch.object(operations, "list_records", return_value=(invalid,)),
            ):
                result = inspector.inspect(parent.operation_id)
            self.assertEqual(result.reason, IdentityUndoEligibilityReason.PARENT_INVALID)

            newer = replace(
                parent,
                operation_id=str(uuid.uuid4()),
                authorization_id=str(uuid.uuid4()),
                created_at="2026-07-22T10:00:00+00:00",
            )
            operations.create(newer)
            result = inspector.inspect(parent.operation_id)
            self.assertEqual(result.reason, IdentityUndoEligibilityReason.PARENT_SUPERSEDED)

    def test_existing_linked_undo_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"content",))
            inspector = OrganizationUndoIdentityEligibilityInspector(operations, undos)
            eligible = inspector.inspect(parent.operation_id)
            intent = build_v2_organization_undo_intent(
                parent=parent,
                undo_id=str(uuid.uuid4()),
                authorization_id=str(uuid.uuid4()),
                configured_root_identity=operations.configured_root_identity,
                created_at="2026-07-22T09:00:00+00:00",
                captures=eligible.captures,
            )
            undos.create(intent.record)

            result = inspector.inspect(parent.operation_id)

            self.assertEqual(result.reason, IdentityUndoEligibilityReason.EXISTING_UNDO)

    def test_cross_family_block_rejects_before_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"content",))
            admission = mock.Mock()
            admission.root = root
            admission.inspect_and_require_clear.side_effect = (
                FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                )
            )
            capture = mock.Mock()

            result = OrganizationUndoIdentityEligibilityInspector(
                operations,
                undos,
                filesystem_operation_admission=admission,
                capture=capture,
            ).inspect(parent.operation_id)

            self.assertEqual(
                result.reason, IdentityUndoEligibilityReason.CROSS_FAMILY_BLOCKED
            )
            self.assertEqual(result.captures, ())
            capture.assert_not_called()

    def test_changed_content_including_same_metadata_replacement_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"recorded",))
            moved = root / parent.expected_moves[0].destination
            before = moved.stat()
            moved.write_bytes(b"replaced")
            os.utime(moved, ns=(before.st_atime_ns, before.st_mtime_ns))

            result = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id)

            self.assertEqual(result.state, IdentityUndoEligibilityState.INELIGIBLE)
            self.assertEqual(result.reason, IdentityUndoEligibilityReason.CONTENT_CHANGED)
            self.assertEqual(result.captures, ())

    def test_missing_unsafe_and_unavailable_sources_fail_closed(self) -> None:
        cases = ("missing", "symlink", "directory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operations, undos, parent = _stored_parent(root, (b"content",))
                moved = root / parent.expected_moves[0].destination
                moved.unlink()
                if case == "symlink":
                    moved.symlink_to(root / "outside")
                elif case == "directory":
                    moved.mkdir()
                result = OrganizationUndoIdentityEligibilityInspector(
                    operations, undos
                ).inspect(parent.operation_id)
                expected = (
                    IdentityUndoEligibilityReason.SOURCE_MISSING
                    if case == "missing"
                    else IdentityUndoEligibilityReason.SOURCE_UNSAFE
                )
                self.assertEqual(result.reason, expected)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"content",))

            def unavailable(path: Path, selected_root: Path):
                del path, selected_root
                raise FileIdentityCaptureError("source_unreadable")

            result = OrganizationUndoIdentityEligibilityInspector(
                operations, undos, capture=unavailable
            ).inspect(parent.operation_id)
            self.assertEqual(result.state, IdentityUndoEligibilityState.UNAVAILABLE)
            self.assertEqual(
                result.reason, IdentityUndoEligibilityReason.INSPECTION_UNAVAILABLE
            )

    def test_occupied_and_unsafe_restore_targets_reject_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"content",))
            target = root / parent.expected_moves[0].source
            target.parent.mkdir(parents=True)
            target.write_bytes(b"occupant")
            before = target.read_bytes()

            result = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id)

            self.assertEqual(
                result.reason, IdentityUndoEligibilityReason.RESTORE_TARGET_OCCUPIED
            )
            self.assertEqual(target.read_bytes(), before)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "root"
            root.mkdir()
            operations, undos, parent = _stored_parent(root, (b"content",))
            outside = base / "outside"
            outside.mkdir()
            (root / "Inbox").symlink_to(outside, target_is_directory=True)

            result = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id)

            self.assertEqual(
                result.reason, IdentityUndoEligibilityReason.UNSAFE_RESTORE_PATH
            )
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_cross_volume_batch_rejects_completely_with_redacted_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha", b"beta"))
            changed = root / parent.expected_moves[1].destination
            root_device = os.lstat(root).st_dev

            def cross_lstat(path: Path):
                current = os.lstat(path)
                if Path(path) != changed:
                    return current
                return SimpleNamespace(st_mode=current.st_mode, st_dev=root_device + 1)

            result = OrganizationUndoIdentityEligibilityInspector(
                operations,
                undos,
                volume_admission=OrganizationVolumeAdmission(
                    root, lstat_reader=cross_lstat
                ),
            ).inspect(parent.operation_id)

            self.assertEqual(result.reason, IdentityUndoEligibilityReason.CROSS_VOLUME)
            self.assertEqual(result.captures, ())
            rendered = repr(result)
            for secret in (str(root), "item-1", parent.operation_id, "sha256"):
                self.assertNotIn(secret, rendered)


def _stored_parent(root: Path, payloads: tuple[bytes, ...]):
    operations = OrganizationOperationRepository(root)
    undos = OrganizationUndoRepository(root)
    moves = []
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
                source_modified_time=100.0 + index,
                reason="test",
                confidence=100,
                operation="move",
                overwrite_risk=False,
                result_state=MOVE_MOVED,
                content_identity=RegularFileContentIdentity(
                    "regular_file", "sha256", hashlib.sha256(payload).hexdigest()
                ),
            )
        )
    parent = OrganizationOperationRecord(
        operation_id=str(uuid.uuid4()),
        authorization_id=str(uuid.uuid4()),
        status=STATUS_VERIFIED,
        scan_generation="scan-one",
        decision_revision=1,
        approved_at="2026-07-22T08:00:00+00:00",
        created_at="2026-07-22T08:00:00+00:00",
        started_at="2026-07-22T08:01:00+00:00",
        completed_at="2026-07-22T08:02:00+00:00",
        configured_root_identity=operations.configured_root_identity,
        expected_moves=tuple(moves),
        verification=OrganizationOperationVerification(
            status=VERIFICATION_PASSED,
            verified_count=len(moves),
        ),
        legacy_operation_log=None,
        failure_code=None,
        duplicate_actions_deferred=0,
        schema_version=2,
    )
    operations.create(parent)
    return operations, undos, parent


if __name__ == "__main__":
    unittest.main()
