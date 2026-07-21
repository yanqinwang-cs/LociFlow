from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

import organizer.executor as executor
from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.execution_service import _DurableMovementRecorder
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
)
from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_PENDING,
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_MOVING,
    STATUS_PREPARED,
    VERIFICATION_NOT_RUN,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.recovery_evidence import (
    RecoveryActionability,
    RecoveryEvidenceInspector,
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
from organizer.file_identity import capture_open_regular_file
from organizer.models import MovePlanItem


class _Recorder:
    def __init__(self, *, fail: str | None = None) -> None:
        self.events: list[str] = []
        self.fail = fail

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail == name:
            raise OSError("record unavailable")

    def before_batch(self, plan_items) -> None:
        self._event("before_batch")

    def before_move(self, index, item) -> None:
        self._event(f"before_move:{index}")

    def after_success(self, index, item, result) -> None:
        self._event(f"after_success:{index}")

    def after_failure(self, index, item, result) -> None:
        self._event(f"after_failure:{index}")

    def after_batch(self, operation_log) -> None:
        self._event("after_batch")


class SameVolumeIdentityRecordingTests(unittest.TestCase):
    def test_success_uses_legacy_log_and_exact_callback_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _item(root, "O1", "Inbox/one", "Documents/one")
            second = _item(root, "O2", "Inbox/two", "Documents/two")
            recorder = _Recorder()

            operation_log = apply_same_volume_identity_move_plan(
                (first, second), root, recorder=recorder
            )

            self.assertEqual(
                recorder.events,
                [
                    "before_batch",
                    "before_move:0",
                    "after_success:0",
                    "before_move:1",
                    "after_success:1",
                    "after_batch",
                ],
            )
            self.assertEqual([result.success for result in operation_log.operations], [True, True])
            payload = json.loads(operation_log.log_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload["operations"][0]),
                {"source", "destination", "success", "message"},
            )
            self.assertNotIn("sha", operation_log.log_path.read_text(encoding="utf-8"))
            self.assertFalse(first.plan_item.source.exists())
            self.assertEqual(first.plan_item.destination.read_bytes(), b"Inbox/one")

    def test_complete_batch_rejection_precedes_callbacks_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strict = _item(root, "O1", "one", "New/one")
            ordinary = strict.plan_item
            recorder = _Recorder()

            for plan in ((ordinary,), (strict, ordinary)):
                with self.subTest(plan=plan), self.assertRaises(StrictExecutorError):
                    apply_same_volume_identity_move_plan(
                        plan, root, recorder=recorder  # type: ignore[arg-type]
                    )

            self.assertEqual(recorder.events, [])
            self.assertFalse(root.joinpath("New").exists())
            self.assertFalse(root.joinpath("AI_Review").exists())

    def test_later_cross_volume_rejects_before_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _item(root, "O1", "one", "New/one")
            second = _item(root, "O2", "two", "New/two")
            recorder = _Recorder()
            device = first.captured_stat.device

            with (
                mock.patch(
                    "organizer.executor_identity._volume_device",
                    side_effect=[device, device + 1],
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                apply_same_volume_identity_move_plan(
                    (first, second), root, recorder=recorder
                )

            self.assertEqual(raised.exception.code, "cross_volume_unsupported")
            self.assertEqual(recorder.events, [])
            self.assertFalse(root.joinpath("New").exists())

    def test_before_move_precedes_parent_creation_and_source_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Deep/Folder/source")
            recorder = _Recorder(fail="before_move:0")

            with (
                mock.patch.object(
                    executor,
                    "open_verified_regular_file",
                    wraps=executor.open_verified_regular_file,
                ) as opened,
                self.assertRaises(MovementRecordingError) as raised,
            ):
                apply_same_volume_identity_move_plan(
                    (item,), root, recorder=recorder
                )

            self.assertEqual(raised.exception.phase, "before_move")
            opened.assert_not_called()
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse(root.joinpath("Deep").exists())

    def test_before_batch_failure_prevents_all_filesystem_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            recorder = _Recorder(fail="before_batch")

            with self.assertRaises(MovementRecordingError) as raised:
                apply_same_volume_identity_move_plan(
                    (item,), root, recorder=recorder
                )

            self.assertEqual(raised.exception.phase, "before_batch")
            self.assertFalse(raised.exception.filesystem_changed)
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse(item.plan_item.destination.parent.exists())
            self.assertFalse(root.joinpath("AI_Review").exists())

    def test_safe_identity_failure_is_recorded_and_stops_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _item(root, "O1", "one", "Dest/one")
            second = _item(root, "O2", "two", "Dest/two")

            class MutatingRecorder(_Recorder):
                def before_move(self, index, item) -> None:
                    super().before_move(index, item)
                    if index == 0:
                        item.source.write_bytes(b"changed")

            recorder = MutatingRecorder()
            operation_log = apply_same_volume_identity_move_plan(
                (first, second), root, recorder=recorder
            )

            self.assertEqual(
                recorder.events,
                ["before_batch", "before_move:0", "after_failure:0", "after_batch"],
            )
            self.assertEqual(len(operation_log.operations), 1)
            self.assertFalse(operation_log.operations[0].success)
            self.assertEqual(
                operation_log.operations[0].message,
                "The file changed before it could be moved. Check the folder and try again.",
            )
            self.assertTrue(second.plan_item.source.exists())
            self.assertFalse(second.plan_item.destination.exists())

    def test_parent_only_change_is_ordinary_but_truthfully_classified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "New/Parent/source")
            recorder = _Recorder(fail="after_batch")

            with (
                mock.patch("organizer.executor.os.link", side_effect=OSError(errno.EIO, "private")),
                self.assertRaises(MovementRecordingError) as raised,
            ):
                apply_same_volume_identity_move_plan(
                    (item,), root, recorder=recorder
                )

            self.assertEqual(raised.exception.phase, "after_batch")
            self.assertTrue(raised.exception.filesystem_changed)
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse(item.plan_item.destination.exists())
            self.assertTrue(item.plan_item.destination.parent.is_dir())
            self.assertEqual(
                recorder.events,
                ["before_batch", "before_move:0", "after_failure:0", "after_batch"],
            )

    def test_after_failure_recorder_error_preserves_parent_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "New/source")
            recorder = _Recorder(fail="after_failure:0")

            with (
                mock.patch(
                    "organizer.executor.os.link",
                    side_effect=OSError(errno.EIO, "private"),
                ),
                self.assertRaises(MovementRecordingError) as raised,
            ):
                apply_same_volume_identity_move_plan(
                    (item,), root, recorder=recorder
                )

            self.assertEqual(raised.exception.phase, "after_failure")
            self.assertTrue(raised.exception.filesystem_changed)
            self.assertIsNotNone(raised.exception.operation_log)
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse(item.plan_item.destination.exists())

    def test_postpublication_failure_leaves_attempting_and_skips_terminal_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            recorder = _Recorder()

            with (
                mock.patch.object(
                    executor,
                    "_verify_destination_binding",
                    side_effect=StrictExecutorError(
                        "destination_verification_failed",
                        filesystem_changed=True,
                        recovery_required=True,
                    ),
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                apply_same_volume_identity_move_plan(
                    (item,), root, recorder=recorder
                )

            self.assertTrue(raised.exception.recovery_required)
            self.assertEqual(recorder.events, ["before_batch", "before_move:0"])
            self.assertTrue(item.plan_item.source.exists())
            self.assertTrue(item.plan_item.destination.exists())
            self.assertFalse(root.joinpath("AI_Review").exists())

    def test_ambiguous_link_error_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            recorder = _Recorder()
            real_link = os.link

            def publish_then_error(source, destination, **kwargs) -> None:
                real_link(source, destination, **kwargs)
                raise OSError(errno.EIO, "private")

            with (
                mock.patch(
                    "organizer.executor.os.link",
                    side_effect=publish_then_error,
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                apply_same_volume_identity_move_plan(
                    (item,), root, recorder=recorder
                )

            self.assertTrue(raised.exception.filesystem_changed)
            self.assertTrue(raised.exception.recovery_required)
            self.assertEqual(recorder.events, ["before_batch", "before_move:0"])
            self.assertTrue(item.plan_item.source.exists())
            self.assertTrue(item.plan_item.destination.exists())

    def test_postpublication_process_interrupts_are_recovery_required(self) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit(2)):
            with (
                self.subTest(interruption=type(interruption).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                item = _item(root, "O1", "source", "Dest/source")
                recorder = _Recorder()
                with (
                    mock.patch.object(
                        executor,
                        "_verify_destination_binding",
                        side_effect=interruption,
                    ),
                    self.assertRaises(StrictExecutorInterrupted),
                ):
                    apply_same_volume_identity_move_plan(
                        (item,), root, recorder=recorder
                    )
                self.assertEqual(recorder.events, ["before_batch", "before_move:0"])
                self.assertTrue(item.plan_item.destination.exists())

    def test_after_success_failure_keeps_completed_move_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            recorder = _Recorder(fail="after_success:0")

            with self.assertRaises(MovementRecordingError) as raised:
                apply_same_volume_identity_move_plan(
                    (item,), root, recorder=recorder
                )

            self.assertEqual(raised.exception.phase, "after_success")
            self.assertTrue(raised.exception.filesystem_changed)
            self.assertEqual(
                recorder.events,
                ["before_batch", "before_move:0", "after_success:0"],
            )
            self.assertFalse(item.plan_item.source.exists())
            self.assertTrue(item.plan_item.destination.exists())
            self.assertIsNotNone(raised.exception.operation_log)

    def test_after_success_occurs_after_final_verification_and_fsync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            fsyncs: list[int] = []
            real_fsync = executor._fsync_after_publication

            def observed_fsync(descriptor: int) -> None:
                real_fsync(descriptor)
                fsyncs.append(descriptor)

            class VerifyingRecorder(_Recorder):
                def after_success(self, index, plan_item, result) -> None:
                    self_test.assertEqual(len(fsyncs), 5)
                    self_test.assertFalse(plan_item.source.exists())
                    self_test.assertTrue(plan_item.destination.exists())
                    super().after_success(index, plan_item, result)

            self_test = self
            recorder = VerifyingRecorder()
            with mock.patch.object(
                executor,
                "_fsync_after_publication",
                side_effect=observed_fsync,
            ):
                apply_same_volume_identity_move_plan(
                    (item,), root, recorder=recorder
                )

            self.assertEqual(recorder.events[-2:], ["after_success:0", "after_batch"])

    def test_durable_attempting_states_remain_visible_to_recovery_inspector(self) -> None:
        for failure_point in ("after_link", "after_unlink"):
            with (
                self.subTest(failure_point=failure_point),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                item = _item(root, "O1", "Inbox/file.txt", "Documents/file.txt", b"data")
                repository, record = _ordinary_operation(root, status=STATUS_PREPARED)
                record = replace(
                    record,
                    schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
                    status=STATUS_PREPARED,
                    started_at=None,
                    completed_at=None,
                    expected_moves=(
                        replace(
                            record.expected_moves[0],
                            result_state=MOVE_PENDING,
                            content_identity=RegularFileContentIdentity(
                                "regular_file", "sha256", item.expected_digest
                            ),
                        ),
                    ),
                    verification=OrganizationOperationVerification(
                        status=VERIFICATION_NOT_RUN
                    ),
                    legacy_operation_log=None,
                )
                repository.save(record)
                recorder = _DurableMovementRecorder(record, root, repository)

                if failure_point == "after_link":
                    patcher = mock.patch.object(
                        executor,
                        "_verify_destination_binding",
                        side_effect=StrictExecutorError(
                            "destination_verification_failed",
                            filesystem_changed=True,
                            recovery_required=True,
                        ),
                    )
                    expected_error = StrictExecutorError
                else:
                    recorder.after_success = mock.Mock(
                        side_effect=OSError("record unavailable")
                    )
                    patcher = nullcontext()
                    expected_error = MovementRecordingError

                with patcher, self.assertRaises(expected_error):
                    apply_same_volume_identity_move_plan(
                        (item,), root, recorder=recorder
                    )

                durable = repository.load(record.operation_id)
                evidence = RecoveryEvidenceInspector(
                    FilesystemOperationInspector(root)
                ).inspect()
                self.assertEqual(durable.status, STATUS_MOVING)
                self.assertEqual(durable.expected_moves[0].result_state, MOVE_ATTEMPTING)
                self.assertEqual(evidence.actionability, RecoveryActionability.INSPECT_ONLY)
                self.assertEqual(evidence.case.available_action_kinds, ())

    def test_production_services_do_not_import_public_strict_api(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "organizer"
        callers = []
        for path in source_root.rglob("*.py"):
            if path.name == "executor.py":
                continue
            if "apply_same_volume_identity_move_plan" in path.read_text(
                encoding="utf-8"
            ):
                callers.append(path.relative_to(source_root))
        self.assertEqual(callers, [])


def _item(
    root: Path,
    change_id: str,
    source_name: str,
    destination_name: str,
    content: bytes | None = None,
) -> IdentityMovePlanItem:
    source = root / source_name
    destination = root / destination_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content if content is not None else source_name.encode("utf-8"))
    with capture_open_regular_file(source, root) as captured:
        identity = captured.identity
    return IdentityMovePlanItem(
        plan_item=MovePlanItem(
            source=source,
            destination=destination,
            reason="test",
            confidence=100,
            operation="move",
            overwrite_risk=False,
        ),
        change_id=change_id,
        expected_digest=identity.digest,
        captured_stat=identity.stat_fingerprint,
    )


def _ordinary_operation(root: Path, *, status: str):
    repository = OrganizationOperationRepository(root)
    record = OrganizationOperationRecord(
        operation_id=str(uuid.uuid4()),
        authorization_id=str(uuid.uuid4()),
        status=status,
        scan_generation="scan-one",
        decision_revision=1,
        approved_at="2026-07-16T10:00:00+00:00",
        created_at="2026-07-16T10:00:00+00:00",
        started_at=None,
        completed_at=None,
        configured_root_identity=repository.configured_root_identity,
        expected_moves=(
            OrganizationExpectedMove(
                change_id="O1",
                source="Inbox/file.txt",
                destination="Documents/file.txt",
                source_size_bytes=4,
                source_modified_time=100.0,
                reason="test",
                confidence=100,
                operation="move",
                overwrite_risk=False,
                result_state=MOVE_PENDING,
            ),
        ),
        verification=OrganizationOperationVerification(
            status=VERIFICATION_NOT_RUN
        ),
        legacy_operation_log=None,
        failure_code=None,
        duplicate_actions_deferred=0,
    )
    repository.create(record)
    return repository, record
