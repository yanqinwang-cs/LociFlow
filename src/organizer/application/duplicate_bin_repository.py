from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from organizer.application.duplicate_bin_models import (
    BATCH_APPROVED,
    BATCH_COMPLETED,
    BATCH_STATUSES,
    BATCH_SUPERSEDED,
    CLAIMING_BATCH_STATUSES,
    DUPLICATE_BIN_BATCH_RECORD_TYPE,
    DUPLICATE_BIN_CATALOG_RECORD_TYPE,
    DUPLICATE_BIN_SCHEMA_VERSION,
    FULL_RESTORE_RECONCILABLE_BATCH_STATUSES,
    ITEM_RESTORED,
    ITEM_STATES,
    SUPERSEDABLE_BATCH_STATUSES,
    ActiveDuplicateCopyClaim,
    ApprovedDuplicateBatch,
    ApprovedDuplicateGroup,
    ApprovedDuplicateMember,
    DuplicateBinCatalog,
    SelectedDuplicateBinItem,
    canonical_digest,
    normalized_copy_claim_key,
)
from organizer.safety import validate_under_root
from organizer.scope import DUPLICATE_BIN_RELATIVE_ROOT

DUPLICATE_BIN_CATALOG = DUPLICATE_BIN_RELATIVE_ROOT / "catalog.json"


class DuplicateBinRepositoryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DuplicateBinRepository:
    """Own the strict root-bound catalog for approved Duplicate Bin batches."""

    def __init__(self, root: Path) -> None:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError(f"duplicate bin root is not a directory: {root}")
        self.root = resolved_root
        self.directory = resolved_root / DUPLICATE_BIN_RELATIVE_ROOT
        self.catalog_path = resolved_root / DUPLICATE_BIN_CATALOG
        self._lock = Lock()

    @property
    def configured_root_identity(self) -> str:
        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()

    def create_or_recover_approved_batch(
        self,
        candidate: ApprovedDuplicateBatch,
    ) -> ApprovedDuplicateBatch:
        with self._lock:
            self._validate_candidate(candidate)
            catalog = self._load_catalog_unlocked()
            existing_by_id = {batch.batch_id: batch for batch in catalog.batches}
            existing = existing_by_id.get(candidate.batch_id)
            if existing is not None:
                if not _same_durable_approval(existing, candidate):
                    raise DuplicateBinRepositoryError("duplicate_batch_id_conflict")
                return existing

            candidate_claims = _claims_for_batch(candidate)
            candidate_keys = {claim.claim_key for claim in candidate_claims}
            replacements: dict[str, ApprovedDuplicateBatch] = {}
            for batch in catalog.batches:
                if batch.status not in CLAIMING_BATCH_STATUSES:
                    continue
                existing_keys = {
                    claim.claim_key
                    for claim in catalog.active_claims
                    if claim.batch_id == batch.batch_id
                }
                if not candidate_keys.intersection(existing_keys):
                    continue
                if batch.status not in SUPERSEDABLE_BATCH_STATUSES:
                    raise DuplicateBinRepositoryError("active_copy_claim_conflict")
                replacements[batch.batch_id] = replace(
                    batch,
                    status=BATCH_SUPERSEDED,
                    batch_version=batch.batch_version + 1,
                    updated_at=candidate.created_at,
                )

            batches = tuple(
                replacements.get(batch.batch_id, batch) for batch in catalog.batches
            ) + (candidate,)
            updated = DuplicateBinCatalog(
                configured_root_identity=self.configured_root_identity,
                catalog_version=catalog.catalog_version + 1,
                batches=tuple(sorted(batches, key=lambda batch: batch.batch_id)),
                active_claims=_all_active_claims(batches),
            )
            self._atomic_write(_catalog_to_dict(updated))
            return candidate

    def load(self, batch_id: str) -> ApprovedDuplicateBatch:
        requested = _uuid_string(batch_id, "batch_id")
        with self._lock:
            for batch in self._load_catalog_unlocked().batches:
                if batch.batch_id == requested:
                    return batch
        raise DuplicateBinRepositoryError("duplicate_batch_not_found")

    def list_batches(self) -> tuple[ApprovedDuplicateBatch, ...]:
        with self._lock:
            return self._load_catalog_unlocked().batches

    def list_active_approved_batches(self) -> tuple[ApprovedDuplicateBatch, ...]:
        return tuple(
            batch for batch in self.list_batches() if batch.status == BATCH_APPROVED
        )

    def read_catalog(self) -> DuplicateBinCatalog:
        """Return the canonical durable catalog without repairing it."""

        with self._lock:
            return self._read_catalog_unlocked()

    def transition_status(
        self,
        batch_id: str,
        *,
        expected_version: int,
        status: str,
        updated_at: str,
    ) -> ApprovedDuplicateBatch:
        """Persist a version-guarded lifecycle transition for later owner services."""

        if status not in BATCH_STATUSES or status == BATCH_SUPERSEDED:
            raise DuplicateBinRepositoryError("invalid_duplicate_batch_status")
        _timestamp(updated_at, "updated_at")
        with self._lock:
            catalog = self._load_catalog_unlocked()
            target = next(
                (batch for batch in catalog.batches if batch.batch_id == batch_id),
                None,
            )
            if target is None:
                raise DuplicateBinRepositoryError("duplicate_batch_not_found")
            if target.batch_version != expected_version:
                raise DuplicateBinRepositoryError("duplicate_batch_version_conflict")
            if target.status != BATCH_APPROVED:
                raise DuplicateBinRepositoryError("duplicate_batch_not_approvable")
            updated_batch = replace(
                target,
                status=status,
                batch_version=target.batch_version + 1,
                updated_at=updated_at,
            )
            batches = tuple(
                updated_batch if batch.batch_id == batch_id else batch
                for batch in catalog.batches
            )
            updated_catalog = replace(
                catalog,
                catalog_version=catalog.catalog_version + 1,
                batches=batches,
                active_claims=_all_active_claims(batches),
            )
            self._atomic_write(_catalog_to_dict(updated_catalog))
            return updated_batch

    def save_execution_state(
        self,
        batch_id: str,
        *,
        expected_version: int,
        status: str,
        items: tuple[SelectedDuplicateBinItem, ...],
        updated_at: str,
    ) -> ApprovedDuplicateBatch:
        if status not in BATCH_STATUSES or status in {BATCH_APPROVED, BATCH_SUPERSEDED}:
            raise DuplicateBinRepositoryError("invalid_duplicate_batch_status")
        if status == BATCH_COMPLETED and any(
            item.state != ITEM_RESTORED for item in items
        ):
            raise DuplicateBinRepositoryError("invalid_duplicate_batch_items")
        _timestamp(updated_at, "updated_at")
        with self._lock:
            catalog = self._load_catalog_unlocked()
            target = next(
                (batch for batch in catalog.batches if batch.batch_id == batch_id),
                None,
            )
            if target is None:
                raise DuplicateBinRepositoryError("duplicate_batch_not_found")
            if target.batch_version != expected_version:
                raise DuplicateBinRepositoryError("duplicate_batch_version_conflict")
            if {item.bin_item_id for item in items} != {
                item.bin_item_id for item in target.items
            }:
                raise DuplicateBinRepositoryError("invalid_duplicate_batch_items")
            updated_batch = replace(
                target,
                status=status,
                batch_version=target.batch_version + 1,
                updated_at=updated_at,
                items=tuple(sorted(items, key=lambda item: item.bin_item_id)),
            )
            batches = tuple(
                updated_batch if batch.batch_id == batch_id else batch
                for batch in catalog.batches
            )
            updated_catalog = replace(
                catalog,
                catalog_version=catalog.catalog_version + 1,
                batches=batches,
                active_claims=_all_active_claims(batches),
            )
            self._atomic_write(_catalog_to_dict(updated_catalog))
            return updated_batch

    def _validate_candidate(self, candidate: ApprovedDuplicateBatch) -> None:
        if candidate.configured_root_identity != self.configured_root_identity:
            raise DuplicateBinRepositoryError("duplicate_batch_wrong_root")
        if candidate.status != BATCH_APPROVED or candidate.batch_version != 1:
            raise DuplicateBinRepositoryError("invalid_duplicate_batch_candidate")
        if _batch_from_dict(_batch_to_dict(candidate)) != candidate:
            raise DuplicateBinRepositoryError("invalid_duplicate_batch_candidate")

    def _load_catalog_unlocked(self) -> DuplicateBinCatalog:
        catalog = self._read_catalog_unlocked()
        reconciled = _reconcile_fully_restored_batches(
            catalog,
            updated_at=datetime.now(UTC).isoformat(),
        )
        if reconciled is not catalog:
            self._atomic_write(_catalog_to_dict(reconciled))
            catalog = reconciled
        return catalog

    def _read_catalog_unlocked(self) -> DuplicateBinCatalog:
        if not os.path.lexists(self.catalog_path):
            return DuplicateBinCatalog(
                configured_root_identity=self.configured_root_identity,
                catalog_version=0,
                batches=(),
                active_claims=(),
            )
        if self.catalog_path.is_symlink():
            raise DuplicateBinRepositoryError("duplicate_bin_catalog_unsafe")
        try:
            resolved = self.catalog_path.resolve(strict=True)
            validate_under_root(resolved, self.root)
            if not resolved.is_file():
                raise ValueError("catalog is not a regular file")
            data = json.loads(resolved.read_text(encoding="utf-8"))
            catalog = _catalog_from_dict(data)
        except DuplicateBinRepositoryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise DuplicateBinRepositoryError(
                "duplicate_bin_catalog_malformed"
            ) from error
        if catalog.configured_root_identity != self.configured_root_identity:
            raise DuplicateBinRepositoryError("duplicate_bin_catalog_wrong_root")
        return catalog

    def _atomic_write(self, data: dict[str, Any]) -> None:
        nearest = _nearest_existing_parent(self.directory)
        try:
            validate_under_root(nearest.resolve(strict=True), self.root)
        except (OSError, ValueError) as error:
            raise DuplicateBinRepositoryError("duplicate_bin_catalog_unsafe") from error
        if not nearest.is_dir():
            raise DuplicateBinRepositoryError("duplicate_bin_catalog_unsafe")
        _reject_symlink_components(self.root, DUPLICATE_BIN_RELATIVE_ROOT)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise DuplicateBinRepositoryError(
                "duplicate_bin_catalog_write_failed"
            ) from error
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise DuplicateBinRepositoryError("duplicate_bin_catalog_unsafe")
        validate_under_root(self.directory.resolve(strict=True), self.root)
        if os.path.lexists(self.catalog_path) and not self.catalog_path.is_file():
            raise DuplicateBinRepositoryError("duplicate_bin_catalog_unsafe")

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.directory,
                prefix=".catalog.json.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.catalog_path)
            temporary_path = None
            _fsync_directory(self.directory)
        except Exception as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
            if isinstance(error, DuplicateBinRepositoryError):
                raise
            raise DuplicateBinRepositoryError(
                "duplicate_bin_catalog_write_failed"
            ) from error


def _catalog_to_dict(catalog: DuplicateBinCatalog) -> dict[str, Any]:
    return {
        "schema_version": catalog.schema_version,
        "record_type": catalog.record_type,
        "configured_root_identity": catalog.configured_root_identity,
        "catalog_version": catalog.catalog_version,
        "batches": [_batch_to_dict(batch) for batch in catalog.batches],
        "active_claims": [asdict(claim) for claim in catalog.active_claims],
    }


def _batch_to_dict(batch: ApprovedDuplicateBatch) -> dict[str, Any]:
    return {
        "schema_version": batch.schema_version,
        "record_type": batch.record_type,
        "batch_id": batch.batch_id,
        "configured_root_identity": batch.configured_root_identity,
        "created_by_session_id": batch.created_by_session_id,
        "originating_scan_generation": batch.originating_scan_generation,
        "decision_revision": batch.decision_revision,
        "approved_at": batch.approved_at,
        "approved_plan_digest": batch.approved_plan_digest,
        "duplicate_batch_digest": batch.duplicate_batch_digest,
        "batch_version": batch.batch_version,
        "status": batch.status,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "groups": [
            {
                "group_id": group.group_id,
                "choice_id": group.choice_id,
                "approved_sha256": group.approved_sha256,
                "approved_size_bytes": group.approved_size_bytes,
                "members": [asdict(member) for member in group.members],
                "selected_member_ids": list(group.selected_member_ids),
                "retained_member_ids": list(group.retained_member_ids),
            }
            for group in batch.groups
        ],
        "items": [asdict(item) for item in batch.items],
    }


def _reconcile_fully_restored_batches(
    catalog: DuplicateBinCatalog,
    *,
    updated_at: str,
) -> DuplicateBinCatalog:
    reconciled_batches = tuple(
        replace(
            batch,
            status=BATCH_COMPLETED,
            batch_version=batch.batch_version + 1,
            updated_at=updated_at,
        )
        if batch.status in FULL_RESTORE_RECONCILABLE_BATCH_STATUSES
        and all(item.state == ITEM_RESTORED for item in batch.items)
        else batch
        for batch in catalog.batches
    )
    if reconciled_batches == catalog.batches:
        return catalog
    return replace(
        catalog,
        catalog_version=catalog.catalog_version + 1,
        batches=reconciled_batches,
        active_claims=_all_active_claims(reconciled_batches),
    )


def _catalog_from_dict(data: object) -> DuplicateBinCatalog:
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "record_type",
        "configured_root_identity",
        "catalog_version",
        "batches",
        "active_claims",
    }:
        raise ValueError("duplicate bin catalog fields do not match schema version 1")
    if data["schema_version"] != DUPLICATE_BIN_SCHEMA_VERSION:
        raise ValueError("unsupported duplicate bin catalog schema version")
    if data["record_type"] != DUPLICATE_BIN_CATALOG_RECORD_TYPE:
        raise ValueError("unexpected duplicate bin catalog record type")
    batches_data = data["batches"]
    claims_data = data["active_claims"]
    if not isinstance(batches_data, list) or not isinstance(claims_data, list):
        raise ValueError("duplicate bin catalog collections are invalid")
    batches = tuple(_batch_from_dict(item) for item in batches_data)
    if len({batch.batch_id for batch in batches}) != len(batches):
        raise ValueError("duplicate bin catalog contains duplicate batch IDs")
    if tuple(sorted(batches, key=lambda batch: batch.batch_id)) != batches:
        raise ValueError("duplicate bin catalog batches are not canonical")
    claims = tuple(_claim_from_dict(item) for item in claims_data)
    if len({claim.claim_key for claim in claims}) != len(claims):
        raise ValueError("duplicate bin catalog contains overlapping claims")
    expected_claims = _all_active_claims(batches)
    if claims != expected_claims:
        raise ValueError("duplicate bin catalog active claims are inconsistent")
    return DuplicateBinCatalog(
        configured_root_identity=_digest(
            data["configured_root_identity"], "root identity"
        ),
        catalog_version=_positive_integer(
            data["catalog_version"], "catalog version", allow_zero=True
        ),
        batches=batches,
        active_claims=claims,
    )


def _batch_from_dict(data: object) -> ApprovedDuplicateBatch:
    required = {
        "schema_version",
        "record_type",
        "batch_id",
        "configured_root_identity",
        "created_by_session_id",
        "originating_scan_generation",
        "decision_revision",
        "approved_at",
        "approved_plan_digest",
        "duplicate_batch_digest",
        "batch_version",
        "status",
        "created_at",
        "updated_at",
        "groups",
        "items",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("duplicate bin batch fields do not match schema version 1")
    if data["schema_version"] != DUPLICATE_BIN_SCHEMA_VERSION:
        raise ValueError("unsupported duplicate bin batch schema version")
    if data["record_type"] != DUPLICATE_BIN_BATCH_RECORD_TYPE:
        raise ValueError("unexpected duplicate bin batch record type")
    status = _string(data["status"], "status")
    if status not in BATCH_STATUSES:
        raise ValueError("duplicate bin batch status is invalid")
    groups_data, items_data = data["groups"], data["items"]
    if not isinstance(groups_data, list) or not isinstance(items_data, list):
        raise ValueError("duplicate bin batch collections are invalid")
    groups = tuple(_group_from_dict(item) for item in groups_data)
    items = tuple(_item_from_dict(item) for item in items_data)
    if not groups or not items:
        raise ValueError("duplicate bin approved batch must contain selections")
    if status == BATCH_COMPLETED and any(
        item.state != ITEM_RESTORED for item in items
    ):
        raise ValueError("completed duplicate bin batch contains unresolved items")
    if (
        all(item.state == ITEM_RESTORED for item in items)
        and status not in FULL_RESTORE_RECONCILABLE_BATCH_STATUSES
        and status != BATCH_COMPLETED
    ):
        raise ValueError("fully restored duplicate bin batch status is invalid")
    if len({group.group_id for group in groups}) != len(groups):
        raise ValueError("duplicate bin group IDs must be unique")
    if len({item.bin_item_id for item in items}) != len(items):
        raise ValueError("duplicate bin item IDs must be unique")
    selected_ids = [
        member_id for group in groups for member_id in group.selected_member_ids
    ]
    item_member_ids = [item.selected_member_id for item in items]
    if (
        len(set(selected_ids)) != len(selected_ids)
        or len(set(item_member_ids)) != len(item_member_ids)
        or set(item_member_ids) != set(selected_ids)
    ):
        raise ValueError("duplicate bin items do not match selected members")
    approved_by_member = {
        member.member_id: (group, member)
        for group in groups
        for member in group.members
        if member.member_id in group.selected_member_ids
    }
    for item in items:
        group, member = approved_by_member[item.selected_member_id]
        expected_fingerprint = canonical_digest(
            {
                "schema": "loci-duplicate-approval-metadata-v1",
                "size_bytes": member.size_bytes,
                "modified_time": member.modified_time,
            }
        )
        if (
            item.previous_relative_path != member.relative_path
            or item.size_bytes != member.size_bytes
            or item.approved_sha256 != group.approved_sha256
            or item.approval_metadata_fingerprint != expected_fingerprint
        ):
            raise ValueError("duplicate bin item approval evidence is inconsistent")
    batch = ApprovedDuplicateBatch(
        batch_id=_uuid_string(data["batch_id"], "batch_id"),
        configured_root_identity=_digest(
            data["configured_root_identity"], "root identity"
        ),
        created_by_session_id=_string(
            data["created_by_session_id"], "session provenance"
        ),
        originating_scan_generation=_string(
            data["originating_scan_generation"], "scan generation"
        ),
        decision_revision=_positive_integer(
            data["decision_revision"], "decision revision", allow_zero=True
        ),
        approved_at=_timestamp(data["approved_at"], "approved_at"),
        approved_plan_digest=_digest(
            data["approved_plan_digest"], "approved plan digest"
        ),
        duplicate_batch_digest=_digest(
            data["duplicate_batch_digest"], "duplicate batch digest"
        ),
        batch_version=_positive_integer(data["batch_version"], "batch version"),
        status=status,
        created_at=_timestamp(data["created_at"], "created_at"),
        updated_at=_timestamp(data["updated_at"], "updated_at"),
        groups=groups,
        items=items,
    )
    if tuple(sorted(groups, key=lambda group: group.group_id)) != groups:
        raise ValueError("duplicate bin groups are not canonical")
    if tuple(sorted(items, key=lambda item: item.bin_item_id)) != items:
        raise ValueError("duplicate bin items are not canonical")
    return batch


def _group_from_dict(data: object) -> ApprovedDuplicateGroup:
    required = {
        "group_id",
        "choice_id",
        "approved_sha256",
        "approved_size_bytes",
        "members",
        "selected_member_ids",
        "retained_member_ids",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("duplicate bin group fields are invalid")
    members_data = data["members"]
    selected_data = data["selected_member_ids"]
    retained_data = data["retained_member_ids"]
    if (
        not isinstance(members_data, list)
        or not isinstance(selected_data, list)
        or not isinstance(retained_data, list)
    ):
        raise ValueError("duplicate bin group collections are invalid")
    members = tuple(_member_from_dict(item) for item in members_data)
    selected = tuple(_uuid_string(item, "selected member ID") for item in selected_data)
    retained = tuple(_uuid_string(item, "retained member ID") for item in retained_data)
    member_ids = {member.member_id for member in members}
    if (
        not members
        or not selected
        or not retained
        or set(selected).intersection(retained)
        or set(selected).union(retained) != member_ids
        or len(member_ids) != len(members)
    ):
        raise ValueError("duplicate bin group membership is invalid")
    if tuple(sorted(members, key=lambda member: member.relative_path)) != members:
        raise ValueError("duplicate bin group members are not canonical")
    if tuple(sorted(selected)) != selected or tuple(sorted(retained)) != retained:
        raise ValueError("duplicate bin membership IDs are not canonical")
    return ApprovedDuplicateGroup(
        group_id=_uuid_string(data["group_id"], "group_id"),
        choice_id=_string(data["choice_id"], "choice_id"),
        approved_sha256=_digest(data["approved_sha256"], "approved SHA-256"),
        approved_size_bytes=_positive_integer(
            data["approved_size_bytes"], "approved size", allow_zero=True
        ),
        members=members,
        selected_member_ids=selected,
        retained_member_ids=retained,
    )


def _member_from_dict(data: object) -> ApprovedDuplicateMember:
    required = {"member_id", "relative_path", "size_bytes", "modified_time"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("duplicate bin member fields are invalid")
    return ApprovedDuplicateMember(
        member_id=_uuid_string(data["member_id"], "member_id"),
        relative_path=_relative_path(data["relative_path"], "member path"),
        size_bytes=_positive_integer(
            data["size_bytes"], "member size", allow_zero=True
        ),
        modified_time=_finite_number(data["modified_time"], "member modified time"),
    )


def _item_from_dict(data: object) -> SelectedDuplicateBinItem:
    required = {
        "bin_item_id",
        "selected_member_id",
        "previous_relative_path",
        "future_relative_path",
        "basename",
        "size_bytes",
        "kind",
        "extension",
        "approved_sha256",
        "approval_metadata_fingerprint",
        "state",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("duplicate bin item fields are invalid")
    item_id = _uuid_string(data["bin_item_id"], "bin_item_id")
    previous = _relative_path(data["previous_relative_path"], "previous path")
    future = _relative_path(data["future_relative_path"], "future path")
    basename = _string(data["basename"], "basename")
    expected_prefix = f"AI_Review/duplicate_bin/items/{item_id}/"
    if (
        future != f"{expected_prefix}{basename}"
        or "/" in basename
        or "\\" in basename
        or basename in {".", ".."}
    ):
        raise ValueError("duplicate bin future item path is invalid")
    if Path(previous).name != basename:
        raise ValueError("duplicate bin basename does not match previous path")
    state = _string(data["state"], "item state")
    if state not in ITEM_STATES:
        raise ValueError("duplicate bin item state is invalid")
    extension = data["extension"]
    if not isinstance(extension, str) or "/" in extension or "\\" in extension:
        raise ValueError("duplicate bin extension is invalid")
    return SelectedDuplicateBinItem(
        bin_item_id=item_id,
        selected_member_id=_uuid_string(
            data["selected_member_id"], "selected_member_id"
        ),
        previous_relative_path=previous,
        future_relative_path=future,
        basename=basename,
        size_bytes=_positive_integer(data["size_bytes"], "item size", allow_zero=True),
        kind=_string(data["kind"], "kind"),
        extension=extension,
        approved_sha256=_digest(data["approved_sha256"], "approved SHA-256"),
        approval_metadata_fingerprint=_digest(
            data["approval_metadata_fingerprint"], "metadata fingerprint"
        ),
        state=state,
    )


def _claim_from_dict(data: object) -> ActiveDuplicateCopyClaim:
    required = {
        "claim_key",
        "batch_id",
        "member_id",
        "bin_item_id",
        "relative_path",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("duplicate bin claim fields are invalid")
    relative = _relative_path(data["relative_path"], "claim path")
    claim_key = _digest(data["claim_key"], "claim key")
    if claim_key != normalized_copy_claim_key(relative):
        raise ValueError("duplicate bin claim key is invalid")
    return ActiveDuplicateCopyClaim(
        claim_key=claim_key,
        batch_id=_uuid_string(data["batch_id"], "claim batch_id"),
        member_id=_uuid_string(data["member_id"], "claim member_id"),
        bin_item_id=(
            None
            if data["bin_item_id"] is None
            else _uuid_string(data["bin_item_id"], "claim bin_item_id")
        ),
        relative_path=relative,
    )


def _claims_for_batch(
    batch: ApprovedDuplicateBatch,
) -> tuple[ActiveDuplicateCopyClaim, ...]:
    if batch.status not in CLAIMING_BATCH_STATUSES:
        return ()
    item_ids_by_member = {
        item.selected_member_id: item.bin_item_id for item in batch.items
    }
    return tuple(
        sorted(
            (
                ActiveDuplicateCopyClaim(
                    claim_key=normalized_copy_claim_key(member.relative_path),
                    batch_id=batch.batch_id,
                    member_id=member.member_id,
                    bin_item_id=item_ids_by_member.get(member.member_id),
                    relative_path=member.relative_path,
                )
                for group in batch.groups
                for member in group.members
            ),
            key=lambda claim: claim.claim_key,
        )
    )


def _all_active_claims(
    batches: tuple[ApprovedDuplicateBatch, ...],
) -> tuple[ActiveDuplicateCopyClaim, ...]:
    claims = tuple(
        sorted(
            (claim for batch in batches for claim in _claims_for_batch(batch)),
            key=lambda claim: claim.claim_key,
        )
    )
    if len({claim.claim_key for claim in claims}) != len(claims):
        raise DuplicateBinRepositoryError("active_copy_claim_conflict")
    return claims


def _same_durable_approval(
    existing: ApprovedDuplicateBatch, candidate: ApprovedDuplicateBatch
) -> bool:
    ignored = {
        "created_by_session_id",
        "approved_at",
        "created_at",
        "updated_at",
        "batch_version",
        "status",
    }
    left = {
        key: value
        for key, value in _batch_to_dict(existing).items()
        if key not in ignored
    }
    right = {
        key: value
        for key, value in _batch_to_dict(candidate).items()
        if key not in ignored
    }
    return left == right


def _relative_path(value: object, label: str) -> str:
    text = _string(value, label)
    if "\\" in text:
        raise ValueError(f"{label} must be a POSIX relative path")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a safe relative path")
    return path.as_posix()


def _uuid_string(value: object, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise ValueError(f"{label} must be an opaque UUID") from error
    if str(parsed) != text:
        raise ValueError(f"{label} must be a canonical UUID")
    return text


def _digest(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return text


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _positive_integer(value: object, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer of at least {minimum}")
    return value


def _finite_number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be finite")
    return float(value)


def _timestamp(value: object, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return text


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _reject_symlink_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if not os.path.lexists(current):
            continue
        if current.is_symlink() or not current.is_dir():
            raise DuplicateBinRepositoryError("duplicate_bin_catalog_unsafe")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
