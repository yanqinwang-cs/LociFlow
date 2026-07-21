from __future__ import annotations

from dataclasses import dataclass

from organizer.application.recovery_evidence import (
    RecoveryActionability,
    RecoveryEvidenceInspector,
    RecoveryEvidenceState,
)


@dataclass(frozen=True)
class BrowserRecoveryProjection:
    state: str
    title: str
    message: str
    affected_items: int

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "title": self.title,
            "message": self.message,
            "affected_items": self.affected_items,
        }


_CLEAR = BrowserRecoveryProjection(
    state="clear",
    title="No unfinished file work was found.",
    message="Loci Flow did not find file work that needs review.",
    affected_items=0,
)
_CANNOT_CONTINUE = BrowserRecoveryProjection(
    state="cannot_continue_automatically",
    title="Loci Flow can’t continue this file operation automatically.",
    message="New file changes remain paused. Get help before changing these files.",
    affected_items=0,
)


class RecoveryProjectionService:
    """Project explicit recovery evidence without creating recovery authority."""

    def __init__(self, inspector: RecoveryEvidenceInspector) -> None:
        self.inspector = inspector

    def current(self) -> BrowserRecoveryProjection:
        try:
            result = self.inspector.inspect()
        except Exception:
            return _CANNOT_CONTINUE

        if result.state is RecoveryEvidenceState.CLEAR:
            return _CLEAR
        if result.case is None:
            return _CANNOT_CONTINUE
        if result.actionability in {
            RecoveryActionability.NON_ACTIONABLE_MALFORMED,
            RecoveryActionability.NON_ACTIONABLE_INCONSISTENT,
            RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS,
            RecoveryActionability.MULTIPLE_UNRESOLVED_OPERATIONS,
        }:
            return _CANNOT_CONTINUE

        if result.actionability in {
            RecoveryActionability.CHECK_RESULT_POSSIBLE,
            RecoveryActionability.CHECK_OR_CONTINUE_POSSIBLE,
        }:
            message = "Loci Flow can check what happened before you continue."
        elif result.actionability is RecoveryActionability.CONTINUE_POSSIBLE:
            message = (
                "Loci Flow found recorded file work that may be able to continue safely."
            )
        else:
            message = "Review what happened before continuing."

        return BrowserRecoveryProjection(
            state="needs_attention",
            title="File changes need attention.",
            message=message,
            affected_items=result.case.affected_items,
        )
