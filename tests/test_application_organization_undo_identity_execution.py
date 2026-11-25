from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.organization_undo_identity_eligibility import (
    OrganizationUndoIdentityEligibilityInspector,
)
from organizer.application.organization_undo_identity_execution import (
    IdentityUndoExecutionState,
    OrganizationIdentityUndoExecutionError,
    execute_v2_organization_undo_intent,
)
from organizer.application.organization_undo_identity_intent import (
    build_v2_organization_undo_intent,
)
from organizer.application.organization_undo_models import (
    UNDO_FAILED,
    UNDO_INTERRUPTED,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_FAILED,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_ITEM_UNATTEMPTED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_VERIFICATION_NOT_RUN,
)
from organizer.executor import apply_same_volume_identity_move_plan
from organizer.executor_identity import StrictExecutorInterrupted
from tests.test_application_undo_identity_eligibility import _stored_parent

_TIME = "2026-07-23T08:00:00+00:00"


class OrganizationUndoIdentityExecutionTests(unittest.TestCase):
    def test_prepared_record_precedes_exact_strict_reverse_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent, intent = _intent(root, (b"alpha", b"beta"))
            parent_before = operations.load(parent.operation_id)
            observed = []

            def execute(plan, selected_root, *, recorder):
                prepared = undos.load(intent.record.undo_id)
                self.assertEqual(prepared.status, UNDO_PREPARED)
                observed.extend(plan)
                return apply_same_volume_identity_move_plan(
                    plan, selected_root, recorder=recorder
                )

            result = execute_v2_organization_undo_intent(
                intent=intent,
                operation_repository=operations,
                undo_repository=undos,
                clock=lambda: _TIME,
                executor=execute,
            )

            self.assertEqual(
                result.state, IdentityUndoExecutionState.RESTORED_UNVERIFIED
            )
            self.assertEqual((result.files_expected, result.files_restored), (2, 2))
            self.assertEqual(operations.load(parent.operation_id), parent_before)
            self.assertEqual(
                tuple(
                    (
                        item.change_id,
                        item.plan_item.source.relative_to(root).as_posix(),
                        item.plan_item.destination.relative_to(root).as_posix(),
                        item.expected_digest,
                        item.captured_stat,
                    )
                    for item in observed
                ),
                tuple(
                    (
                        move.change_id,
                        move.destination,
                        move.source,
                        move.content_identity.digest,
                        evidence.stat_fingerprint,
                    )
                    for move, evidence in zip(
                        parent.expected_moves, intent.executor_evidence
                    )
                ),
            )
            for index, payload in enumerate((b"alpha", b"beta"), start=1):
                self.assertEqual(
                    (root / f"Inbox/item-{index}.txt").read_bytes(), payload
                )
                self.assertFalse((root / f"Documents/item-{index}.txt").exists())

            persisted = undos.load(intent.record.undo_id)
            self.assertEqual(persisted.status, UNDO_RESTORED_UNVERIFIED)
            self.assertEqual(
                tuple(item.result_state for item in persisted.expected_restores),
                (UNDO_ITEM_RESTORED, UNDO_ITEM_RESTORED),
            )
            self.assertEqual(persisted.started_at, _TIME)
            self.assertIsNone(persisted.completed_at)
            self.assertEqual(persisted.verification.status, UNDO_VERIFICATION_NOT_RUN)
            self.assertIsNotNone(persisted.legacy_undo_log)
            log_text = (root / persisted.legacy_undo_log).read_text(encoding="utf-8")
            self.assertNotIn("sha", log_text.casefold())
            self.assertNotIn(str(root), repr(result))
            self.assertNotIn(result.undo_id, repr(result))

    def test_creation_link_and_tampered_evidence_reject_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, parent, intent = _intent(root, (b"alpha",))
            executor = mock.Mock()
            parent_before = operations.load(parent.operation_id)
            with mock.patch.object(undos, "create", side_effect=OSError("private")):
                with self.assertRaises(
                    OrganizationIdentityUndoExecutionError
                ) as raised:
                    execute_v2_organization_undo_intent(
                        intent=intent,
                        operation_repository=operations,
                        undo_repository=undos,
                        executor=executor,
                    )
            self.assertEqual(raised.exception.code, "durable_intent_failed")
            executor.assert_not_called()
            self.assertTrue((root / "Documents/item-1.txt").exists())
            self.assertEqual(operations.load(parent.operation_id), parent_before)

            undos.create(intent.record)
            with self.assertRaises(OrganizationIdentityUndoExecutionError) as linked:
                execute_v2_organization_undo_intent(
                    intent=intent,
                    operation_repository=operations,
                    undo_repository=undos,
                    executor=executor,
                )
            self.assertEqual(linked.exception.code, "existing_undo")
            executor.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, intent = _intent(root, (b"alpha",))
            changed_identity = RegularFileContentIdentity(
                "regular_file", "sha256", "f" * 64
            )
            tampered = replace(
                intent,
                executor_evidence=(
                    replace(
                        intent.executor_evidence[0], content_identity=changed_identity
                    ),
                ),
            )
            executor = mock.Mock()
            with self.assertRaises(OrganizationIdentityUndoExecutionError) as raised:
                execute_v2_organization_undo_intent(
                    intent=tampered,
                    operation_repository=operations,
                    undo_repository=undos,
                    executor=executor,
                )
            self.assertEqual(raised.exception.code, "invalid_intent")
            executor.assert_not_called()
            self.assertEqual(undos.list_records(), ())

            invalid_fingerprint = replace(
                intent,
                executor_evidence=(
                    replace(intent.executor_evidence[0], stat_fingerprint=None),
                ),
            )
            with self.assertRaises(OrganizationIdentityUndoExecutionError) as raised:
                execute_v2_organization_undo_intent(
                    intent=invalid_fingerprint,
                    operation_repository=operations,
                    undo_repository=undos,
                    executor=executor,
                )
            self.assertEqual(raised.exception.code, "invalid_intent")
            executor.assert_not_called()
            self.assertEqual(undos.list_records(), ())

    def test_preflight_collision_rejects_before_durable_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, intent = _intent(root, (b"alpha", b"beta"))
            target = root / "Inbox/item-1.txt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"occupied")

            with self.assertRaises(OrganizationIdentityUndoExecutionError) as raised:
                execute_v2_organization_undo_intent(
                    intent=intent,
                    operation_repository=operations,
                    undo_repository=undos,
                    clock=lambda: _TIME,
                )

            self.assertEqual(raised.exception.code, "invalid_intent")
            self.assertEqual(undos.list_records(), ())
            self.assertEqual(target.read_bytes(), b"occupied")
            self.assertTrue((root / "Documents/item-1.txt").exists())

    def test_ordinary_failure_and_partial_batch_are_recorded_truthfully(self) -> None:
        for payloads, mutate_index, expected_status, expected_states in (
            (
                (b"alpha", b"beta"),
                0,
                UNDO_FAILED,
                (UNDO_ITEM_FAILED, UNDO_ITEM_UNATTEMPTED),
            ),
            (
                (b"alpha", b"beta"),
                1,
                UNDO_PARTIAL,
                (UNDO_ITEM_RESTORED, UNDO_ITEM_FAILED),
            ),
        ):
            with (
                self.subTest(status=expected_status),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                operations, undos, _parent, intent = _intent(root, payloads)
                real_save = undos.save
                mutated = False

                def save(record):
                    nonlocal mutated
                    real_save(record)
                    if (
                        not mutated
                        and record.expected_restores[mutate_index].result_state
                        == UNDO_ITEM_ATTEMPTING
                    ):
                        mutated = True
                        source = (
                            root / record.expected_restores[mutate_index].moved_from
                        )
                        source.write_bytes(b"wrong")

                with mock.patch.object(undos, "save", side_effect=save):
                    result = execute_v2_organization_undo_intent(
                        intent=intent,
                        operation_repository=operations,
                        undo_repository=undos,
                        clock=lambda: _TIME,
                    )

                self.assertEqual(result.state.value, expected_status)
                persisted = undos.load(intent.record.undo_id)
                self.assertEqual(persisted.status, expected_status)
                self.assertEqual(
                    tuple(item.result_state for item in persisted.expected_restores),
                    expected_states,
                )
                self.assertTrue(mutated)
                self.assertTrue(persisted.legacy_undo_log)
                log = json.loads((root / persisted.legacy_undo_log).read_text())
                self.assertFalse(log["operations"][-1]["success"])

    def test_post_publication_error_is_interrupted_with_attempting_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, intent = _intent(root, (b"alpha",))

            with mock.patch(
                "organizer.executor.os.unlink", side_effect=OSError("private")
            ):
                result = execute_v2_organization_undo_intent(
                    intent=intent,
                    operation_repository=operations,
                    undo_repository=undos,
                    clock=lambda: _TIME,
                )

            self.assertEqual(result.state, IdentityUndoExecutionState.INTERRUPTED)
            persisted = undos.load(intent.record.undo_id)
            self.assertEqual(persisted.status, UNDO_INTERRUPTED)
            self.assertEqual(
                persisted.expected_restores[0].result_state, UNDO_ITEM_ATTEMPTING
            )
            self.assertTrue((root / "Documents/item-1.txt").exists())
            self.assertTrue((root / "Inbox/item-1.txt").exists())
            self.assertIsNone(persisted.legacy_undo_log)

    def test_post_publication_interrupt_persists_then_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, intent = _intent(root, (b"alpha",))

            with (
                mock.patch(
                    "organizer.executor.os.unlink", side_effect=KeyboardInterrupt()
                ),
                self.assertRaises(StrictExecutorInterrupted),
            ):
                execute_v2_organization_undo_intent(
                    intent=intent,
                    operation_repository=operations,
                    undo_repository=undos,
                    clock=lambda: _TIME,
                )

            persisted = undos.load(intent.record.undo_id)
            self.assertEqual(persisted.status, UNDO_INTERRUPTED)
            self.assertEqual(
                persisted.expected_restores[0].result_state, UNDO_ITEM_ATTEMPTING
            )
            self.assertTrue((root / "Documents/item-1.txt").exists())
            self.assertTrue((root / "Inbox/item-1.txt").exists())

    def test_prepublication_process_interrupt_propagates_without_success_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, intent = _intent(root, (b"alpha",))

            with (
                mock.patch(
                    "organizer.executor.os.link", side_effect=KeyboardInterrupt()
                ),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                execute_v2_organization_undo_intent(
                    intent=intent,
                    operation_repository=operations,
                    undo_repository=undos,
                    clock=lambda: _TIME,
                )

            self.assertNotIsInstance(raised.exception, StrictExecutorInterrupted)
            persisted = undos.load(intent.record.undo_id)
            self.assertNotEqual(persisted.status, UNDO_RESTORED_UNVERIFIED)
            self.assertEqual(
                persisted.expected_restores[0].result_state, UNDO_ITEM_ATTEMPTING
            )
            self.assertTrue((root / "Documents/item-1.txt").exists())
            self.assertFalse((root / "Inbox/item-1.txt").exists())

    def test_recovery_publication_failure_reports_primary_condition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, intent = _intent(root, (b"alpha",))
            real_save = undos.save
            calls = 0

            def save(record):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("private")
                real_save(record)

            with (
                mock.patch.object(undos, "save", side_effect=save),
                mock.patch(
                    "organizer.executor.os.unlink", side_effect=OSError("private")
                ),
                self.assertRaises(OrganizationIdentityUndoExecutionError) as raised,
            ):
                execute_v2_organization_undo_intent(
                    intent=intent,
                    operation_repository=operations,
                    undo_repository=undos,
                    clock=lambda: _TIME,
                )

            self.assertEqual(raised.exception.code, "recovery_required")
            persisted = undos.load(intent.record.undo_id)
            self.assertNotEqual(persisted.status, UNDO_RESTORED_UNVERIFIED)
            self.assertEqual(
                persisted.expected_restores[0].result_state, UNDO_ITEM_ATTEMPTING
            )
            self.assertTrue((root / "Documents/item-1.txt").exists())
            self.assertTrue((root / "Inbox/item-1.txt").exists())

    def test_recorder_failures_never_publish_false_terminal_state(self) -> None:
        expected = {
            1: (UNDO_ITEM_PENDING, True, False),
            2: (UNDO_ITEM_PENDING, True, False),
            3: (UNDO_ITEM_ATTEMPTING, False, True),
            4: (UNDO_ITEM_RESTORED, False, True),
        }
        for fail_on, (item_state, moved_exists, restored_exists) in expected.items():
            with (
                self.subTest(save_call=fail_on),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                operations, undos, _parent, intent = _intent(root, (b"alpha",))
                real_save = undos.save
                calls = 0

                def save(record):
                    nonlocal calls
                    calls += 1
                    if calls == fail_on:
                        raise OSError("private")
                    real_save(record)

                with mock.patch.object(undos, "save", side_effect=save):
                    result = execute_v2_organization_undo_intent(
                        intent=intent,
                        operation_repository=operations,
                        undo_repository=undos,
                        clock=lambda: _TIME,
                    )

                self.assertEqual(result.state, IdentityUndoExecutionState.INTERRUPTED)
                persisted = undos.load(intent.record.undo_id)
                self.assertEqual(persisted.status, UNDO_INTERRUPTED)
                self.assertEqual(
                    persisted.expected_restores[0].result_state, item_state
                )
                self.assertEqual((root / "Documents/item-1.txt").exists(), moved_exists)
                self.assertEqual((root / "Inbox/item-1.txt").exists(), restored_exists)
                self.assertEqual(
                    persisted.verification.status, UNDO_VERIFICATION_NOT_RUN
                )

    def test_after_failure_recorder_failure_remains_interrupted_and_attempting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            operations, undos, _parent, intent = _intent(root, (b"alpha",))
            real_save = undos.save
            calls = 0

            def save(record):
                nonlocal calls
                calls += 1
                if calls == 2:
                    real_save(record)
                    (root / "Documents/item-1.txt").write_bytes(b"wrong")
                    return
                if calls == 3:
                    raise OSError("private")
                real_save(record)

            with mock.patch.object(undos, "save", side_effect=save):
                result = execute_v2_organization_undo_intent(
                    intent=intent,
                    operation_repository=operations,
                    undo_repository=undos,
                    clock=lambda: _TIME,
                )

            self.assertEqual(result.state, IdentityUndoExecutionState.INTERRUPTED)
            persisted = undos.load(intent.record.undo_id)
            self.assertEqual(persisted.status, UNDO_INTERRUPTED)
            self.assertEqual(
                persisted.expected_restores[0].result_state, UNDO_ITEM_ATTEMPTING
            )
            self.assertTrue((root / "Documents/item-1.txt").exists())
            self.assertFalse((root / "Inbox/item-1.txt").exists())

    def test_production_and_terminal_verification_owners_remain_disconnected(
        self,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        production = (
            repository_root / "src/organizer/application/undo_service.py",
            repository_root / "src/organizer/application/activity_service.py",
            repository_root / "src/organizer/web/routes/activity_api.py",
        )
        for path in production:
            self.assertNotIn(
                "organization_undo_identity_execution",
                path.read_text(encoding="utf-8"),
            )
        execution_source = (
            repository_root
            / "src/organizer/application/organization_undo_identity_execution.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "undo_operation_log(",
            "verify_organization_undo",
            "activity_service",
            "shutil.move",
        ):
            self.assertNotIn(forbidden, execution_source)
        self.assertIn("apply_same_volume_identity_move_plan", execution_source)


def _intent(root: Path, payloads: tuple[bytes, ...]):
    operations, undos, parent = _stored_parent(root, payloads)
    eligibility = OrganizationUndoIdentityEligibilityInspector(
        operations, undos
    ).inspect(parent.operation_id)
    intent = build_v2_organization_undo_intent(
        parent=parent,
        undo_id="11111111-1111-4111-8111-111111111111",
        authorization_id="22222222-2222-4222-8222-222222222222",
        configured_root_identity=operations.configured_root_identity,
        created_at="2026-07-23T07:00:00+00:00",
        captures=eligibility.captures,
    )
    return operations, undos, parent, intent


if __name__ == "__main__":
    unittest.main()
