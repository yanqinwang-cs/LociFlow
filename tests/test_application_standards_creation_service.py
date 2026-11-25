from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from organizer.application.runtime_lifecycle import (
    ApplicationStoppingError,
    RuntimeLifecycle,
    RuntimeOperationKind,
)
from organizer.application.scan_service import scan_root
from organizer.application.standards_creation_application_service import (
    StandardsCreationApplicationService,
)
from organizer.application.standards_repository import StandardsRepository
from organizer.application.standards_service import StandardsService
from organizer.organization_rules import (
    STANDARD_KEEP_FOLDER_UNCHANGED,
    STANDARD_KEEP_MATCHING_FILES_TOGETHER,
)
from organizer.web.standards_creation_store import StandardsCreationStore


class _RecordingLifecycle(RuntimeLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.begun: list[RuntimeOperationKind] = []

    def begin_operation(self, kind: RuntimeOperationKind):  # type: ignore[no-untyped-def]
        self.begun.append(kind)
        return super().begin_operation(kind)


class _FailingBrowserCommitStore(StandardsCreationStore):
    fail_commit = False

    def commit_preview_token(self, prepared):  # type: ignore[no-untyped-def]
        super().commit_preview_token(prepared)
        if self.fail_commit:
            raise RuntimeError("browser commit failed")


class StandardsCreationApplicationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        (self.root / "Documents").mkdir()
        (self.root / "Documents" / "CS1010X notes.txt").write_text(
            "notes", encoding="utf-8"
        )
        self.scan = scan_root(self.root)
        self.lifecycle = _RecordingLifecycle()
        self.standards = StandardsService(
            StandardsRepository(self.root),
            runtime_lifecycle=self.lifecycle,
        )
        self.store = _FailingBrowserCommitStore(self.root)
        self.service = StandardsCreationApplicationService(
            self.standards,
            self.store,
            self.lifecycle,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _context(self, session_id: str = "session-one"):
        return self.service.issue_creation_context(
            session_id=session_id,
            scan_generation="scan-one",
            scan_result=self.scan,
            expected_revision=0,
        )

    def test_context_and_preview_each_use_one_shared_mutation_admission(self) -> None:
        context = self._context()
        root_choice = next(
            item for item in context.scope_choices if item.kind == "root"
        )
        result = self.service.preview_standard(
            session_id="session-one",
            scan_generation="scan-one",
            scan_result=self.scan,
            expected_revision=0,
            scope_id=root_choice.id,
            preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
            matcher_kind="course_code",
            matcher_value="CS1010X",
        )

        self.assertEqual(
            self.lifecycle.begun,
            [RuntimeOperationKind.STATE_MUTATION, RuntimeOperationKind.STATE_MUTATION],
        )
        self.assertEqual(self.lifecycle.snapshot().active_count, 0)
        self.assertEqual(result.preview.matching_file_count, 1)
        self.assertNotIn(str(self.root), repr(result))
        resolved = self.store.resolve_preview_token(
            token=result.preview_token,
            session_id="session-one",
            root_identity=self.standards.repository.configured_root_identity,
            scan_generation="scan-one",
            expected_revision=0,
        )
        self.assertEqual(resolved, result.preview.preview_id)

    def test_shutdown_rejects_context_without_replacing_existing_context(self) -> None:
        context = self._context()
        scope_id = context.scope_choices[0].id
        self.lifecycle.begin_shutdown()

        with self.assertRaises(ApplicationStoppingError):
            self._context()

        self.store.validate_scope_reference(
            scope_id=scope_id,
            session_id="session-one",
            root_identity=self.standards.repository.configured_root_identity,
            scan_generation="scan-one",
            expected_revision=0,
        )
        self.assertEqual(self.lifecycle.snapshot().active_count, 0)

    def test_browser_commit_failure_rolls_back_only_new_application_preview(
        self,
    ) -> None:
        context = self._context()
        root_choice = context.scope_choices[0]
        first = self.service.preview_standard(
            session_id="session-one",
            scan_generation="scan-one",
            scan_result=self.scan,
            expected_revision=0,
            scope_id=root_choice.id,
            preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
        )
        self.store.fail_commit = True

        with self.assertRaisesRegex(RuntimeError, "browser commit failed"):
            self.service.preview_standard(
                session_id="session-one",
                scan_generation="scan-one",
                scan_result=self.scan,
                expected_revision=0,
                scope_id=root_choice.id,
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                matcher_kind="project_code",
                matcher_value="BootAI",
            )

        application_preview_id = self.store.resolve_preview_token(
            token=first.preview_token,
            session_id="session-one",
            root_identity=self.standards.repository.configured_root_identity,
            scan_generation="scan-one",
            expected_revision=0,
        )
        self.assertEqual(application_preview_id, first.preview.preview_id)
        self.assertEqual(
            self.standards.latest_preview_id_admitted("session-one"),
            first.preview.preview_id,
        )
        self.assertEqual(self.lifecycle.snapshot().active_count, 0)

    def test_application_commit_failure_creates_no_browser_reference(self) -> None:
        context = self._context()
        root_choice = context.scope_choices[0]
        first = self.service.preview_standard(
            session_id="session-one",
            scan_generation="scan-one",
            scan_result=self.scan,
            expected_revision=0,
            scope_id=root_choice.id,
            preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
        )
        original_commit = self.standards.commit_preview_admitted

        def partial_commit(preview, *, owner_key):  # type: ignore[no-untyped-def]
            original_commit(preview, owner_key=owner_key)
            raise RuntimeError("application commit failed")

        with (
            patch.object(
                self.standards,
                "commit_preview_admitted",
                side_effect=partial_commit,
            ),
            self.assertRaisesRegex(RuntimeError, "application commit failed"),
        ):
            self.service.preview_standard(
                session_id="session-one",
                scan_generation="scan-one",
                scan_result=self.scan,
                expected_revision=0,
                scope_id=root_choice.id,
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                matcher_kind="project_code",
                matcher_value="BootAI",
            )

        self.assertEqual(len(self.store._previews), 1)
        self.assertIn(first.preview_token, self.store._previews)
        self.assertEqual(
            self.standards.latest_preview_id_admitted("session-one"),
            first.preview.preview_id,
        )
        self.assertEqual(self.lifecycle.snapshot().active_count, 0)

    def test_response_projection_failure_occurs_before_either_commit(self) -> None:
        context = self._context()
        root_choice = context.scope_choices[0]
        first = self.service.preview_standard(
            session_id="session-one",
            scan_generation="scan-one",
            scan_result=self.scan,
            expected_revision=0,
            scope_id=root_choice.id,
            preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
        )

        with (
            patch(
                "organizer.application.standards_creation_application_service."
                "StandardsCreationPreviewResult",
                side_effect=RuntimeError("projection failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "projection failed"),
        ):
            self.service.preview_standard(
                session_id="session-one",
                scan_generation="scan-one",
                scan_result=self.scan,
                expected_revision=0,
                scope_id=root_choice.id,
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                matcher_kind="project_code",
                matcher_value="BootAI",
            )

        self.assertEqual(len(self.store._previews), 1)
        self.assertIn(first.preview_token, self.store._previews)
        self.assertEqual(
            self.standards.latest_preview_id_admitted("session-one"),
            first.preview.preview_id,
        )
        self.assertEqual(self.lifecycle.snapshot().active_count, 0)

    def test_context_is_session_root_scan_revision_generation_and_expiry_bound(
        self,
    ) -> None:
        now = [datetime(2026, 7, 19, 10, 0, tzinfo=UTC)]
        store = StandardsCreationStore(self.root, now=lambda: now[0])
        first = store.issue_creation_context(
            session_id="session-one",
            root_identity="root-one",
            scan_generation="scan-one",
            expected_revision=3,
            metadata_items=self.scan.metadata_items,
        )
        first_id = first.scope_choices[0].id
        self.assertEqual(
            datetime.fromisoformat(first.expires_at) - now[0], timedelta(minutes=15)
        )
        for session, root, scan, revision in (
            ("session-two", "root-one", "scan-one", 3),
            ("session-one", "root-two", "scan-one", 3),
            ("session-one", "root-one", "scan-two", 3),
        ):
            with (
                self.subTest(session=session, root=root, scan=scan),
                self.assertRaisesRegex(ValueError, "folder_scope_stale"),
            ):
                store.validate_scope_reference(
                    scope_id=first_id,
                    session_id=session,
                    root_identity=root,
                    scan_generation=scan,
                    expected_revision=revision,
                )
        with self.assertRaisesRegex(ValueError, "standard_revision_mismatch"):
            store.validate_scope_reference(
                scope_id=first_id,
                session_id="session-one",
                root_identity="root-one",
                scan_generation="scan-one",
                expected_revision=4,
            )

        second = store.issue_creation_context(
            session_id="session-one",
            root_identity="root-one",
            scan_generation="scan-one",
            expected_revision=3,
            metadata_items=self.scan.metadata_items,
        )
        self.assertNotEqual(first_id, second.scope_choices[0].id)
        with self.assertRaisesRegex(ValueError, "folder_scope_stale"):
            store.validate_scope_reference(
                scope_id=first_id,
                session_id="session-one",
                root_identity="root-one",
                scan_generation="scan-one",
                expected_revision=3,
            )
        restarted = StandardsCreationStore(self.root, now=lambda: now[0])
        with self.assertRaisesRegex(ValueError, "folder_scope_stale"):
            restarted.validate_scope_reference(
                scope_id=second.scope_choices[0].id,
                session_id="session-one",
                root_identity="root-one",
                scan_generation="scan-one",
                expected_revision=3,
            )
        now[0] += timedelta(minutes=15)
        with self.assertRaisesRegex(ValueError, "folder_scope_stale"):
            store.validate_scope_reference(
                scope_id=second.scope_choices[0].id,
                session_id="session-one",
                root_identity="root-one",
                scan_generation="scan-one",
                expected_revision=3,
            )


if __name__ == "__main__":
    unittest.main()
