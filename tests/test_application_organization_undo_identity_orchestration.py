from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from organizer.application.organization_undo_identity_authorization import (
    V2_UNDO_AUTHORIZATION_AVAILABLE,
    V2_UNDO_AUTHORIZATION_CONSUMED,
    V2_UNDO_AUTHORIZATION_INVALIDATED,
    V2UndoAuthorizationStore,
)
from organizer.application.organization_undo_identity_orchestration import (
    UNDO_FILE_MOVE_CONFIRMATION,
    IdentityUndoConfirmationState,
    IdentityUndoPreviewState,
    OrganizationIdentityUndoOrchestrationError,
    OrganizationIdentityUndoOrchestrationService,
)
from organizer.application.organization_undo_models import (
    UNDO_INTERRUPTED,
    UNDO_ITEM_PENDING,
    UNDO_RESTORED,
)
from organizer.application.undo_identity_verification_publication import (
    IdentityUndoVerificationPublicationResult,
    IdentityUndoVerificationPublicationState,
)
from organizer.executor import apply_same_volume_identity_move_plan
from tests.test_application_undo_identity_eligibility import _stored_parent

_NOW = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
_TOKEN = "11111111-1111-4111-8111-111111111111"
_UNDO = "22222222-2222-4222-8222-222222222222"


class OrganizationIdentityUndoOrchestrationTests(unittest.TestCase):
    def test_preview_is_bounded_and_creates_only_an_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, operations, undos, parent = _service(
                Path(directory).resolve(), (b"alpha", b"beta")
            )
            preview = service.preview(
                parent_operation_id=parent.operation_id,
                browser_session_id="session",
            )

            self.assertEqual(preview.state, IdentityUndoPreviewState.READY)
            self.assertEqual(preview.item_count, 2)
            self.assertEqual(preview.confirmation_phrase, UNDO_FILE_MOVE_CONFIRMATION)
            self.assertEqual(preview.authorization_id, _TOKEN)
            self.assertEqual(undos.list_records(), ())
            self.assertEqual(len(operations.list_records()), 1)
            self.assertEqual(
                store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_AVAILABLE
            )
            rendered = repr(preview)
            for secret in (
                str(Path(directory).resolve()),
                "Documents",
                parent.expected_moves[0].content_identity.digest,
                parent.operation_id,
                _TOKEN,
            ):
                self.assertNotIn(secret, rendered)

    def test_confirm_revalidates_consumes_executes_and_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            events: list[str] = []
            service, store, _operations, undos, parent = _service(
                root, (b"alpha", b"beta")
            )
            preview = service.preview(
                parent_operation_id=parent.operation_id,
                browser_session_id="session",
            )
            real_create = undos.create
            real_consume = store.consume

            def create(record):
                events.append("create")
                real_create(record)

            def consume(token):
                self.assertEqual(undos.load(_UNDO).status, "prepared")
                events.append("consume")
                return real_consume(token)

            def execute(plan, selected_root, *, recorder):
                self.assertEqual(events, ["create", "consume"])
                events.append("execute")
                return apply_same_volume_identity_move_plan(
                    plan, selected_root, recorder=recorder
                )

            with (
                mock.patch.object(undos, "create", side_effect=create),
                mock.patch.object(store, "consume", side_effect=consume),
            ):
                service._executor = execute
                result = service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="session",
                    confirmation=UNDO_FILE_MOVE_CONFIRMATION,
                )

            self.assertEqual(events, ["create", "consume", "execute"])
            self.assertEqual(result.state, IdentityUndoConfirmationState.RESTORED)
            self.assertEqual(result.files_restored, 2)
            self.assertEqual(
                store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_CONSUMED
            )
            self.assertEqual(undos.load(_UNDO).status, UNDO_RESTORED)
            self.assertEqual((root / "Inbox/item-1.txt").read_bytes(), b"alpha")
            self.assertFalse((root / "Documents/item-1.txt").exists())
            with self.assertRaises(OrganizationIdentityUndoOrchestrationError):
                service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="session",
                    confirmation=UNDO_FILE_MOVE_CONFIRMATION,
                )

    def test_wrong_confirmation_or_binding_never_consumes_or_creates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _operations, undos, parent = _service(
                Path(directory).resolve(), (b"alpha",)
            )
            preview = service.preview(
                parent_operation_id=parent.operation_id,
                browser_session_id="session",
            )
            with self.assertRaises(OrganizationIdentityUndoOrchestrationError):
                service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="session",
                    confirmation="undo",
                )
            self.assertEqual(
                store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_AVAILABLE
            )
            with self.assertRaises(OrganizationIdentityUndoOrchestrationError):
                service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="wrong",
                    confirmation=UNDO_FILE_MOVE_CONFIRMATION,
                )
            self.assertEqual(
                store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_AVAILABLE
            )
            self.assertEqual(undos.list_records(), ())

    def test_changed_content_or_target_invalidates_without_record(self) -> None:
        for change in ("content", "target"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                service, store, _operations, undos, parent = _service(
                    root, (b"alpha",)
                )
                preview = service.preview(
                    parent_operation_id=parent.operation_id,
                    browser_session_id="session",
                )
                if change == "content":
                    (root / "Documents/item-1.txt").write_bytes(b"changed")
                else:
                    target = root / "Inbox/item-1.txt"
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b"occupied")
                result = service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="session",
                    confirmation=UNDO_FILE_MOVE_CONFIRMATION,
                )
                self.assertEqual(result.state, IdentityUndoConfirmationState.STALE)
                self.assertEqual(
                    store.snapshot(_TOKEN).state,
                    V2_UNDO_AUTHORIZATION_INVALIDATED,
                )
                self.assertEqual(undos.list_records(), ())

    def test_expired_authorization_returns_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current = [_NOW]
            root = Path(directory).resolve()
            operations, undos, parent = _stored_parent(root, (b"alpha",))
            store = V2UndoAuthorizationStore(
                operations.configured_root_identity,
                now=lambda: current[0],
                id_source=lambda: _TOKEN,
                lifetime=timedelta(seconds=1),
            )
            service = OrganizationIdentityUndoOrchestrationService(
                operations,
                undos,
                store,
                now=lambda: current[0],
                id_source=lambda: _UNDO,
            )
            preview = service.preview(
                parent_operation_id=parent.operation_id,
                browser_session_id="session",
            )
            current[0] += timedelta(seconds=2)
            result = service.confirm(
                parent_operation_id=parent.operation_id,
                authorization_id=preview.authorization_id,
                browser_session_id="session",
                confirmation=UNDO_FILE_MOVE_CONFIRMATION,
            )
            self.assertEqual(result.state, IdentityUndoConfirmationState.EXPIRED)
            self.assertEqual(undos.list_records(), ())

    def test_verification_outcomes_are_mapped_without_retry(self) -> None:
        outcomes = (
            (
                IdentityUndoVerificationPublicationState.PUBLISHED_VERIFICATION_FAILED,
                IdentityUndoConfirmationState.UNDO_VERIFICATION_FAILED,
            ),
            (
                IdentityUndoVerificationPublicationState.VERIFICATION_INDETERMINATE,
                IdentityUndoConfirmationState.RESTORED_UNVERIFIED,
            ),
        )
        for publication_state, expected in outcomes:
            with (
                self.subTest(state=publication_state.value),
                tempfile.TemporaryDirectory() as directory,
            ):
                service, _store, _operations, _undos, parent = _service(
                    Path(directory).resolve(), (b"alpha",)
                )
                publisher = mock.Mock()
                publisher.publish.return_value = (
                    IdentityUndoVerificationPublicationResult(
                        state=publication_state,
                        published=(
                            publication_state
                            is (
                                IdentityUndoVerificationPublicationState
                                .PUBLISHED_VERIFICATION_FAILED
                            )
                        ),
                    )
                )
                service.verification_publisher = publisher
                preview = service.preview(
                    parent_operation_id=parent.operation_id,
                    browser_session_id="session",
                )
                result = service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="session",
                    confirmation=UNDO_FILE_MOVE_CONFIRMATION,
                )
                self.assertEqual(result.state, expected)
                publisher.publish.assert_called_once_with(_UNDO)

    def test_create_failure_leaves_token_available_and_calls_no_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _operations, undos, parent = _service(
                Path(directory).resolve(), (b"alpha",)
            )
            preview = service.preview(
                parent_operation_id=parent.operation_id,
                browser_session_id="session",
            )
            executor = mock.Mock()
            service._executor = executor
            with (
                mock.patch.object(undos, "create", side_effect=OSError("private")),
                self.assertRaises(OrganizationIdentityUndoOrchestrationError) as raised,
            ):
                service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="session",
                    confirmation=UNDO_FILE_MOVE_CONFIRMATION,
                )
            self.assertEqual(raised.exception.code, "durable_intent_failed")
            self.assertEqual(
                store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_AVAILABLE
            )
            executor.assert_not_called()

    def test_create_exception_after_publication_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _operations, undos, parent = _service(
                Path(directory).resolve(), (b"alpha",)
            )
            preview = service.preview(
                parent_operation_id=parent.operation_id,
                browser_session_id="session",
            )
            real_create = undos.create
            executor = mock.Mock()
            service._executor = executor

            def publish_then_fail(record):
                real_create(record)
                raise OSError("directory durability uncertain")

            with mock.patch.object(
                undos, "create", side_effect=publish_then_fail
            ):
                result = service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="session",
                    confirmation=UNDO_FILE_MOVE_CONFIRMATION,
                )
            self.assertEqual(result.state, IdentityUndoConfirmationState.INTERRUPTED)
            self.assertEqual(undos.load(_UNDO).status, UNDO_INTERRUPTED)
            self.assertEqual(
                store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_CONSUMED
            )
            executor.assert_not_called()

    def test_post_create_consumption_failure_persists_interrupted_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _operations, undos, parent = _service(
                Path(directory).resolve(), (b"alpha", b"beta")
            )
            preview = service.preview(
                parent_operation_id=parent.operation_id,
                browser_session_id="session",
            )
            executor = mock.Mock()
            service._executor = executor
            with mock.patch.object(store, "consume", return_value=False):
                result = service.confirm(
                    parent_operation_id=parent.operation_id,
                    authorization_id=preview.authorization_id,
                    browser_session_id="session",
                    confirmation=UNDO_FILE_MOVE_CONFIRMATION,
                )
            self.assertEqual(result.state, IdentityUndoConfirmationState.INTERRUPTED)
            durable = undos.load(_UNDO)
            self.assertEqual(durable.status, UNDO_INTERRUPTED)
            self.assertTrue(
                all(
                    item.result_state == UNDO_ITEM_PENDING
                    for item in durable.expected_restores
                )
            )
            self.assertEqual(
                store.snapshot(_TOKEN).state, V2_UNDO_AUTHORIZATION_INVALIDATED
            )
            executor.assert_not_called()

    def test_live_services_connect_v2_while_routes_avoid_schema_owner(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        for relative in (
            "src/organizer/application/undo_service.py",
            "src/organizer/web/app.py",
        ):
            source = (repository_root / relative).read_text(encoding="utf-8")
            self.assertIn("organization_undo_identity_orchestration", source)
        activity_route = (
            repository_root / "src/organizer/web/routes/activity_api.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "organization_undo_identity_orchestration", activity_route
        )
        activity = (
            repository_root / "src/organizer/application/activity_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "record.schema_version == ORGANIZATION_OPERATION_SCHEMA_VERSION",
            activity,
        )
        orchestration = (
            repository_root
            / "src/organizer/application/organization_undo_identity_orchestration.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "RuntimeLifecycle",
            "RuntimeOperationKind",
            "OrganizationExecutionCoordinator",
            "undo_operation_log",
        ):
            self.assertNotIn(forbidden, orchestration)


def _service(root: Path, payloads: tuple[bytes, ...]):
    operations, undos, parent = _stored_parent(root, payloads)
    store = V2UndoAuthorizationStore(
        operations.configured_root_identity,
        now=lambda: _NOW,
        id_source=lambda: _TOKEN,
    )
    service = OrganizationIdentityUndoOrchestrationService(
        operations,
        undos,
        store,
        now=lambda: _NOW,
        id_source=lambda: _UNDO,
    )
    return service, store, operations, undos, parent


if __name__ == "__main__":
    unittest.main()
