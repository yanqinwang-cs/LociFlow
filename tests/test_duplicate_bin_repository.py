from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock
import uuid

from organizer.application.duplicate_bin_models import (
    BATCH_APPROVED,
    BATCH_CANCELLED,
    BATCH_COMPLETED,
    BATCH_MOVED,
    BATCH_MOVING,
    BATCH_SUPERSEDED,
    ITEM_ATTENTION,
    ITEM_AVAILABLE,
    ITEM_MOVING,
    ITEM_RESTORED,
    ITEM_UNATTEMPTED,
    ApprovedDuplicateBatch,
    ApprovedDuplicateGroup,
    ApprovedDuplicateMember,
    SelectedDuplicateBinItem,
    canonical_digest,
)
from organizer.application.duplicate_bin_repository import (
    DuplicateBinRepository,
    DuplicateBinRepositoryError,
)
from tests.test_application_duplicate_bin_movement_service import _approved_batch


STAMP = "2026-07-16T10:00:00+00:00"


class DuplicateBinRepositoryTests(unittest.TestCase):
    def test_restart_reconciles_legacy_fully_restored_catalog_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = DuplicateBinRepository(root)
            batch = _approved_batch(root, repository, selected=2)
            repository.create_or_recover_approved_batch(batch)
            stale = repository.save_execution_state(
                batch.batch_id,
                expected_version=1,
                status=BATCH_MOVED,
                items=tuple(replace(item, state=ITEM_RESTORED) for item in batch.items),
                updated_at="2026-07-16T10:05:00+00:00",
            )
            stale_catalog = json.loads(
                repository.catalog_path.read_text(encoding="utf-8")
            )
            member_contents = {
                member.relative_path: (root / member.relative_path).read_bytes()
                for group in batch.groups
                for member in group.members
            }
            files_before = {
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            }

            restarted = DuplicateBinRepository(root)
            repaired = restarted.load(batch.batch_id)
            repaired_catalog = json.loads(
                restarted.catalog_path.read_text(encoding="utf-8")
            )

            self.assertEqual(stale.status, BATCH_MOVED)
            self.assertEqual(len(stale_catalog["active_claims"]), 3)
            self.assertEqual(repaired.status, BATCH_COMPLETED)
            self.assertEqual(repaired.batch_version, stale.batch_version + 1)
            self.assertNotEqual(repaired.updated_at, stale.updated_at)
            self.assertEqual(
                repaired_catalog["catalog_version"],
                stale_catalog["catalog_version"] + 1,
            )
            self.assertEqual(repaired_catalog["active_claims"], [])
            self.assertEqual(
                {
                    member.relative_path: (root / member.relative_path).read_bytes()
                    for group in batch.groups
                    for member in group.members
                },
                member_contents,
            )
            self.assertEqual(
                {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                files_before,
            )

            repaired_bytes = restarted.catalog_path.read_bytes()
            with mock.patch.object(
                DuplicateBinRepository,
                "_atomic_write",
                autospec=True,
            ) as atomic_write:
                second_restart = DuplicateBinRepository(root)
            atomic_write.assert_not_called()
            self.assertEqual(
                second_restart.load(batch.batch_id).batch_version,
                repaired.batch_version,
            )
            self.assertEqual(second_restart.catalog_path.read_bytes(), repaired_bytes)

    def test_restart_keeps_partial_attention_and_unattempted_batches_claiming(
        self,
    ) -> None:
        for unresolved_state in (
            ITEM_AVAILABLE,
            ITEM_ATTENTION,
            ITEM_UNATTEMPTED,
            ITEM_MOVING,
        ):
            with self.subTest(state=unresolved_state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repository = DuplicateBinRepository(root)
                batch = _approved_batch(root, repository, selected=2)
                repository.create_or_recover_approved_batch(batch)
                items = tuple(
                    replace(
                        item,
                        state=ITEM_RESTORED if index == 0 else unresolved_state,
                    )
                    for index, item in enumerate(batch.items)
                )
                unresolved = repository.save_execution_state(
                    batch.batch_id,
                    expected_version=1,
                    status=BATCH_MOVED,
                    items=items,
                    updated_at="2026-07-16T10:05:00+00:00",
                )
                catalog_before = repository.catalog_path.read_bytes()

                with mock.patch.object(
                    DuplicateBinRepository,
                    "_atomic_write",
                    autospec=True,
                ) as atomic_write:
                    restarted = DuplicateBinRepository(root)
                atomic_write.assert_not_called()
                current = restarted.load(batch.batch_id)
                catalog = json.loads(
                    restarted.catalog_path.read_text(encoding="utf-8")
                )
                self.assertEqual(current.status, BATCH_MOVED)
                self.assertEqual(current.batch_version, unresolved.batch_version)
                self.assertEqual(len(catalog["active_claims"]), 3)
                self.assertEqual(restarted.catalog_path.read_bytes(), catalog_before)

    def test_restart_rejects_completed_batch_with_unresolved_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = DuplicateBinRepository(root)
            batch = _batch(repository, path="Inbox/copy.txt")
            repository.create_or_recover_approved_batch(batch)
            malformed = json.loads(
                repository.catalog_path.read_text(encoding="utf-8")
            )
            malformed["batches"][0]["status"] = BATCH_COMPLETED
            malformed["active_claims"] = []
            repository.catalog_path.write_text(
                json.dumps(malformed),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DuplicateBinRepositoryError, "malformed"):
                DuplicateBinRepository(root)

    def test_completed_batches_release_claims_but_unresolved_batches_retain_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = DuplicateBinRepository(Path(directory).resolve())
            batch = _batch(repository, path="Inbox/copy.txt")
            repository.create_or_recover_approved_batch(batch)
            with self.assertRaisesRegex(
                DuplicateBinRepositoryError, "invalid_duplicate_batch_items"
            ):
                repository.save_execution_state(
                    batch.batch_id,
                    expected_version=1,
                    status=BATCH_COMPLETED,
                    items=batch.items,
                    updated_at="2026-07-16T10:04:00+00:00",
                )
            completed = repository.save_execution_state(
                batch.batch_id,
                expected_version=1,
                status=BATCH_COMPLETED,
                items=tuple(replace(item, state=ITEM_RESTORED) for item in batch.items),
                updated_at="2026-07-16T10:05:00+00:00",
            )

            catalog = json.loads(repository.catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(completed.status, BATCH_COMPLETED)
            self.assertEqual(catalog["active_claims"], [])
            restarted = DuplicateBinRepository(repository.root)
            self.assertEqual(restarted.load(batch.batch_id).status, BATCH_COMPLETED)
            replacement = _batch(
                restarted,
                path="Inbox/copy.txt",
                generation="scan-two",
                revision=2,
            )
            restarted.create_or_recover_approved_batch(replacement)
            self.assertEqual(restarted.load(batch.batch_id).status, BATCH_COMPLETED)
            self.assertEqual(restarted.list_active_approved_batches(), (replacement,))

        with tempfile.TemporaryDirectory() as directory:
            repository = DuplicateBinRepository(Path(directory).resolve())
            batch = _batch(repository, path="Inbox/copy.txt")
            repository.create_or_recover_approved_batch(batch)
            repository.save_execution_state(
                batch.batch_id,
                expected_version=1,
                status=BATCH_MOVED,
                items=tuple(replace(item, state=ITEM_AVAILABLE) for item in batch.items),
                updated_at="2026-07-16T10:05:00+00:00",
            )
            restarted = DuplicateBinRepository(repository.root)
            catalog = json.loads(restarted.catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(len(catalog["active_claims"]), 2)
            replacement = _batch(
                restarted,
                path="Inbox/copy.txt",
                generation="scan-two",
                revision=2,
            )
            with self.assertRaisesRegex(
                DuplicateBinRepositoryError, "active_copy_claim_conflict"
            ):
                restarted.create_or_recover_approved_batch(replacement)

    def test_atomic_catalog_round_trip_exact_recovery_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = DuplicateBinRepository(root)
            batch = _batch(repository, path="Inbox/copy.txt")

            created = repository.create_or_recover_approved_batch(batch)
            catalog_before = repository.catalog_path.read_bytes()
            recovered = repository.create_or_recover_approved_batch(
                replace(
                    batch,
                    created_by_session_id="replacement-browser-session",
                    approved_at="2026-07-16T10:01:00+00:00",
                    created_at="2026-07-16T10:01:00+00:00",
                    updated_at="2026-07-16T10:01:00+00:00",
                )
            )

            self.assertEqual(created, recovered)
            self.assertEqual(repository.catalog_path.read_bytes(), catalog_before)
            self.assertFalse((repository.directory / "items").exists())
            restarted = DuplicateBinRepository(root)
            self.assertEqual(restarted.load(batch.batch_id), batch)
            self.assertEqual(restarted.list_active_approved_batches(), (batch,))
            claims = json.loads(repository.catalog_path.read_text(encoding="utf-8"))[
                "active_claims"
            ]
            self.assertEqual(len(claims), 2)
            self.assertEqual(
                {claim["relative_path"] for claim in claims},
                {"Inbox/copy.txt", "Retained/copy.txt"},
            )
            self.assertEqual(
                sum(claim["bin_item_id"] is not None for claim in claims),
                1,
            )

    def test_catalog_missing_retained_member_claim_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = DuplicateBinRepository(Path(directory).resolve())
            batch = _batch(repository, path="Inbox/copy.txt")
            repository.create_or_recover_approved_batch(batch)
            data = json.loads(repository.catalog_path.read_text(encoding="utf-8"))
            data["active_claims"] = [
                claim
                for claim in data["active_claims"]
                if claim["bin_item_id"] is not None
            ]
            repository.catalog_path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(DuplicateBinRepositoryError, "malformed"):
                repository.list_batches()

    def test_strict_schema_wrong_root_and_duplicate_id_conflict_are_rejected(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            repository = DuplicateBinRepository(Path(first).resolve())
            other = DuplicateBinRepository(Path(second).resolve())
            batch = _batch(repository, path="Inbox/copy.txt")
            with self.assertRaisesRegex(DuplicateBinRepositoryError, "wrong_root"):
                repository.create_or_recover_approved_batch(
                    replace(
                        batch, configured_root_identity=other.configured_root_identity
                    )
                )
            repository.create_or_recover_approved_batch(batch)
            conflicting = _batch(
                repository,
                path="Archive/copy.txt",
                batch_id=batch.batch_id,
            )
            with self.assertRaisesRegex(DuplicateBinRepositoryError, "id_conflict"):
                repository.create_or_recover_approved_batch(conflicting)

            data = json.loads(repository.catalog_path.read_text(encoding="utf-8"))
            data["unknown"] = True
            repository.catalog_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(DuplicateBinRepositoryError, "malformed"):
                repository.list_batches()

    def test_overlap_normalization_and_unstarted_supersession_are_atomic(self) -> None:
        equivalent_paths = (
            ("Inbox/Copy.txt", "inbox/copy.txt"),
            ("Inbox/Caf\u00e9.txt", "Inbox/Cafe\u0301.txt"),
        )
        for first_path, second_path in equivalent_paths:
            with (
                self.subTest(paths=(first_path, second_path)),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository = DuplicateBinRepository(Path(directory).resolve())
                first = _batch(
                    repository, path=first_path, generation="scan-one", revision=1
                )
                second = _batch(
                    repository, path=second_path, generation="scan-two", revision=2
                )
                repository.create_or_recover_approved_batch(first)
                repository.create_or_recover_approved_batch(second)

                self.assertEqual(
                    repository.load(first.batch_id).status, BATCH_SUPERSEDED
                )
                self.assertEqual(
                    repository.load(second.batch_id).status, BATCH_APPROVED
                )
                self.assertFalse(
                    repository.load(first.batch_id).can_receive_authorization
                )
                self.assertEqual(repository.list_active_approved_batches(), (second,))

    def test_started_overlap_rejects_replacement_and_cancelled_is_non_executable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = DuplicateBinRepository(Path(directory).resolve())
            first = _batch(repository, path="Inbox/copy.txt", generation="scan-one")
            repository.create_or_recover_approved_batch(first)
            moving = repository.transition_status(
                first.batch_id,
                expected_version=1,
                status=BATCH_MOVING,
                updated_at="2026-07-16T10:02:00+00:00",
            )
            self.assertFalse(moving.can_receive_authorization)
            replacement = _batch(
                repository, path="inbox/COPY.txt", generation="scan-two"
            )
            with self.assertRaisesRegex(
                DuplicateBinRepositoryError, "active_copy_claim_conflict"
            ):
                repository.create_or_recover_approved_batch(replacement)
            self.assertEqual(repository.list_active_approved_batches(), ())

        with tempfile.TemporaryDirectory() as directory:
            repository = DuplicateBinRepository(Path(directory).resolve())
            first = _batch(repository, path="Inbox/copy.txt", generation="scan-one")
            repository.create_or_recover_approved_batch(first)
            cancelled = repository.transition_status(
                first.batch_id,
                expected_version=1,
                status=BATCH_CANCELLED,
                updated_at="2026-07-16T10:03:00+00:00",
            )
            self.assertFalse(cancelled.can_receive_authorization)
            replacement = _batch(
                repository, path="Inbox/copy.txt", generation="scan-two"
            )
            repository.create_or_recover_approved_batch(replacement)
            self.assertEqual(repository.load(first.batch_id).status, BATCH_CANCELLED)
            self.assertEqual(repository.list_active_approved_batches(), (replacement,))

    def test_all_selected_and_retained_overlap_directions_supersede(self) -> None:
        overlap_cases = (
            (
                "selected-selected",
                ("Copies/copy-a.txt", "Copies/copy-b.txt"),
                ("Copies/copy-a.txt", "Copies/copy-c.txt"),
            ),
            (
                "selected-retained",
                ("Copies/copy-a.txt", "Copies/copy-b.txt"),
                ("Copies/copy-c.txt", "Copies/copy-a.txt"),
            ),
            (
                "retained-selected",
                ("Copies/copy-a.txt", "Copies/copy-b.txt"),
                ("Copies/copy-b.txt", "Copies/copy-c.txt"),
            ),
            (
                "retained-retained",
                ("Copies/copy-a.txt", "Copies/copy-b.txt"),
                ("Copies/copy-c.txt", "Copies/copy-b.txt"),
            ),
        )
        for label, first_paths, second_paths in overlap_cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                repository = DuplicateBinRepository(Path(directory).resolve())
                first = _batch(
                    repository,
                    path=first_paths[0],
                    retained_path=first_paths[1],
                    generation="scan-one",
                )
                second = _batch(
                    repository,
                    path=second_paths[0],
                    retained_path=second_paths[1],
                    generation="scan-two",
                )
                repository.create_or_recover_approved_batch(first)
                repository.create_or_recover_approved_batch(second)

                self.assertEqual(
                    repository.load(first.batch_id).status,
                    BATCH_SUPERSEDED,
                )
                self.assertEqual(repository.list_active_approved_batches(), (second,))

    def test_reversed_selection_supersedes_unstarted_and_rejects_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = DuplicateBinRepository(Path(directory).resolve())
            first = _batch(
                repository,
                path="Copies/copy-a.txt",
                retained_path="Copies/copy-b.txt",
                generation="scan-one",
            )
            reversed_batch = _batch(
                repository,
                path="Copies/copy-b.txt",
                retained_path="Copies/copy-a.txt",
                generation="scan-two",
            )
            repository.create_or_recover_approved_batch(first)
            repository.create_or_recover_approved_batch(reversed_batch)
            self.assertEqual(repository.load(first.batch_id).status, BATCH_SUPERSEDED)
            self.assertEqual(
                repository.list_active_approved_batches(),
                (reversed_batch,),
            )

        with tempfile.TemporaryDirectory() as directory:
            repository = DuplicateBinRepository(Path(directory).resolve())
            first = _batch(
                repository,
                path="Copies/copy-a.txt",
                retained_path="Copies/copy-b.txt",
                generation="scan-one",
            )
            reversed_batch = _batch(
                repository,
                path="Copies/copy-b.txt",
                retained_path="Copies/copy-a.txt",
                generation="scan-two",
            )
            repository.create_or_recover_approved_batch(first)
            repository.transition_status(
                first.batch_id,
                expected_version=1,
                status=BATCH_MOVING,
                updated_at="2026-07-16T10:04:00+00:00",
            )
            with self.assertRaisesRegex(
                DuplicateBinRepositoryError,
                "active_copy_claim_conflict",
            ):
                repository.create_or_recover_approved_batch(reversed_batch)


def _batch(
    repository: DuplicateBinRepository,
    *,
    path: str,
    retained_path: str | None = None,
    generation: str = "scan-one",
    revision: int = 1,
    batch_id: str | None = None,
) -> ApprovedDuplicateBatch:
    actual_retained_path = retained_path or f"Retained/{Path(path).name}"
    identity = f"{generation}:{revision}:{path}:{actual_retained_path}"
    actual_batch_id = batch_id or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"batch:{identity}")
    )
    selected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"member:{path}"))
    retained_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"member:{actual_retained_path}"))
    group_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"group:{identity}"))
    item_id = str(uuid.uuid5(uuid.UUID(actual_batch_id), selected_id))
    basename = Path(path).name
    group = ApprovedDuplicateGroup(
        group_id=group_id,
        choice_id="D1",
        approved_sha256="a" * 64,
        approved_size_bytes=4,
        members=tuple(
            sorted(
                (
                    ApprovedDuplicateMember(selected_id, path, 4, 1.0),
                    ApprovedDuplicateMember(retained_id, actual_retained_path, 4, 1.0),
                ),
                key=lambda member: member.relative_path,
            )
        ),
        selected_member_ids=(selected_id,),
        retained_member_ids=(retained_id,),
    )
    item = SelectedDuplicateBinItem(
        bin_item_id=item_id,
        selected_member_id=selected_id,
        previous_relative_path=path,
        future_relative_path=f"AI_Review/duplicate_bin/items/{item_id}/{basename}",
        basename=basename,
        size_bytes=4,
        kind="text",
        extension=".txt",
        approved_sha256="a" * 64,
        approval_metadata_fingerprint=canonical_digest(
            {
                "schema": "loci-duplicate-approval-metadata-v1",
                "size_bytes": 4,
                "modified_time": 1.0,
            }
        ),
    )
    return ApprovedDuplicateBatch(
        batch_id=actual_batch_id,
        configured_root_identity=repository.configured_root_identity,
        created_by_session_id="browser-one",
        originating_scan_generation=generation,
        decision_revision=revision,
        approved_at=STAMP,
        approved_plan_digest=("c" if path == "Inbox/copy.txt" else "d") * 64,
        duplicate_batch_digest=("e" if generation == "scan-one" else "f") * 64,
        batch_version=1,
        status=BATCH_APPROVED,
        created_at=STAMP,
        updated_at=STAMP,
        groups=(group,),
        items=(item,),
    )


if __name__ == "__main__":
    unittest.main()
