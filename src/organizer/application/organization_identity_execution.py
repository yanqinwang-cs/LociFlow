from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable
import uuid

from organizer.application.execution_coordinator import approved_plan_identity
from organizer.application.file_identity_capture import (
    CapturedRegularFileIdentity,
    FileIdentityCaptureError,
    capture_regular_file_identity,
)
from organizer.application.identity_verification_publication import (
    IdentityVerificationPublicationResult,
    OrganizationIdentityVerificationPublicationService,
)
from organizer.application.organization_decision_models import (
    STATUS_READY_TO_MOVE,
    ApprovedOrganizationPlan,
)
from organizer.application.organization_identity_intent import (
    V2OrganizationIdentityIntent,
    build_v2_organization_intent,
)
from organizer.application.organization_movement_preflight import (
    OrganizationMovementPreflightError,
    preflight_organization_movement,
)
from organizer.application.organization_movement_recorder import (
    DurableOrganizationMovementRecorder,
)
from organizer.application.organization_operation_models import (
    AUTHORIZATION_AVAILABLE,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVEMENT_FAILED,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    MovementAuthorization,
    OrganizationOperationRecord,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.volume_admission import (
    OrganizationVolumeAdmission,
    VolumeAdmissionError,
)
from organizer.executor import (
    MovementRecordingError,
    apply_same_volume_identity_move_plan,
)
from organizer.executor_identity import (
    IdentityMovePlanItem,
    StrictExecutorError,
    StrictExecutorInterrupted,
    validate_same_volume_identity_move_plan,
)
from organizer.models import MovePlanItem, OperationLog


_SAFE_MESSAGE = (
    "The approved files could not be moved safely. Check the folder and try again."
)
_ERROR_CODES = {
    "approved_plan_stale",
    "source_changed",
    "protected_item_selected",
    "unsafe_destination",
    "overwrite_detected",
    "cross_volume_plan",
    "indeterminate_volume",
    "unsafe_volume_path",
    "identity_capture_failed",
    "invalid_intent",
    "strict_plan_invalid",
    "durable_intent_failed",
}


class OrganizationIdentityExecutionError(RuntimeError):
    """A bounded failure that occurred before a usable execution result."""

    def __init__(self, code: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("identity movement error code is invalid")
        self.code = code
        self.safe_message = _SAFE_MESSAGE
        super().__init__(self.safe_message)


class IdentityOrganizationExecutionState(str, Enum):
    VERIFIED = STATUS_VERIFIED
    VERIFICATION_FAILED = STATUS_VERIFICATION_FAILED
    MOVED_UNVERIFIED = STATUS_MOVED_UNVERIFIED
    PARTIAL = STATUS_PARTIAL_MOVEMENT
    FAILED = STATUS_MOVEMENT_FAILED
    INTERRUPTED = STATUS_INTERRUPTED


@dataclass(frozen=True, repr=False)
class IdentityOrganizationExecutionResult:
    operation_id: str
    state: IdentityOrganizationExecutionState
    started_at: str | None
    completed_at: str | None
    files_expected: int
    files_moved: int
    files_failed: int
    files_unattempted: int
    verification_status: str

    def __repr__(self) -> str:
        return (
            "IdentityOrganizationExecutionResult("
            f"state={self.state.value!r}, files_expected={self.files_expected}, "
            f"files_moved={self.files_moved}, files_failed={self.files_failed}, "
            f"files_unattempted={self.files_unattempted})"
        )


class OrganizationIdentityExecutionService:
    """Compose the disconnected identity-safe forward movement path.

    The future production caller owns lifecycle, coordinator, admission, and
    browser confirmation. This owner starts only after that explicit envelope.
    """

    def __init__(
        self,
        operation_repository: OrganizationOperationRepository,
        *,
        consume_authorization: Callable[[str], bool],
        volume_admission: OrganizationVolumeAdmission | None = None,
        capture: Callable[[Path, Path], CapturedRegularFileIdentity] = (
            capture_regular_file_identity
        ),
        executor: Callable[..., OperationLog] = (
            apply_same_volume_identity_move_plan
        ),
        publish_verification: (
            Callable[[str], IdentityVerificationPublicationResult] | None
        ) = None,
        operation_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = operation_repository.root
        volume = volume_admission or OrganizationVolumeAdmission(root)
        if volume.root != root:
            raise ValueError("identity movement admission belongs to another root")
        self.operation_repository = operation_repository
        self.root = root
        self._consume_authorization = consume_authorization
        self._volume_admission = volume
        self._capture = capture
        self._executor = executor
        self._operation_id_factory = operation_id_factory or (
            lambda: str(uuid.uuid4())
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._publish_verification = publish_verification or (
            OrganizationIdentityVerificationPublicationService(
                operation_repository,
                now=self._clock,
            ).publish
        )

    def execute(
        self,
        *,
        approved_plan: ApprovedOrganizationPlan,
        authorization: MovementAuthorization,
    ) -> IdentityOrganizationExecutionResult:
        self._validate_authoritative_inputs(approved_plan, authorization)
        try:
            preflight = preflight_organization_movement(
                approved_plan,
                self.root,
                volume_admission=self._volume_admission,
            )
        except OrganizationMovementPreflightError as error:
            raise OrganizationIdentityExecutionError(error.code) from error
        except VolumeAdmissionError as error:
            raise OrganizationIdentityExecutionError(error.code) from error
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            raise OrganizationIdentityExecutionError(
                "approved_plan_stale"
            ) from error

        captures: dict[str, CapturedRegularFileIdentity] = {}
        try:
            for move in preflight.expected_moves:
                captures[move.change_id] = self._capture(
                    self.root / move.source,
                    self.root,
                )
        except FileIdentityCaptureError as error:
            raise OrganizationIdentityExecutionError(
                "identity_capture_failed"
            ) from error
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            raise OrganizationIdentityExecutionError(
                "identity_capture_failed"
            ) from error

        try:
            intent = build_v2_organization_intent(
                operation_id=self._operation_id_factory(),
                created_at=self._timestamp(),
                authorization=authorization,
                approved_plan=approved_plan,
                configured_root_identity=(
                    self.operation_repository.configured_root_identity
                ),
                expected_moves=preflight.expected_moves,
                captures=captures,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            raise OrganizationIdentityExecutionError("invalid_intent") from error

        strict_plan = self._strict_plan(intent)
        try:
            validate_same_volume_identity_move_plan(strict_plan, self.root)
        except StrictExecutorError as error:
            raise OrganizationIdentityExecutionError(
                "strict_plan_invalid"
            ) from error

        try:
            self.operation_repository.create(intent.record)
        except Exception as error:
            raise OrganizationIdentityExecutionError(
                "durable_intent_failed"
            ) from error

        try:
            consumed = self._consume_authorization(
                authorization.authorization_id
            )
        except Exception:
            consumed = False
        if consumed is not True:
            return self._interrupted_result(intent.record)

        recorder = DurableOrganizationMovementRecorder(
            intent.record,
            self.root,
            self.operation_repository,
            clock=self._timestamp,
        )
        try:
            self._executor(strict_plan, self.root, recorder=recorder)
        except StrictExecutorInterrupted:
            self._try_publish_interrupted(intent.record)
            raise
        except StrictExecutorError as error:
            if error.recovery_required or error.filesystem_changed:
                return self._interrupted_result(intent.record)
            return self._failed_result(intent.record)
        except MovementRecordingError as error:
            return self._interrupted_result(
                intent.record,
                operation_log=error.operation_log,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._interrupted_result(intent.record)

        try:
            executed = self.operation_repository.load(intent.record.operation_id)
        except Exception:
            return self._interrupted_result(intent.record)
        if executed.status != STATUS_MOVED_UNVERIFIED:
            return _result(executed)

        try:
            self._publish_verification(executed.operation_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return _result(executed)
        try:
            published = self.operation_repository.load(executed.operation_id)
        except Exception:
            return _result(executed)
        return _result(published)

    def _validate_authoritative_inputs(
        self,
        approved_plan: ApprovedOrganizationPlan,
        authorization: MovementAuthorization,
    ) -> None:
        if (
            not isinstance(approved_plan, ApprovedOrganizationPlan)
            or approved_plan.status != STATUS_READY_TO_MOVE
            or not approved_plan.reviewed_move_items
            or not isinstance(authorization, MovementAuthorization)
            or authorization.status != AUTHORIZATION_AVAILABLE
            or authorization.consumed_at is not None
            or authorization.scan_generation != approved_plan.scan_generation
            or authorization.decision_revision
            != approved_plan.decision_revision
            or authorization.approved_at != approved_plan.approved_at
            or authorization.approved_plan_identity
            != approved_plan_identity(approved_plan)
            or authorization.configured_root_identity
            != self.operation_repository.configured_root_identity
        ):
            raise OrganizationIdentityExecutionError("approved_plan_stale")

    def _strict_plan(
        self,
        intent: V2OrganizationIdentityIntent,
    ) -> tuple[IdentityMovePlanItem, ...]:
        try:
            evidence = {
                item.change_id: item for item in intent.executor_evidence
            }
            if len(evidence) != len(intent.record.expected_moves):
                raise ValueError
            items: list[IdentityMovePlanItem] = []
            for move in intent.record.expected_moves:
                if move.content_identity is None:
                    raise ValueError
                items.append(
                    IdentityMovePlanItem(
                        plan_item=MovePlanItem(
                            source=self.root / move.source,
                            destination=self.root / move.destination,
                            reason=move.reason,
                            confidence=move.confidence,
                            operation=move.operation,
                            overwrite_risk=move.overwrite_risk,
                        ),
                        change_id=move.change_id,
                        expected_digest=move.content_identity.digest,
                        captured_stat=evidence[
                            move.change_id
                        ].stat_fingerprint,
                    )
                )
            return tuple(items)
        except (AttributeError, KeyError, StrictExecutorError, ValueError) as error:
            raise OrganizationIdentityExecutionError(
                "strict_plan_invalid"
            ) from error

    def _failed_result(
        self,
        original: OrganizationOperationRecord,
    ) -> IdentityOrganizationExecutionResult:
        try:
            current = self.operation_repository.load(original.operation_id)
            failed = replace(
                current,
                status=STATUS_MOVEMENT_FAILED,
                completed_at=self._timestamp(),
                expected_moves=tuple(
                    replace(item, result_state=MOVE_UNATTEMPTED)
                    if item.result_state == MOVE_PENDING
                    else item
                    for item in current.expected_moves
                ),
                failure_code="movement_failed",
            )
            self.operation_repository.save(failed)
        except Exception:
            return self._interrupted_result(original)
        return _result(failed)

    def _interrupted_result(
        self,
        original: OrganizationOperationRecord,
        *,
        operation_log: OperationLog | None = None,
    ) -> IdentityOrganizationExecutionResult:
        interrupted = self._interrupted_record(
            original,
            operation_log=operation_log,
        )
        try:
            self.operation_repository.save(interrupted)
        except Exception:
            pass
        return _result(interrupted)

    def _try_publish_interrupted(
        self,
        original: OrganizationOperationRecord,
    ) -> None:
        interrupted = self._interrupted_record(original)
        try:
            self.operation_repository.save(interrupted)
        except Exception:
            pass

    def _interrupted_record(
        self,
        original: OrganizationOperationRecord,
        *,
        operation_log: OperationLog | None = None,
    ) -> OrganizationOperationRecord:
        try:
            current = self.operation_repository.load(original.operation_id)
        except Exception:
            current = original
        legacy_operation_log = current.legacy_operation_log
        if operation_log is not None:
            try:
                resolved_log = operation_log.log_path.resolve(strict=True)
                legacy_operation_log = resolved_log.relative_to(self.root).as_posix()
            except (OSError, ValueError):
                pass
        return replace(
            current,
            status=STATUS_INTERRUPTED,
            completed_at=self._timestamp(),
            legacy_operation_log=legacy_operation_log,
            failure_code="movement_recovery_required",
        )

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("identity movement clock is invalid")
        if value.tzinfo is None:
            raise ValueError("identity movement clock is invalid")
        return value.astimezone(timezone.utc).isoformat()


def _result(
    record: OrganizationOperationRecord,
) -> IdentityOrganizationExecutionResult:
    try:
        state = IdentityOrganizationExecutionState(record.status)
    except ValueError as error:
        raise OrganizationIdentityExecutionError("durable_intent_failed") from error
    return IdentityOrganizationExecutionResult(
        operation_id=record.operation_id,
        state=state,
        started_at=record.started_at,
        completed_at=record.completed_at,
        files_expected=len(record.expected_moves),
        files_moved=sum(
            item.result_state == MOVE_MOVED for item in record.expected_moves
        ),
        files_failed=sum(
            item.result_state == MOVE_FAILED for item in record.expected_moves
        ),
        files_unattempted=sum(
            item.result_state in {MOVE_PENDING, MOVE_UNATTEMPTED}
            for item in record.expected_moves
        ),
        verification_status=record.verification.status,
    )
