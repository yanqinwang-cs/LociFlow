from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import unicodedata


DUPLICATE_BIN_SCHEMA_VERSION = 1
DUPLICATE_BIN_BATCH_RECORD_TYPE = "loci_duplicate_bin_approved_batch"
DUPLICATE_BIN_CATALOG_RECORD_TYPE = "loci_duplicate_bin_catalog"

BATCH_APPROVED = "approved"
BATCH_MOVING = "moving"
BATCH_PARTIALLY_MOVED = "partially_moved"
BATCH_COMPLETED = "completed"
BATCH_INTERRUPTED = "interrupted"
BATCH_ATTENTION = "attention"
BATCH_SUPERSEDED = "superseded"
BATCH_CANCELLED = "cancelled"

BATCH_STATUSES = frozenset(
    {
        BATCH_APPROVED,
        BATCH_MOVING,
        BATCH_PARTIALLY_MOVED,
        BATCH_COMPLETED,
        BATCH_INTERRUPTED,
        BATCH_ATTENTION,
        BATCH_SUPERSEDED,
        BATCH_CANCELLED,
    }
)
CLAIMING_BATCH_STATUSES = BATCH_STATUSES - {BATCH_SUPERSEDED, BATCH_CANCELLED}
SUPERSEDABLE_BATCH_STATUSES = frozenset({BATCH_APPROVED})

ITEM_APPROVED = "approved"


@dataclass(frozen=True)
class ApprovedDuplicateMember:
    member_id: str
    relative_path: str
    size_bytes: int
    modified_time: float


@dataclass(frozen=True)
class ApprovedDuplicateGroup:
    group_id: str
    choice_id: str
    approved_sha256: str
    approved_size_bytes: int
    members: tuple[ApprovedDuplicateMember, ...]
    selected_member_ids: tuple[str, ...]
    retained_member_ids: tuple[str, ...]


@dataclass(frozen=True)
class SelectedDuplicateBinItem:
    bin_item_id: str
    selected_member_id: str
    previous_relative_path: str
    future_relative_path: str
    basename: str
    size_bytes: int
    kind: str
    extension: str
    approved_sha256: str
    approval_metadata_fingerprint: str
    state: str = ITEM_APPROVED


@dataclass(frozen=True)
class ActiveDuplicateCopyClaim:
    claim_key: str
    batch_id: str
    member_id: str
    bin_item_id: str | None
    relative_path: str


@dataclass(frozen=True)
class SafeDuplicateBatchProjection:
    id: str
    status: str
    copies: int

    def to_dict(self) -> dict[str, str | int]:
        return {"id": self.id, "status": self.status, "copies": self.copies}


@dataclass(frozen=True)
class ApprovedDuplicateBatch:
    batch_id: str
    configured_root_identity: str
    created_by_session_id: str
    originating_scan_generation: str
    decision_revision: int
    approved_at: str
    approved_plan_digest: str
    duplicate_batch_digest: str
    batch_version: int
    status: str
    created_at: str
    updated_at: str
    groups: tuple[ApprovedDuplicateGroup, ...]
    items: tuple[SelectedDuplicateBinItem, ...]
    schema_version: int = DUPLICATE_BIN_SCHEMA_VERSION
    record_type: str = DUPLICATE_BIN_BATCH_RECORD_TYPE

    @property
    def can_receive_authorization(self) -> bool:
        return self.status == BATCH_APPROVED

    def safe_projection(self) -> SafeDuplicateBatchProjection:
        return SafeDuplicateBatchProjection(
            id=self.batch_id,
            status=self.status,
            copies=len(self.items),
        )


@dataclass(frozen=True)
class DuplicateBinCatalog:
    configured_root_identity: str
    catalog_version: int
    batches: tuple[ApprovedDuplicateBatch, ...]
    active_claims: tuple[ActiveDuplicateCopyClaim, ...]
    schema_version: int = DUPLICATE_BIN_SCHEMA_VERSION
    record_type: str = DUPLICATE_BIN_CATALOG_RECORD_TYPE


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def normalized_copy_claim_key(relative_path: str) -> str:
    parts = relative_path.split("/")
    normalized = [unicodedata.normalize("NFC", part).casefold() for part in parts]
    return canonical_digest(
        {
            "schema": "loci-duplicate-copy-claim-v1",
            "path_parts": normalized,
        }
    )
