from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.organization_undo_identity_eligibility import (
    OrganizationUndoIdentityEligibilityInspector,
)
from organizer.application.organization_undo_identity_intent import (
    OrganizationUndoIdentityCapture,
    build_v2_organization_undo_intent,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from tests.test_application_undo_identity_eligibility import _stored_parent


class OrganizationUndoIdentityIntentTests(unittest.TestCase):
    def test_builder_reverses_parent_and_inherits_identity_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha", b"beta"))
            eligible = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id)
            parent_before = parent
            captures_before = eligible.captures

            intent = _build(parent, operations.configured_root_identity, eligible.captures)

            self.assertEqual(intent.record.schema_version, 2)
            self.assertEqual(intent.record.status, "prepared")
            self.assertEqual(intent.record.operation_id, parent.operation_id)
            self.assertIsNone(intent.record.legacy_undo_log)
            self.assertEqual(
                tuple(
                    (
                        item.change_id,
                        item.moved_from,
                        item.restore_target,
                        item.result_state,
                    )
                    for item in intent.record.expected_restores
                ),
                (
                    ("O1", "Documents/item-1.txt", "Inbox/item-1.txt", "pending"),
                    ("O2", "Documents/item-2.txt", "Inbox/item-2.txt", "pending"),
                ),
            )
            for move, restore in zip(
                parent.expected_moves, intent.record.expected_restores
            ):
                self.assertIs(restore.content_identity, move.content_identity)
            self.assertEqual(
                tuple(item.change_id for item in intent.executor_evidence),
                ("O1", "O2"),
            )
            self.assertEqual(parent, parent_before)
            self.assertEqual(eligible.captures, captures_before)
            with self.assertRaises(FrozenInstanceError):
                intent.executor_evidence[0].change_id = "changed"

    def test_builder_is_deterministic_and_round_trips_existing_v2_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha", b"beta"))
            captures = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id).captures

            first = _build(parent, operations.configured_root_identity, captures)
            second = _build(parent, operations.configured_root_identity, captures)
            self.assertEqual(first, second)

            undos.create(first.record)
            self.assertEqual(undos.load(first.record.undo_id), first.record)
            self.assertEqual(undos.linked_records(parent.operation_id), (first.record,))
            self.assertTrue(
                all(item.content_identity is not None for item in first.record.expected_restores)
            )

    def test_capture_identity_may_not_rebase_authoritative_parent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha",))
            captures = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id).captures
            changed = replace(
                captures[0],
                observed_content_identity=RegularFileContentIdentity(
                    "regular_file", "sha256", "f" * 64
                ),
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                _build(parent, operations.configured_root_identity, (changed,))

            self.assertEqual(
                parent.expected_moves[0].content_identity.digest,
                captures[0].observed_content_identity.digest,
            )

    def test_exact_capture_coverage_and_unique_evidence_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha", b"beta"))
            captures = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id).captures
            extra = replace(captures[0], change_id="O3")
            cases = (
                captures[:1],
                captures + (extra,),
                captures + (captures[0],),
            )
            for candidate in cases:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    _build(parent, operations.configured_root_identity, candidate)

    def test_builder_rejects_noncanonical_parent_root_ids_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha",))
            captures = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id).captures
            invalid_parent = replace(
                parent,
                expected_moves=(
                    replace(parent.expected_moves[0], source="Inbox/../item.txt"),
                ),
            )
            with self.assertRaises(ValueError):
                _build(invalid_parent, operations.configured_root_identity, captures)
            with self.assertRaises(ValueError):
                _build(parent, "not-a-root", captures)
            with self.assertRaises(ValueError):
                _build(parent, operations.configured_root_identity, captures, undo_id="bad")
            with self.assertRaises(ValueError):
                _build(parent, operations.configured_root_identity, captures, created_at="")

    def test_builder_performs_no_io_hashing_repository_write_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha",))
            captures = OrganizationUndoIdentityEligibilityInspector(
                operations, undos
            ).inspect(parent.operation_id).captures
            original_records = undos.list_records()

            with (
                mock.patch.object(undos, "create") as create,
                mock.patch("organizer.executor.apply_move_plan") as legacy_executor,
                mock.patch(
                    "organizer.executor.apply_same_volume_identity_move_plan"
                ) as strict_executor,
                mock.patch(
                    "organizer.application.file_identity_capture.capture_regular_file_identity"
                ) as capture,
            ):
                _build(parent, operations.configured_root_identity, captures)

            create.assert_not_called()
            legacy_executor.assert_not_called()
            strict_executor.assert_not_called()
            capture.assert_not_called()
            self.assertEqual(undos.list_records(), original_records)

    def test_application_service_owns_v2_undo_dispatch_not_routes(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        protected = (
            repository_root / "src/organizer/web/routes/activity_api.py",
            repository_root / "src/organizer/web/routes/organization_api.py",
        )
        forbidden = (
            "organization_undo_identity_eligibility",
            "organization_undo_identity_intent",
        )
        for path in protected:
            source = path.read_text(encoding="utf-8")
            for name in forbidden:
                self.assertNotIn(name, source)
        undo_service = (
            repository_root / "src/organizer/application/undo_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("OrganizationIdentityUndoOrchestrationService", undo_service)


def _build(
    parent,
    root_identity,
    captures: tuple[OrganizationUndoIdentityCapture, ...],
    *,
    undo_id: str = "11111111-1111-4111-8111-111111111111",
    created_at: str = "2026-07-22T09:00:00+00:00",
):
    return build_v2_organization_undo_intent(
        parent=parent,
        undo_id=undo_id,
        authorization_id="22222222-2222-4222-8222-222222222222",
        configured_root_identity=root_identity,
        created_at=created_at,
        captures=captures,
    )


if __name__ == "__main__":
    unittest.main()
