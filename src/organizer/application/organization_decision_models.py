from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from organizer.models import ReviewedPlanItem


DECISION_UNDECIDED = "undecided"
RECOMMENDATION_ACCEPT = "accept"
RECOMMENDATION_KEEP = "keep"
STATUS_IN_PROGRESS = "in_progress"
STATUS_READY_FOR_APPROVAL = "ready_for_approval"
STATUS_READY_TO_MOVE = "ready_to_move"


@dataclass(frozen=True)
class RecommendationDecision:
    change_id: str
    decision: str = DECISION_UNDECIDED


@dataclass(frozen=True)
class DestinationChoiceDecision:
    choice_id: str
    option_id: str | None = None


@dataclass(frozen=True)
class DuplicateChoiceDecision:
    choice_id: str
    option_id: str | None = None
    selected_copy_ids: tuple[str, ...] = ()
    selected_paths: tuple[Path, ...] = ()
    group_sha256: str | None = None
    group_size_bytes: int | None = None


@dataclass(frozen=True)
class SourceFingerprint:
    item_id: str
    relative_path: Path
    size_bytes: int
    modified_time: float


@dataclass(frozen=True)
class OrganizationApprovalSummary:
    pending_recommendations: int
    pending_choices: int
    accepted_moves: int
    kept_recommendations: int
    duplicate_copies_for_future_bin: int
    choices_remaining: int
    ready_for_approval: bool
    files_to_move: int
    names_to_change: int = 0
    folders_to_create: int = 0

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "pending_recommendations": self.pending_recommendations,
            "pending_choices": self.pending_choices,
            "accepted_moves": self.accepted_moves,
            "kept_recommendations": self.kept_recommendations,
            "duplicate_copies_for_future_bin": (
                self.duplicate_copies_for_future_bin
            ),
            "choices_remaining": self.choices_remaining,
            "ready_for_approval": self.ready_for_approval,
            "files_to_move": self.files_to_move,
            "names_to_change": self.names_to_change,
            "folders_to_create": self.folders_to_create,
        }


@dataclass(frozen=True)
class ApprovedOrganizationPlan:
    status: str
    scan_generation: str
    decision_revision: int
    approved_at: str
    reviewed_move_items: tuple[ReviewedPlanItem, ...]
    duplicate_decisions: tuple[DuplicateChoiceDecision, ...]
    source_fingerprints: tuple[SourceFingerprint, ...]
    summary: OrganizationApprovalSummary

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "approved_at": self.approved_at,
            "summary": {
                "files_to_move": self.summary.files_to_move,
                "duplicate_copies_for_future_bin": (
                    self.summary.duplicate_copies_for_future_bin
                ),
                "names_to_change": self.summary.names_to_change,
                "folders_to_create": self.summary.folders_to_create,
                "choices_remaining": 0,
            },
        }


@dataclass(frozen=True)
class OrganizationDecisionSession:
    scan_generation: str
    decision_revision: int
    created_at: str
    updated_at: str
    recommendation_decisions: tuple[RecommendationDecision, ...]
    destination_decisions: tuple[DestinationChoiceDecision, ...]
    duplicate_decisions: tuple[DuplicateChoiceDecision, ...]
    status: str
    approved_plan: ApprovedOrganizationPlan | None = None

