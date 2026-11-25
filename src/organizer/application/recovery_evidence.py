from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from organizer.application.content_identity import OrdinaryFileIdentityCompatibility
from organizer.application.duplicate_bin_models import canonical_json_bytes
from organizer.application.duplicate_bin_movement_models import (
    MOVE_ATTEMPTING as DUPLICATE_MOVE_ATTEMPTING,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_MOVED as DUPLICATE_MOVE_MOVED,
)
from organizer.application.duplicate_bin_movement_models import (
    MOVE_PENDING as DUPLICATE_MOVE_PENDING,
)
from organizer.application.duplicate_bin_movement_models import (
    OP_INTERRUPTED,
    OP_MOVED_UNVERIFIED,
    OP_MOVING,
    OP_PREPARED,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_ATTEMPTING as DUPLICATE_RESTORE_ATTEMPTING,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_PENDING as DUPLICATE_RESTORE_PENDING,
)
from organizer.application.duplicate_bin_restore_models import (
    ITEM_RESTORED as DUPLICATE_RESTORE_RESTORED,
)
from organizer.application.duplicate_bin_restore_models import (
    RESTORE_INTERRUPTED,
    RESTORE_PREPARED,
    RESTORE_RESTORED_UNVERIFIED,
    RESTORE_RESTORING,
)
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationInspector,
    _InconsistentOperationState,
)
from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_MOVED,
    MOVE_PENDING,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVING,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_PREPARED,
)
from organizer.application.organization_undo_models import (
    UNDO_INTERRUPTED,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_PENDING,
    UNDO_ITEM_RESTORED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
)
from organizer.duplicates import hash_file


class RecoveryEvidenceState(str, Enum):
    CLEAR = "clear"
    UNRESOLVED = "unresolved"
    NON_ACTIONABLE = "non_actionable"


class RecoveryOperationFamily(str, Enum):
    ORGANIZATION_MOVEMENT = "organization_movement"
    ORGANIZATION_UNDO = "organization_undo"
    DUPLICATE_BIN_MOVEMENT = "duplicate_bin_movement"
    DUPLICATE_BIN_RESTORE = "duplicate_bin_restore"


class RecoveryActionability(str, Enum):
    INSPECT_ONLY = "inspect_only"
    CHECK_RESULT_POSSIBLE = "check_result_possible"
    CONTINUE_POSSIBLE = "continue_possible"
    CHECK_OR_CONTINUE_POSSIBLE = "check_or_continue_possible"
    NON_ACTIONABLE_MALFORMED = "non_actionable_malformed"
    NON_ACTIONABLE_INCONSISTENT = "non_actionable_inconsistent"
    NON_ACTIONABLE_AMBIGUOUS = "non_actionable_ambiguous"
    MULTIPLE_UNRESOLVED_OPERATIONS = "multiple_unresolved_operations"


class RecoveryActionKind(str, Enum):
    CHECK_RESULT = "check_result"
    CONTINUE = "continue"


class FilesystemEvidence(str, Enum):
    SOURCE_PRESENT_DESTINATION_ABSENT = "source_present_destination_absent"
    SOURCE_ABSENT_DESTINATION_PRESENT = "source_absent_destination_present"
    BOTH_PRESENT = "both_present"
    NEITHER_PRESENT = "neither_present"
    EXPECTED_IDENTITY_MATCHES = "expected_identity_matches"
    EXPECTED_IDENTITY_MISMATCH = "expected_identity_mismatch"
    UNSAFE_SYMLINK = "unsafe_symlink"
    UNREADABLE = "unreadable"
    AMBIGUOUS = "ambiguous"


class _ItemDisposition(str, Enum):
    PENDING = "pending"
    COMPLETE = "complete"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RecoveryItemEvidence:
    recorded_state: str
    observations: tuple[FilesystemEvidence, ...]
    disposition: _ItemDisposition
    evidence_fingerprint: str


@dataclass(frozen=True)
class RecoveryEvidenceDigest:
    value: str


@dataclass(frozen=True)
class RecoveryEvidenceCase:
    family: RecoveryOperationFamily
    record_identity: str
    durable_status: str
    affected_items: int
    actionability: RecoveryActionability
    available_action_kinds: tuple[RecoveryActionKind, ...]
    items: tuple[RecoveryItemEvidence, ...]
    digest: RecoveryEvidenceDigest


@dataclass(frozen=True)
class SafeRecoveryEvidenceProjection:
    state: str
    title: str
    message: str
    affected_items: int
    is_actionable: bool
    available_action_kinds: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "title": self.title,
            "message": self.message,
            "affected_items": self.affected_items,
            "is_actionable": self.is_actionable,
            "available_action_kinds": list(self.available_action_kinds),
        }


@dataclass(frozen=True)
class RecoveryEvidenceResult:
    state: RecoveryEvidenceState
    actionability: RecoveryActionability | None
    unresolved_count: int
    case: RecoveryEvidenceCase | None

    def safe_projection(self) -> SafeRecoveryEvidenceProjection:
        if self.state is RecoveryEvidenceState.CLEAR:
            return SafeRecoveryEvidenceProjection(
                state="clear",
                title="No unfinished file operation.",
                message="Loci Flow can continue.",
                affected_items=0,
                is_actionable=False,
                available_action_kinds=(),
            )
        if self.case is None:
            multiple = (
                self.actionability
                is RecoveryActionability.MULTIPLE_UNRESOLVED_OPERATIONS
            )
            return SafeRecoveryEvidenceProjection(
                state="needs_attention",
                title=(
                    "More than one file operation needs attention."
                    if multiple
                    else "A file operation needs attention."
                ),
                message="Loci Flow can’t continue this file operation automatically.",
                affected_items=self.unresolved_count if multiple else 0,
                is_actionable=False,
                available_action_kinds=(),
            )
        actions = tuple(item.value for item in self.case.available_action_kinds)
        return SafeRecoveryEvidenceProjection(
            state="needs_attention",
            title="A file operation needs attention.",
            message=(
                "Review what happened before continuing."
                if actions
                or self.case.actionability is RecoveryActionability.INSPECT_ONLY
                else "Loci Flow can’t continue this file operation automatically."
            ),
            affected_items=self.case.affected_items,
            is_actionable=bool(actions),
            available_action_kinds=actions,
        )


@dataclass(frozen=True)
class _PathEvidence:
    present: bool
    identity_matches: bool | None
    observed_content_digest: str | None = None
    exceptional: FilesystemEvidence | None = None


@dataclass(frozen=True)
class _UnresolvedRecord:
    family: RecoveryOperationFamily
    record: object


class RecoveryEvidenceInspector:
    """Explicit, deterministic and strictly read-only recovery evidence owner."""

    def __init__(self, inspector: FilesystemOperationInspector) -> None:
        self.inspector = inspector
        self.root = inspector.root

    def inspect(self) -> RecoveryEvidenceResult:
        try:
            state = self._read_state()
        except _InconsistentOperationState:
            return _non_actionable(RecoveryActionability.NON_ACTIONABLE_INCONSISTENT)
        except (OSError, UnicodeError, ValueError):
            return _non_actionable(RecoveryActionability.NON_ACTIONABLE_MALFORMED)
        except Exception:
            return _non_actionable(RecoveryActionability.NON_ACTIONABLE_MALFORMED)

        unresolved = state["unresolved"]
        if not unresolved:
            return RecoveryEvidenceResult(RecoveryEvidenceState.CLEAR, None, 0, None)
        if len(unresolved) > 1:
            return RecoveryEvidenceResult(
                RecoveryEvidenceState.NON_ACTIONABLE,
                RecoveryActionability.MULTIPLE_UNRESOLVED_OPERATIONS,
                len(unresolved),
                None,
            )
        try:
            evidence_case = self._case(unresolved[0], state)
        except (OSError, UnicodeError, ValueError):
            return _non_actionable(
                RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS,
                unresolved_count=1,
            )
        except Exception:
            return _non_actionable(
                RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS,
                unresolved_count=1,
            )
        return RecoveryEvidenceResult(
            RecoveryEvidenceState.UNRESOLVED,
            evidence_case.actionability,
            1,
            evidence_case,
        )

    def _read_state(self) -> dict[str, object]:
        organization = self.inspector.organization_operations.list_records()
        undos = self.inspector.organization_undos.list_records()
        catalog = self.inspector.duplicate_bin_batches.read_catalog()
        move_authorizations = self.inspector.duplicate_bin_authorizations.list_records()
        duplicate_moves = self.inspector.duplicate_bin_operations.list_records()
        restore_authorizations = (
            self.inspector.duplicate_bin_restores.list_authorizations()
        )
        restores = self.inspector.duplicate_bin_restores.list_operations()

        self.inspector._validate_ordinary(organization, undos)
        self.inspector._validate_duplicate_bin_movement(
            catalog.batches, move_authorizations, duplicate_moves
        )
        self.inspector._validate_duplicate_bin_restore(
            catalog.batches,
            duplicate_moves,
            restore_authorizations,
            restores,
        )

        unresolved = tuple(
            [
                _UnresolvedRecord(RecoveryOperationFamily.ORGANIZATION_MOVEMENT, item)
                for item in organization
                if item.status
                in {
                    STATUS_PREPARED,
                    STATUS_MOVING,
                    STATUS_MOVED_UNVERIFIED,
                    STATUS_INTERRUPTED,
                    STATUS_PARTIAL_MOVEMENT,
                }
            ]
            + [
                _UnresolvedRecord(RecoveryOperationFamily.ORGANIZATION_UNDO, item)
                for item in undos
                if item.status
                in {
                    UNDO_PREPARED,
                    UNDO_UNDOING,
                    UNDO_RESTORED_UNVERIFIED,
                    UNDO_INTERRUPTED,
                    UNDO_PARTIAL,
                }
            ]
            + [
                _UnresolvedRecord(RecoveryOperationFamily.DUPLICATE_BIN_MOVEMENT, item)
                for item in duplicate_moves
                if item.status
                in {OP_PREPARED, OP_MOVING, OP_MOVED_UNVERIFIED, OP_INTERRUPTED}
            ]
            + [
                _UnresolvedRecord(RecoveryOperationFamily.DUPLICATE_BIN_RESTORE, item)
                for item in restores
                if item.status
                in {
                    RESTORE_PREPARED,
                    RESTORE_RESTORING,
                    RESTORE_RESTORED_UNVERIFIED,
                    RESTORE_INTERRUPTED,
                }
            ]
        )
        return {
            "organization": organization,
            "undos": undos,
            "catalog": catalog,
            "move_authorizations": move_authorizations,
            "duplicate_moves": duplicate_moves,
            "restore_authorizations": restore_authorizations,
            "restores": restores,
            "unresolved": unresolved,
        }

    def _case(
        self, unresolved: _UnresolvedRecord, state: dict[str, object]
    ) -> RecoveryEvidenceCase:
        family = unresolved.family
        record = unresolved.record
        if (
            family is RecoveryOperationFamily.ORGANIZATION_MOVEMENT
            and record.status == STATUS_PARTIAL_MOVEMENT
        ):
            return _partial_case(
                self.inspector.organization_operations.configured_root_identity,
                family,
                record.operation_id,
                record,
                len(record.expected_moves),
            )
        if (
            family is RecoveryOperationFamily.ORGANIZATION_UNDO
            and record.status == UNDO_PARTIAL
        ):
            return _partial_case(
                self.inspector.organization_operations.configured_root_identity,
                family,
                record.undo_id,
                record,
                len(record.expected_restores),
            )
        if family is RecoveryOperationFamily.ORGANIZATION_MOVEMENT:
            items = tuple(
                self._item(
                    item.result_state,
                    item.source,
                    item.destination,
                    expected_size=item.source_size_bytes,
                    expected_mtime=item.source_modified_time,
                )
                for item in record.expected_moves
            )
            identity = record.operation_id
            related = ()
            finished_state = STATUS_MOVED_UNVERIFIED
            completed_states = {MOVE_MOVED}
            pending_states = {MOVE_PENDING, MOVE_ATTEMPTING}
        elif family is RecoveryOperationFamily.ORGANIZATION_UNDO:
            parent = next(
                (
                    item
                    for item in state["organization"]
                    if item.operation_id == record.operation_id
                ),
                None,
            )
            if parent is None:
                raise _InconsistentOperationState
            items = tuple(
                self._item(
                    item.result_state,
                    item.moved_from,
                    item.restore_target,
                    expected_size=item.baseline_size_bytes,
                    expected_mtime=item.baseline_modified_time,
                )
                for item in record.expected_restores
            )
            identity = record.undo_id
            related = (parent,)
            finished_state = UNDO_RESTORED_UNVERIFIED
            completed_states = {UNDO_ITEM_RESTORED}
            pending_states = {UNDO_ITEM_PENDING, UNDO_ITEM_ATTEMPTING}
        elif family is RecoveryOperationFamily.DUPLICATE_BIN_MOVEMENT:
            batch = next(
                (
                    item
                    for item in state["catalog"].batches
                    if item.batch_id == record.batch_id
                ),
                None,
            )
            authorization = next(
                (
                    item
                    for item in state["move_authorizations"]
                    if item.authorization_id == record.authorization_id
                ),
                None,
            )
            if batch is None or authorization is None:
                raise _InconsistentOperationState
            items = tuple(
                self._item(
                    item.result_state,
                    item.source,
                    item.destination,
                    expected_size=item.source_size_bytes,
                    expected_hash=item.expected_sha256,
                )
                for item in record.expected_moves
            )
            identity = record.operation_id
            related = (batch, authorization)
            finished_state = OP_MOVED_UNVERIFIED
            completed_states = {DUPLICATE_MOVE_MOVED}
            pending_states = {DUPLICATE_MOVE_PENDING, DUPLICATE_MOVE_ATTEMPTING}
        else:
            batch_ids = {item.batch_id for item in record.items}
            batches = tuple(
                item for item in state["catalog"].batches if item.batch_id in batch_ids
            )
            parents = tuple(
                item
                for item in state["duplicate_moves"]
                if item.operation_id in record.parent_movement_operation_ids
            )
            authorization = next(
                (
                    item
                    for item in state["restore_authorizations"]
                    if item.authorization_id == record.authorization_id
                ),
                None,
            )
            if not batches or not parents or authorization is None:
                raise _InconsistentOperationState
            items = tuple(
                self._item(
                    item.state,
                    item.protected_source,
                    item.previous_destination,
                    expected_size=item.expected_size_bytes,
                    expected_hash=item.expected_sha256,
                )
                for item in record.items
            )
            identity = record.restore_operation_id
            related = batches + parents + (authorization,)
            finished_state = RESTORE_RESTORED_UNVERIFIED
            completed_states = {DUPLICATE_RESTORE_RESTORED}
            pending_states = {DUPLICATE_RESTORE_PENDING, DUPLICATE_RESTORE_ATTEMPTING}

        actionability, actions = _classify(
            family,
            record.status,
            finished_state,
            items,
            completed_states,
            pending_states,
        )
        if family is RecoveryOperationFamily.ORGANIZATION_MOVEMENT and (
            record.status == STATUS_MOVED_UNVERIFIED
            or record.identity_compatibility
            is OrdinaryFileIdentityCompatibility.IDENTITY_V2
        ):
            # Neither historical metadata nor the disconnected v2 verifier is
            # a public recovery capability. Review remains the only safe action.
            actionability = RecoveryActionability.INSPECT_ONLY
            actions = ()
        elif family is RecoveryOperationFamily.ORGANIZATION_UNDO and (
            record.status == UNDO_RESTORED_UNVERIFIED
            or record.identity_compatibility
            is OrdinaryFileIdentityCompatibility.IDENTITY_V2
        ):
            # Historical v1 records lack authoritative content identity, and
            # the v2 verifier is not yet a public recovery capability. Neither
            # representation can safely advertise result checking here.
            actionability = RecoveryActionability.INSPECT_ONLY
            actions = ()
        digest = _digest(
            self.inspector.organization_operations.configured_root_identity,
            family,
            identity,
            record,
            related,
            items,
        )
        return RecoveryEvidenceCase(
            family=family,
            record_identity=identity,
            durable_status=record.status,
            affected_items=len(items),
            actionability=actionability,
            available_action_kinds=actions,
            items=items,
            digest=RecoveryEvidenceDigest(digest),
        )

    def _item(
        self,
        recorded_state: str,
        source: str,
        destination: str,
        *,
        expected_size: int,
        expected_mtime: float | None = None,
        expected_hash: str | None = None,
    ) -> RecoveryItemEvidence:
        source_evidence = _path_evidence(
            self.root,
            source,
            expected_size,
            expected_mtime,
            expected_hash,
        )
        destination_evidence = _path_evidence(
            self.root,
            destination,
            expected_size,
            expected_mtime,
            expected_hash,
        )
        observations, disposition, fingerprint = _combine(
            source_evidence, destination_evidence
        )
        return RecoveryItemEvidence(
            recorded_state,
            observations,
            disposition,
            fingerprint,
        )


def _partial_case(
    root_identity: str,
    family: RecoveryOperationFamily,
    record_identity: str,
    record: object,
    affected_items: int,
) -> RecoveryEvidenceCase:
    """Project terminal-but-blocking partial history without inspecting paths."""

    return RecoveryEvidenceCase(
        family=family,
        record_identity=record_identity,
        durable_status=record.status,
        affected_items=affected_items,
        actionability=RecoveryActionability.INSPECT_ONLY,
        available_action_kinds=(),
        items=(),
        digest=RecoveryEvidenceDigest(
            _digest(
                root_identity,
                family,
                record_identity,
                record,
                (),
                (),
            )
        ),
    )


def _classify(
    family: RecoveryOperationFamily,
    durable_status: str,
    finished_state: str,
    items: tuple[RecoveryItemEvidence, ...],
    completed_states: set[str],
    pending_states: set[str],
) -> tuple[RecoveryActionability, tuple[RecoveryActionKind, ...]]:
    if not items or any(
        item.disposition is _ItemDisposition.AMBIGUOUS for item in items
    ):
        return RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS, ()
    all_complete = all(item.disposition is _ItemDisposition.COMPLETE for item in items)
    all_pending = all(item.disposition is _ItemDisposition.PENDING for item in items)
    recorded_complete = all(item.recorded_state in completed_states for item in items)
    recorded_pre_move = all(item.recorded_state in pending_states for item in items)

    ordinary_without_content_identity = family in {
        RecoveryOperationFamily.ORGANIZATION_MOVEMENT,
        RecoveryOperationFamily.ORGANIZATION_UNDO,
    }
    if ordinary_without_content_identity and durable_status != finished_state:
        return RecoveryActionability.INSPECT_ONLY, ()

    if durable_status == finished_state:
        if all_complete and recorded_complete:
            return (
                RecoveryActionability.CHECK_RESULT_POSSIBLE,
                (RecoveryActionKind.CHECK_RESULT,),
            )
        return RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS, ()
    if durable_status in {
        STATUS_PREPARED,
        UNDO_PREPARED,
        OP_PREPARED,
        RESTORE_PREPARED,
    }:
        if all_pending and recorded_pre_move:
            return (
                RecoveryActionability.CONTINUE_POSSIBLE,
                (RecoveryActionKind.CONTINUE,),
            )
        return RecoveryActionability.NON_ACTIONABLE_AMBIGUOUS, ()
    if all_complete:
        return (
            RecoveryActionability.CHECK_RESULT_POSSIBLE,
            (RecoveryActionKind.CHECK_RESULT,),
        )
    if all_pending:
        return (
            RecoveryActionability.CONTINUE_POSSIBLE,
            (RecoveryActionKind.CONTINUE,),
        )
    return (
        RecoveryActionability.CHECK_OR_CONTINUE_POSSIBLE,
        (RecoveryActionKind.CHECK_RESULT, RecoveryActionKind.CONTINUE),
    )


def _path_evidence(
    root: Path,
    relative: str,
    expected_size: int,
    expected_mtime: float | None,
    expected_hash: str | None,
) -> _PathEvidence:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
        return _PathEvidence(False, None, exceptional=FilesystemEvidence.AMBIGUOUS)
    current = root
    try:
        for index, component in enumerate(parts):
            current = current / component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                return _PathEvidence(False, None)
            if stat.S_ISLNK(metadata.st_mode):
                return _PathEvidence(
                    True, None, exceptional=FilesystemEvidence.UNSAFE_SYMLINK
                )
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                return _PathEvidence(
                    True, None, exceptional=FilesystemEvidence.AMBIGUOUS
                )
        if not stat.S_ISREG(metadata.st_mode):
            return _PathEvidence(True, None, exceptional=FilesystemEvidence.AMBIGUOUS)
        identity_matches = metadata.st_size == expected_size and (
            expected_mtime is None or metadata.st_mtime == expected_mtime
        )
        before = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        live_hash = hash_file(current)
        after_metadata = os.lstat(current)
        after = (
            after_metadata.st_dev,
            after_metadata.st_ino,
            after_metadata.st_size,
            after_metadata.st_mtime_ns,
        )
        if before != after or stat.S_ISLNK(after_metadata.st_mode):
            return _PathEvidence(True, None, None, FilesystemEvidence.AMBIGUOUS)
        if expected_hash is not None and live_hash != expected_hash:
            identity_matches = False
        return _PathEvidence(True, identity_matches, live_hash)
    except (OSError, UnicodeError, ValueError):
        return _PathEvidence(False, None, exceptional=FilesystemEvidence.UNREADABLE)


def _combine(
    source: _PathEvidence, destination: _PathEvidence
) -> tuple[tuple[FilesystemEvidence, ...], _ItemDisposition, str]:
    fingerprint = hashlib.sha256(
        canonical_json_bytes(
            {
                "source_present": source.present,
                "source_content": source.observed_content_digest,
                "source_exception": (
                    source.exceptional.value if source.exceptional else None
                ),
                "destination_present": destination.present,
                "destination_content": destination.observed_content_digest,
                "destination_exception": (
                    destination.exceptional.value if destination.exceptional else None
                ),
            }
        )
    ).hexdigest()
    exceptional = tuple(
        item
        for item in (source.exceptional, destination.exceptional)
        if item is not None
    )
    if exceptional:
        return (
            tuple(sorted(set(exceptional), key=lambda item: item.value)),
            _ItemDisposition.AMBIGUOUS,
            fingerprint,
        )
    if source.present and destination.present:
        presence = FilesystemEvidence.BOTH_PRESENT
    elif not source.present and not destination.present:
        presence = FilesystemEvidence.NEITHER_PRESENT
    elif source.present:
        presence = FilesystemEvidence.SOURCE_PRESENT_DESTINATION_ABSENT
    else:
        presence = FilesystemEvidence.SOURCE_ABSENT_DESTINATION_PRESENT
    identity = (
        source.identity_matches if source.present else destination.identity_matches
    )
    identity_evidence = (
        FilesystemEvidence.EXPECTED_IDENTITY_MATCHES
        if identity is True
        else FilesystemEvidence.EXPECTED_IDENTITY_MISMATCH
        if identity is False
        else FilesystemEvidence.AMBIGUOUS
    )
    if (
        presence is FilesystemEvidence.SOURCE_PRESENT_DESTINATION_ABSENT
        and source.identity_matches is True
    ):
        disposition = _ItemDisposition.PENDING
    elif (
        presence is FilesystemEvidence.SOURCE_ABSENT_DESTINATION_PRESENT
        and destination.identity_matches is True
    ):
        disposition = _ItemDisposition.COMPLETE
    else:
        disposition = _ItemDisposition.AMBIGUOUS
    return (presence, identity_evidence), disposition, fingerprint


def _digest(
    root_identity: str,
    family: RecoveryOperationFamily,
    record_identity: str,
    record: object,
    related: tuple[object, ...],
    items: tuple[RecoveryItemEvidence, ...],
) -> str:
    value = {
        "schema": "loci-flow-recovery-evidence-v1",
        "root_identity": root_identity,
        "family": family.value,
        "record_identity": record_identity,
        "record": asdict(record),
        "related": [asdict(item) for item in related],
        "observations": [
            {
                "recorded_state": item.recorded_state,
                "observations": [value.value for value in item.observations],
                "disposition": item.disposition.value,
                "evidence_fingerprint": item.evidence_fingerprint,
            }
            for item in items
        ],
    }
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _non_actionable(
    actionability: RecoveryActionability,
    *,
    unresolved_count: int = 0,
) -> RecoveryEvidenceResult:
    return RecoveryEvidenceResult(
        RecoveryEvidenceState.NON_ACTIONABLE,
        actionability,
        unresolved_count,
        None,
    )
