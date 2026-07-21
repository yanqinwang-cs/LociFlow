from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from organizer.executor_identity import (
    IdentityMovePlanItem,
    StrictExecutorError,
    VolumePlacement,
    validate_identity_move_plan,
    validate_same_volume_identity_move_plan,
)
from organizer.file_identity import capture_open_regular_file
from organizer.models import MovePlanItem


class StrictExecutorIdentityModelTests(unittest.TestCase):
    def test_failure_properties_are_explicit_and_safe(self) -> None:
        ordinary = StrictExecutorError("destination_publication_failed")
        self.assertFalse(ordinary.filesystem_changed)
        self.assertFalse(ordinary.recovery_required)
        recovery = StrictExecutorError(
            "destination_verification_failed",
            filesystem_changed=True,
            recovery_required=True,
        )
        self.assertTrue(recovery.filesystem_changed)
        self.assertTrue(recovery.recovery_required)
        with self.assertRaises(ValueError):
            StrictExecutorError(
                "destination_verification_failed",
                recovery_required=True,
            )
        self.assertNotIn("destination_verification_failed", str(recovery))

    def test_valid_model_is_immutable_mandatory_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _strict_item(root, "O1", "source.txt", "Dest/source.txt")

            self.assertNotIn(str(root), repr(item))
            self.assertNotIn(item.expected_digest, repr(item))
            self.assertNotIn("O1", repr(item))
            with self.assertRaises(FrozenInstanceError):
                item.change_id = "changed"
            with self.assertRaises(StrictExecutorError) as missing:
                replace(item, expected_digest=None)
            self.assertEqual(missing.exception.code, "missing_identity")
            with self.assertRaises(StrictExecutorError) as malformed:
                replace(item, expected_digest="A" * 64)
            self.assertEqual(malformed.exception.code, "malformed_identity")

    def test_validates_same_volume_without_callbacks_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _strict_item(root, "O1", "source.txt", "Missing/source.txt")
            destination_parent = root / "Missing"

            plan = validate_identity_move_plan((item,), root)
            same = validate_same_volume_identity_move_plan((item,), root)

            self.assertEqual(plan, same)
            self.assertEqual(plan.items[0].placement, VolumePlacement.SAME_VOLUME)
            self.assertFalse(destination_parent.exists())
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse(item.plan_item.destination.exists())

    def test_empty_ordinary_mixed_and_duplicate_plans_reject_completely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _strict_item(root, "O1", "first.txt", "Dest/first.txt")
            second = _strict_item(root, "O2", "second.txt", "Dest/second.txt")
            ordinary = second.plan_item
            candidates = (
                (),
                (ordinary,),
                (first, ordinary),
                (first, replace(second, change_id="O1")),
                (first, replace(second, plan_item=replace(second.plan_item, source=first.plan_item.source))),
                (first, replace(second, plan_item=replace(second.plan_item, destination=first.plan_item.destination))),
            )
            for candidate in candidates:
                with self.subTest(candidate=candidate), self.assertRaises(
                    StrictExecutorError
                ) as raised:
                    validate_identity_move_plan(candidate, root)
                self.assertEqual(raised.exception.code, "invalid_strict_plan")
            self.assertTrue(first.plan_item.source.exists())
            self.assertTrue(second.plan_item.source.exists())

    def test_changed_source_collision_and_unsafe_parent_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            changed = _strict_item(root, "O1", "changed", "Dest/changed")
            changed.plan_item.source.write_bytes(b"replacement")
            with self.assertRaises(StrictExecutorError) as raised:
                validate_identity_move_plan((changed,), root)
            self.assertEqual(raised.exception.code, "source_identity_changed")

            collision = _strict_item(root, "O2", "collision", "Dest/collision")
            collision.plan_item.destination.parent.mkdir(parents=True, exist_ok=True)
            collision.plan_item.destination.write_bytes(b"existing")
            with self.assertRaises(StrictExecutorError) as raised:
                validate_identity_move_plan((collision,), root)
            self.assertEqual(raised.exception.code, "destination_collision")

            unsafe = _strict_item(root, "O3", "unsafe", "Link/unsafe")
            (root / "Link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(StrictExecutorError) as raised:
                validate_identity_move_plan((unsafe,), root)
            self.assertEqual(raised.exception.code, "invalid_strict_plan")
            self.assertFalse((outside / "unsafe").exists())

    def test_volume_classification_and_same_volume_gate_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _strict_item(root, "O1", "first", "Dest/first")
            second = _strict_item(root, "O2", "second", "Dest/second")
            source_device = first.captured_stat.device

            with mock.patch(
                "organizer.executor_identity._volume_device",
                return_value=source_device + 1,
            ):
                cross = validate_identity_move_plan((first,), root)
                self.assertEqual(
                    cross.items[0].placement,
                    VolumePlacement.CROSS_VOLUME,
                )
                with self.assertRaises(StrictExecutorError) as raised:
                    validate_same_volume_identity_move_plan((first,), root)
                self.assertEqual(raised.exception.code, "cross_volume_unsupported")

            with mock.patch(
                "organizer.executor_identity._volume_device",
                side_effect=OSError("private stat detail"),
            ):
                unknown = validate_identity_move_plan((first,), root)
                self.assertEqual(
                    unknown.items[0].placement,
                    VolumePlacement.INDETERMINATE,
                )
                with self.assertRaises(StrictExecutorError) as raised:
                    validate_same_volume_identity_move_plan((first,), root)
                self.assertEqual(
                    raised.exception.code,
                    "destination_volume_indeterminate",
                )

            calls = 0

            def mixed_devices(path: Path) -> int:
                nonlocal calls
                calls += 1
                return source_device if calls == 1 else source_device + 1

            with mock.patch(
                "organizer.executor_identity._volume_device",
                side_effect=mixed_devices,
            ):
                mixed = validate_identity_move_plan((first, second), root)
                self.assertEqual(
                    [entry.placement for entry in mixed.items],
                    [VolumePlacement.SAME_VOLUME, VolumePlacement.CROSS_VOLUME],
                )
                calls = 0
                with self.assertRaises(StrictExecutorError):
                    validate_same_volume_identity_move_plan((first, second), root)

    def test_safe_errors_and_production_boundary_contain_no_sensitive_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _strict_item(root, "private-id", "private-name", "Dest/private")
            item.plan_item.destination.parent.mkdir()
            item.plan_item.destination.write_bytes(b"existing")
            with self.assertRaises(StrictExecutorError) as raised:
                validate_identity_move_plan((item,), root)
            rendered = str(raised.exception)
            for secret in (str(root), "private-id", "private-name", item.expected_digest):
                self.assertNotIn(secret, rendered)

            repository_root = Path(__file__).resolve().parents[1]
            production = (
                repository_root / "src/organizer/application/execution_service.py",
                repository_root
                / "src/organizer/application/organization_movement_service.py",
                repository_root / "src/organizer/application/undo_service.py",
            )
            for path in production:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("executor_identity", text)
                self.assertNotIn("IdentityMovePlanItem", text)


def _strict_item(
    root: Path,
    change_id: str,
    source_name: str,
    destination_name: str,
) -> IdentityMovePlanItem:
    source = root / source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(source_name.encode("utf-8"))
    with capture_open_regular_file(source, root) as captured:
        identity = captured.identity
    return IdentityMovePlanItem(
        plan_item=MovePlanItem(
            source=source,
            destination=root / destination_name,
            reason="organize",
            confidence=90,
            operation="dry-run move",
            overwrite_risk=False,
        ),
        change_id=change_id,
        expected_digest=identity.digest,
        captured_stat=identity.stat_fingerprint,
    )


if __name__ == "__main__":
    unittest.main()
