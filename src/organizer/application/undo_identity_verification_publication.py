from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.application.organization_undo_identity_verification import (
    IdentityUndoMismatchCategory,
    IdentityUndoVerificationEligibilityError,
    IdentityUndoVerificationResult,
    IdentityUndoVerificationState,
    verify_identity_organization_undo,
)
from organizer.application.organization_undo_models import (
    ORGANIZATION_UNDO_SCHEMA_VERSION_V2,
    UNDO_RESTORED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_VERIFICATION_FAILED,
    UNDO_VERIFICATION_FAILED_STATE,
    UNDO_VERIFICATION_PASSED,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)


class IdentityUndoVerificationPublicationState(str, Enum):
    PUBLISHED_RESTORED = "published_restored"
    PUBLISHED_VERIFICATION_FAILED = "published_verification_failed"
    VERIFICATION_INDETERMINATE = "verification_indeterminate"
    STALE = "stale"


@dataclass(frozen=True)
class IdentityUndoVerificationPublicationResult:
    state: IdentityUndoVerificationPublicationState
    published: bool


_DURABLE_MISMATCH_CODES = {
    IdentityUndoMismatchCategory.MOVED_FROM_PRESENT: (
        "undo_identity_moved_from_present"
    ),
    IdentityUndoMismatchCategory.RESTORED_TARGET_MISSING: (
        "undo_identity_restored_target_missing"
    ),
    IdentityUndoMismatchCategory.RESTORED_TARGET_UNSAFE: (
        "undo_identity_restored_target_unsafe"
    ),
    IdentityUndoMismatchCategory.RESTORED_TARGET_SYMLINK: (
        "undo_identity_restored_target_symlink"
    ),
    IdentityUndoMismatchCategory.RESTORED_TARGET_NOT_REGULAR: (
        "undo_identity_restored_target_not_regular"
    ),
    IdentityUndoMismatchCategory.RESTORED_IDENTITY_MISMATCH: (
        "undo_identity_restored_mismatch"
    ),
    IdentityUndoMismatchCategory.ITEM_MAPPING_MISMATCH: (
        "undo_identity_item_mapping_mismatch"
    ),
}


class OrganizationUndoIdentityVerificationPublicationService:
    """Publish one terminal v2 Undo verification result without initiating Undo."""

    def __init__(
        self,
        operation_repository: OrganizationOperationRepository,
        undo_repository: OrganizationUndoRepository,
        *,
        verifier: Callable[..., IdentityUndoVerificationResult] = (
            verify_identity_organization_undo
        ),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if operation_repository.root != undo_repository.root:
            raise ValueError("organization repositories belong to different roots")
        self._operation_repository = operation_repository
        self._undo_repository = undo_repository
        self._verifier = verifier
        self._now = now or (lambda: datetime.now(timezone.utc))

    def publish(
        self,
        undo_id: str,
    ) -> IdentityUndoVerificationPublicationResult:
        try:
            original_undo = self._undo_repository.load(undo_id)
            original_parent = self._operation_repository.load(
                original_undo.operation_id
            )
        except (AttributeError, TypeError, ValueError):
            return _result(IdentityUndoVerificationPublicationState.STALE)
        if (
            original_undo.schema_version
            != ORGANIZATION_UNDO_SCHEMA_VERSION_V2
            or original_undo.status != UNDO_RESTORED_UNVERIFIED
        ):
            return _result(IdentityUndoVerificationPublicationState.STALE)

        try:
            verification = self._verifier(
                original_undo,
                original_parent,
                self._undo_repository.root,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except IdentityUndoVerificationEligibilityError:
            return _result(IdentityUndoVerificationPublicationState.STALE)
        except Exception:
            return _result(
                IdentityUndoVerificationPublicationState.VERIFICATION_INDETERMINATE
            )

        try:
            replacement = self._terminal_record(original_undo, verification)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            replacement = None
        if replacement is None:
            return _result(
                IdentityUndoVerificationPublicationState.VERIFICATION_INDETERMINATE
            )

        try:
            current_undo = self._undo_repository.load(original_undo.undo_id)
            current_parent = self._operation_repository.load(
                original_parent.operation_id
            )
        except (AttributeError, TypeError, ValueError):
            return _result(IdentityUndoVerificationPublicationState.STALE)
        if current_undo != original_undo or current_parent != original_parent:
            return _result(IdentityUndoVerificationPublicationState.STALE)

        try:
            self._undo_repository.save(replacement)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return _result(
                IdentityUndoVerificationPublicationState.VERIFICATION_INDETERMINATE
            )
        return _result(
            IdentityUndoVerificationPublicationState.PUBLISHED_RESTORED
            if replacement.status == UNDO_RESTORED
            else IdentityUndoVerificationPublicationState.PUBLISHED_VERIFICATION_FAILED,
            published=True,
        )

    def _terminal_record(
        self,
        original: OrganizationUndoRecord,
        result: IdentityUndoVerificationResult,
    ) -> OrganizationUndoRecord | None:
        item_count = len(original.expected_restores)
        if (
            not isinstance(result, IdentityUndoVerificationResult)
            or result.affected_items != item_count
            or isinstance(result.verified_count, bool)
            or not isinstance(result.verified_count, int)
            or not 0 <= result.verified_count <= item_count
            or not isinstance(result.mismatch_categories, tuple)
        ):
            return None
        if result.state is IdentityUndoVerificationState.INDETERMINATE:
            return None
        if result.state is IdentityUndoVerificationState.PASSED:
            if result.verified_count != item_count or result.mismatch_categories:
                return None
            status = UNDO_RESTORED
            verification = UndoVerificationResult(
                status=UNDO_VERIFICATION_PASSED,
                verified_count=item_count,
            )
            failure_code = None
        elif result.state is IdentityUndoVerificationState.MISMATCH:
            try:
                categories = tuple(
                    category
                    for category in IdentityUndoMismatchCategory
                    if category in result.mismatch_categories
                )
            except Exception:
                return None
            if (
                not categories
                or len(categories) != len(result.mismatch_categories)
            ):
                return None
            status = UNDO_VERIFICATION_FAILED
            verification = UndoVerificationResult(
                status=UNDO_VERIFICATION_FAILED_STATE,
                verified_count=result.verified_count,
                mismatch_codes=tuple(
                    _DURABLE_MISMATCH_CODES[category]
                    for category in categories
                ),
            )
            failure_code = UNDO_VERIFICATION_FAILED
        else:
            return None

        try:
            completed_at = self._now().astimezone(timezone.utc).isoformat()
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
    state: IdentityUndoVerificationPublicationState,
    *,
    published: bool = False,
) -> IdentityUndoVerificationPublicationResult:
    return IdentityUndoVerificationPublicationResult(
        state=state,
        published=published,
    )
