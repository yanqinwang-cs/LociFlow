from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    MOVE_UNATTEMPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVEMENT_FAILED,
    STATUS_MOVING,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_PREPARED,
    OrganizationOperationRecord,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)
from organizer.models import MovePlanItem, MoveResult, OperationLog
from organizer.safety import validate_under_root


class DurableOrganizationMovementRecorder:
    """Persist schema-neutral ordinary movement transitions."""

    def __init__(
        self,
        record: OrganizationOperationRecord,
        root: Path,
        repository: OrganizationOperationRepository,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if record.status != STATUS_PREPARED or repository.root != root.resolve():
            raise ValueError("movement recorder inputs are invalid")
        self.record = record
        self.root = repository.root
        self.repository = repository
        self._clock = clock or _utc_timestamp

    def before_batch(self, plan_items: tuple[MovePlanItem, ...]) -> None:
        if len(plan_items) != len(self.record.expected_moves):
            raise ValueError("approved move batch changed")
        for plan_item, expected in zip(plan_items, self.record.expected_moves):
            if (
                _relative(plan_item.source, self.root) != expected.source
                or _relative(plan_item.destination, self.root) != expected.destination
            ):
                raise ValueError("approved move batch changed")
        self.record = replace(
            self.record,
            status=STATUS_MOVING,
            started_at=self._clock(),
        )
        self.repository.save(self.record)

    def before_move(self, index: int, item: MovePlanItem) -> None:
        self._update_move(index, item, MOVE_PENDING, MOVE_ATTEMPTING)

    def after_success(
        self,
        index: int,
        item: MovePlanItem,
        result: MoveResult,
    ) -> None:
        self._require_result(item, result, success=True)
        self._update_move(index, item, MOVE_ATTEMPTING, MOVE_MOVED)

    def after_failure(
        self,
        index: int,
        item: MovePlanItem,
        result: MoveResult,
    ) -> None:
        self._require_result(item, result, success=False)
        self._update_move(
            index,
            item,
            MOVE_ATTEMPTING,
            MOVE_FAILED,
            failure_code="movement_failed",
        )

    def after_batch(self, operation_log: OperationLog) -> None:
        moves = tuple(
            replace(move, result_state=MOVE_UNATTEMPTED)
            if move.result_state == MOVE_PENDING
            else move
            for move in self.record.expected_moves
        )
        moved = sum(move.result_state == MOVE_MOVED for move in moves)
        failed = sum(move.result_state == MOVE_FAILED for move in moves)
        unattempted = sum(move.result_state == MOVE_UNATTEMPTED for move in moves)
        if moved == len(moves):
            status = STATUS_MOVED_UNVERIFIED
            completed_at = None
        elif moved:
            status = STATUS_PARTIAL_MOVEMENT
            completed_at = self._clock()
        else:
            status = STATUS_MOVEMENT_FAILED
            completed_at = self._clock()
        self.record = replace(
            self.record,
            status=status,
            completed_at=completed_at,
            expected_moves=moves,
            legacy_operation_log=_relative(operation_log.log_path, self.root),
            failure_code=(
                None
                if status == STATUS_MOVED_UNVERIFIED
                else "partial_movement"
                if moved and (failed or unattempted)
                else "movement_failed"
            ),
        )
        self.repository.save(self.record)

    def _update_move(
        self,
        index: int,
        item: MovePlanItem,
        expected_state: str,
        state: str,
        *,
        failure_code: str | None = None,
    ) -> None:
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(self.record.expected_moves)
        ):
            raise ValueError("approved move batch changed")
        expected = self.record.expected_moves[index]
        if (
            expected.result_state != expected_state
            or _relative(item.source, self.root) != expected.source
            or _relative(item.destination, self.root) != expected.destination
        ):
            raise ValueError("approved move batch changed")
        moves = list(self.record.expected_moves)
        moves[index] = replace(
            expected,
            result_state=state,
            failure_code=failure_code,
        )
        updated = replace(self.record, expected_moves=tuple(moves))
        self.repository.save(updated)
        self.record = updated

    def _require_result(
        self,
        item: MovePlanItem,
        result: MoveResult,
        *,
        success: bool,
    ) -> None:
        if (
            not isinstance(result, MoveResult)
            or result.success is not success
            or result.source != item.source
            or result.destination != item.destination
        ):
            raise ValueError("movement result does not match approved item")


def _relative(path: Path, root: Path) -> str:
    return (
        validate_under_root(path.resolve(strict=False), root)
        .relative_to(root)
        .as_posix()
    )


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()
