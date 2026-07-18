from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from organizer.application.scan_service import scan_root
from organizer.application.standards_models import StandardsApplicationError
from organizer.application.standards_repository import StandardsRepository
from organizer.application.standards_service import StandardsService
from organizer.organization_rules import (
    ORGANIZATION_RULES_RELATIVE_PATH,
    STANDARD_KEEP_FOLDER_UNCHANGED,
    STANDARD_KEEP_MATCHING_FILES_TOGETHER,
    STANDARD_LEGACY_ALIAS,
    STANDARD_LEGACY_IGNORED_TERM,
)


class StandardsServiceTests(unittest.TestCase):
    def test_preview_does_not_persist_and_supports_guided_presets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Documents").mkdir()
            (root / "Documents" / "CS1010X notes.txt").write_text("x", encoding="utf-8")
            scan = scan_root(root)
            service = StandardsService(StandardsRepository(root))
            folder = next(item for item in scan.metadata_items if item.relative_path == Path("Documents"))
            folder_scope = service.resolve_folder_scope(folder, scan_generation="scan-1")

            keep_folder = service.preview(
                preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
                scope=folder_scope,
                expected_revision=0,
                scan_result=scan,
            )
            course = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(scan_generation="scan-1"),
                matcher_kind="course_code",
                matcher_value="CS1010X",
                expected_revision=0,
                scan_result=scan,
            )
            project = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(scan_generation="scan-1"),
                matcher_kind="project_code",
                matcher_value="EvoSim",
                expected_revision=0,
            )

            self.assertEqual(keep_folder.matching_file_count, 1)
            self.assertEqual(course.matching_file_count, 1)
            self.assertEqual(project.matcher_kind, "project_code")
            self.assertFalse((root / ORGANIZATION_RULES_RELATIVE_PATH).exists())

    def test_first_material_save_migrates_supported_v1_semantics_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Documents").mkdir()
            path = root / ORGANIZATION_RULES_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "locked_anchors": ["CS1010X"],
                        "ignored_terms": ["Scratch", "summary"],
                        "anchor_aliases": {"Programming Methodology": "CS1010X"},
                        "preferred_granularities": ["course_code"],
                    }
                ),
                encoding="utf-8",
            )
            service = StandardsService(StandardsRepository(root))
            before_migration = service.list_standards()
            legacy_locked_id = next(
                item.id
                for item in before_migration.items
                if item.preset_type == "legacy_locked_anchor"
            )
            preview = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(scan_generation="scan-1"),
                matcher_kind="project_code",
                matcher_value="BootAI",
                expected_revision=0,
            )

            result = service.save_preview(preview.preview_id, expected_revision=0)
            restarted = StandardsRepository(root).load().catalog

            self.assertEqual(result.catalog_revision, 1)
            self.assertIsNotNone(restarted)
            self.assertEqual(restarted.revision, 1)
            types = {item.type for item in restarted.standards}
            self.assertEqual(
                types,
                {
                    STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                    STANDARD_LEGACY_IGNORED_TERM,
                    STANDARD_LEGACY_ALIAS,
                },
            )
            serialized = path.read_text(encoding="utf-8").lower()
            self.assertNotIn('"term": "summary"', serialized)
            self.assertNotIn("preferred_granularities", serialized)
            self.assertIn(
                legacy_locked_id,
                {item.id for item in result.projection.items},
            )

    def test_save_is_revision_safe_and_exact_duplicate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = StandardsService(StandardsRepository(root))
            scope = service.resolve_root_scope()
            first = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=scope,
                matcher_kind="course_code",
                matcher_value="CS1010X",
                expected_revision=0,
            )
            created = service.save_preview(first.preview_id, expected_revision=0)
            repeated_token = service.save_preview(first.preview_id, expected_revision=0)
            duplicate = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=scope,
                matcher_kind="course_code",
                matcher_value="CS1010X",
                expected_revision=1,
            )
            duplicate_result = service.save_preview(duplicate.preview_id, expected_revision=1)

            self.assertEqual(created.standard.id, repeated_token.standard.id)
            self.assertEqual(created.standard.id, duplicate_result.standard.id)
            self.assertFalse(duplicate_result.changed)
            self.assertEqual(StandardsRepository(root).load().revision, 1)
            with self.assertRaisesRegex(
                StandardsApplicationError, "standard_revision_mismatch"
            ):
                service.preview(
                    preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
                    scope=scope,
                    expected_revision=0,
                )

    def test_preview_expiry_supersession_and_wrong_root_or_stale_scope_reject(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root = Path(first)
            now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
            service = StandardsService(StandardsRepository(root), now=lambda: now[0])
            scope = service.resolve_root_scope()
            old = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=scope,
                matcher_kind="project_code",
                matcher_value="EvoSim",
                expected_revision=0,
            )
            current = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=scope,
                matcher_kind="project_code",
                matcher_value="BootAI",
                expected_revision=0,
            )
            with self.assertRaisesRegex(StandardsApplicationError, "preview_superseded"):
                service.save_preview(old.preview_id, expected_revision=0)
            now[0] += timedelta(minutes=16)
            with self.assertRaisesRegex(StandardsApplicationError, "preview_expired"):
                service.save_preview(current.preview_id, expected_revision=0)

            wrong_scope = StandardsService(StandardsRepository(Path(second))).resolve_root_scope()
            with self.assertRaisesRegex(StandardsApplicationError, "wrong_root_scope"):
                service.preview(
                    preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
                    scope=wrong_scope,
                    expected_revision=0,
                )

            stale = root / "Stale"
            stale.mkdir()
            stale_scan = scan_root(root)
            stale_metadata = next(
                item for item in stale_scan.metadata_items
                if item.relative_path == Path("Stale")
            )
            stale_scope = service.resolve_folder_scope(
                stale_metadata,
                scan_generation="scan-stale",
            )
            stale.rmdir()
            with self.assertRaisesRegex(StandardsApplicationError, "folder_scope_stale"):
                service.preview(
                    preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
                    scope=stale_scope,
                    expected_revision=0,
                )

    def test_invalid_matcher_kind_and_value_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = StandardsService(StandardsRepository(Path(directory)))
            for kind, value in (("person_name", "Ada"), ("course_code", "")):
                with self.subTest(kind=kind, value=value), self.assertRaises(
                    StandardsApplicationError
                ):
                    service.preview(
                        preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                        scope=service.resolve_root_scope(),
                        matcher_kind=kind,
                        matcher_value=value,
                        expected_revision=0,
                    )

            for value in ("Boot*", "Boot.+", "../Boot", "Boot AI"):
                with self.subTest(project_value=value), self.assertRaisesRegex(
                    StandardsApplicationError, "invalid_matcher"
                ):
                    service.preview(
                        preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                        scope=service.resolve_root_scope(),
                        matcher_kind="project_code",
                        matcher_value=value,
                        expected_revision=0,
                    )

    def test_previews_are_owned_and_superseded_per_browser_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = StandardsService(StandardsRepository(Path(directory)))
            first_session = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(),
                matcher_kind="project_code",
                matcher_value="BootAI",
                expected_revision=0,
                owner_key="session-one",
            )
            second_session = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(),
                matcher_kind="project_code",
                matcher_value="EvoSim",
                expected_revision=0,
                owner_key="session-two",
            )
            newer_first_session = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(),
                matcher_kind="project_code",
                matcher_value="LociFlow",
                expected_revision=0,
                owner_key="session-one",
            )

            with self.assertRaisesRegex(StandardsApplicationError, "preview_expired"):
                service.save_preview(
                    second_session.preview_id,
                    expected_revision=0,
                    owner_key="session-one",
                )
            with self.assertRaisesRegex(StandardsApplicationError, "preview_superseded"):
                service.save_preview(
                    first_session.preview_id,
                    expected_revision=0,
                    owner_key="session-one",
                )
            result = service.save_preview(
                newer_first_session.preview_id,
                expected_revision=0,
                owner_key="session-one",
            )
            self.assertTrue(result.created)

    def test_migrated_ignored_term_conflicts_with_new_matching_standard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ORGANIZATION_RULES_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"version": 1, "ignored_terms": ["Scratch"]}',
                encoding="utf-8",
            )
            service = StandardsService(StandardsRepository(root))
            preview = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(),
                matcher_kind="project_code",
                matcher_value="Scratch",
                expected_revision=0,
            )

            self.assertEqual(preview.attention_code, "standard_conflict")
            with self.assertRaisesRegex(StandardsApplicationError, "standard_conflict"):
                service.save_preview(preview.preview_id, expected_revision=0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)

    def test_activate_deactivate_noop_immutable_and_conflict_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Documents").mkdir()
            scan = scan_root(root)
            service = StandardsService(StandardsRepository(root))
            folder = next(item for item in scan.metadata_items if item.relative_path == Path("Documents"))
            preview = service.preview(
                preset_type=STANDARD_KEEP_FOLDER_UNCHANGED,
                scope=service.resolve_folder_scope(folder, scan_generation="scan-1"),
                expected_revision=0,
            )
            created = service.save_preview(preview.preview_id, expected_revision=0)
            deactivated = service.set_active(
                created.standard.id, active=False, expected_revision=1
            )
            noop = service.set_active(
                created.standard.id, active=False, expected_revision=2
            )
            activated = service.set_active(
                created.standard.id, active=True, expected_revision=2
            )

            self.assertFalse(deactivated.standard.active)
            self.assertEqual(deactivated.standard.record_revision, 2)
            self.assertFalse(noop.changed)
            self.assertEqual(noop.catalog_revision, 2)
            self.assertTrue(activated.standard.active)
            self.assertEqual(activated.catalog_revision, 3)
            with self.assertRaisesRegex(StandardsApplicationError, "immutable_standard"):
                service.set_active(
                    "builtin-protect-project-folders",
                    active=False,
                    expected_revision=3,
                )

            conflict = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(),
                matcher_kind="project_code",
                matcher_value="EvoSim",
                expected_revision=3,
            )
            self.assertEqual(conflict.attention_code, "standard_conflict")
            with self.assertRaisesRegex(StandardsApplicationError, "standard_conflict"):
                service.save_preview(conflict.preview_id, expected_revision=3)

    def test_first_v1_deactivation_migrates_and_preserves_the_stable_read_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ORGANIZATION_RULES_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"version": 1, "locked_anchors": ["CS1010X"]}',
                encoding="utf-8",
            )
            service = StandardsService(StandardsRepository(root))
            standard_id = next(
                item.id
                for item in service.list_standards().items
                if item.preset_type == "legacy_locked_anchor"
            )

            result = service.set_active(
                standard_id,
                active=False,
                expected_revision=0,
            )
            restarted = StandardsService(StandardsRepository(root)).list_standards()

            self.assertEqual(result.standard.id, standard_id)
            self.assertFalse(result.standard.active)
            self.assertEqual(result.standard.record_revision, 2)
            self.assertEqual(result.catalog_revision, 1)
            self.assertFalse(next(item for item in restarted.items if item.id == standard_id).active)

    def test_plan_state_blocks_and_successful_mutation_invalidates_without_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = _DecisionStore()
            service = StandardsService(
                StandardsRepository(root),
                decision_store=decisions,
                execution_coordinator=_Coordinator(),
                operation_repository=_Operations(),
            )
            preview = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(),
                matcher_kind="project_code",
                matcher_value="EvoSim",
                expected_revision=0,
            )
            decisions.approved = True
            with self.assertRaisesRegex(StandardsApplicationError, "active_plan_conflict"):
                service.save_preview(preview.preview_id, expected_revision=0)
            self.assertTrue(decisions.approved)
            self.assertEqual(decisions.invalidations, 0)
            decisions.approved = False
            result = service.save_preview(preview.preview_id, expected_revision=0)

            self.assertTrue(result.fresh_scan_required)
            self.assertEqual(decisions.invalidations, 1)

    def test_movement_authorization_operation_and_duplicate_batch_each_block(self) -> None:
        blockers = (
            {"execution_coordinator": _Coordinator(blocked=True)},
            {"operation_repository": _Operations(blocked=True)},
            {"duplicate_bin_repository": _DuplicateBatches(blocked=True)},
            {"duplicate_bin_operation_repository": _Records("moving")},
            {"duplicate_bin_restore_repository": _Records("restoring")},
        )
        for dependencies in blockers:
            with self.subTest(dependencies=tuple(dependencies)):
                with tempfile.TemporaryDirectory() as directory:
                    service = StandardsService(
                        StandardsRepository(Path(directory)),
                        **dependencies,
                    )
                    preview = service.preview(
                        preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                        scope=service.resolve_root_scope(),
                        matcher_kind="project_code",
                        matcher_value="EvoSim",
                        expected_revision=0,
                    )
                    with self.assertRaisesRegex(
                        StandardsApplicationError, "active_plan_conflict"
                    ):
                        service.save_preview(preview.preview_id, expected_revision=0)

        with tempfile.TemporaryDirectory() as directory:
            service = StandardsService(
                StandardsRepository(Path(directory)),
                duplicate_bin_operation_repository=_Records("moved"),
                duplicate_bin_restore_repository=_Records("restored"),
            )
            preview = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(),
                matcher_kind="project_code",
                matcher_value="EvoSim",
                expected_revision=0,
            )
            self.assertTrue(
                service.save_preview(preview.preview_id, expected_revision=0).changed
            )

    def test_safe_projection_has_revision_mutability_and_no_raw_storage_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = StandardsService(StandardsRepository(root))
            preview = service.preview(
                preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
                scope=service.resolve_root_scope(),
                matcher_kind="course_code",
                matcher_value="CS1010X",
                expected_revision=0,
            )
            result = service.save_preview(preview.preview_id, expected_revision=0)
            payload = result.projection.to_dict()
            serialized = json.dumps(payload).lower()
            saved = next(item for item in result.projection.items if item.id == result.standard.id)

            self.assertEqual(payload["revision"], 1)
            self.assertTrue(saved.mutable)
            self.assertNotIn("configured_root_identity", serialized)
            self.assertNotIn("matcher_kind", serialized)
            self.assertNotIn("created_at", serialized)
            self.assertNotIn(str(root).lower(), serialized)


class _DecisionStore:
    def __init__(self) -> None:
        self.approved = False
        self.invalidations = 0

    def has_approved_plan(self, root: Path) -> bool:
        return self.approved

    def invalidate_for_root(self, root: Path) -> None:
        self.invalidations += 1


class _Coordinator:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked

    def has_available_movement_authorization(self, root_identity: str) -> bool:
        return self.blocked


class _Operations:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked

    def blocking_record(self):
        return object() if self.blocked else None


class _DuplicateBatches:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked

    def list_active_approved_batches(self):
        return (object(),) if self.blocked else ()


class _Records:
    def __init__(self, status: str) -> None:
        self.status = status

    def list_records(self):
        return (type("Record", (), {"status": self.status})(),)

    def list_operations(self):
        return (type("Record", (), {"status": self.status})(),)


if __name__ == "__main__":
    unittest.main()
