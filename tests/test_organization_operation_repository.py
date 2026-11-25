from __future__ import annotations

import copy
import json
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
from organizer.application.organization_operation_models import (
    ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
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
            self.assertEqual(
                uuid.UUID(data["operation_id"]), uuid.UUID(record.operation_id)
            )
            self.assertEqual(data["expected_moves"][0]["source"], "inbox/a.txt")
            self.assertNotIn("content_identity", data["expected_moves"][0])
            self.assertEqual(
                loaded.identity_compatibility,
                OrdinaryFileIdentityCompatibility.HISTORICAL_V1,
            )
            self.assertNotIn(str(root), json.dumps(data))
            self.assertFalse(any(repository.directory.glob("*.tmp")))

    def test_reading_v1_never_rewrites_or_migrates_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationOperationRepository(Path(directory))
            record = _record(repository)
            repository.create(record)
            path = repository.directory / f"{record.operation_id}.json"
            before = path.read_bytes()

            self.assertEqual(repository.load(record.operation_id), record)

            self.assertEqual(path.read_bytes(), before)

    def test_v2_round_trip_is_strict_complete_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationOperationRepository(Path(directory))
            record = _v2_record(repository)

            repository.create(record)
            path = repository.directory / f"{record.operation_id}.json"
            first = path.read_bytes()
            loaded = repository.load(record.operation_id)
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
                [
                    item["content_identity"]["digest"]
                    for item in payload["expected_moves"]
                ],
                ["a" * 64, "b" * 64],
            )

    def test_v2_rejects_missing_mixed_malformed_and_unsupported_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationOperationRepository(Path(directory))
            record = _v2_record(repository)
            repository.create(record)
            path = repository.directory / f"{record.operation_id}.json"
            valid = json.loads(path.read_text(encoding="utf-8"))
            invalid_payloads = []

            null_identity = copy.deepcopy(valid)
            null_identity["expected_moves"][0]["content_identity"] = None
            invalid_payloads.append(null_identity)

            missing = copy.deepcopy(valid)
            missing["expected_moves"][0].pop("content_identity")
            invalid_payloads.append(missing)

            mixed = copy.deepcopy(valid)
            mixed["expected_moves"][1].pop("content_identity")
            invalid_payloads.append(mixed)

            uppercase = copy.deepcopy(valid)
            uppercase["expected_moves"][0]["content_identity"]["digest"] = "A" * 64
            invalid_payloads.append(uppercase)

            unsupported_kind = copy.deepcopy(valid)
            unsupported_kind["expected_moves"][0]["content_identity"]["kind"] = (
                "directory"
            )
            invalid_payloads.append(unsupported_kind)

            unsupported_algorithm = copy.deepcopy(valid)
            unsupported_algorithm["expected_moves"][0]["content_identity"][
                "algorithm"
            ] = "sha512"
            invalid_payloads.append(unsupported_algorithm)

            unexpected_identity = copy.deepcopy(valid)
            unexpected_identity["expected_moves"][0]["content_identity"]["path"] = (
                "Inbox/a.txt"
            )
            invalid_payloads.append(unexpected_identity)

            unsupported_item = copy.deepcopy(valid)
            unsupported_item["expected_moves"][0]["item_type"] = "directory"
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
                    repository.load(record.operation_id)

    def test_v1_rejects_identity_as_an_incompatible_record_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrganizationOperationRepository(Path(directory))
            record = _record(repository)
            repository.create(record)
            path = repository.directory / f"{record.operation_id}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["expected_moves"][0]["content_identity"] = {
                "kind": "regular_file",
                "algorithm": "sha256",
                "digest": "a" * 64,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(ValueError):
                repository.load(record.operation_id)

            with self.assertRaisesRegex(ValueError, "canonical"):
                repository.save(
                    replace(
                        record,
                        expected_moves=(
                            replace(
                                record.expected_moves[0],
                                content_identity=RegularFileContentIdentity(
                                    "regular_file", "sha256", "a" * 64
                                ),
                            ),
                        ),
                    )
                )

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
            with self.assertRaisesRegex(
                ValueError, "invalid organization operation ID"
            ):
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


def _v2_record(
    repository: OrganizationOperationRepository,
) -> OrganizationOperationRecord:
    first = _record(repository)
    first_move = replace(
        first.expected_moves[0],
        content_identity=RegularFileContentIdentity("regular_file", "sha256", "a" * 64),
    )
    second_move = replace(
        first_move,
        change_id="O2",
        source="inbox/b.txt",
        destination="Organized/b.txt",
        content_identity=RegularFileContentIdentity("regular_file", "sha256", "b" * 64),
    )
    return replace(
        first,
        expected_moves=(first_move, second_move),
        schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION_V2,
    )
