from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

from organizer.application.content_identity import (
    OrdinaryFileIdentityCompatibility,
    RegularFileContentIdentity,
)
from organizer.application.organization_undo_models import (
    ORGANIZATION_UNDO_RECORD_TYPE,
    ORGANIZATION_UNDO_SCHEMA_VERSION,
    ORGANIZATION_UNDO_SCHEMA_VERSION_V2,
    UNDO_PREPARED,
    OrganizationUndoItem,
    OrganizationUndoRecord,
    UndoVerificationResult,
)
from organizer.application.organization_undo_repository import (
    OrganizationUndoRepository,
)


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
            self.assertNotIn("content_identity", payload["expected_restores"][0])
            self.assertEqual(
                loaded.identity_compatibility,
                OrdinaryFileIdentityCompatibility.HISTORICAL_V1,
            )

    def test_reading_v1_never_rewrites_or_migrates_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationUndoRepository(Path(directory))
            record = _record(repository)
            repository.create(record)
            path = repository.directory / f"{record.undo_id}.json"
            before = path.read_bytes()

            self.assertEqual(repository.load(record.undo_id), record)

            self.assertEqual(path.read_bytes(), before)

    def test_v2_round_trip_is_strict_complete_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationUndoRepository(Path(directory))
            record = _v2_record(repository)

            repository.create(record)
            path = repository.directory / f"{record.undo_id}.json"
            first = path.read_bytes()
            loaded = repository.load(record.undo_id)
            repository.save(loaded)

            self.assertEqual(loaded, record)
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(
                loaded.identity_compatibility,
                OrdinaryFileIdentityCompatibility.IDENTITY_V2,
            )
            payload = json.loads(first)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(
                [item["content_identity"]["digest"] for item in payload["expected_restores"]],
                ["a" * 64, "b" * 64],
            )

    def test_v2_rejects_missing_mixed_malformed_and_incompatible_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationUndoRepository(Path(directory))
            record = _v2_record(repository)
            repository.create(record)
            path = repository.directory / f"{record.undo_id}.json"
            valid = json.loads(path.read_text(encoding="utf-8"))
            invalid_payloads = []

            null_identity = copy.deepcopy(valid)
            null_identity["expected_restores"][0]["content_identity"] = None
            invalid_payloads.append(null_identity)

            missing = copy.deepcopy(valid)
            missing["expected_restores"][0].pop("content_identity")
            invalid_payloads.append(missing)

            mixed = copy.deepcopy(valid)
            mixed["expected_restores"][1].pop("content_identity")
            invalid_payloads.append(mixed)

            malformed = copy.deepcopy(valid)
            malformed["expected_restores"][0]["content_identity"]["digest"] = "z" * 64
            invalid_payloads.append(malformed)

            unsupported = copy.deepcopy(valid)
            unsupported["expected_restores"][0]["content_identity"]["kind"] = "symlink"
            invalid_payloads.append(unsupported)

            incompatible_parent_shape = copy.deepcopy(valid)
            incompatible_parent_shape["operation_id"] = None
            invalid_payloads.append(incompatible_parent_shape)

            unsupported_item = copy.deepcopy(valid)
            unsupported_item["expected_restores"][0]["item_type"] = "directory"
            invalid_payloads.append(unsupported_item)

            unexpected_top_level = copy.deepcopy(valid)
            unexpected_top_level["identity_version"] = 2
            invalid_payloads.append(unexpected_top_level)

            unknown_version = copy.deepcopy(valid)
            unknown_version["schema_version"] = 3
            invalid_payloads.append(unknown_version)

            boolean_version = copy.deepcopy(valid)
            boolean_version["schema_version"] = True
            invalid_payloads.append(boolean_version)

            numeric_version = copy.deepcopy(valid)
            numeric_version["schema_version"] = 2.0
            invalid_payloads.append(numeric_version)

            for payload in invalid_payloads:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    repository.load(record.undo_id)

    def test_v1_rejects_identity_as_an_incompatible_record_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationUndoRepository(Path(directory))
            record = _record(repository)
            repository.create(record)
            path = repository.directory / f"{record.undo_id}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["expected_restores"][0]["content_identity"] = {
                "kind": "regular_file",
                "algorithm": "sha256",
                "digest": "a" * 64,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(ValueError):
                repository.load(record.undo_id)

            with self.assertRaisesRegex(ValueError, "canonical"):
                repository.save(
                    replace(
                        record,
                        expected_restores=(
                            replace(
                                record.expected_restores[0],
                                content_identity=RegularFileContentIdentity(
                                    "regular_file", "sha256", "a" * 64
                                ),
                            ),
                        ),
                    )
                )

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


def _v2_record(repository: OrganizationUndoRepository) -> OrganizationUndoRecord:
    first = _record(repository)
    first_item = replace(
        first.expected_restores[0],
        content_identity=RegularFileContentIdentity(
            "regular_file", "sha256", "a" * 64
        ),
    )
    second_item = replace(
        first_item,
        change_id="O2",
        restore_target="Inbox/b.pdf",
        moved_from="Documents/b.pdf",
        content_identity=RegularFileContentIdentity(
            "regular_file", "sha256", "b" * 64
        ),
    )
    return replace(
        first,
        expected_restores=(first_item, second_item),
        schema_version=ORGANIZATION_UNDO_SCHEMA_VERSION_V2,
    )


if __name__ == "__main__":
    unittest.main()
