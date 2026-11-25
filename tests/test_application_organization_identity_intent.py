from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.execution_coordinator import (
    approved_plan_identity,
)
from organizer.application.file_identity_capture import (
    CapturedRegularFileIdentity,
    RegularFileStatFingerprint,
)
from organizer.application.organization_decision_models import (
    ApprovedOrganizationPlan,
    OrganizationApprovalSummary,
)
from organizer.application.organization_identity_intent import (
    build_v2_organization_intent,
)
from organizer.application.organization_operation_models import (
    MovementAuthorization,
    OrganizationExpectedMove,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.models import MovePlanItem, ReviewedPlanItem


class OrganizationIdentityIntentTests(unittest.TestCase):
    def test_builds_deterministic_complete_v2_intent_in_approved_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, authorization, moves, captures, root_identity = _inputs(root)
            operation_id = "11111111-1111-4111-8111-111111111111"
            created_at = "2026-07-21T04:00:00+00:00"

            first = build_v2_organization_intent(
                operation_id=operation_id,
                created_at=created_at,
                authorization=authorization,
                approved_plan=plan,
                configured_root_identity=root_identity,
                expected_moves=moves,
                captures=reversed(tuple(captures.items())),
            )
            second = build_v2_organization_intent(
                operation_id=operation_id,
                created_at=created_at,
                authorization=authorization,
                approved_plan=plan,
                configured_root_identity=root_identity,
                expected_moves=moves,
                captures=captures,
            )

            self.assertEqual(first, second)
            self.assertEqual(first.record.schema_version, 2)
            self.assertEqual(first.record.operation_id, operation_id)
            self.assertEqual(first.record.created_at, created_at)
            self.assertEqual(first.record.status, "prepared")
            self.assertEqual(
                [item.change_id for item in first.record.expected_moves],
                ["O1", "O2"],
            )
            self.assertEqual(
                [item.content_identity.digest for item in first.record.expected_moves],
                ["a" * 64, "b" * 64],
            )
            self.assertEqual(
                [item.change_id for item in first.executor_evidence],
                ["O1", "O2"],
            )
            self.assertEqual(
                [item.stat_fingerprint.inode for item in first.executor_evidence],
                [1, 2],
            )

    def test_exact_capture_coverage_and_unique_change_ids_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, authorization, moves, captures, root_identity = _inputs(root)
            cases = (
                {"O1": captures["O1"]},
                {**captures, "O3": captures["O1"]},
                [("O1", captures["O1"]), ("O1", captures["O1"]), ("O2", captures["O2"])],
            )
            for candidate in cases:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    _build(plan, authorization, moves, candidate, root_identity)

            duplicate_moves = (moves[0], replace(moves[1], change_id="O1"))
            with self.assertRaises(ValueError):
                _build(plan, authorization, duplicate_moves, captures, root_identity)

    def test_rejects_reordered_mixed_and_unsupported_operation_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, authorization, moves, captures, root_identity = _inputs(root)
            candidates = (
                tuple(reversed(moves)),
                (replace(moves[0], content_identity=captures["O1"].content_identity), moves[1]),
                (replace(moves[0], result_state="moved"), moves[1]),
            )
            for candidate in candidates:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    _build(plan, authorization, candidate, captures, root_identity)

            unsupported_plan = replace(
                plan,
                reviewed_move_items=(
                    replace(plan.reviewed_move_items[0], category="duplicate"),
                    plan.reviewed_move_items[1],
                ),
            )
            unsupported_authorization = replace(
                authorization,
                approved_plan_identity=approved_plan_identity(unsupported_plan),
            )
            with self.assertRaises(ValueError):
                _build(unsupported_plan, unsupported_authorization, moves, captures, root_identity)

    def test_rejects_invalid_identity_authorization_root_uuid_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, authorization, moves, captures, root_identity = _inputs(root)
            with self.assertRaises(ValueError):
                replace(
                    captures["O1"].stat_fingerprint,
                    file_type="directory",
                )
            malformed_capture = replace(captures["O1"], content_identity="not-an-identity")
            with self.assertRaises(ValueError):
                _build(plan, authorization, moves, {**captures, "O1": malformed_capture}, root_identity)
            changed_after_preflight = replace(
                captures["O1"],
                stat_fingerprint=replace(
                    captures["O1"].stat_fingerprint,
                    size_bytes=999,
                ),
            )
            with self.assertRaises(ValueError):
                _build(
                    plan,
                    authorization,
                    moves,
                    {**captures, "O1": changed_after_preflight},
                    root_identity,
                )
            for changed_authorization in (
                replace(authorization, status="consumed", consumed_at="now"),
                replace(authorization, decision_revision=99),
                replace(authorization, configured_root_identity="c" * 64),
            ):
                with self.assertRaises(ValueError):
                    _build(plan, changed_authorization, moves, captures, root_identity)
            with self.assertRaises(ValueError):
                _build(plan, authorization, moves, captures, "not-a-root-identity")
            with self.assertRaises(ValueError):
                _build(plan, authorization, moves, captures, root_identity, operation_id="not-a-uuid")
            with self.assertRaises(ValueError):
                _build(plan, authorization, moves, captures, root_identity, created_at="")

    def test_builder_has_no_repository_executor_or_authorization_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, authorization, moves, captures, root_identity = _inputs(root)
            repository = OrganizationOperationRepository(root)
            original = authorization

            with (
                mock.patch.object(repository, "create") as create,
                mock.patch("organizer.executor.apply_move_plan") as execute,
            ):
                _build(plan, authorization, moves, captures, root_identity)

            create.assert_not_called()
            execute.assert_not_called()
            self.assertEqual(authorization, original)
            self.assertFalse(repository.directory.exists())

    def test_production_execution_modules_do_not_import_capture_or_v2_builder(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        protected = (
            repository_root / "src/organizer/application/execution_service.py",
            repository_root / "src/organizer/application/organization_movement_service.py",
            repository_root / "src/organizer/application/undo_service.py",
        )
        forbidden = ("file_identity_capture", "organization_identity_intent")
        for path in protected:
            source = path.read_text(encoding="utf-8")
            for name in forbidden:
                self.assertNotIn(name, source)


def _build(
    plan,
    authorization,
    moves,
    captures,
    root_identity,
    *,
    operation_id="11111111-1111-4111-8111-111111111111",
    created_at="2026-07-21T04:00:00+00:00",
):
    return build_v2_organization_intent(
        operation_id=operation_id,
        created_at=created_at,
        authorization=authorization,
        approved_plan=plan,
        configured_root_identity=root_identity,
        expected_moves=moves,
        captures=captures,
    )


def _inputs(root: Path):
    root_identity = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    reviewed = tuple(
        ReviewedPlanItem(
            id=f"O{index}",
            category="organization",
            plan_item=MovePlanItem(
                source=root / f"source-{index}.txt",
                destination=root / "Organized" / f"source-{index}.txt",
                reason="organize",
                confidence=90,
                operation="dry-run move",
                overwrite_risk=False,
            ),
            decision="approved",
        )
        for index in (1, 2)
    )
    plan = ApprovedOrganizationPlan(
        status="ready_to_move",
        scan_generation="scan-one",
        decision_revision=3,
        approved_at="2026-07-21T03:00:00+00:00",
        reviewed_move_items=reviewed,
        duplicate_decisions=(),
        source_fingerprints=(),
        summary=OrganizationApprovalSummary(0, 0, 2, 0, 0, 0, True, 2),
    )
    authorization = MovementAuthorization(
        authorization_id=str(uuid.uuid4()),
        scan_generation=plan.scan_generation,
        decision_revision=plan.decision_revision,
        approved_at=plan.approved_at,
        approved_plan_identity=approved_plan_identity(plan),
        issued_at="2026-07-21T03:30:00+00:00",
        consumed_at=None,
        status="available",
        browser_session_id="session",
        configured_root_identity=root_identity,
    )
    moves = tuple(
        OrganizationExpectedMove(
            change_id=f"O{index}",
            source=f"source-{index}.txt",
            destination=f"Organized/source-{index}.txt",
            source_size_bytes=index,
            source_modified_time=float(index),
            reason="organize",
            confidence=90,
            operation="dry-run move",
            overwrite_risk=False,
        )
        for index in (1, 2)
    )
    captures = {
        f"O{index}": CapturedRegularFileIdentity(
            content_identity=RegularFileContentIdentity(
                "regular_file", "sha256", character * 64
            ),
            stat_fingerprint=RegularFileStatFingerprint(
                file_type="regular_file",
                device=1,
                inode=index,
                size_bytes=index,
                modified_time_ns=index * 1_000_000_000,
                changed_time_ns=index,
            ),
        )
        for index, character in ((1, "a"), (2, "b"))
    }
    return plan, authorization, moves, captures, root_identity


if __name__ == "__main__":
    unittest.main()
