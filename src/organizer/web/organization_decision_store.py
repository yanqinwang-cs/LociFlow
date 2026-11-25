from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from organizer.application.duplicate_bin_approval_service import (
    DuplicateBinApprovalError,
    create_or_recover_duplicate_batch,
)
from organizer.application.duplicate_bin_models import SafeDuplicateBatchProjection
from organizer.application.duplicate_bin_repository import DuplicateBinRepository
from organizer.application.execution_coordinator import OrganizationExecutionCoordinator
from organizer.application.filesystem_operation_admission import (
    FilesystemOperationAdmission,
    FilesystemOperationBlockedError,
    FilesystemOperationInspector,
    RecoveryAdmissionState,
)
from organizer.application.organization_decision_models import (
    STATUS_READY_TO_MOVE,
    OrganizationDecisionSession,
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
from organizer.application.runtime_lifecycle import (
    RuntimeLifecycle,
    RuntimeOperationKind,
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
        *,
        runtime_lifecycle: RuntimeLifecycle | None = None,
        execution_coordinator: OrganizationExecutionCoordinator | None = None,
        filesystem_operation_admission: FilesystemOperationAdmission | None = None,
    ) -> None:
        self._runtime_lifecycle = runtime_lifecycle or RuntimeLifecycle()
        self._execution_coordinator = execution_coordinator or (
            OrganizationExecutionCoordinator(self._runtime_lifecycle)
        )
        if self._execution_coordinator.runtime_lifecycle is not self._runtime_lifecycle:
            raise ValueError("decision store lifecycle ownership is inconsistent")
        self._filesystem_operation_admission = filesystem_operation_admission
        self._lock = Lock()
        self._sessions: dict[str, OrganizationDecisionSnapshot] = {}
        self._invalidated_generations: set[tuple[Path, str]] = set()
        self._observed_generations: set[tuple[Path, str]] = set()
        self._duplicate_bin_repository = duplicate_bin_repository

    def current(
        self,
        browser_session_id: str,
        scan: ScanJobSnapshot,
        projection: InternalOrganizationProjection,
    ) -> OrganizationDecisionSnapshot:
        with self._lock:
            self._reject_invalidated_generation(projection.root, scan.job_id)
            if scan.job_id is not None:
                self._observed_generations.add((projection.root.resolve(), scan.job_id))
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
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
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
            self._validate_generation(scan, projection, scan_generation)
            stored = self._sessions.get(browser_session_id)
            current = (
                stored
                if stored is not None and stored.session.scan_generation == scan.job_id
                else OrganizationDecisionSnapshot(
                    projection,
                    create_organization_decision_session(projection),
                )
            )
            self._validate_revision(current.session, expected_revision)

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
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
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
        with self._runtime_lifecycle.begin_operation(
            RuntimeOperationKind.STATE_MUTATION
        ):
            if not self._execution_coordinator.try_acquire():
                raise OrganizationDecisionError("movement_or_undo_in_progress")
            try:
                with self._lock:
                    self._validate_generation(scan, projection, scan_generation)
                    stored = self._sessions.get(browser_session_id)
                    if (
                        stored is not None
                        and stored.session.scan_generation != scan.job_id
                    ):
                        self._sessions.pop(browser_session_id, None)
                        stored = None
                    current = stored or OrganizationDecisionSnapshot(
                        projection,
                        create_organization_decision_session(projection),
                    )
                    if current.session.status == STATUS_READY_TO_MOVE:
                        approved = current.session.approved_plan
                        if (
                            approved is None
                            or approved.decision_revision != expected_revision
                        ):
                            raise OrganizationDecisionError(
                                "approval_already_completed"
                            )
                    else:
                        self._validate_revision(current.session, expected_revision)

                    self._admission_for(
                        current.projection.root
                    ).inspect_and_require_clear()

                    if current.session.status == STATUS_READY_TO_MOVE:
                        return current
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
            finally:
                self._execution_coordinator.release()

    def _admission_for(self, root: Path) -> FilesystemOperationAdmission:
        admission = self._filesystem_operation_admission
        if admission is None:
            return FilesystemOperationAdmission(FilesystemOperationInspector(root))
        if admission.root != root.resolve(strict=True):
            raise FilesystemOperationBlockedError(
                RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE
            )
        return admission

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

    def has_approved_plan(self, root: Path) -> bool:
        resolved = root.resolve()
        with self._lock:
            return any(
                snapshot.projection.root.resolve() == resolved
                and snapshot.session.status == STATUS_READY_TO_MOVE
                and snapshot.session.approved_plan is not None
                for snapshot in self._sessions.values()
            )

    def invalidate_for_root(self, root: Path) -> None:
        """Discard only non-approved planning state after a material Standards change."""
        resolved = root.resolve()
        with self._lock:
            matching = [
                session_id
                for session_id, snapshot in self._sessions.items()
                if snapshot.projection.root.resolve() == resolved
            ]
            if any(
                self._sessions[session_id].session.status == STATUS_READY_TO_MOVE
                for session_id in matching
            ):
                raise OrganizationDecisionError("active_plan_conflict")
            self._invalidated_generations.update(
                (observed_root, generation)
                for observed_root, generation in self._observed_generations
                if observed_root == resolved
            )
            for session_id in matching:
                snapshot = self._sessions[session_id]
                self._invalidated_generations.add(
                    (resolved, snapshot.session.scan_generation)
                )
                self._sessions.pop(session_id, None)

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

    def _validate_generation(
        self,
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
        self._reject_invalidated_generation(projection.root, scan.job_id)

    def _reject_invalidated_generation(
        self,
        root: Path,
        generation: str | None,
    ) -> None:
        if (
            generation is not None
            and (root.resolve(), generation) in self._invalidated_generations
        ):
            raise OrganizationDecisionError("scan_generation_mismatch")

    @staticmethod
    def _validate_revision(
        session: OrganizationDecisionSession,
        submitted: int,
    ) -> None:
        if submitted != session.decision_revision:
            raise OrganizationDecisionError("decision_revision_mismatch")
