from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from organizer.application.duplicate_bin_approval_service import (
    create_or_recover_duplicate_batch,
)
from organizer.application.duplicate_bin_repository import (
    DuplicateBinRepository,
    DuplicateBinRepositoryError,
)
from organizer.application.organization_decision_service import (
    OrganizationDecisionError,
    approve_organization_plan,
    create_organization_decision_session,
    update_organization_decision,
)
from organizer.application.organization_slice_service import (
    build_internal_organization_projection,
)
from organizer.application.scan_service import scan_root
from organizer.web.organization_decision_store import (
    OrganizationDecisionSnapshot,
    OrganizationDecisionStore,
)
from organizer.web.scan_jobs import ScanJobSnapshot


COMPLETED_AT = datetime(2026, 7, 16, 10, tzinfo=timezone.utc)
APPROVED_AT = "2026-07-16T10:01:00+00:00"


class DuplicateBinApprovalServiceTests(unittest.TestCase):
    def test_identity_is_canonical_session_independent_and_safely_projected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            projection = _projection(root, "scan-one")
            approved = _approved_plan(projection, session_mutations=1)
            repository = DuplicateBinRepository(root)

            first = create_or_recover_duplicate_batch(
                approved_plan=approved,
                projection=projection,
                browser_session_id="browser-one",
                repository=repository,
            )
            second = create_or_recover_duplicate_batch(
                approved_plan=replace(
                    approved, approved_at="2026-07-16T11:00:00+00:00"
                ),
                projection=projection,
                browser_session_id="browser-two",
                repository=repository,
            )
            assert first is not None and second is not None

            self.assertEqual(first.batch_id, second.batch_id)
            self.assertEqual(first, second)
            self.assertEqual(
                set(first.safe_projection().to_dict()), {"id", "status", "copies"}
            )
            self.assertEqual(first.safe_projection().copies, 1)
            serialized = repository.catalog_path.read_text(encoding="utf-8")
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("browser-two", serialized)
            self.assertFalse((repository.directory / "items").exists())

    def test_generation_revision_and_duplicate_state_change_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = DuplicateBinRepository(root)
            first_projection = _projection(root, "scan-one")
            generation_projection = _projection(root, "scan-two")
            first_plan = _approved_plan(first_projection, session_mutations=1)
            generation_plan = _approved_plan(generation_projection, session_mutations=1)
            revision_plan = _approved_plan(first_projection, session_mutations=3)
            state_plan = _approved_plan(
                first_projection,
                session_mutations=1,
                selected_index=1,
            )

            first = create_or_recover_duplicate_batch(
                approved_plan=first_plan,
                projection=first_projection,
                browser_session_id="browser",
                repository=repository,
            )
            generation = create_or_recover_duplicate_batch(
                approved_plan=generation_plan,
                projection=generation_projection,
                browser_session_id="browser",
                repository=repository,
            )
            revision = create_or_recover_duplicate_batch(
                approved_plan=revision_plan,
                projection=first_projection,
                browser_session_id="browser",
                repository=repository,
            )
            state = create_or_recover_duplicate_batch(
                approved_plan=state_plan,
                projection=first_projection,
                browser_session_id="browser",
                repository=repository,
            )
            assert (
                first is not None
                and generation is not None
                and revision is not None
                and state is not None
            )
            self.assertEqual(
                len(
                    {
                        first.batch_id,
                        generation.batch_id,
                        revision.batch_id,
                        state.batch_id,
                    }
                ),
                4,
            )
            self.assertEqual(
                first.duplicate_batch_digest, revision.duplicate_batch_digest
            )
            self.assertNotEqual(
                first.duplicate_batch_digest, state.duplicate_batch_digest
            )

    def test_store_persists_before_publication_and_failure_leaves_session_unapproved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            projection = _projection(root, "scan-one")
            scan = _scan(root, projection, "scan-one")
            repository = _FailingRepository(root)
            store = OrganizationDecisionStore(repository)
            _select_first_copy(store, "browser", scan, projection)

            with self.assertRaisesRegex(ValueError, "duplicate_batch_unavailable"):
                store.approve(
                    "browser",
                    scan,
                    projection,
                    scan_generation="scan-one",
                    expected_revision=1,
                )

            current = store.current("browser", scan, projection)
            self.assertEqual(current.session.status, "ready_for_approval")
            self.assertIsNone(current.session.approved_plan)
            self.assertFalse(repository.catalog_path.exists())

    def test_publication_failure_retry_recovers_one_durable_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            projection = _projection(root, "scan-one")
            scan = _scan(root, projection, "scan-one")
            repository = DuplicateBinRepository(root)
            store = OrganizationDecisionStore(repository)
            selected = _select_first_copy(store, "browser", scan, projection)
            store._sessions = _FailOnceDictionary(store._sessions)

            with self.assertRaisesRegex(RuntimeError, "publication failed"):
                store.approve(
                    "browser",
                    scan,
                    projection,
                    scan_generation="scan-one",
                    expected_revision=selected.session.decision_revision,
                )
            durable_id = repository.list_active_approved_batches()[0].batch_id
            recovered = store.approve(
                "browser",
                scan,
                projection,
                scan_generation="scan-one",
                expected_revision=selected.session.decision_revision,
            )

            self.assertEqual(len(repository.list_batches()), 1)
            assert recovered.duplicate_batch is not None
            self.assertEqual(recovered.duplicate_batch.id, durable_id)
            self.assertEqual(recovered.session.status, "ready_to_move")

    def test_no_selection_creates_no_catalog_and_new_scan_alone_preserves_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = DuplicateBinRepository(root)
            projection = _projection(root, "scan-one")
            scan = _scan(root, projection, "scan-one")
            store = OrganizationDecisionStore(repository)
            choice = projection.duplicate_choices_by_id["D1"]
            kept = store.update(
                "browser",
                scan,
                projection,
                scan_generation="scan-one",
                expected_revision=0,
                kind="choice",
                choice_id="D1",
                option_id="keep-all",
            )
            approved = store.approve(
                "browser",
                scan,
                projection,
                scan_generation="scan-one",
                expected_revision=kept.session.decision_revision,
            )
            self.assertIsNone(approved.duplicate_batch)
            self.assertFalse(repository.catalog_path.exists())

            replacement_store = OrganizationDecisionStore(repository)
            selected = _select_first_copy(replacement_store, "other", scan, projection)
            replacement_store.approve(
                "other",
                scan,
                projection,
                scan_generation="scan-one",
                expected_revision=selected.session.decision_revision,
            )
            active_before = repository.list_active_approved_batches()
            new_projection = _projection(root, "scan-two")
            replacement_store.current(
                "other",
                _scan(root, new_projection, "scan-two"),
                new_projection,
            )
            self.assertEqual(repository.list_active_approved_batches(), active_before)
            self.assertEqual(choice.option_ids, ("keep-all", "choose-for-bin"))

    def test_explicit_later_approval_supersedes_unstarted_but_not_started_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = DuplicateBinRepository(root)
            store = OrganizationDecisionStore(repository)
            first_projection = _projection(root, "scan-one")
            first_scan = _scan(root, first_projection, "scan-one")
            first_selected = _select_first_copy(
                store, "browser", first_scan, first_projection
            )
            first_approved = store.approve(
                "browser",
                first_scan,
                first_projection,
                scan_generation="scan-one",
                expected_revision=first_selected.session.decision_revision,
            )
            assert first_approved.duplicate_batch is not None

            second_projection = _projection(root, "scan-two")
            second_scan = _scan(root, second_projection, "scan-two")
            second_selected = _select_first_copy(
                store, "browser", second_scan, second_projection
            )
            second_approved = store.approve(
                "browser",
                second_scan,
                second_projection,
                scan_generation="scan-two",
                expected_revision=second_selected.session.decision_revision,
            )
            assert second_approved.duplicate_batch is not None
            self.assertEqual(
                repository.load(first_approved.duplicate_batch.id).status,
                "superseded",
            )
            self.assertEqual(
                repository.list_active_approved_batches()[0].batch_id,
                second_approved.duplicate_batch.id,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = DuplicateBinRepository(root)
            store = OrganizationDecisionStore(repository)
            first_projection = _projection(root, "scan-one")
            first_scan = _scan(root, first_projection, "scan-one")
            selected = _select_first_copy(
                store, "browser", first_scan, first_projection
            )
            approved = store.approve(
                "browser",
                first_scan,
                first_projection,
                scan_generation="scan-one",
                expected_revision=selected.session.decision_revision,
            )
            assert approved.duplicate_batch is not None
            repository.transition_status(
                approved.duplicate_batch.id,
                expected_version=1,
                status="moving",
                updated_at="2026-07-16T10:03:00+00:00",
            )
            second_projection = _projection(root, "scan-two")
            second_scan = _scan(root, second_projection, "scan-two")
            selected_again = _select_first_copy(
                store, "browser", second_scan, second_projection
            )
            with self.assertRaisesRegex(
                OrganizationDecisionError, "duplicate_batch_conflict"
            ):
                store.approve(
                    "browser",
                    second_scan,
                    second_projection,
                    scan_generation="scan-two",
                    expected_revision=selected_again.session.decision_revision,
                )
            self.assertEqual(
                store.current("browser", second_scan, second_projection).session.status,
                "ready_for_approval",
            )


class _FailingRepository(DuplicateBinRepository):
    def create_or_recover_approved_batch(self, candidate):
        del candidate
        raise DuplicateBinRepositoryError("duplicate_bin_catalog_write_failed")


class _FailOnceDictionary(dict):
    def __init__(self, values):
        super().__init__(values)
        self.failed = False

    def __setitem__(self, key, value):
        if not self.failed:
            self.failed = True
            raise RuntimeError("publication failed")
        return super().__setitem__(key, value)


def _scan(root: Path, projection, generation: str) -> ScanJobSnapshot:
    del projection
    return ScanJobSnapshot(
        status="completed",
        job_id=generation,
        completed_at=COMPLETED_AT,
        result=scan_root(root),
    )


def _approved_plan(projection, *, session_mutations: int, selected_index: int = 0):
    session = create_organization_decision_session(projection, timestamp=APPROVED_AT)
    duplicate = projection.duplicate_choices_by_id["D1"]
    if session_mutations == 3:
        session = update_organization_decision(
            session,
            projection,
            kind="choice",
            choice_id="D1",
            option_id="keep-all",
        )
        session = update_organization_decision(
            session,
            projection,
            kind="choice",
            choice_id="D1",
            option_id="choose-for-bin",
            copy_ids=(duplicate.copy_ids[1],),
        )
        session = update_organization_decision(
            session,
            projection,
            kind="choice",
            choice_id="D1",
            option_id="choose-for-bin",
            copy_ids=(duplicate.copy_ids[selected_index],),
        )
    else:
        session = update_organization_decision(
            session,
            projection,
            kind="choice",
            choice_id="D1",
            option_id="choose-for-bin",
            copy_ids=(duplicate.copy_ids[selected_index],),
        )
    approved = approve_organization_plan(session, projection, timestamp=APPROVED_AT)
    assert approved.approved_plan is not None
    return approved.approved_plan


def _select_first_copy(
    store, browser, scan, projection
) -> OrganizationDecisionSnapshot:
    duplicate = projection.duplicate_choices_by_id["D1"]
    return store.update(
        browser,
        scan,
        projection,
        scan_generation=projection.scan_generation,
        expected_revision=0,
        kind="choice",
        choice_id="D1",
        option_id="choose-for-bin",
        copy_ids=(duplicate.copy_ids[0],),
    )


def _prepare_duplicates(root: Path) -> None:
    (root / "Inbox").mkdir(exist_ok=True)
    (root / "Archive").mkdir(exist_ok=True)
    for path in (root / "Inbox" / "copy.txt", root / "Archive" / "copy.txt"):
        if not path.exists():
            path.write_text("same", encoding="utf-8")


def _projection(root: Path, generation: str):
    _prepare_duplicates(root)
    return build_internal_organization_projection(
        scan_result=scan_root(root),
        scan_generation=generation,
        completed_at=COMPLETED_AT,
    )


if __name__ == "__main__":
    unittest.main()
