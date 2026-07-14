from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from organizer.application.organization_operation_models import (
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)
from organizer.application.organization_operation_repository import (
    OrganizationOperationRepository,
)


class OrganizationOperationRepositoryTests(unittest.TestCase):
    def test_round_trip_is_versioned_root_relative_and_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = OrganizationOperationRepository(root)
            record = _record(repository)

            repository.create(record)
            loaded = repository.load(record.operation_id)
            path = repository.directory / f"{record.operation_id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(loaded, record)
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["record_type"], "loci_flow_organization_operation")
            self.assertEqual(uuid.UUID(data["operation_id"]), uuid.UUID(record.operation_id))
            self.assertEqual(data["expected_moves"][0]["source"], "inbox/a.txt")
            self.assertNotIn(str(root), json.dumps(data))
            self.assertFalse(any(repository.directory.glob("*.tmp")))

    def test_save_replaces_atomically_and_lists_blocking_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationOperationRepository(Path(directory))
            record = _record(repository)
            repository.create(record)

            moving = replace(record, status="moving")
            repository.save(moving)

            self.assertEqual(repository.load(record.operation_id), moving)
            self.assertEqual(repository.blocking_record(), moving)
            self.assertEqual(
                repository.find_for_plan("scan-one", 3),
                moving,
            )

    def test_failed_atomic_replace_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationOperationRepository(Path(directory))
            record = _record(repository)
            with mock.patch(
                "organizer.application.organization_operation_repository.os.replace",
                side_effect=OSError("injected replacement failure"),
            ):
                with self.assertRaises(OSError):
                    repository.create(record)

            self.assertFalse(
                (repository.directory / f"{record.operation_id}.json").exists()
            )
            self.assertFalse(any(repository.directory.glob("*.tmp")))

    def test_invalid_ids_and_absolute_record_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = OrganizationOperationRepository(root)
            with self.assertRaisesRegex(ValueError, "invalid organization operation ID"):
                repository.load("../outside")

            record = _record(repository)
            repository.create(record)
            path = repository.directory / f"{record.operation_id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["expected_moves"][0]["source"] = str(root / "absolute.txt")
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "safe relative path"):
                repository.load(record.operation_id)

    def test_symlinked_artifact_parent_cannot_write_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "AI_Review").symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("Symlinks are not supported")
            repository = OrganizationOperationRepository(root)

            with self.assertRaisesRegex(ValueError, "outside root"):
                repository.create(_record(repository))

            self.assertFalse((outside / "organization_operations").exists())


def _record(repository: OrganizationOperationRepository) -> OrganizationOperationRecord:
    return OrganizationOperationRecord(
        operation_id=str(uuid.uuid4()),
        authorization_id=str(uuid.uuid4()),
        status="prepared",
        scan_generation="scan-one",
        decision_revision=3,
        approved_at="2026-07-15T01:00:00+00:00",
        created_at="2026-07-15T01:01:00+00:00",
        started_at=None,
        completed_at=None,
        configured_root_identity=repository.configured_root_identity,
        expected_moves=(
            OrganizationExpectedMove(
                change_id="O1",
                source="inbox/a.txt",
                destination="Organized/a.txt",
                source_size_bytes=4,
                source_modified_time=1.0,
                reason="organize",
                confidence=90,
                operation="dry-run move",
                overwrite_risk=False,
            ),
        ),
        verification=OrganizationOperationVerification(),
        legacy_operation_log=None,
        failure_code=None,
        duplicate_actions_deferred=0,
    )
