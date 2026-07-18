from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock
import uuid

import organizer.application.duplicate_bin_movement_service as movement_service
from organizer.application.duplicate_bin_authorization_repository import (
    DuplicateBinAuthorizationRepository,
)
from organizer.application.duplicate_bin_models import (
    BATCH_APPROVED,
    BATCH_CANCELLED,
    BATCH_EXECUTING,
    ITEM_MOVING,
    ApprovedDuplicateBatch,
    ApprovedDuplicateGroup,
    ApprovedDuplicateMember,
    SelectedDuplicateBinItem,
    canonical_digest,
)
from organizer.application.duplicate_bin_movement_service import (
    CONFIRM_MOVE_COPIES_TO_DUPLICATE_BIN,
    DuplicateBinMovementError,
    execute_duplicate_bin_movement,
    preview_duplicate_bin_movement,
)
from organizer.application.duplicate_bin_operation_repository import (
    DuplicateBinOperationRepository,
)
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeOperationKind,
)
from organizer.duplicates import hash_file


ISSUED = datetime(2026, 7, 16, 10, tzinfo=timezone.utc)


class DuplicateBinMovementServiceTests(unittest.TestCase):
    def test_stopping_rejects_preview_without_superseding_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            existing = _preview(owners, issued=ISSUED)
            before = owners.authorizations.path.read_bytes()
            owners.coordinator.runtime_lifecycle.begin_shutdown()

            with self.assertRaises(ApplicationStoppingError):
                _preview(owners, issued=ISSUED + timedelta(minutes=1))

            self.assertEqual(owners.authorizations.path.read_bytes(), before)
            self.assertEqual(
                owners.authorizations.load(existing.confirmation_token).status,
                "available",
            )
            self.assertEqual(owners.operations.list_records(), ())

    def test_preview_is_durable_exactly_fifteen_minutes_and_supersedes_unused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            first = _preview(owners, session="browser-one", issued=ISSUED)
            second = _preview(
                owners,
                session="browser-one",
                issued=ISSUED + timedelta(minutes=1),
            )

            self.assertEqual(
                datetime.fromisoformat(first.expires_at) - ISSUED,
                timedelta(minutes=15),
            )
            self.assertEqual(
                owners.authorizations.load(first.confirmation_token).status,
                "superseded",
            )
            self.assertEqual(
                owners.authorizations.load(second.confirmation_token).status,
                "available",
            )
            self.assertEqual(
                set(first.to_dict()),
                {"copies_to_move", "confirmation", "confirmation_token", "expires_at"},
            )

    def test_preview_rejects_unknown_wrong_root_cancelled_and_started_batches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            with self.assertRaisesRegex(
                DuplicateBinMovementError, "duplicate_batch_not_found"
            ):
                preview_duplicate_bin_movement(
                    batch_id=str(uuid.uuid4()),
                    browser_session_id="browser-one",
                    batch_repository=owners.batches,
                    authorization_repository=owners.authorizations,
                    operation_repository=owners.operations,
                    coordinator=owners.coordinator,
                    issued_at=ISSUED.isoformat(),
                )

        with (
            tempfile.TemporaryDirectory() as first_directory,
            tempfile.TemporaryDirectory() as second_directory,
        ):
            owners = _owners(Path(first_directory).resolve(), selected=1)
            wrong_root_authorizations = DuplicateBinAuthorizationRepository(
                Path(second_directory).resolve()
            )
            with self.assertRaisesRegex(
                DuplicateBinMovementError, "duplicate_batch_wrong_root"
            ):
                preview_duplicate_bin_movement(
                    batch_id=owners.batch.batch_id,
                    browser_session_id="browser-one",
                    batch_repository=owners.batches,
                    authorization_repository=wrong_root_authorizations,
                    operation_repository=owners.operations,
                    coordinator=owners.coordinator,
                    issued_at=ISSUED.isoformat(),
                )

        for state in (BATCH_CANCELLED, BATCH_EXECUTING):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                owners = _owners(Path(directory).resolve(), selected=1)
                if state == BATCH_CANCELLED:
                    owners.batches.transition_status(
                        owners.batch.batch_id,
                        expected_version=owners.batch.batch_version,
                        status=state,
                        updated_at=ISSUED.isoformat(),
                    )
                else:
                    owners.batches.save_execution_state(
                        owners.batch.batch_id,
                        expected_version=owners.batch.batch_version,
                        status=state,
                        items=tuple(
                            replace(item, state=ITEM_MOVING)
                            for item in owners.batch.items
                        ),
                        updated_at=ISSUED.isoformat(),
                    )
                with self.assertRaisesRegex(
                    DuplicateBinMovementError, "duplicate_batch_not_approved"
                ):
                    _preview(owners, issued=ISSUED + timedelta(minutes=1))

    def test_success_is_durable_before_executor_verified_and_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=2)
            preview = _preview(owners, issued=ISSUED)
            real_move = shutil.move
            observed: list[str] = []

            def checked_move(source: str, destination: str) -> str:
                self.assertEqual(
                    owners.coordinator.runtime_lifecycle.snapshot().active_kinds,
                    (RuntimeOperationKind.DUPLICATE_BIN_MOVEMENT,),
                )
                record = owners.operations.find_for_authorization(
                    preview.confirmation_token
                )
                assert record is not None
                self.assertEqual(record.status, "moving")
                observed.append(source)
                return str(real_move(source, destination))

            real_verify = movement_service._verify

            def checked_verify(*args, **kwargs):
                self.assertEqual(
                    owners.coordinator.runtime_lifecycle.snapshot().active_kinds,
                    (RuntimeOperationKind.DUPLICATE_BIN_MOVEMENT,),
                )
                return real_verify(*args, **kwargs)

            with (
                mock.patch("organizer.executor.shutil.move", side_effect=checked_move),
                mock.patch.object(
                    movement_service,
                    "_verify",
                    side_effect=checked_verify,
                ),
            ):
                result = _execute(owners, preview, now=ISSUED + timedelta(minutes=1))

            self.assertEqual(result.status, "moved")
            self.assertEqual(result.copies_moved, 2)
            self.assertEqual(len(observed), 2)
            operation = owners.operations.load(result.id)
            self.assertEqual(operation.verification.status, "passed")
            self.assertEqual(operation.verification.verified_count, 2)
            self.assertTrue(operation.executor_log_path)
            self.assertTrue((owners.root / operation.executor_log_path).is_file())
            self.assertTrue(
                all(
                    item.attempted_at is not None and item.completed_at is not None
                    for item in operation.expected_moves
                )
            )
            self.assertEqual(
                tuple(item.source for item in operation.expected_moves),
                tuple(
                    item.previous_relative_path
                    for item in sorted(
                        owners.batch.items, key=lambda candidate: candidate.bin_item_id
                    )
                ),
            )
            self.assertEqual(
                owners.coordinator.runtime_lifecycle.snapshot().active_count,
                0,
            )

            self.assertEqual(
                owners.authorizations.load(preview.confirmation_token).operation_id,
                result.id,
            )
            self.assertNotIn(
                operation.preflight_digest,
                owners.authorizations.path.read_text(encoding="utf-8"),
            )
            batch = owners.batches.load(owners.batch.batch_id)
            self.assertEqual(batch.status, "moved")
            self.assertTrue(all(item.state == "available" for item in batch.items))
            for item in batch.items:
                self.assertFalse((owners.root / item.previous_relative_path).exists())
                self.assertEqual(
                    hash_file(owners.root / item.future_relative_path),
                    item.approved_sha256,
                )
            for group in batch.groups:
                for member in group.members:
                    if member.member_id in group.retained_member_ids:
                        self.assertTrue((owners.root / member.relative_path).exists())

            with mock.patch(
                "organizer.application.duplicate_bin_movement_service.apply_move_plan"
            ) as apply_again:
                replay = _execute(
                    owners,
                    preview,
                    now=ISSUED + timedelta(minutes=2),
                )
            self.assertEqual(replay.id, result.id)
            apply_again.assert_not_called()

            self.assertTrue(owners.coordinator.try_acquire())
            try:
                with mock.patch(
                    "organizer.application.duplicate_bin_movement_service.apply_move_plan"
                ) as apply_while_busy:
                    busy_replay = _execute(
                        owners,
                        preview,
                        now=ISSUED + timedelta(minutes=3),
                    )
                self.assertEqual(busy_replay.id, result.id)
                apply_while_busy.assert_not_called()
            finally:
                owners.coordinator.release()

            changed = replace(
                operation,
                expected_moves=(
                    replace(
                        operation.expected_moves[0],
                        source="Elsewhere/copy-1.txt",
                    ),
                    *operation.expected_moves[1:],
                ),
            )
            with self.assertRaisesRegex(ValueError, "binding cannot change"):
                owners.operations.save(changed)

            restarted = DuplicateBinOperationRepository(owners.root)
            self.assertEqual(restarted.load(result.id), operation)
            with self.assertRaisesRegex(ValueError, "authorization already"):
                restarted.create(replace(operation, operation_id=str(uuid.uuid4())))

    def test_stopping_rejects_before_operation_or_filesystem_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            preview = _preview(owners, issued=ISSUED)
            selected = owners.root / owners.batch.items[0].previous_relative_path
            owners.coordinator.runtime_lifecycle.begin_shutdown()

            with self.assertRaises(ApplicationStoppingError):
                _execute(owners, preview, now=ISSUED + timedelta(minutes=1))

            self.assertTrue(selected.is_file())
            self.assertEqual(owners.operations.list_records(), ())
            self.assertEqual(
                owners.coordinator.runtime_lifecycle.snapshot().active_count,
                0,
            )

    def test_expired_wrong_session_and_superseded_authorizations_do_not_move(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            first = _preview(owners, issued=ISSUED)
            second = _preview(owners, issued=ISSUED + timedelta(minutes=1))
            cases = (
                (
                    first,
                    "browser-one",
                    ISSUED + timedelta(minutes=2),
                    "invalid_confirmation_token",
                ),
                (
                    second,
                    "other-browser",
                    ISSUED + timedelta(minutes=2),
                    "invalid_confirmation_token",
                ),
                (
                    second,
                    "browser-one",
                    ISSUED + timedelta(minutes=16),
                    "confirmation_expired",
                ),
            )
            for preview, session, now, code in cases:
                with (
                    self.subTest(code=code),
                    self.assertRaisesRegex(DuplicateBinMovementError, code),
                ):
                    _execute(owners, preview, session=session, now=now)
            self.assertEqual(owners.operations.list_records(), ())
            self.assertEqual(
                owners.batches.load(owners.batch.batch_id).batch_id,
                owners.batch.batch_id,
            )
            self.assertTrue(
                all(
                    (owners.root / item.previous_relative_path).exists()
                    for item in owners.batch.items
                )
            )

    def test_changed_retained_copy_and_destination_collisions_fail_before_operation(
        self,
    ) -> None:
        for case in ("retained", "destination", "case_collision"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                owners = _owners(Path(directory).resolve(), selected=1)
                preview = _preview(owners, issued=ISSUED)
                if case == "retained":
                    retained = next(
                        member
                        for group in owners.batch.groups
                        for member in group.members
                        if member.member_id in group.retained_member_ids
                    )
                    (owners.root / retained.relative_path).write_text(
                        "changed", encoding="utf-8"
                    )
                    expected = "retained_copy_changed"
                elif case == "destination":
                    item = owners.batch.items[0]
                    destination = owners.root / item.future_relative_path
                    destination.parent.mkdir(parents=True)
                    destination.write_text("occupied", encoding="utf-8")
                    expected = "unsafe_duplicate_bin_destination"
                else:
                    item = owners.batch.items[0]
                    destination = owners.root / item.future_relative_path
                    destination.parent.parent.mkdir(parents=True)
                    (
                        destination.parent.parent / destination.parent.name.upper()
                    ).mkdir()
                    expected = "unsafe_duplicate_bin_destination"
                with self.assertRaisesRegex(DuplicateBinMovementError, expected):
                    _execute(owners, preview, now=ISSUED + timedelta(minutes=1))
                self.assertEqual(owners.operations.list_records(), ())
                self.assertTrue(
                    all(
                        (owners.root / item.previous_relative_path).exists()
                        for item in owners.batch.items
                    )
                )

    def test_selected_missing_symlink_hash_and_mid_hash_changes_fail_preflight(
        self,
    ) -> None:
        cases = (
            "selected_changed",
            "selected_missing",
            "retained_missing",
            "symlink",
            "mid_hash",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                owners = _owners(Path(directory).resolve(), selected=1)
                preview = _preview(owners, issued=ISSUED)
                selected_path = (
                    owners.root / owners.batch.items[0].previous_relative_path
                )
                retained = next(
                    member
                    for group in owners.batch.groups
                    for member in group.members
                    if member.member_id in group.retained_member_ids
                )
                retained_path = owners.root / retained.relative_path
                patcher = None
                if case == "selected_changed":
                    selected_path.write_bytes(b"x" * selected_path.stat().st_size)
                    expected = "selected_copy_changed"
                elif case == "selected_missing":
                    selected_path.unlink()
                    expected = "selected_copy_changed"
                elif case == "retained_missing":
                    retained_path.unlink()
                    expected = "retained_copy_missing"
                elif case == "symlink":
                    target = owners.root.parent / f"outside-{uuid.uuid4()}.txt"
                    target.write_bytes(selected_path.read_bytes())
                    selected_path.unlink()
                    try:
                        selected_path.symlink_to(target)
                    except (NotImplementedError, OSError):
                        target.unlink(missing_ok=True)
                        self.skipTest("symlinks are unavailable")
                    expected = "duplicate_member_unsafe"
                else:
                    real_hash = hash_file

                    def unstable(path):
                        digest = real_hash(path)
                        stat_result = path.stat()
                        path.touch()
                        if path.stat().st_mtime_ns == stat_result.st_mtime_ns:
                            path.write_bytes(path.read_bytes())
                        return digest

                    patcher = mock.patch(
                        "organizer.application.duplicate_bin_movement_service.hash_file",
                        side_effect=unstable,
                    )
                    patcher.start()
                    expected = "duplicate_member_changed_during_hash"
                try:
                    with self.assertRaisesRegex(DuplicateBinMovementError, expected):
                        _execute(owners, preview, now=ISSUED + timedelta(minutes=1))
                finally:
                    if patcher is not None:
                        patcher.stop()
                    if case == "symlink":
                        target.unlink(missing_ok=True)
                self.assertEqual(owners.operations.list_records(), ())

    def test_verification_failure_is_attention_without_rollback(self) -> None:
        for case in (
            "source_present",
            "destination_changed",
            "destination_missing",
            "destination_symlink",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                owners = _owners(Path(directory).resolve(), selected=1)
                preview = _preview(owners, issued=ISSUED)
                real_move = shutil.move

                def corrupt(source: str, destination: str) -> str:
                    if case == "source_present":
                        Path(destination).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                        return destination
                    result = str(real_move(source, destination))
                    destination_path = Path(destination)
                    if case == "destination_changed":
                        destination_path.write_bytes(b"changed after movement")
                    elif case == "destination_missing":
                        destination_path.unlink()
                    elif case == "destination_symlink":
                        destination_path.unlink()
                        destination_path.symlink_to(
                            owners.root / "Archive/retained.txt"
                        )
                    return result

                with mock.patch("organizer.executor.shutil.move", side_effect=corrupt):
                    result = _execute(
                        owners,
                        preview,
                        now=ISSUED + timedelta(minutes=1),
                    )
                self.assertEqual(result.status, "verification_failed")
                self.assertEqual(result.copies_failed, 1)
                self.assertEqual(
                    owners.batches.load(owners.batch.batch_id).items[0].state,
                    "attention",
                )
                destination = owners.root / owners.batch.items[0].future_relative_path
                if case != "destination_missing":
                    self.assertTrue(destination.exists())

    def test_partial_failure_is_truthful_and_coordinator_blocks_concurrency(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=2)
            preview = _preview(owners, issued=ISSUED)
            real_move = shutil.move
            calls = 0

            def partial(source: str, destination: str) -> str:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected failure")
                return str(real_move(source, destination))

            with mock.patch("organizer.executor.shutil.move", side_effect=partial):
                result = _execute(owners, preview, now=ISSUED + timedelta(minutes=1))
            self.assertEqual(result.status, "partially_moved")
            self.assertEqual((result.copies_moved, result.copies_failed), (1, 1))
            self.assertEqual(
                owners.batches.load(owners.batch.batch_id).status, "partially_moved"
            )
            with mock.patch(
                "organizer.application.duplicate_bin_movement_service.apply_move_plan"
            ) as apply_again:
                replay = _execute(
                    owners,
                    preview,
                    now=ISSUED + timedelta(minutes=2),
                )
            self.assertEqual(replay.id, result.id)
            apply_again.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            preview = _preview(owners, issued=ISSUED)
            self.assertTrue(owners.coordinator.try_acquire())
            try:
                with self.assertRaisesRegex(
                    DuplicateBinMovementError, "movement_or_undo_in_progress"
                ):
                    _execute(owners, preview, now=ISSUED + timedelta(minutes=1))
            finally:
                owners.coordinator.release()

    def test_wrong_root_token_and_interrupted_restart_replay_never_move_again(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as first_directory,
            tempfile.TemporaryDirectory() as second_directory,
        ):
            first = _owners(Path(first_directory).resolve(), selected=1)
            second = _owners(Path(second_directory).resolve(), selected=1)
            preview = _preview(first, issued=ISSUED)
            with self.assertRaisesRegex(
                DuplicateBinMovementError, "invalid_confirmation_token"
            ):
                execute_duplicate_bin_movement(
                    browser_session_id="browser-one",
                    confirmation_token=preview.confirmation_token,
                    confirmation=CONFIRM_MOVE_COPIES_TO_DUPLICATE_BIN,
                    batch_repository=second.batches,
                    authorization_repository=first.authorizations,
                    operation_repository=second.operations,
                    coordinator=second.coordinator,
                    now=(ISSUED + timedelta(minutes=1)).isoformat(),
                )
            self.assertEqual(second.operations.list_records(), ())

        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            preview = _preview(owners, issued=ISSUED)

            def interrupt(plan, root, *, log_dir_name, recorder):
                del root, log_dir_name
                recorder.before_batch(tuple(plan))
                raise KeyboardInterrupt

            with (
                mock.patch(
                    "organizer.application.duplicate_bin_movement_service.apply_move_plan",
                    side_effect=interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                _execute(owners, preview, now=ISSUED + timedelta(minutes=1))

            restarted_batches = DuplicateBinRepository(owners.root)
            restarted_authorizations = DuplicateBinAuthorizationRepository(owners.root)
            restarted_operations = DuplicateBinOperationRepository(owners.root)
            busy_coordinator = OrganizationExecutionCoordinator()
            self.assertTrue(busy_coordinator.try_acquire())
            try:
                with mock.patch(
                    "organizer.application.duplicate_bin_movement_service.apply_move_plan"
                ) as apply_again:
                    replay = execute_duplicate_bin_movement(
                        browser_session_id="browser-one",
                        confirmation_token=preview.confirmation_token,
                        confirmation=CONFIRM_MOVE_COPIES_TO_DUPLICATE_BIN,
                        batch_repository=restarted_batches,
                        authorization_repository=restarted_authorizations,
                        operation_repository=restarted_operations,
                        coordinator=busy_coordinator,
                        now=(ISSUED + timedelta(minutes=2)).isoformat(),
                    )
            finally:
                busy_coordinator.release()
            self.assertEqual(replay.status, "moving")
            self.assertTrue(replay.recovery_required)
            apply_again.assert_not_called()
            self.assertTrue(
                all(
                    (owners.root / item.previous_relative_path).exists()
                    for item in owners.batch.items
                )
            )

    def test_admitted_duplicate_movement_completes_after_shutdown_begins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owners = _owners(Path(directory).resolve(), selected=1)
            preview = _preview(owners, issued=ISSUED)
            verifying = Event()
            continue_verification = Event()
            outcomes = []
            real_verify = movement_service._verify

            def paused_verify(*args, **kwargs):
                verifying.set()
                continue_verification.wait(timeout=2)
                return real_verify(*args, **kwargs)

            worker = Thread(
                target=lambda: outcomes.append(
                    _execute(owners, preview, now=ISSUED + timedelta(minutes=1))
                )
            )
            with mock.patch.object(
                movement_service,
                "_verify",
                side_effect=paused_verify,
            ):
                worker.start()
                self.assertTrue(verifying.wait(1))
                stopping = owners.coordinator.runtime_lifecycle.begin_shutdown()
                self.assertEqual(stopping.active_count, 1)
                continue_verification.set()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0].status, "moved")
            self.assertEqual(
                owners.coordinator.runtime_lifecycle.wait_until_idle().active_count,
                0,
            )


class _Owners:
    def __init__(
        self, root: Path, batch, batches, authorizations, operations, coordinator
    ):
        self.root = root
        self.batch = batch
        self.batches = batches
        self.authorizations = authorizations
        self.operations = operations
        self.coordinator = coordinator


def _owners(root: Path, *, selected: int) -> _Owners:
    batches = DuplicateBinRepository(root)
    batch = _approved_batch(root, batches, selected=selected)
    batches.create_or_recover_approved_batch(batch)
    return _Owners(
        root,
        batch,
        batches,
        DuplicateBinAuthorizationRepository(root),
        DuplicateBinOperationRepository(root),
        OrganizationExecutionCoordinator(),
    )


def _approved_batch(
    root: Path, repository: DuplicateBinRepository, *, selected: int = 1
):
    content = b"exact duplicate bytes"
    selected_paths = [f"Inbox/copy-{index}.txt" for index in range(1, selected + 1)]
    retained_path = "Archive/retained.txt"
    paths = selected_paths + [retained_path]
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    members = tuple(
        sorted(
            (
                ApprovedDuplicateMember(
                    member_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"member:{relative}")),
                    relative_path=relative,
                    size_bytes=len(content),
                    modified_time=(root / relative).stat().st_mtime,
                )
                for relative in paths
            ),
            key=lambda item: item.relative_path,
        )
    )
    selected_member_ids = tuple(
        sorted(
            member.member_id
            for member in members
            if member.relative_path in selected_paths
        )
    )
    retained_member_ids = tuple(
        sorted(
            member.member_id
            for member in members
            if member.relative_path == retained_path
        )
    )
    batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"batch:{selected}"))
    group = ApprovedDuplicateGroup(
        group_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"group:{selected}")),
        choice_id="D1",
        approved_sha256=hash_file(root / paths[0]),
        approved_size_bytes=len(content),
        members=members,
        selected_member_ids=selected_member_ids,
        retained_member_ids=retained_member_ids,
    )
    items = []
    for member in members:
        if member.member_id not in selected_member_ids:
            continue
        item_id = str(uuid.uuid5(uuid.UUID(batch_id), member.member_id))
        basename = Path(member.relative_path).name
        items.append(
            SelectedDuplicateBinItem(
                bin_item_id=item_id,
                selected_member_id=member.member_id,
                previous_relative_path=member.relative_path,
                future_relative_path=f"AI_Review/duplicate_bin/items/{item_id}/{basename}",
                basename=basename,
                size_bytes=member.size_bytes,
                kind="text",
                extension=".txt",
                approved_sha256=group.approved_sha256,
                approval_metadata_fingerprint=canonical_digest(
                    {
                        "schema": "loci-duplicate-approval-metadata-v1",
                        "size_bytes": member.size_bytes,
                        "modified_time": member.modified_time,
                    }
                ),
            )
        )
    stamp = ISSUED.isoformat()
    return ApprovedDuplicateBatch(
        batch_id=batch_id,
        configured_root_identity=repository.configured_root_identity,
        created_by_session_id="approval-browser",
        originating_scan_generation="scan-one",
        decision_revision=1,
        approved_at=stamp,
        approved_plan_digest="a" * 64,
        duplicate_batch_digest="b" * 64,
        batch_version=1,
        status=BATCH_APPROVED,
        created_at=stamp,
        updated_at=stamp,
        groups=(group,),
        items=tuple(sorted(items, key=lambda item: item.bin_item_id)),
    )


def _preview(owners: _Owners, *, session: str = "browser-one", issued: datetime):
    return preview_duplicate_bin_movement(
        batch_id=owners.batch.batch_id,
        browser_session_id=session,
        batch_repository=owners.batches,
        authorization_repository=owners.authorizations,
        operation_repository=owners.operations,
        coordinator=owners.coordinator,
        issued_at=issued.isoformat(),
    )


def _execute(
    owners: _Owners,
    preview,
    *,
    session: str = "browser-one",
    now: datetime,
):
    return execute_duplicate_bin_movement(
        browser_session_id=session,
        confirmation_token=preview.confirmation_token,
        confirmation=CONFIRM_MOVE_COPIES_TO_DUPLICATE_BIN,
        batch_repository=owners.batches,
        authorization_repository=owners.authorizations,
        operation_repository=owners.operations,
        coordinator=owners.coordinator,
        now=now.isoformat(),
    )


if __name__ == "__main__":
    unittest.main()
