from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from organizer.application.duplicate_bin_approval_service import (
    DuplicateBinApprovalError,
    create_or_recover_duplicate_batch,
)
from organizer.application.duplicate_bin_models import SafeDuplicateBatchProjection
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.organization_decision_models import (
    OrganizationDecisionSession,
    STATUS_READY_TO_MOVE,
)
from organizer.application.organization_decision_service import (
    OrganizationDecisionError,
    accept_recommended_changes,
    approve_organization_plan,
    create_organization_decision_session,
    update_organization_decision,
)
from organizer.application.organization_slice_service import (
    InternalOrganizationProjection,
)
from organizer.web.scan_jobs import ScanJobSnapshot


@dataclass(frozen=True)
class OrganizationDecisionSnapshot:
    projection: InternalOrganizationProjection
    session: OrganizationDecisionSession
    duplicate_batch: SafeDuplicateBatchProjection | None = None


class OrganizationDecisionStore:
    """Hold immutable, generation-bound Loci decisions per browser session."""

    def __init__(
        self,
        duplicate_bin_repository: DuplicateBinRepository | None = None,
    ) -> None:
        self._lock = Lock()
        self._sessions: dict[str, OrganizationDecisionSnapshot] = {}
        self._duplicate_bin_repository = duplicate_bin_repository

    def current(
        self,
        browser_session_id: str,
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
    ) -> OrganizationDecisionSnapshot:
        with self._lock:
            stored = self._sessions.get(browser_session_id)
            if stored is not None and stored.session.scan_generation == scan.job_id:
                return stored
            if stored is not None:
                self._sessions.pop(browser_session_id, None)
            return OrganizationDecisionSnapshot(
                projection=projection,
                session=create_organization_decision_session(projection),
            )

    def update(
        self,
        browser_session_id: str,
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
        *,
        scan_generation: str,
        expected_revision: int,
        kind: str,
        change_id: str | None = None,
        decision: str | None = None,
        choice_id: str | None = None,
        option_id: str | None = None,
        copy_ids: tuple[str, ...] = (),
    ) -> OrganizationDecisionSnapshot:
        with self._lock:
            current = self._mutable_current(
                browser_session_id,
                scan,
                projection,
                scan_generation,
                expected_revision,
            )
            updated = update_organization_decision(
                current.session,
                current.projection,
                kind=kind,
                change_id=change_id,
                decision=decision,
                choice_id=choice_id,
                option_id=option_id,
                copy_ids=copy_ids,
            )
            snapshot = OrganizationDecisionSnapshot(current.projection, updated)
            self._sessions[browser_session_id] = snapshot
            return snapshot

    def validate_mutation(
        self,
        browser_session_id: str,
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
        *,
        scan_generation: str,
        expected_revision: int,
    ) -> None:
        """Validate concurrency guards before interpreting a submitted decision."""

        with self._lock:
            self._mutable_current(
                browser_session_id,
                scan,
                projection,
                scan_generation,
                expected_revision,
            )

    def accept_all_recommended(
        self,
        browser_session_id: str,
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
        *,
        scan_generation: str,
        expected_revision: int,
        expected_change_ids: tuple[str, ...],
    ) -> OrganizationDecisionSnapshot:
        with self._lock:
            current = self._mutable_current(
                browser_session_id,
                scan,
                projection,
                scan_generation,
                expected_revision,
            )
            updated = accept_recommended_changes(
                current.session,
                current.projection,
                expected_change_ids,
            )
            snapshot = OrganizationDecisionSnapshot(current.projection, updated)
            self._sessions[browser_session_id] = snapshot
            return snapshot

    def approve(
        self,
        browser_session_id: str,
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
        *,
        scan_generation: str,
        expected_revision: int,
    ) -> OrganizationDecisionSnapshot:
        with self._lock:
            self._validate_generation(scan, projection, scan_generation)
            stored = self._sessions.get(browser_session_id)
            if stored is not None and stored.session.scan_generation != scan.job_id:
                self._sessions.pop(browser_session_id, None)
                stored = None
            current = stored or OrganizationDecisionSnapshot(
                projection,
                create_organization_decision_session(projection),
            )
            if current.session.status == STATUS_READY_TO_MOVE:
                approved = current.session.approved_plan
                if (
                    approved is not None
                    and approved.decision_revision == expected_revision
                ):
                    return current
                raise OrganizationDecisionError("approval_already_completed")
            self._validate_revision(current.session, expected_revision)
            updated = approve_organization_plan(
                current.session,
                current.projection,
            )
            assert updated.approved_plan is not None
            repository = self._duplicate_bin_repository
            if repository is None and updated.approved_plan.duplicate_decisions:
                repository = DuplicateBinRepository(current.projection.root)
            try:
                batch = (
                    create_or_recover_duplicate_batch(
                        approved_plan=updated.approved_plan,
                        projection=current.projection,
                        browser_session_id=browser_session_id,
                        repository=repository,
                    )
                    if repository is not None
                    else None
                )
            except DuplicateBinApprovalError as error:
                raise OrganizationDecisionError(error.code) from error
            snapshot = OrganizationDecisionSnapshot(
                current.projection,
                updated,
                batch.safe_projection() if batch is not None else None,
            )
            self._sessions[browser_session_id] = snapshot
            return snapshot

    def approved_for_movement(
        self,
        browser_session_id: str,
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
    ) -> OrganizationDecisionSnapshot:
        """Return the exact session-owned approval without recreating it."""

        with self._lock:
            if (
                scan.status != "completed"
                or scan.job_id is None
                or projection.scan_generation != scan.job_id
            ):
                raise OrganizationDecisionError("scan_generation_mismatch")
            current = self._sessions.get(browser_session_id)
            if current is not None and current.session.scan_generation != scan.job_id:
                self._sessions.pop(browser_session_id, None)
                current = None
            if (
                current is None
                or current.session.status != STATUS_READY_TO_MOVE
                or current.session.approved_plan is None
            ):
                raise OrganizationDecisionError("approved_plan_not_found")
            return current

    def _mutable_current(
        self,
        browser_session_id: str,
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
        scan_generation: str,
        expected_revision: int,
    ) -> OrganizationDecisionSnapshot:
        self._validate_generation(scan, projection, scan_generation)
        stored = self._sessions.get(browser_session_id)
        if stored is not None and stored.session.scan_generation != scan.job_id:
            self._sessions.pop(browser_session_id, None)
            stored = None
        current = stored or OrganizationDecisionSnapshot(
            projection,
            create_organization_decision_session(projection),
        )
        self._validate_revision(current.session, expected_revision)
        return current

    @staticmethod
    def _validate_generation(
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
        submitted: str,
    ) -> None:
        if (
            scan.status != "completed"
            or scan.job_id is None
            or submitted != scan.job_id
            or projection.scan_generation != scan.job_id
        ):
            raise OrganizationDecisionError("scan_generation_mismatch")

    @staticmethod
    def _validate_revision(
        session: OrganizationDecisionSession,
        submitted: int,
    ) -> None:
        if submitted != session.decision_revision:
            raise OrganizationDecisionError("decision_revision_mismatch")
