from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum

from organizer.application.organization_identity_verification import (
    IdentityMismatchCategory,
    IdentityOrganizationVerificationResult,
    IdentityVerificationEligibilityError,
    IdentityVerificationState,
    verify_identity_organization_operation,
)
from organizer.application.organization_operation_models import (
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    STATUS_MOVED_UNVERIFIED,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)


class IdentityVerificationPublicationState(str, Enum):
    PUBLISHED_VERIFIED = "published_verified"
    PUBLISHED_VERIFICATION_FAILED = "published_verification_failed"
    VERIFICATION_INDETERMINATE = "verification_indeterminate"
    STALE = "stale"


@dataclass(frozen=True)
class IdentityVerificationPublicationResult:
    state: IdentityVerificationPublicationState
    published: bool


_DURABLE_MISMATCH_CODES = {
    IdentityMismatchCategory.SOURCE_PRESENT: "identity_source_present",
    IdentityMismatchCategory.DESTINATION_MISSING: "identity_destination_missing",
    IdentityMismatchCategory.DESTINATION_UNSAFE: "identity_destination_unsafe",
    IdentityMismatchCategory.DESTINATION_SYMLINK: "identity_destination_symlink",
    IdentityMismatchCategory.DESTINATION_NOT_REGULAR: (
        "identity_destination_not_regular"
    ),
    IdentityMismatchCategory.DESTINATION_IDENTITY_MISMATCH: (
        "identity_destination_mismatch"
    ),
    IdentityMismatchCategory.ITEM_SHAPE_MISMATCH: "identity_item_shape_mismatch",
}


class OrganizationIdentityVerificationPublicationService:
    """Publish one authoritative v2 verification result, but never initiate it.

    This disconnected C1-B owner detects a record changed during inspection by
    reloading immediately before the repository's atomic replacement. A0-C2
    must additionally hold lifecycle and execution-coordinator ownership across
    this complete call before connecting it to production.
    """

    def __init__(
        self,
        repository: OrganizationOperationRepository,
        *,
        verifier: Callable[..., IdentityOrganizationVerificationResult] = (
            verify_identity_organization_operation
        ),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._verifier = verifier
        self._now = now or (lambda: datetime.now(UTC))

    def publish(self, operation_id: str) -> IdentityVerificationPublicationResult:
        try:
            original = self._repository.load(operation_id)
        except ValueError:
            return _result(IdentityVerificationPublicationState.STALE)
        if (
            original.schema_version != ORGANIZATION_OPERATION_SCHEMA_VERSION_V2
            or original.status != STATUS_MOVED_UNVERIFIED
        ):
            return _result(IdentityVerificationPublicationState.STALE)

        try:
            verification = self._verifier(original, self._repository.root)
        except (KeyboardInterrupt, SystemExit):
            raise
        except IdentityVerificationEligibilityError:
            return _result(IdentityVerificationPublicationState.STALE)
        except Exception:
            return _result(
                IdentityVerificationPublicationState.VERIFICATION_INDETERMINATE
            )

        try:
            replacement = self._terminal_record(original, verification)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            replacement = None
        if replacement is None:
            return _result(
                IdentityVerificationPublicationState.VERIFICATION_INDETERMINATE
            )

        try:
            current = self._repository.load(original.operation_id)
        except ValueError:
            return _result(IdentityVerificationPublicationState.STALE)
        if current != original:
            return _result(IdentityVerificationPublicationState.STALE)

        try:
            self._repository.save(replacement)
        except Exception:
            return _result(
                IdentityVerificationPublicationState.VERIFICATION_INDETERMINATE
            )
        return _result(
            IdentityVerificationPublicationState.PUBLISHED_VERIFIED
            if replacement.status == STATUS_VERIFIED
            else IdentityVerificationPublicationState.PUBLISHED_VERIFICATION_FAILED,
            published=True,
        )

    def _terminal_record(
        self,
        original: OrganizationOperationRecord,
        result: IdentityOrganizationVerificationResult,
    ) -> OrganizationOperationRecord | None:
        item_count = len(original.expected_moves)
        if (
            not isinstance(result, IdentityOrganizationVerificationResult)
            or result.affected_items != item_count
            or isinstance(result.verified_count, bool)
            or not isinstance(result.verified_count, int)
            or not 0 <= result.verified_count <= item_count
            or not isinstance(result.mismatch_categories, tuple)
        ):
            return None
        if result.state is IdentityVerificationState.INDETERMINATE:
            return None
        if result.state is IdentityVerificationState.PASSED:
            if result.verified_count != item_count or result.mismatch_categories:
                return None
            status = STATUS_VERIFIED
            verification = OrganizationOperationVerification(
                status=VERIFICATION_PASSED,
                verified_count=item_count,
            )
            failure_code = None
        elif result.state is IdentityVerificationState.MISMATCH:
            try:
                categories = tuple(
                    category
                    for category in IdentityMismatchCategory
                    if category in result.mismatch_categories
                )
            except Exception:
                return None
            if not categories or len(categories) != len(result.mismatch_categories):
                return None
            status = STATUS_VERIFICATION_FAILED
            verification = OrganizationOperationVerification(
                status=VERIFICATION_FAILED,
                verified_count=result.verified_count,
                mismatch_codes=tuple(
                    _DURABLE_MISMATCH_CODES[category] for category in categories
                ),
            )
            failure_code = "verification_failed"
        else:
            return None

        try:
            completed_at = self._now().astimezone(UTC).isoformat()
        except Exception:
            return None
        return replace(
            original,
            status=status,
            completed_at=completed_at,
            verification=verification,
            failure_code=failure_code,
        )


def _result(
    state: IdentityVerificationPublicationState,
    *,
    published: bool = False,
) -> IdentityVerificationPublicationResult:
    return IdentityVerificationPublicationResult(state=state, published=published)
