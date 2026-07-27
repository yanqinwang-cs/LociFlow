from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json

from organizer.application.current_recovery_admission_status import (
    CurrentRecoveryAdmissionStatus,
)
from organizer.application.execution_coordinator import (
    OrganizationExecutionCoordinator,
)
from organizer.application.identity_verification_publication import (
    IdentityVerificationPublicationState,
    OrganizationIdentityVerificationPublicationService,
)
from organizer.application.organization_identity_verification import (
    IdentityVerificationEligibilityError,
    require_identity_organization_verification_eligible,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_identity_verification import (
    IdentityUndoVerificationEligibilityError,
    require_identity_organization_undo_verification_eligible,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)
from organizer.application.recovery_check_context import (
    RECOVERY_CHECK_LABEL,
    RecoveryCheckContext,
    RecoveryCheckContextError,
    RecoveryCheckContextStore,
    RecoveryCheckFamily,
)
from organizer.application.recovery_evidence import (
    RecoveryEvidenceInspector,
    RecoveryEvidenceState,
    RecoveryOperationFamily,
)
from organizer.application.runtime_lifecycle import (
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.application.undo_identity_verification_publication import (
    IdentityUndoVerificationPublicationState,
    OrganizationUndoIdentityVerificationPublicationService,
)


class RecoveryCheckError(RuntimeError):
    _CODES = {
        "check_unavailable",
        "context_unavailable",
        "recovery_details_changed",
    }

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("recovery Check error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class RecoveryCheckCandidate:
    target_family: RecoveryCheckFamily
    operation_id: str
    parent_operation_id: str | None
    durable_record_digest: str
    filesystem_observation_digest: str

    def __repr__(self) -> str:
        return "RecoveryCheckCandidate(eligible=True)"


@dataclass(frozen=True)
class RecoveryCheckContextResponse:
    case_id: str
    action_id: str
    expires_at: str
    label: str = RECOVERY_CHECK_LABEL

    def to_dict(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "action_id": self.action_id,
            "expires_at": self.expires_at,
            "label": self.label,
        }


class RecoveryCheckResultState(StrEnum):
    CONFIRMED = "confirmed"
    NOT_CONFIRMED = "not_confirmed"
    COULD_NOT_CHECK = "could_not_check"


@dataclass(frozen=True)
class RecoveryCheckResult:
    state: RecoveryCheckResultState
    title: str
    message: str
    recovery_attention: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "title": self.title,
            "message": self.message,
            "recovery_attention": self.recovery_attention,
        }


class RecoveryCheckEligibilityInspector:
    """Resolve one structurally verifiable ordinary recovery record."""

    def __init__(
        self,
        evidence_inspector: RecoveryEvidenceInspector,
        operation_repository: OrganizationOperationRepository,
        undo_repository: OrganizationUndoRepository,
    ) -> None:
        if (
            evidence_inspector.root != operation_repository.root
            or operation_repository.root != undo_repository.root
        ):
            raise ValueError("recovery Check owners belong to different roots")
        self._evidence_inspector = evidence_inspector
        self._operation_repository = operation_repository
        self._undo_repository = undo_repository
        self.configured_root_identity = (
            operation_repository.configured_root_identity
        )

    def inspect(self) -> RecoveryCheckCandidate:
        evidence = self._evidence_inspector.inspect()
        if (
            evidence.state is not RecoveryEvidenceState.UNRESOLVED
            or evidence.unresolved_count != 1
            or evidence.case is None
        ):
            raise RecoveryCheckError("check_unavailable")
        case = evidence.case
        try:
            if (
                case.family
                is RecoveryOperationFamily.ORGANIZATION_MOVEMENT
            ):
                record = self._operation_repository.load(
                    case.record_identity
                )
                require_identity_organization_verification_eligible(
                    record,
                    self._operation_repository.root,
                )
                if case.durable_status != record.status:
                    raise RecoveryCheckError("check_unavailable")
                return RecoveryCheckCandidate(
                    target_family=(
                        RecoveryCheckFamily.ORGANIZATION_MOVEMENT
                    ),
                    operation_id=record.operation_id,
                    parent_operation_id=None,
                    durable_record_digest=_canonical_digest(
                        {"movement": asdict(record)}
                    ),
                    filesystem_observation_digest=(
                        _observation_digest(case.items)
                    ),
                )
            if case.family is RecoveryOperationFamily.ORGANIZATION_UNDO:
                undo = self._undo_repository.load(case.record_identity)
                parent = self._operation_repository.load(undo.operation_id)
                require_identity_organization_undo_verification_eligible(
                    undo,
                    parent,
                    self._undo_repository.root,
                )
                if case.durable_status != undo.status:
                    raise RecoveryCheckError("check_unavailable")
                return RecoveryCheckCandidate(
                    target_family=RecoveryCheckFamily.ORGANIZATION_UNDO,
                    operation_id=undo.undo_id,
                    parent_operation_id=parent.operation_id,
                    durable_record_digest=_canonical_digest(
                        {
                            "undo": asdict(undo),
                            "parent": asdict(parent),
                        }
                    ),
                    filesystem_observation_digest=(
                        _observation_digest(case.items)
                    ),
                )
        except RecoveryCheckError:
            raise
        except (
            AttributeError,
            IdentityUndoVerificationEligibilityError,
            IdentityVerificationEligibilityError,
            TypeError,
            ValueError,
        ) as error:
            raise RecoveryCheckError("check_unavailable") from error
        raise RecoveryCheckError("check_unavailable")


class RecoveryCheckService:
    """Create and execute one-use identity-safe verification-only checks."""

    def __init__(
        self,
        runtime_lifecycle: RuntimeLifecycle,
        coordinator: OrganizationExecutionCoordinator,
        eligibility: RecoveryCheckEligibilityInspector,
        contexts: RecoveryCheckContextStore,
        movement_publication: OrganizationIdentityVerificationPublicationService,
        undo_publication: OrganizationUndoIdentityVerificationPublicationService,
        current_recovery_status: CurrentRecoveryAdmissionStatus,
    ) -> None:
        if coordinator.runtime_lifecycle is not runtime_lifecycle:
            raise ValueError("recovery Check lifecycle ownership is invalid")
        if (
            contexts.configured_root_identity
            != eligibility.configured_root_identity
        ):
            raise ValueError("recovery Check root ownership is invalid")
        self._runtime_lifecycle = runtime_lifecycle
        self._coordinator = coordinator
        self._eligibility = eligibility
        self._contexts = contexts
        self._movement_publication = movement_publication
        self._undo_publication = undo_publication
        self._current_recovery_status = current_recovery_status

    def create_context(
        self,
        *,
        browser_session_id: str,
    ) -> RecoveryCheckContextResponse:
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
            if not self._coordinator.try_acquire():
                raise RecoveryCheckError("check_unavailable")
            try:
                candidate = self._eligibility.inspect()
                context = self._contexts.issue(
                    browser_session_id=browser_session_id,
                    target_family=candidate.target_family,
                    operation_id=candidate.operation_id,
                    parent_operation_id=candidate.parent_operation_id,
                    durable_record_digest=candidate.durable_record_digest,
                    filesystem_observation_digest=(
                        candidate.filesystem_observation_digest
                    ),
                )
            except (RecoveryCheckContextError, ValueError) as error:
                raise RecoveryCheckError("check_unavailable") from error
            finally:
                self._coordinator.release()
        return RecoveryCheckContextResponse(
            case_id=context.case_id,
            action_id=context.action_id,
            expires_at=context.expires_at,
        )

    def check_result(
        self,
        *,
        browser_session_id: str,
        case_id: str,
        action_id: str,
    ) -> RecoveryCheckResult:
        consumed = False
        attention = True
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
            if not self._coordinator.try_acquire():
                raise RecoveryCheckError("check_unavailable")
            try:
                context = self._consume_context(
                    browser_session_id=browser_session_id,
                    case_id=case_id,
                    action_id=action_id,
                )
                consumed = True
                try:
                    candidate = self._eligibility.inspect()
                except RecoveryCheckError as error:
                    raise RecoveryCheckError(
                        "recovery_details_changed"
                    ) from error
                if not _same_binding(context, candidate):
                    raise RecoveryCheckError("recovery_details_changed")
                state, title, message = self._publish(context)
            finally:
                if consumed:
                    attention = self._refresh_attention()
                self._coordinator.release()
        return RecoveryCheckResult(state, title, message, attention)

    def _consume_context(
        self,
        *,
        browser_session_id: str,
        case_id: str,
        action_id: str,
    ) -> RecoveryCheckContext:
        try:
            return self._contexts.consume_bound(
                case_id=case_id,
                action_id=action_id,
                browser_session_id=browser_session_id,
                configured_root_identity=(
                    self._eligibility.configured_root_identity
                ),
            )
        except (RecoveryCheckContextError, ValueError) as error:
            raise RecoveryCheckError("context_unavailable") from error

    def _publish(
        self,
        context: RecoveryCheckContext,
    ) -> tuple[RecoveryCheckResultState, str, str]:
        if (
            context.target_family
            is RecoveryCheckFamily.ORGANIZATION_MOVEMENT
        ):
            result = self._movement_publication.publish(
                context.operation_id
            )
            if (
                result.state
                is IdentityVerificationPublicationState.PUBLISHED_VERIFIED
            ):
                return (
                    RecoveryCheckResultState.CONFIRMED,
                    "Check complete",
                    "The file move was confirmed.",
                )
            if (
                result.state
                is IdentityVerificationPublicationState.PUBLISHED_VERIFICATION_FAILED
            ):
                return (
                    RecoveryCheckResultState.NOT_CONFIRMED,
                    "Files need review",
                    "The file move could not be confirmed.",
                )
            if (
                result.state
                is IdentityVerificationPublicationState.VERIFICATION_INDETERMINATE
            ):
                return _could_not_check()
            raise RecoveryCheckError("recovery_details_changed")

        result = self._undo_publication.publish(context.operation_id)
        if (
            result.state
            is IdentityUndoVerificationPublicationState.PUBLISHED_RESTORED
        ):
            return (
                RecoveryCheckResultState.CONFIRMED,
                "Check complete",
                "The restore was confirmed.",
            )
        if (
            result.state
            is IdentityUndoVerificationPublicationState.PUBLISHED_VERIFICATION_FAILED
        ):
            return (
                RecoveryCheckResultState.NOT_CONFIRMED,
                "Files need review",
                "The restore could not be confirmed.",
            )
        if (
            result.state
            is IdentityUndoVerificationPublicationState.VERIFICATION_INDETERMINATE
        ):
            return _could_not_check()
        raise RecoveryCheckError("recovery_details_changed")

    def _refresh_attention(self) -> bool:
        try:
            snapshot = self._current_recovery_status.refresh()
            return snapshot.is_blocked
        except Exception:
            return True


def _same_binding(
    context: RecoveryCheckContext,
    candidate: RecoveryCheckCandidate,
) -> bool:
    return (
        context.target_family is candidate.target_family
        and context.operation_id == candidate.operation_id
        and context.parent_operation_id == candidate.parent_operation_id
        and context.durable_record_digest
        == candidate.durable_record_digest
        and context.filesystem_observation_digest
        == candidate.filesystem_observation_digest
    )


def _observation_digest(items: tuple[object, ...]) -> str:
    return _canonical_digest(
        {
            "schema": "loci-flow-recovery-check-observation-v1",
            "items": [
                {
                    "observations": [
                        observation.value
                        for observation in item.observations
                    ],
                    "disposition": item.disposition.value,
                    "fingerprint": item.evidence_fingerprint,
                }
                for item in items
            ],
        }
    )


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _could_not_check() -> tuple[RecoveryCheckResultState, str, str]:
    return (
        RecoveryCheckResultState.COULD_NOT_CHECK,
        "Check unavailable",
        "Loci Flow could not check the result. Review the folder and try again.",
    )
