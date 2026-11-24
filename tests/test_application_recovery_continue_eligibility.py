from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from organizer.application.file_identity_capture import FileIdentityCaptureError
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
)
from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    ORGANIZATION_OPERATION_SCHEMA_VERSION,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVING,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_PREPARED,
)
from organizer.application.organization_undo_models import (
    UNDO_INTERRUPTED,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_FAILED,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_ITEM_UNATTEMPTED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
)
from organizer.application.recovery_continue_eligibility import (
    RecoveryContinueEligibilityInspector,
    RecoveryContinueEligibilityState,
    RecoveryContinueFamily,
)
from organizer.application.volume_admission import (
    VolumeAdmissionResult,
    VolumeAdmissionState,
)
from organizer.models import MoveResult
from tests.test_application_organization_identity_execution import (
    _inputs,
    _service,
)
from tests.test_application_organization_undo_identity_execution import _intent


class RecoveryContinueEligibilityTests(unittest.TestCase):
    def test_movement_moved_plus_pending_is_exactly_bound(self) -> None:
        for status in (STATUS_MOVING, STATUS_INTERRUPTED):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository, record = _movement_candidate(root, status)

                first = _inspect(root)
                second = _inspect(root)

                self.assertEqual(first, second)
                self.assertEqual(
                    first.state,
                    RecoveryContinueEligibilityState.ELIGIBLE,
                )
                self.assertEqual(
                    first.family,
                    RecoveryContinueFamily.ORGANIZATION_MOVEMENT,
                )
                self.assertEqual(first.operation_id, record.operation_id)
                self.assertEqual(first.completed_change_ids, ("O1",))
                self.assertEqual(first.pending_change_ids, ("O2",))
                self.assertEqual(
                    tuple(item.change_id for item in first.pending_capture_evidence),
                    ("O2",),
                )
                self.assertEqual(repository.load(record.operation_id), record)
                self._assert_redacted(first, root, record.operation_id)

    def test_undo_restored_plus_pending_is_exactly_bound(self) -> None:
        for status in (UNDO_UNDOING, UNDO_INTERRUPTED):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                operations, undos, parent, undo = _undo_candidate(root, status)

                result = _inspect(root)

                self.assertEqual(
                    result.state,
                    RecoveryContinueEligibilityState.ELIGIBLE,
                )
                self.assertEqual(
                    result.family,
                    RecoveryContinueFamily.ORGANIZATION_UNDO,
                )
                self.assertEqual(result.operation_id, undo.undo_id)
                self.assertEqual(result.parent_operation_id, parent.operation_id)
                self.assertEqual(result.completed_change_ids, ("O1",))
                self.assertEqual(result.pending_change_ids, ("O2",))
                self.assertEqual(undos.load(undo.undo_id), undo)
                self.assertEqual(operations.load(parent.operation_id), parent)
                self._assert_redacted(result, root, undo.undo_id)

    def test_disallowed_states_never_offer_a_reduced_subset(self) -> None:
        movement_states = (
            MOVE_ATTEMPTING,
            MOVE_FAILED,
            MOVE_UNATTEMPTED,
        )
        for state in movement_states:
            with self.subTest(family="movement", state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository, record = _movement_candidate(root, STATUS_INTERRUPTED)
                items = list(record.expected_moves)
                items[1] = replace(
                    items[1],
                    result_state=state,
                    failure_code="failed" if state == MOVE_FAILED else None,
                )
                repository.save(replace(record, expected_moves=tuple(items)))
                self.assertEqual(
                    _inspect(root).state,
                    RecoveryContinueEligibilityState.INELIGIBLE,
                )

        undo_states = (
            UNDO_ITEM_ATTEMPTING,
            UNDO_ITEM_FAILED,
            UNDO_ITEM_UNATTEMPTED,
        )
        for state in undo_states:
            with self.subTest(family="undo", state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _operations, undos, _parent, undo = _undo_candidate(
                    root,
                    UNDO_INTERRUPTED,
                )
                items = list(undo.expected_restores)
                items[1] = replace(
                    items[1],
                    result_state=state,
                    failure_code="failed" if state == UNDO_ITEM_FAILED else None,
                )
                undos.save(replace(undo, expected_restores=tuple(items)))
                self.assertEqual(
                    _inspect(root).state,
                    RecoveryContinueEligibilityState.INELIGIBLE,
                )

    def test_prepared_check_partial_and_v1_remain_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, record = _movement_candidate(root, STATUS_INTERRUPTED)
            first = record.expected_moves[0]
            os.replace(root / first.destination, root / first.source)
            repository.save(
                replace(
                    record,
                    status=STATUS_PREPARED,
                    started_at=None,
                    completed_at=None,
                    failure_code=None,
                    expected_moves=tuple(
                        replace(item, result_state=MOVE_PENDING)
                        for item in record.expected_moves
                    ),
                )
            )
            self.assertEqual(
                _inspect(root).state,
                RecoveryContinueEligibilityState.INELIGIBLE,
            )

        movement_statuses = (STATUS_MOVED_UNVERIFIED, STATUS_PARTIAL_MOVEMENT)
        for status in movement_statuses:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository, record = _movement_candidate(root, STATUS_INTERRUPTED)
                repository.save(replace(record, status=status))
                self.assertEqual(
                    _inspect(root).state,
                    RecoveryContinueEligibilityState.INELIGIBLE,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, record = _movement_candidate(root, STATUS_INTERRUPTED)
            repository.save(
                replace(
                    record,
                    schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION,
                    expected_moves=tuple(
                        replace(item, content_identity=None)
                        for item in record.expected_moves
                    ),
                )
            )
            self.assertEqual(
                _inspect(root).state,
                RecoveryContinueEligibilityState.INELIGIBLE,
            )

        for status in (UNDO_RESTORED_UNVERIFIED, UNDO_PARTIAL):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _operations, undos, _parent, undo = _undo_candidate(
                    root,
                    UNDO_INTERRUPTED,
                )
                undos.save(replace(undo, status=status))
                self.assertEqual(
                    _inspect(root).state,
                    RecoveryContinueEligibilityState.INELIGIBLE,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _operations, undos, _parent, undo = _undo_candidate(
                root,
                UNDO_INTERRUPTED,
            )
            first = undo.expected_restores[0]
            os.replace(root / first.restore_target, root / first.moved_from)
            undos.save(
                replace(
                    undo,
                    status=UNDO_PREPARED,
                    started_at=None,
                    completed_at=None,
                    failure_code=None,
                    expected_restores=tuple(
                        replace(item, result_state=UNDO_ITEM_PENDING)
                        for item in undo.expected_restores
                    ),
                )
            )
            self.assertEqual(
                _inspect(root).state,
                RecoveryContinueEligibilityState.INELIGIBLE,
            )

    def test_every_completed_and_pending_filesystem_fact_is_reproved(self) -> None:
        mutations = (
            ("completed_source_reappears", lambda root, record: _write(root / record.expected_moves[0].source, b"x")),
            ("completed_destination_disappears", lambda root, record: (root / record.expected_moves[0].destination).unlink()),
            ("completed_destination_changes", lambda root, record: (root / record.expected_moves[0].destination).write_bytes(b"changed")),
            ("pending_source_disappears", lambda root, record: (root / record.expected_moves[1].source).unlink()),
            ("pending_source_changes", lambda root, record: (root / record.expected_moves[1].source).write_bytes(b"changed")),
            ("pending_destination_appears", lambda root, record: _write(root / record.expected_moves[1].destination, b"x")),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _repository, record = _movement_candidate(root, STATUS_INTERRUPTED)
                mutate(root, record)
                self.assertEqual(
                    _inspect(root).state,
                    RecoveryContinueEligibilityState.INELIGIBLE,
                )

    def test_cross_volume_is_ineligible_and_read_failure_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _movement_candidate(root, STATUS_INTERRUPTED)
            volume = mock.Mock()
            volume.root = root
            volume.inspect_path_pairs.return_value = VolumeAdmissionResult(
                VolumeAdmissionState.CROSS_VOLUME
            )
            result = RecoveryContinueEligibilityInspector(
                FilesystemOperationInspector(root),
                volume_admission=volume,
            ).inspect()
            self.assertEqual(
                result.state,
                RecoveryContinueEligibilityState.INELIGIBLE,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _movement_candidate(root, STATUS_INTERRUPTED)
            result = RecoveryContinueEligibilityInspector(
                FilesystemOperationInspector(root),
                capture=mock.Mock(
                    side_effect=FileIdentityCaptureError("source_unreadable")
                ),
            ).inspect()
            self.assertEqual(
                result.state,
                RecoveryContinueEligibilityState.UNAVAILABLE,
            )

    def test_multiple_or_malformed_state_never_selects_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, record = _movement_candidate(root, STATUS_INTERRUPTED)
            duplicate = replace(
                record,
                operation_id=str(uuid.uuid4()),
                authorization_id=str(uuid.uuid4()),
            )
            repository.create(duplicate)
            self.assertEqual(
                _inspect(root).state,
                RecoveryContinueEligibilityState.INELIGIBLE,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository, record = _movement_candidate(root, STATUS_INTERRUPTED)
            path = repository.directory / f"{record.operation_id}.json"
            path.write_text("{bad", encoding="utf-8")
            self.assertEqual(
                _inspect(root).state,
                RecoveryContinueEligibilityState.UNAVAILABLE,
            )

    def _assert_redacted(self, result, root: Path, operation_id: str) -> None:
        rendered = repr(result)
        for forbidden in (
            str(root),
            operation_id,
            result.durable_record_digest,
            result.completed_subset_digest,
            result.pending_subset_digest,
            result.filesystem_observation_digest,
        ):
            self.assertNotIn(forbidden, rendered)


def _inspect(root: Path):
    return RecoveryContinueEligibilityInspector(
        FilesystemOperationInspector(root)
    ).inspect()


def _movement_candidate(root: Path, status: str):
    plan, authorization, repository = _inputs(
        root,
        ("alpha.txt", "beta.txt"),
    )

    def interrupted(plan_items, execute_root, *, recorder):
        ordinary = tuple(item.plan_item for item in plan_items)
        recorder.before_batch(ordinary)
        first = ordinary[0]
        recorder.before_move(0, first)
        first.destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(first.source, first.destination)
        recorder.after_success(
            0,
            first,
            MoveResult(first.source, first.destination, True, "moved"),
        )
        raise RuntimeError("injected interruption")

    _service(repository, executor=interrupted).execute(
        approved_plan=plan,
        authorization=authorization,
    )
    record = repository.list_records()[0]
    if status == STATUS_MOVING:
        record = replace(
            record,
            status=STATUS_MOVING,
            completed_at=None,
            failure_code=None,
        )
        repository.save(record)
    return repository, record


def _undo_candidate(root: Path, status: str):
    operations, undos, parent, intent = _intent(
        root,
        (b"first", b"second"),
    )
    items = list(intent.record.expected_restores)
    first = items[0]
    target = root / first.restore_target
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(root / first.moved_from, target)
    items[0] = replace(first, result_state=UNDO_ITEM_RESTORED)
    undo = replace(
        intent.record,
        status=status,
        started_at="2026-07-27T03:00:00+00:00",
        completed_at=(
            "2026-07-27T03:01:00+00:00"
            if status == UNDO_INTERRUPTED
            else None
        ),
        failure_code=(
            "undo_recovery_required"
            if status == UNDO_INTERRUPTED
            else None
        ),
        expected_restores=tuple(items),
    )
    undos.create(undo)
    return operations, undos, parent, undo


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


if __name__ == "__main__":
    unittest.main()
