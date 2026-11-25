from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from organizer.application.recovery_evidence import (
    RecoveryActionability,
    RecoveryEvidenceState,
)
from organizer.application.recovery_projection_service import (
    RecoveryProjectionService,
)


class RecoveryProjectionServiceTests(unittest.TestCase):
    def test_clear_projection_has_exact_safe_contract(self) -> None:
        projection = _project(RecoveryEvidenceState.CLEAR, None, None)
        self.assertEqual(
            projection.to_dict(),
            {
                "state": "clear",
                "title": "No unfinished file work was found.",
                "message": "Loci Flow did not find file work that needs review.",
                "affected_items": 0,
                "can_check_result": False,
            },
        )

    def test_check_and_inspect_only_copy_are_explanatory(self) -> None:
        check = _project(
            RecoveryEvidenceState.UNRESOLVED,
            RecoveryActionability.CHECK_RESULT_POSSIBLE,
            2,
            eligible=True,
        )
        inspect_only = _project(
            RecoveryEvidenceState.UNRESOLVED,
            RecoveryActionability.INSPECT_ONLY,
            1,
        )
        self.assertEqual(
            check.message,
            "Loci Flow can check what happened before you continue.",
        )
        self.assertEqual(inspect_only.title, "File changes need attention.")
        self.assertEqual(
            inspect_only.message, "Review what happened before continuing."
        )
        self.assertNotIn("can check", inspect_only.message)
        self.assertEqual(check.affected_items, 2)
        self.assertTrue(check.can_check_result)
        self.assertFalse(inspect_only.can_check_result)

    def test_possible_action_enum_does_not_create_capability(self) -> None:
        projection = _project(
            RecoveryEvidenceState.UNRESOLVED,
            RecoveryActionability.CONTINUE_POSSIBLE,
            3,
        ).to_dict()
        self.assertEqual(
            projection["message"], "Review what happened before continuing."
        )
        self.assertFalse(projection["can_check_result"])
        self.assertEqual(
            set(projection),
            {"state", "title", "message", "affected_items", "can_check_result"},
        )

    def test_non_actionable_and_multiple_results_have_zero_count(self) -> None:
        for actionability in (
            RecoveryActionability.NON_ACTIONABLE_MALFORMED,
            RecoveryActionability.NON_ACTIONABLE_INCONSISTENT,
            RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS,
            RecoveryActionability.MULTIPLE_UNRESOLVED_OPERATIONS,
        ):
            with self.subTest(actionability=actionability):
                projection = _project(
                    RecoveryEvidenceState.NON_ACTIONABLE,
                    actionability,
                    None,
                )
                self.assertEqual(projection.state, "cannot_continue_automatically")
                self.assertEqual(projection.affected_items, 0)
                self.assertFalse(projection.can_check_result)

    def test_unexpected_inspection_failure_is_safe(self) -> None:
        inspector = mock.Mock()
        inspector.inspect.side_effect = RuntimeError("/private/root secret.json")
        eligibility = mock.Mock()
        projection = (
            RecoveryProjectionService(inspector, eligibility).current().to_dict()
        )
        self.assertEqual(projection["state"], "cannot_continue_automatically")
        self.assertFalse(projection["can_check_result"])
        eligibility.inspect.assert_not_called()
        self.assertNotIn("private", str(projection).lower())
        self.assertNotIn("secret", str(projection).lower())

    def test_eligibility_failure_is_safe_generic_and_read_only(self) -> None:
        inspector = mock.Mock()
        inspector.inspect.return_value = SimpleNamespace(
            state=RecoveryEvidenceState.UNRESOLVED,
            actionability=RecoveryActionability.CHECK_RESULT_POSSIBLE,
            case=SimpleNamespace(affected_items=2),
        )
        eligibility = mock.Mock()
        eligibility.inspect.side_effect = RuntimeError("/private/root secret.json")
        projection = RecoveryProjectionService(inspector, eligibility).current()
        self.assertFalse(projection.can_check_result)
        self.assertEqual(projection.message, "Review what happened before continuing.")
        self.assertEqual(eligibility.inspect.call_count, 1)


def _project(state, actionability, affected_items, *, eligible=False):
    inspector = mock.Mock()
    eligibility = mock.Mock()
    if eligible:
        eligibility.inspect.return_value = object()
    else:
        eligibility.inspect.side_effect = RuntimeError("not eligible")
    case = (
        None
        if affected_items is None
        else SimpleNamespace(affected_items=affected_items)
    )
    inspector.inspect.return_value = SimpleNamespace(
        state=state,
        actionability=actionability,
        case=case,
    )
    return RecoveryProjectionService(inspector, eligibility).current()


if __name__ == "__main__":
    unittest.main()
