from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from organizer.application.organization_decision_service import (
    OrganizationDecisionError,
    accept_recommended_changes,
    approve_organization_plan,
    build_organization_approval_summary,
    create_organization_decision_session,
    organization_review_to_dict,
    update_organization_decision,
)
from organizer.application.organization_slice_service import (
    build_internal_organization_projection,
)
from organizer.application.scan_service import scan_root
from organizer.models import ReviewedPlanItem
from organizer.web.organization_decision_store import OrganizationDecisionStore
from organizer.web.scan_jobs import ScanJobSnapshot

COMPLETED_AT = datetime(2026, 7, 15, 12, tzinfo=UTC)


class OrganizationDecisionServiceTests(unittest.TestCase):
    def test_internal_projection_retains_authoritative_candidates_without_public_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _recommendation_result(root)
            projection = _projection(result)

            self.assertEqual(
                set(projection.recommendations_by_id),
                {item.id for item in projection.public_slice.recommended_changes},
            )
            candidate = projection.recommendations_by_id["O1"]
            self.assertTrue(candidate.move_item.destination.is_absolute())
            self.assertEqual(candidate.file_id.split(":", 1)[0], "scan-one")
            public = json.dumps(projection.public_slice.to_dict()).lower()
            self.assertNotIn(str(root).lower(), public)
            self.assertNotIn("sha256", public)

    def test_destination_and_duplicate_mappings_retain_exact_server_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination_result = _destination_choice_result(root)
            destination_projection = _projection(destination_result)
            choice = destination_projection.destination_choices_by_id["C1"]
            self.assertEqual(
                [option_id for option_id, _move in choice.options],
                ["destination-1", "keep"],
            )
            self.assertIsNotNone(dict(choice.options)["destination-1"])
            self.assertIsNone(dict(choice.options)["keep"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate_projection = _projection(_duplicate_result(root))
            duplicate = duplicate_projection.duplicate_choices_by_id["D1"]
            self.assertEqual(len(duplicate.sha256), 64)
            self.assertEqual(len(duplicate.copy_ids), 2)
            self.assertEqual(duplicate.option_ids, ("keep-all", "choose-for-bin"))

    def test_recommendations_update_immutably_and_bulk_accept_one_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projection = _projection(_recommendation_result(Path(directory)))
            session = create_organization_decision_session(
                projection,
                timestamp="2026-07-15T12:00:00+00:00",
            )
            self.assertEqual(session.decision_revision, 0)
            self.assertEqual(session.status, "in_progress")

            first = update_organization_decision(
                session,
                projection,
                kind="recommendation",
                change_id="O1",
                decision="keep",
            )
            self.assertEqual(first.decision_revision, 1)
            self.assertEqual(session.recommendation_decisions[0].decision, "undecided")
            pending = tuple(
                item.change_id
                for item in first.recommendation_decisions
                if item.decision == "undecided"
            )
            completed = accept_recommended_changes(first, projection, pending)
            summary = build_organization_approval_summary(completed, projection)
            self.assertEqual(completed.decision_revision, 2)
            self.assertEqual(summary.files_to_move, 1)
            self.assertEqual(summary.kept_recommendations, 1)
            self.assertTrue(summary.ready_for_approval)

            with self.assertRaisesRegex(
                OrganizationDecisionError,
                "recommendation_set_changed",
            ):
                accept_recommended_changes(first, projection, ())
            self.assertEqual(first.decision_revision, 1)

    def test_destination_and_duplicate_decisions_validate_only_offered_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projection = _projection(_destination_choice_result(Path(directory)))
            session = create_organization_decision_session(projection)
            moved = update_organization_decision(
                session,
                projection,
                kind="choice",
                choice_id="C1",
                option_id="destination-1",
            )
            self.assertEqual(moved.destination_decisions[0].option_id, "destination-1")
            moved_summary = build_organization_approval_summary(moved, projection)
            self.assertEqual(moved_summary.files_to_move, 1)
            moved_complete = update_organization_decision(
                moved,
                projection,
                kind="choice",
                choice_id="C2",
                option_id="keep",
            )
            approved_move = approve_organization_plan(moved_complete, projection)
            assert approved_move.approved_plan is not None
            self.assertEqual(
                approved_move.approved_plan.reviewed_move_items[0].plan_item,
                dict(projection.destination_choices_by_id["C1"].options)[
                    "destination-1"
                ],
            )
            kept = update_organization_decision(
                session,
                projection,
                kind="choice",
                choice_id="C1",
                option_id="keep",
            )
            kept = update_organization_decision(
                kept,
                projection,
                kind="choice",
                choice_id="C2",
                option_id="keep",
            )
            self.assertEqual(
                build_organization_approval_summary(kept, projection).files_to_move,
                0,
            )
            with self.assertRaisesRegex(OrganizationDecisionError, "invalid_option"):
                update_organization_decision(
                    session,
                    projection,
                    kind="choice",
                    choice_id="C1",
                    option_id="destination-99",
                )

        with tempfile.TemporaryDirectory() as directory:
            projection = _projection(_duplicate_result(Path(directory)))
            session = create_organization_decision_session(projection)
            offered = projection.duplicate_choices_by_id["D1"].copy_ids
            selected = update_organization_decision(
                session,
                projection,
                kind="choice",
                choice_id="D1",
                option_id="choose-for-bin",
                copy_ids=(offered[0],),
            )
            self.assertEqual(selected.duplicate_decisions[0].selected_copy_ids, (offered[0],))
            kept = update_organization_decision(
                session,
                projection,
                kind="choice",
                choice_id="D1",
                option_id="keep-all",
            )
            self.assertEqual(kept.duplicate_decisions[0].selected_copy_ids, ())
            invalid_sets = ((), offered, (offered[0], offered[0]), ("other:file",))
            for copy_ids in invalid_sets:
                with self.subTest(copy_ids=copy_ids), self.assertRaisesRegex(
                    OrganizationDecisionError,
                    "invalid_option",
                ):
                    update_organization_decision(
                        session,
                        projection,
                        kind="choice",
                        choice_id="D1",
                        option_id="choose-for-bin",
                        copy_ids=copy_ids,
                    )
            with self.assertRaisesRegex(OrganizationDecisionError, "invalid_option"):
                update_organization_decision(
                    session,
                    projection,
                    kind="choice",
                    choice_id="D1",
                    option_id="keep-all",
                    copy_ids=(offered[0],),
                )

    def test_approval_materializes_reviewed_moves_and_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projection = _projection(_recommendation_result(Path(directory)))
            session = create_organization_decision_session(projection)
            pending = tuple(item.change_id for item in session.recommendation_decisions)
            completed = accept_recommended_changes(session, projection, pending)
            approved = approve_organization_plan(
                completed,
                projection,
                timestamp="2026-07-15T13:00:00+00:00",
            )

            self.assertEqual(approved.status, "ready_to_move")
            self.assertEqual(approved.decision_revision, 1)
            assert approved.approved_plan is not None
            self.assertTrue(
                all(
                    isinstance(item, ReviewedPlanItem)
                    for item in approved.approved_plan.reviewed_move_items
                )
            )
            self.assertEqual(len(approved.approved_plan.reviewed_move_items), 2)
            safe = json.dumps(organization_review_to_dict(approved, projection)).lower()
            self.assertNotIn(str(Path(directory)).lower(), safe)
            self.assertNotIn("sha256", safe)
            with self.assertRaises(FrozenInstanceError):
                approved.status = "changed"  # type: ignore[misc]
            self.assertFalse((Path(directory) / "AI_Review" / "operation_logs").exists())

    def test_approval_rejects_incomplete_changed_source_and_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = _projection(_recommendation_result(root))
            session = create_organization_decision_session(projection)
            with self.assertRaisesRegex(OrganizationDecisionError, "incomplete_decisions"):
                approve_organization_plan(session, projection)

            completed = accept_recommended_changes(
                session,
                projection,
                tuple(item.change_id for item in session.recommendation_decisions),
            )
            first_source = projection.recommendations_by_id["O1"].source_metadata.path
            first_source.write_text("changed size", encoding="utf-8")
            with self.assertRaisesRegex(OrganizationDecisionError, "source_changed"):
                approve_organization_plan(completed, projection)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = _projection(_recommendation_result(root))
            session = create_organization_decision_session(projection)
            completed = accept_recommended_changes(
                session,
                projection,
                tuple(item.change_id for item in session.recommendation_decisions),
            )
            destination = projection.recommendations_by_id["O1"].move_item.destination
            destination.write_text("collision", encoding="utf-8")
            with self.assertRaisesRegex(OrganizationDecisionError, "overwrite_detected"):
                approve_organization_plan(completed, projection)

    def test_approval_rejects_missing_mtime_changed_symlink_and_outside_sources(self) -> None:
        for mutation in ("missing", "mtime", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                projection = _projection(_recommendation_result(root))
                session = create_organization_decision_session(projection)
                completed = accept_recommended_changes(
                    session,
                    projection,
                    tuple(projection.recommendations_by_id),
                )
                source = projection.recommendations_by_id["O1"].source_metadata.path
                if mutation == "missing":
                    source.unlink()
                elif mutation == "mtime":
                    scanned_mtime = source.stat().st_mtime
                    os.utime(source, (scanned_mtime + 10, scanned_mtime + 10))
                else:
                    source.unlink()
                    replacement = root / "replacement.pptx"
                    replacement.write_text("a", encoding="utf-8")
                    source.symlink_to(replacement)
                with self.assertRaisesRegex(OrganizationDecisionError, "source_changed"):
                    approve_organization_plan(completed, projection)

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            projection = _projection(_recommendation_result(root))
            session = create_organization_decision_session(projection)
            completed = accept_recommended_changes(
                session,
                projection,
                tuple(projection.recommendations_by_id),
            )
            candidate = projection.recommendations_by_id["O1"]
            outside_source = Path(outside) / candidate.source_metadata.name
            outside_source.write_bytes(candidate.source_metadata.path.read_bytes())
            outside_metadata = replace(candidate.source_metadata, path=outside_source)
            candidates = dict(projection.recommendations_by_id)
            candidates["O1"] = replace(candidate, source_metadata=outside_metadata)
            forged = replace(
                projection,
                recommendations_by_id=MappingProxyType(candidates),
            )
            with self.assertRaisesRegex(OrganizationDecisionError, "source_changed"):
                approve_organization_plan(completed, forged)

    def test_approval_rejects_unsafe_destinations_protection_and_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            projection = _projection(_recommendation_result(root))
            completed = accept_recommended_changes(
                create_organization_decision_session(projection),
                projection,
                tuple(projection.recommendations_by_id),
            )
            candidate = projection.recommendations_by_id["O1"]
            candidates = dict(projection.recommendations_by_id)
            candidates["O1"] = replace(
                candidate,
                move_item=replace(
                    candidate.move_item,
                    destination=Path(outside) / candidate.source_metadata.name,
                ),
            )
            forged = replace(
                projection,
                recommendations_by_id=MappingProxyType(candidates),
            )
            with self.assertRaisesRegex(OrganizationDecisionError, "unsafe_destination"):
                approve_organization_plan(completed, forged)

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            projection = _projection(_recommendation_result(root))
            completed = accept_recommended_changes(
                create_organization_decision_session(projection),
                projection,
                tuple(projection.recommendations_by_id),
            )
            destination_parent = projection.recommendations_by_id[
                "O1"
            ].move_item.destination.parent
            destination_parent.rmdir()
            destination_parent.symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaisesRegex(OrganizationDecisionError, "unsafe_destination"):
                approve_organization_plan(completed, projection)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = _projection(_recommendation_result(root))
            completed = accept_recommended_changes(
                create_organization_decision_session(projection),
                projection,
                tuple(projection.recommendations_by_id),
            )
            protected = replace(
                projection,
                protected_item_ids=frozenset(
                    {projection.recommendations_by_id["O1"].file_id}
                ),
            )
            with self.assertRaisesRegex(OrganizationDecisionError, "protected_item_selected"):
                approve_organization_plan(completed, protected)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = _projection(_recommendation_result(root))
            completed = accept_recommended_changes(
                create_organization_decision_session(projection),
                projection,
                tuple(projection.recommendations_by_id),
            )
            candidates = dict(projection.recommendations_by_id)
            first_destination = candidates["O1"].move_item.destination
            candidates["O2"] = replace(
                candidates["O2"],
                move_item=replace(
                    candidates["O2"].move_item,
                    destination=first_destination,
                ),
            )
            conflicting = replace(
                projection,
                recommendations_by_id=MappingProxyType(candidates),
            )
            with self.assertRaisesRegex(OrganizationDecisionError, "unsafe_destination"):
                approve_organization_plan(completed, conflicting)

    def test_duplicate_approval_rehashes_and_rejects_changed_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = _projection(_duplicate_result(root))
            session = create_organization_decision_session(projection)
            duplicate = projection.duplicate_choices_by_id["D1"]
            completed = update_organization_decision(
                session,
                projection,
                kind="choice",
                choice_id="D1",
                option_id="choose-for-bin",
                copy_ids=(duplicate.copy_ids[0],),
            )
            approved = approve_organization_plan(completed, projection)
            assert approved.approved_plan is not None
            self.assertEqual(
                approved.approved_plan.summary.duplicate_copies_for_future_bin,
                1,
            )

            duplicate.files[0].path.write_text("different", encoding="utf-8")
            with self.assertRaisesRegex(
                OrganizationDecisionError,
                "duplicate_group_changed",
            ):
                approve_organization_plan(completed, projection)

    def test_store_isolates_sessions_rejects_stale_state_and_replays_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _recommendation_result(root)
            projection = _projection(result)
            scan = _scan(result, "scan-one")
            store = OrganizationDecisionStore()
            first = store.update(
                "browser-one",
                scan,
                projection,
                scan_generation="scan-one",
                expected_revision=0,
                kind="recommendation",
                change_id="O1",
                decision="keep",
            )
            second = store.current("browser-two", scan, projection)
            self.assertEqual(first.session.decision_revision, 1)
            self.assertEqual(second.session.decision_revision, 0)

            with self.assertRaisesRegex(
                OrganizationDecisionError,
                "decision_revision_mismatch",
            ):
                store.update(
                    "browser-one",
                    scan,
                    projection,
                    scan_generation="scan-one",
                    expected_revision=0,
                    kind="recommendation",
                    change_id="O2",
                    decision="accept",
                )
            with self.assertRaisesRegex(
                OrganizationDecisionError,
                "scan_generation_mismatch",
            ):
                store.update(
                    "browser-one",
                    replace(scan, job_id="scan-two"),
                    projection,
                    scan_generation="scan-one",
                    expected_revision=1,
                    kind="recommendation",
                    change_id="O2",
                    decision="accept",
                )

            all_ids = tuple(projection.recommendations_by_id)
            ready_session = accept_recommended_changes(
                create_organization_decision_session(projection),
                projection,
                all_ids,
            )
            store._sessions["ready"] = replace(
                store.current("ready", scan, projection),
                session=ready_session,
            )
            ready = store.approve(
                "ready",
                scan,
                projection,
                scan_generation="scan-one",
                expected_revision=1,
            )
            replay = store.approve(
                "ready",
                scan,
                projection,
                scan_generation="scan-one",
                expected_revision=1,
            )
            self.assertIs(ready, replay)
            with self.assertRaisesRegex(
                OrganizationDecisionError,
                "approval_already_completed",
            ):
                store.approve(
                    "ready",
                    scan,
                    projection,
                    scan_generation="scan-one",
                    expected_revision=0,
                )

            projection_two = _projection(result, generation="scan-two")
            fresh = store.current(
                "ready",
                _scan(result, "scan-two"),
                projection_two,
            )
            self.assertEqual(fresh.session.decision_revision, 0)
            self.assertEqual(fresh.session.status, "in_progress")


def _recommendation_result(root: Path):
    destination = root / "Organized" / "EvoSim_project_slides" / "slides"
    destination.mkdir(parents=True)
    (root / "EvoSim_project_slides.pptx").write_text("a", encoding="utf-8")
    (root / "EvoSim_project_slides_final.pptx").write_text("b", encoding="utf-8")
    return scan_root(root)


def _destination_choice_result(root: Path):
    result = _recommendation_result(root)
    report = deepcopy(result.report)
    report["anchor_decisions"]["suggested_groups"][0]["evidence"] = "deterministic_group"
    return replace(result, report=report)


def _duplicate_result(root: Path):
    (root / "Inbox").mkdir()
    (root / "Archive").mkdir()
    (root / "Inbox" / "copy.txt").write_text("same", encoding="utf-8")
    (root / "Archive" / "copy.txt").write_text("same", encoding="utf-8")
    return scan_root(root)


def _projection(result, *, generation: str = "scan-one"):
    return build_internal_organization_projection(
        scan_result=result,
        scan_generation=generation,
        completed_at=COMPLETED_AT,
    )


def _scan(result, generation: str) -> ScanJobSnapshot:
    return ScanJobSnapshot(
        status="completed",
        job_id=generation,
        completed_at=COMPLETED_AT,
        result=result,
    )


if __name__ == "__main__":
    unittest.main()
