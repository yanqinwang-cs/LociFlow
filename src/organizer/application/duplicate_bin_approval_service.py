from __future__ import annotations

import hashlib
from pathlib import Path
import uuid

from organizer.application.duplicate_bin_models import (
    BATCH_APPROVED,
    ApprovedDuplicateBatch,
    ApprovedDuplicateGroup,
    ApprovedDuplicateMember,
    SelectedDuplicateBinItem,
    canonical_digest,
    canonical_json_bytes,
)
from organizer.application.duplicate_bin_repository import (
    DuplicateBinRepository,
    DuplicateBinRepositoryError,
)
from organizer.application.file_kinds import consumer_file_kind
from organizer.application.organization_decision_models import ApprovedOrganizationPlan
from organizer.application.organization_slice_service import (
    InternalOrganizationProjection,
)
from organizer.safety import validate_under_root
from organizer.scope import validate_duplicate_bin_item_destination


_BATCH_NAMESPACE = uuid.UUID("106e34de-754f-54fc-a7eb-700c53a365d6")
_GROUP_NAMESPACE = uuid.UUID("03c4fe9f-73e2-522e-a7ee-ec40324fe492")
_MEMBER_NAMESPACE = uuid.UUID("415c2fca-8986-58c7-bcc4-c3ee8a0e18b8")
_BATCH_DISCRIMINATOR = "loci-duplicate-batch-v1"


class DuplicateBinApprovalError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def create_or_recover_duplicate_batch(
    *,
    approved_plan: ApprovedOrganizationPlan,
    projection: InternalOrganizationProjection,
    browser_session_id: str,
    repository: DuplicateBinRepository,
) -> ApprovedDuplicateBatch | None:
    """Durably approve selected duplicate copies without moving any file."""

    if not approved_plan.duplicate_decisions:
        return None
    if repository.root != projection.root.resolve(strict=True):
        raise DuplicateBinApprovalError("duplicate_batch_wrong_root")
    if not browser_session_id:
        raise DuplicateBinApprovalError("duplicate_batch_unavailable")

    groups = _approved_groups(
        approved_plan, projection, repository.configured_root_identity
    )
    duplicate_payload = {
        "schema": "loci-duplicate-batch-digest-v1",
        "groups": [_canonical_group(group) for group in groups],
    }
    duplicate_batch_digest = canonical_digest(duplicate_payload)
    approved_plan_digest = canonical_digest(
        {
            "schema": "loci-approved-organization-plan-digest-v1",
            "ordinary_moves": _canonical_ordinary_moves(approved_plan, projection.root),
            "source_fingerprints": _canonical_source_fingerprints(approved_plan),
            "duplicate_groups": duplicate_payload["groups"],
        }
    )
    idempotency_key = hashlib.sha256(
        canonical_json_bytes(
            {
                "version": _BATCH_DISCRIMINATOR,
                "configured_root_identity": repository.configured_root_identity,
                "originating_scan_generation": approved_plan.scan_generation,
                "decision_revision": approved_plan.decision_revision,
                "approved_plan_digest": approved_plan_digest,
                "duplicate_batch_digest": duplicate_batch_digest,
            }
        )
    ).hexdigest()
    batch_id = str(uuid.uuid5(_BATCH_NAMESPACE, idempotency_key))
    items = _selected_items(
        batch_id=batch_id,
        groups=groups,
        projection=projection,
    )
    candidate = ApprovedDuplicateBatch(
        batch_id=batch_id,
        configured_root_identity=repository.configured_root_identity,
        created_by_session_id=browser_session_id,
        originating_scan_generation=approved_plan.scan_generation,
        decision_revision=approved_plan.decision_revision,
        approved_at=approved_plan.approved_at,
        approved_plan_digest=approved_plan_digest,
        duplicate_batch_digest=duplicate_batch_digest,
        batch_version=1,
        status=BATCH_APPROVED,
        created_at=approved_plan.approved_at,
        updated_at=approved_plan.approved_at,
        groups=groups,
        items=items,
    )
    try:
        return repository.create_or_recover_approved_batch(candidate)
    except DuplicateBinRepositoryError as error:
        if error.code == "active_copy_claim_conflict":
            raise DuplicateBinApprovalError("duplicate_batch_conflict") from error
        raise DuplicateBinApprovalError("duplicate_batch_unavailable") from error


def _approved_groups(
    approved_plan: ApprovedOrganizationPlan,
    projection: InternalOrganizationProjection,
    root_identity: str,
) -> tuple[ApprovedDuplicateGroup, ...]:
    groups: list[ApprovedDuplicateGroup] = []
    for decision in approved_plan.duplicate_decisions:
        duplicate = projection.duplicate_choices_by_id.get(decision.choice_id)
        if duplicate is None:
            raise DuplicateBinApprovalError("duplicate_batch_unavailable")
        selected_copy_ids = set(decision.selected_copy_ids)
        rows = sorted(
            zip(duplicate.copy_ids, duplicate.files),
            key=lambda row: row[1].relative_path.as_posix(),
        )
        members = tuple(
            ApprovedDuplicateMember(
                member_id=str(
                    uuid.uuid5(
                        _MEMBER_NAMESPACE,
                        f"{root_identity}\0{metadata.relative_path.as_posix()}",
                    )
                ),
                relative_path=metadata.relative_path.as_posix(),
                size_bytes=metadata.size_bytes,
                modified_time=metadata.modified_time,
            )
            for _copy_id, metadata in rows
        )
        selected_members = tuple(
            member.member_id
            for member, (copy_id, _metadata) in zip(members, rows)
            if copy_id in selected_copy_ids
        )
        retained_members = tuple(
            member.member_id
            for member, (copy_id, _metadata) in zip(members, rows)
            if copy_id not in selected_copy_ids
        )
        if not selected_members or not retained_members:
            raise DuplicateBinApprovalError("duplicate_batch_unavailable")
        group_identity = canonical_digest(
            {
                "schema": "loci-duplicate-group-identity-v1",
                "configured_root_identity": root_identity,
                "sha256": duplicate.sha256,
                "size_bytes": duplicate.size_bytes,
                "member_ids": sorted(member.member_id for member in members),
            }
        )
        groups.append(
            ApprovedDuplicateGroup(
                group_id=str(uuid.uuid5(_GROUP_NAMESPACE, group_identity)),
                choice_id=decision.choice_id,
                approved_sha256=duplicate.sha256,
                approved_size_bytes=duplicate.size_bytes,
                members=members,
                selected_member_ids=tuple(sorted(selected_members)),
                retained_member_ids=tuple(sorted(retained_members)),
            )
        )
    return tuple(sorted(groups, key=lambda group: group.group_id))


def _selected_items(
    *,
    batch_id: str,
    groups: tuple[ApprovedDuplicateGroup, ...],
    projection: InternalOrganizationProjection,
) -> tuple[SelectedDuplicateBinItem, ...]:
    metadata_by_path = {
        metadata.relative_path.as_posix(): metadata
        for metadata in projection.metadata_items
        if not metadata.is_dir
    }
    items: list[SelectedDuplicateBinItem] = []
    for group in groups:
        selected_ids = set(group.selected_member_ids)
        for member in group.members:
            if member.member_id not in selected_ids:
                continue
            metadata = metadata_by_path.get(member.relative_path)
            if metadata is None:
                raise DuplicateBinApprovalError("duplicate_batch_unavailable")
            bin_item_id = str(uuid.uuid5(uuid.UUID(batch_id), member.member_id))
            future_relative = (
                Path("AI_Review")
                / "duplicate_bin"
                / "items"
                / bin_item_id
                / metadata.name
            )
            try:
                validate_duplicate_bin_item_destination(
                    projection.root / future_relative,
                    projection.root,
                )
            except (OSError, ValueError) as error:
                raise DuplicateBinApprovalError(
                    "duplicate_batch_unavailable"
                ) from error
            fingerprint = canonical_digest(
                {
                    "schema": "loci-duplicate-approval-metadata-v1",
                    "size_bytes": metadata.size_bytes,
                    "modified_time": metadata.modified_time,
                }
            )
            items.append(
                SelectedDuplicateBinItem(
                    bin_item_id=bin_item_id,
                    selected_member_id=member.member_id,
                    previous_relative_path=member.relative_path,
                    future_relative_path=future_relative.as_posix(),
                    basename=metadata.name,
                    size_bytes=metadata.size_bytes,
                    kind=consumer_file_kind(metadata.extension, is_dir=False),
                    extension=metadata.extension.casefold(),
                    approved_sha256=group.approved_sha256,
                    approval_metadata_fingerprint=fingerprint,
                )
            )
    return tuple(sorted(items, key=lambda item: item.bin_item_id))


def _canonical_group(group: ApprovedDuplicateGroup) -> dict[str, object]:
    return {
        "group_id": group.group_id,
        "choice_id": group.choice_id,
        "approved_sha256": group.approved_sha256,
        "approved_size_bytes": group.approved_size_bytes,
        "members": [
            {
                "member_id": member.member_id,
                "relative_path": member.relative_path,
                "size_bytes": member.size_bytes,
                "modified_time": member.modified_time,
                "metadata_fingerprint": canonical_digest(
                    {
                        "schema": "loci-duplicate-approval-metadata-v1",
                        "size_bytes": member.size_bytes,
                        "modified_time": member.modified_time,
                    }
                ),
            }
            for member in group.members
        ],
        "selected_member_ids": list(group.selected_member_ids),
        "retained_member_ids": list(group.retained_member_ids),
    }


def _canonical_ordinary_moves(
    approved_plan: ApprovedOrganizationPlan,
    root: Path,
) -> list[dict[str, object]]:
    rows = []
    for item in approved_plan.reviewed_move_items:
        move = item.plan_item
        rows.append(
            {
                "change_id": item.id,
                "source": _root_relative(move.source, root),
                "destination": _root_relative(move.destination, root),
                "reason": move.reason,
                "confidence": move.confidence,
                "operation": move.operation,
                "overwrite_risk": move.overwrite_risk,
            }
        )
    return sorted(rows, key=lambda row: (str(row["source"]), str(row["destination"])))


def _canonical_source_fingerprints(
    approved_plan: ApprovedOrganizationPlan,
) -> list[dict[str, object]]:
    return sorted(
        (
            {
                "item_id": item.item_id,
                "relative_path": item.relative_path.as_posix(),
                "size_bytes": item.size_bytes,
                "modified_time": item.modified_time,
            }
            for item in approved_plan.source_fingerprints
        ),
        key=lambda row: str(row["relative_path"]),
    )


def _root_relative(path: Path, root: Path) -> str:
    resolved = validate_under_root(
        path.resolve(strict=False), root.resolve(strict=True)
    )
    return resolved.relative_to(root.resolve(strict=True)).as_posix()
