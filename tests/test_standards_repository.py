from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from organizer.application.standards_repository import (
    StandardsRepository,
    StandardsRepositoryError,
)
from organizer.organization_rules import (
    ORGANIZATION_RULES_RELATIVE_PATH,
    STANDARD_KEEP_FOLDER_UNCHANGED,
    StandardRecord,
    StandardsCatalog,
    StandardScope,
    normalize_standard_relative_path,
)


class StandardsRepositoryTests(unittest.TestCase):
    def test_strict_v2_round_trip_revision_and_restart_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = StandardsRepository(root)
            catalog = _catalog(repository, revision=1)

            repository.save_catalog(catalog, expected_revision=0)
            restarted = StandardsRepository(root)

            self.assertEqual(restarted.load().catalog, catalog)
            self.assertEqual(restarted.load().revision, 1)
            self.assertEqual(
                repository.configured_root_identity,
                restarted.configured_root_identity,
            )

    def test_wrong_root_unknown_fields_and_malformed_v2_reject(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            source = StandardsRepository(Path(first))
            source.save_catalog(_catalog(source, revision=1), expected_revision=0)
            target_path = Path(second) / ORGANIZATION_RULES_RELATIVE_PATH
            target_path.parent.mkdir(parents=True)
            target_path.write_bytes(source.path.read_bytes())

            with self.assertRaisesRegex(StandardsRepositoryError, "wrong_root_scope"):
                StandardsRepository(Path(second)).load()

            data = json.loads(target_path.read_text(encoding="utf-8"))
            data["unexpected"] = True
            target_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(
                StandardsRepositoryError, "standards_storage_unavailable"
            ):
                StandardsRepository(Path(second)).load()

    def test_v1_is_readable_and_not_rewritten_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ORGANIZATION_RULES_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            original = b'{"version":1,"locked_anchors":["CS1010X"]}\n'
            path.write_bytes(original)

            snapshot = StandardsRepository(root).load()

            self.assertTrue(snapshot.is_v1)
            self.assertIsNone(snapshot.catalog)
            self.assertEqual(path.read_bytes(), original)

    def test_stale_revision_and_failed_atomic_write_preserve_original_v1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ORGANIZATION_RULES_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            original = b'{"version":1,"locked_anchors":[]}\n'
            path.write_bytes(original)
            repository = StandardsRepository(root)
            catalog = _catalog(repository, revision=1)

            with self.assertRaisesRegex(
                StandardsRepositoryError, "standard_revision_mismatch"
            ):
                repository.save_catalog(catalog, expected_revision=2)
            with mock.patch.object(
                repository,
                "_atomic_write",
                side_effect=StandardsRepositoryError("standards_storage_unavailable"),
            ):
                with self.assertRaisesRegex(
                    StandardsRepositoryError, "standards_storage_unavailable"
                ):
                    repository.save_catalog(catalog, expected_revision=0)

            self.assertEqual(path.read_bytes(), original)

    def test_scope_normalization_is_nfc_and_rejects_absolute_or_escape_paths(
        self,
    ) -> None:
        self.assertEqual(
            normalize_standard_relative_path("Cafe\u0301/Notes"), "Café/Notes"
        )
        for value in ("/tmp/private", "../private", "."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_standard_relative_path(value)


def _catalog(repository: StandardsRepository, *, revision: int) -> StandardsCatalog:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    record = StandardRecord(
        id=str(uuid.uuid4()),
        type=STANDARD_KEEP_FOLDER_UNCHANGED,
        active=True,
        scope=StandardScope("folder", "Documents"),
        parameters=(),
        created_at=timestamp,
        updated_at=timestamp,
        record_revision=1,
        archived_at=None,
    )
    return StandardsCatalog(
        configured_root_identity=repository.configured_root_identity,
        revision=revision,
        standards=(record,),
    )


if __name__ == "__main__":
    unittest.main()
