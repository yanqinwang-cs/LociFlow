from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from organizer.application.organization_movement_preflight import (
    OrganizationMovementPreflightError,
    preflight_organization_movement,
)
from tests.test_application_organization_identity_execution import _inputs


class OrganizationMovementPreflightTests(unittest.TestCase):
    def test_complete_preflight_is_ordered_read_only_and_includes_volume_admission(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, _authorization, repository = _inputs(
                root,
                ("alpha.txt", "beta.txt"),
            )
            volume = mock.Mock(root=root)

            result = preflight_organization_movement(
                plan,
                root,
                volume_admission=volume,
            )

            volume.inspect_and_require_allowed.assert_called_once_with(plan)
            self.assertEqual(
                tuple(item.change_id for item in result.expected_moves),
                ("O1", "O2"),
            )
            self.assertEqual(
                tuple(item.source for item in result.expected_moves),
                ("Inbox/alpha.txt", "Inbox/beta.txt"),
            )
            self.assertTrue(
                all(item.content_identity is None for item in result.expected_moves)
            )
            self.assertFalse((root / "Documents").exists())
            self.assertFalse(repository.directory.exists())

    def test_cardinality_duplicates_and_overlap_fail_without_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, _authorization, repository = _inputs(
                root,
                ("alpha.txt", "beta.txt"),
            )
            first, second = plan.reviewed_move_items
            cases = (
                replace(plan, source_fingerprints=plan.source_fingerprints[:1]),
                replace(
                    plan,
                    reviewed_move_items=(
                        first,
                        replace(second, id=first.id),
                    ),
                ),
                replace(
                    plan,
                    reviewed_move_items=(
                        replace(
                            first,
                            plan_item=replace(
                                first.plan_item,
                                destination=second.plan_item.source,
                            ),
                        ),
                        second,
                    ),
                ),
            )

            for candidate in cases:
                with self.subTest(candidate=candidate.reviewed_move_items):
                    volume = mock.Mock(root=root)
                    with self.assertRaises(OrganizationMovementPreflightError):
                        preflight_organization_movement(
                            candidate,
                            root,
                            volume_admission=volume,
                        )
                    self.assertFalse(repository.directory.exists())
                    self.assertFalse((root / "Documents").exists())

    def test_shared_owner_contains_no_hashing_or_directory_creation(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "src"
            / "organizer"
            / "application"
            / "organization_movement_preflight.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "file_identity_capture",
            "sha256",
            ".mkdir(",
            "repository.create",
            "apply_move_plan",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
