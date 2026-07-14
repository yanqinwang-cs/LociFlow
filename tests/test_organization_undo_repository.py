from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from organizer.application.organization_undo_models import (
    ORGANIZATION_UNDO_RECORD_TYPE,
    ORGANIZATION_UNDO_SCHEMA_VERSION,
    UNDO_PREPARED,
    OrganizationUndoItem,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.application.organization_undo_repository import OrganizationUndoRepository


class OrganizationUndoRepositoryTests(unittest.TestCase):
    def test_round_trip_is_versioned_root_relative_linked_and_restart_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = OrganizationUndoRepository(root)
            record = _record(repository)
            repository.create(record)

            reconstructed = OrganizationUndoRepository(root)
            loaded = reconstructed.load(record.undo_id)

            self.assertEqual(loaded, record)
            self.assertEqual(reconstructed.linked_records(record.operation_id), (record,))
            payload = json.loads((repository.directory / f"{record.undo_id}.json").read_text())
            self.assertEqual(payload["schema_version"], ORGANIZATION_UNDO_SCHEMA_VERSION)
            self.assertEqual(payload["record_type"], ORGANIZATION_UNDO_RECORD_TYPE)
            self.assertFalse(Path(payload["expected_restores"][0]["restore_target"]).is_absolute())

    def test_save_replaces_atomically_and_cleans_temporary_file_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = OrganizationUndoRepository(root)
            record = _record(repository)
            repository.create(record)
            updated = replace(record, status="interrupted", completed_at="2026-07-15T08:01:00+00:00")
            repository.save(updated)
            self.assertEqual(repository.load(record.undo_id), updated)

            with mock.patch(
                "organizer.application.organization_undo_repository.os.replace",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    repository.save(record)
            self.assertFalse(any(repository.directory.glob("*.tmp")))

    def test_rejects_wrong_root_malformed_schema_and_symlink_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root = Path(directory).resolve()
            repository = OrganizationUndoRepository(root)
            record = _record(repository)
            with self.assertRaisesRegex(ValueError, "another root"):
                OrganizationUndoRepository(Path(other)).create(record)

            repository.create(record)
            path = repository.directory / f"{record.undo_id}.json"
            payload = json.loads(path.read_text())
            payload["unexpected"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fields do not match"):
                repository.load(record.undo_id)

            if hasattr(os, "symlink"):
                path.unlink()
                target = repository.directory / "target.json"
                target.write_text("{}", encoding="utf-8")
                path.symlink_to(target)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    repository.load(record.undo_id)


def _record(repository: OrganizationUndoRepository) -> OrganizationUndoRecord:
    return OrganizationUndoRecord(
        undo_id=str(uuid.uuid4()),
        operation_id=str(uuid.uuid4()),
        authorization_id=str(uuid.uuid4()),
        configured_root_identity=repository.configured_root_identity,
        status=UNDO_PREPARED,
        created_at="2026-07-15T08:00:00+00:00",
        started_at=None,
        completed_at=None,
        expected_restores=(
            OrganizationUndoItem(
                change_id="O1",
                restore_target="Inbox/a.pdf",
                moved_from="Documents/a.pdf",
                baseline_size_bytes=4,
                baseline_modified_time=100.0,
            ),
        ),
        verification=UndoVerificationResult(),
        legacy_undo_log=None,
        failure_code=None,
    )


if __name__ == "__main__":
    unittest.main()
