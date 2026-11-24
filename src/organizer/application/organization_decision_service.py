from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path

from organizer.application.organization_decision_models import (
    DECISION_UNDECIDED,
    RECOMMENDATION_ACCEPT,
    RECOMMENDATION_KEEP,
    STATUS_IN_PROGRESS,
    STATUS_READY_FOR_APPROVAL,
    STATUS_READY_TO_MOVE,
    ApprovedOrganizationPlan,
    DestinationChoiceDecision,
    DuplicateChoiceDecision,
    OrganizationApprovalSummary,
    OrganizationDecisionSession,
    RecommendationDecision,
    SourceFingerprint,
)
from organizer.application.organization_slice_service import (
    InternalDestinationChoice,
    InternalMoveCandidate,
    InternalOrganizationProjection,
)
from organizer.duplicates import hash_file
from organizer.models import FileMetadata, MovePlanItem, ReviewedPlanItem
from organizer.review_session import (
    CATEGORY_ORGANIZATION,
    DECISION_APPROVED,
    find_approved_move_conflicts,
)
from organizer.safety import validate_under_root
from organizer.scope import (
    evaluate_actionable_source_path,
    is_actionable_destination_path,
)


class OrganizationDecisionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def create_organization_decision_session(
    projection: InternalOrganizationProjection,
    *,
    timestamp: str | None = None,
) -> OrganizationDecisionSession:
    created_at = timestamp or _utc_timestamp()
    has_decisions = bool(
        projection.recommendations_by_id
        or projection.destination_choices_by_id
        or projection.duplicate_choices_by_id
    )
    return OrganizationDecisionSession(
        scan_generation=projection.scan_generation,
        decision_revision=0,
        created_at=created_at,
        updated_at=created_at,
        recommendation_decisions=tuple(
            RecommendationDecision(change_id)
            for change_id in projection.recommendations_by_id
        ),
        destination_decisions=tuple(
            DestinationChoiceDecision(choice_id)
            for choice_id in projection.destination_choices_by_id
        ),
        duplicate_decisions=tuple(
            DuplicateChoiceDecision(choice_id)
            for choice_id in projection.duplicate_choices_by_id
        ),
        status=(STATUS_IN_PROGRESS if has_decisions else STATUS_READY_FOR_APPROVAL),
    )


def update_organization_decision(
    session: OrganizationDecisionSession,
    projection: InternalOrganizationProjection,
    *,
    kind: str,
    change_id: str | None = None,
    decision: str | None = None,
    choice_id: str | None = None,
    option_id: str | None = None,
    copy_ids: tuple[str, ...] = (),
) -> OrganizationDecisionSession:
    _validate_projection_generation(session, projection)
    if kind == "recommendation":
        if change_id not in projection.recommendations_by_id:
            raise OrganizationDecisionError("unknown_change_id")
        if decision not in {RECOMMENDATION_ACCEPT, RECOMMENDATION_KEEP}:
            raise OrganizationDecisionError("invalid_option")
        _validate_not_approved(session)
        recommendations = tuple(
            replace(item, decision=decision)
            if item.change_id == change_id
            else item
            for item in session.recommendation_decisions
        )
        updated = replace(session, recommendation_decisions=recommendations)
    elif kind == "choice":
        if choice_id in projection.destination_choices_by_id:
            candidate = projection.destination_choices_by_id[choice_id]
            if option_id not in {item[0] for item in candidate.options} or copy_ids:
                raise OrganizationDecisionError("invalid_option")
            _validate_not_approved(session)
            destinations = tuple(
                replace(item, option_id=option_id)
                if item.choice_id == choice_id
                else item
                for item in session.destination_decisions
            )
            updated = replace(session, destination_decisions=destinations)
        elif choice_id in projection.duplicate_choices_by_id:
            duplicate = projection.duplicate_choices_by_id[choice_id]
            selected = _validated_duplicate_selection(
                duplicate.copy_ids,
                option_id,
                copy_ids,
            )
            _validate_not_approved(session)
            paths_by_id = {
                item_id: metadata.path
                for item_id, metadata in zip(duplicate.copy_ids, duplicate.files)
            }
            duplicates = tuple(
                replace(
                    item,
                    option_id=option_id,
                    selected_copy_ids=selected,
                    selected_paths=tuple(paths_by_id[item_id] for item_id in selected),
                    group_sha256=duplicate.sha256,
                    group_size_bytes=duplicate.size_bytes,
                )
                if item.choice_id == choice_id
                else item
                for item in session.duplicate_decisions
            )
            updated = replace(session, duplicate_decisions=duplicates)
        else:
            raise OrganizationDecisionError("unknown_choice_id")
    else:
        raise OrganizationDecisionError("invalid_option")
    return _finish_mutation(updated, projection)


def accept_recommended_changes(
    session: OrganizationDecisionSession,
    projection: InternalOrganizationProjection,
    expected_change_ids: tuple[str, ...],
) -> OrganizationDecisionSession:
    _validate_projection_generation(session, projection)
    if len(set(expected_change_ids)) != len(expected_change_ids):
        raise OrganizationDecisionError("recommendation_set_changed")
    pending = {
        item.change_id
        for item in session.recommendation_decisions
        if item.decision == DECISION_UNDECIDED
    }
    if not pending:
        raise OrganizationDecisionError("recommendation_set_changed")
    if set(expected_change_ids) != pending:
        raise OrganizationDecisionError("recommendation_set_changed")
    _validate_not_approved(session)
    recommendations = tuple(
        replace(item, decision=RECOMMENDATION_ACCEPT)
        if item.change_id in pending
        else item
        for item in session.recommendation_decisions
    )
    return _finish_mutation(
        replace(session, recommendation_decisions=recommendations),
        projection,
    )


def build_organization_approval_summary(
    session: OrganizationDecisionSession,
    projection: InternalOrganizationProjection,
) -> OrganizationApprovalSummary:
    _validate_projection_generation(session, projection)
    pending_recommendations = sum(
        item.decision == DECISION_UNDECIDED
        for item in session.recommendation_decisions
    )
    pending_destinations = sum(
        item.option_id is None for item in session.destination_decisions
    )
    pending_duplicates = sum(
        item.option_id is None for item in session.duplicate_decisions
    )
    accepted_recommendations = sum(
        item.decision == RECOMMENDATION_ACCEPT
        for item in session.recommendation_decisions
    )
    accepted_destination_moves = sum(
        _destination_move_for_decision(item, projection) is not None
        for item in session.destination_decisions
        if item.option_id is not None
    )
    selected_duplicates = sum(
        len(item.selected_copy_ids) for item in session.duplicate_decisions
    )
    choices_remaining = (
        pending_recommendations + pending_destinations + pending_duplicates
    )
    files_to_move = accepted_recommendations + accepted_destination_moves
    return OrganizationApprovalSummary(
        pending_recommendations=pending_recommendations,
        pending_choices=pending_destinations + pending_duplicates,
        accepted_moves=files_to_move,
        kept_recommendations=sum(
            item.decision == RECOMMENDATION_KEEP
            for item in session.recommendation_decisions
        ),
        duplicate_copies_for_future_bin=selected_duplicates,
        choices_remaining=choices_remaining,
        ready_for_approval=choices_remaining == 0,
        files_to_move=files_to_move,
    )


def approve_organization_plan(
    session: OrganizationDecisionSession,
    projection: InternalOrganizationProjection,
    *,
    timestamp: str | None = None,
) -> OrganizationDecisionSession:
    _validate_mutable_session(session, projection)
    summary = build_organization_approval_summary(session, projection)
    if not summary.ready_for_approval:
        raise OrganizationDecisionError("incomplete_decisions")

    reviewed_items: list[ReviewedPlanItem] = []
    fingerprints: list[SourceFingerprint] = []
    recommendation_decisions = {
        item.change_id: item for item in session.recommendation_decisions
    }
    for change_id, candidate in projection.recommendations_by_id.items():
        if recommendation_decisions[change_id].decision != RECOMMENDATION_ACCEPT:
            continue
        _validate_candidate(candidate, projection)
        reviewed_items.append(_reviewed_item(change_id, candidate.move_item))
        fingerprints.append(_fingerprint(candidate.file_id, candidate.source_metadata))

    destination_decisions = {
        item.choice_id: item for item in session.destination_decisions
    }
    for choice_id, candidate in projection.destination_choices_by_id.items():
        decision = destination_decisions[choice_id]
        move_item = _destination_move_for_decision(decision, projection)
        if move_item is None:
            continue
        _validate_destination_candidate(candidate, move_item, projection)
        reviewed_items.append(_reviewed_item(choice_id, move_item))
        fingerprints.append(_fingerprint(candidate.file_id, candidate.source_metadata))

    conflicts = find_approved_move_conflicts(reviewed_items, projection.root)
    if conflicts or _has_move_chain_conflict(reviewed_items):
        raise OrganizationDecisionError("unsafe_destination")

    approved_duplicates: list[DuplicateChoiceDecision] = []
    duplicate_decisions = {
        item.choice_id: item for item in session.duplicate_decisions
    }
    for choice_id, duplicate in projection.duplicate_choices_by_id.items():
        decision = duplicate_decisions[choice_id]
        if decision.option_id != "choose-for-bin":
            continue
        _validate_duplicate_group(decision, duplicate, projection)
        approved_duplicates.append(decision)

    approved_at = timestamp or _utc_timestamp()
    approved = ApprovedOrganizationPlan(
        status=STATUS_READY_TO_MOVE,
        scan_generation=session.scan_generation,
        decision_revision=session.decision_revision,
        approved_at=approved_at,
        reviewed_move_items=tuple(reviewed_items),
        duplicate_decisions=tuple(approved_duplicates),
        source_fingerprints=tuple(fingerprints),
        summary=summary,
    )
    return replace(
        session,
        status=STATUS_READY_TO_MOVE,
        updated_at=approved_at,
        approved_plan=approved,
    )


def organization_review_to_dict(
    session: OrganizationDecisionSession,
    projection: InternalOrganizationProjection,
) -> dict[str, object]:
    summary = build_organization_approval_summary(session, projection)
    return {
        "status": session.status,
        "scan_generation": session.scan_generation,
        "decision_revision": session.decision_revision,
        "recommendations": [
            {"change_id": item.change_id, "decision": item.decision}
            for item in session.recommendation_decisions
        ],
        "choices": [
            {
                "choice_id": item.choice_id,
                "option_id": item.option_id,
                "selected_copy_ids": [],
            }
            for item in session.destination_decisions
        ]
        + [
            {
                "choice_id": item.choice_id,
                "option_id": item.option_id,
                "selected_copy_ids": list(item.selected_copy_ids),
            }
            for item in session.duplicate_decisions
        ],
        "summary": summary.to_dict(),
        "approval": (
            session.approved_plan.to_safe_dict()
            if session.approved_plan is not None
            else None
        ),
    }


def _finish_mutation(
    session: OrganizationDecisionSession,
    projection: InternalOrganizationProjection,
) -> OrganizationDecisionSession:
    updated = replace(
        session,
        decision_revision=session.decision_revision + 1,
        updated_at=_utc_timestamp(),
    )
    summary = build_organization_approval_summary(updated, projection)
    return replace(
        updated,
        status=(
            STATUS_READY_FOR_APPROVAL
            if summary.ready_for_approval
            else STATUS_IN_PROGRESS
        ),
    )


def _validate_mutable_session(
    session: OrganizationDecisionSession,
    projection: InternalOrganizationProjection,
) -> None:
    _validate_projection_generation(session, projection)
    _validate_not_approved(session)


def _validate_not_approved(session: OrganizationDecisionSession) -> None:
    if session.status == STATUS_READY_TO_MOVE or session.approved_plan is not None:
        raise OrganizationDecisionError("approval_already_completed")


def _validate_projection_generation(
    session: OrganizationDecisionSession,
    projection: InternalOrganizationProjection,
) -> None:
    if session.scan_generation != projection.scan_generation:
        raise OrganizationDecisionError("scan_generation_mismatch")


def _validated_duplicate_selection(
    offered_ids: tuple[str, ...],
    option_id: str | None,
    copy_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if option_id == "keep-all":
        if copy_ids:
            raise OrganizationDecisionError("invalid_option")
        return ()
    if option_id != "choose-for-bin":
        raise OrganizationDecisionError("invalid_option")
    if not copy_ids or len(copy_ids) >= len(offered_ids):
        raise OrganizationDecisionError("invalid_option")
    if len(set(copy_ids)) != len(copy_ids):
        raise OrganizationDecisionError("invalid_option")
    if not set(copy_ids).issubset(set(offered_ids)):
        raise OrganizationDecisionError("invalid_option")
    return tuple(sorted(copy_ids))


def _destination_move_for_decision(
    decision: DestinationChoiceDecision,
    projection: InternalOrganizationProjection,
) -> MovePlanItem | None:
    candidate = projection.destination_choices_by_id[decision.choice_id]
    return dict(candidate.options).get(decision.option_id)


def _validate_candidate(
    candidate: InternalMoveCandidate,
    projection: InternalOrganizationProjection,
) -> None:
    if candidate.file_id in projection.protected_item_ids:
        raise OrganizationDecisionError("protected_item_selected")
    _validate_source(candidate.source_metadata, projection)
    _validate_destination(candidate.move_item.destination, projection)


def _validate_destination_candidate(
    candidate: InternalDestinationChoice,
    move_item: MovePlanItem,
    projection: InternalOrganizationProjection,
) -> None:
    if candidate.file_id in projection.protected_item_ids:
        raise OrganizationDecisionError("protected_item_selected")
    _validate_source(candidate.source_metadata, projection)
    _validate_destination(move_item.destination, projection)


def _validate_source(
    metadata: FileMetadata,
    projection: InternalOrganizationProjection,
) -> None:
    path = metadata.path
    if path.is_symlink() or not path.exists() or not path.is_file():
        raise OrganizationDecisionError("source_changed")
    try:
        validate_under_root(path.resolve(), projection.root)
        stat = path.stat()
    except (OSError, ValueError):
        raise OrganizationDecisionError("source_changed") from None
    if stat.st_size != metadata.size_bytes or stat.st_mtime != metadata.modified_time:
        raise OrganizationDecisionError("source_changed")
    eligibility = evaluate_actionable_source_path(
        metadata.relative_path,
        projection.metadata_items,
    )
    if not eligibility.allowed:
        raise OrganizationDecisionError("protected_item_selected")


def _validate_destination(
    destination: Path,
    projection: InternalOrganizationProjection,
) -> None:
    try:
        validate_under_root(destination.resolve(strict=False), projection.root)
        relative = destination.relative_to(projection.root)
        if not is_actionable_destination_path(relative, projection.metadata_items):
            raise ValueError("destination is protected")
        existing_parent = _nearest_existing_parent(destination.parent)
        validate_under_root(existing_parent.resolve(), projection.root)
        if not existing_parent.is_dir():
            raise ValueError("destination parent is not a directory")
    except (OSError, ValueError):
        raise OrganizationDecisionError("unsafe_destination") from None
    if os.path.lexists(destination):
        raise OrganizationDecisionError("overwrite_detected")


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _validate_duplicate_group(
    decision: DuplicateChoiceDecision,
    duplicate,
    projection: InternalOrganizationProjection,
) -> None:
    try:
        _validated_duplicate_selection(
            duplicate.copy_ids,
            decision.option_id,
            decision.selected_copy_ids,
        )
        for metadata in duplicate.files:
            eligibility = evaluate_actionable_source_path(
                metadata.relative_path,
                projection.metadata_items,
            )
            if not eligibility.allowed:
                raise OrganizationDecisionError("protected_item_selected")
            if metadata.path.is_symlink() or not metadata.path.is_file():
                raise ValueError("copy unavailable")
            validate_under_root(metadata.path.resolve(), projection.root)
            if metadata.path.stat().st_size != duplicate.size_bytes:
                raise ValueError("copy size changed")
            if hash_file(metadata.path) != duplicate.sha256:
                raise ValueError("copy hash changed")
    except OrganizationDecisionError:
        raise
    except (OSError, ValueError):
        raise OrganizationDecisionError("duplicate_group_changed") from None


def _reviewed_item(item_id: str, move_item: MovePlanItem) -> ReviewedPlanItem:
    return ReviewedPlanItem(
        id=item_id,
        category=CATEGORY_ORGANIZATION,
        plan_item=move_item,
        decision=DECISION_APPROVED,
    )


def _fingerprint(item_id: str, metadata: FileMetadata) -> SourceFingerprint:
    return SourceFingerprint(
        item_id=item_id,
        relative_path=metadata.relative_path,
        size_bytes=metadata.size_bytes,
        modified_time=metadata.modified_time,
    )


def _has_move_chain_conflict(items: list[ReviewedPlanItem]) -> bool:
    sources = {item.plan_item.source.resolve(strict=False) for item in items}
    destinations = {
        item.plan_item.destination.resolve(strict=False) for item in items
    }
    return bool(sources & destinations)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
