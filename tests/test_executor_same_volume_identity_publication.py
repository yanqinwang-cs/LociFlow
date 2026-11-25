from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import organizer.executor as executor
from organizer.executor_identity import (
    IdentityMovePlanItem,
    StrictExecutorError,
    StrictExecutorInterrupted,
)
from organizer.file_identity import (
    StableFileIdentityError,
    capture_open_regular_file,
)
from organizer.models import MovePlanItem


class SameVolumeIdentityPublicationTests(unittest.TestCase):
    def test_success_retains_descriptor_verifies_link_counts_and_fsyncs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "Inbox/source.txt", "Deep/Folder/source.txt")
            observed_links: list[int] = []
            fsync_kinds: list[str] = []
            creation_fsyncs: list[str] = []
            real_verify = executor._verify_destination_binding

            def observing_verify(*args, **kwargs) -> None:
                source_descriptor = args[3]
                observed_links.append(os.fstat(source_descriptor).st_nlink)
                real_verify(*args, **kwargs)

            def observing_fsync(descriptor: int) -> None:
                mode = os.fstat(descriptor).st_mode
                fsync_kinds.append(
                    "directory" if executor.stat.S_ISDIR(mode) else "file"
                )
                os.fsync(descriptor)

            def observing_creation_fsync(descriptor: int) -> None:
                self.assertTrue(executor.stat.S_ISDIR(os.fstat(descriptor).st_mode))
                creation_fsyncs.append("directory")
                os.fsync(descriptor)

            # stat.S_ISDIR is used through the imported executor module to avoid
            # exposing descriptors or paths in the production result.
            with (
                mock.patch.object(
                    executor,
                    "_verify_destination_binding",
                    side_effect=observing_verify,
                ),
                mock.patch.object(
                    executor,
                    "_fsync_after_publication",
                    side_effect=observing_fsync,
                ),
                mock.patch.object(
                    executor,
                    "_fsync_before_publication",
                    side_effect=observing_creation_fsync,
                ),
            ):
                result = executor._execute_same_volume_identity_batch((item,), root)

            destination = item.plan_item.destination
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].completed)
            self.assertTrue(result[0].destination_parent_created)
            self.assertFalse(item.plan_item.source.exists())
            self.assertEqual(destination.read_bytes(), b"Inbox/source.txt")
            self.assertEqual(destination.stat().st_nlink, 1)
            self.assertEqual(observed_links, [2, 2, 1])
            self.assertEqual(creation_fsyncs, ["directory"] * 4)
            self.assertEqual(
                fsync_kinds,
                ["file", "directory", "directory", "file", "directory"],
            )

    def test_multiple_items_move_only_after_complete_batch_prevalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _item(root, "O1", "first", "Dest/first")
            second = _item(root, "O2", "second", "Dest/second")

            results = executor._execute_same_volume_identity_batch(
                (first, second), root
            )

            self.assertEqual(len(results), 2)
            self.assertFalse(first.plan_item.source.exists())
            self.assertFalse(second.plan_item.source.exists())
            self.assertEqual(first.plan_item.destination.read_bytes(), b"first")
            self.assertEqual(second.plan_item.destination.read_bytes(), b"second")
            self.assertFalse((root / "AI_Review").exists())

    def test_later_cross_volume_and_indeterminate_reject_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _item(root, "O1", "first", "Dest/first")
            second = _item(root, "O2", "second", "Dest/second")
            device = first.captured_stat.device
            cases = (
                ([device, device + 1], "cross_volume_unsupported"),
                ([device, OSError("private")], "destination_volume_indeterminate"),
            )
            for side_effect, code in cases:
                with (
                    self.subTest(code=code),
                    mock.patch(
                        "organizer.executor_identity._volume_device",
                        side_effect=side_effect,
                    ),
                    self.assertRaises(StrictExecutorError) as raised,
                ):
                    executor._execute_same_volume_identity_batch((first, second), root)
                self.assertEqual(raised.exception.code, code)
                self.assertTrue(first.plan_item.source.exists())
                self.assertTrue(second.plan_item.source.exists())
                self.assertFalse(root.joinpath("Dest").exists())

    def test_mixed_collision_overlap_and_extra_hard_link_reject_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _item(root, "O1", "first", "Dest/first")
            second = _item(root, "O2", "second", "Dest/second")
            cases = (
                (first.plan_item,),
                (first, second.plan_item),
            )
            for plan in cases:
                with self.assertRaises(StrictExecutorError):
                    executor._execute_same_volume_identity_batch(plan, root)
                self.assertFalse(root.joinpath("Dest").exists())

            first.plan_item.destination.parent.mkdir()
            first.plan_item.destination.write_bytes(b"existing")
            with self.assertRaises(StrictExecutorError) as collision:
                executor._execute_same_volume_identity_batch((first,), root)
            self.assertEqual(collision.exception.code, "destination_collision")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            os.link(item.plan_item.source, root / "another-name")
            with self.assertRaises(StrictExecutorError) as links:
                executor._execute_same_volume_identity_batch((item,), root)
            self.assertEqual(links.exception.code, "unsupported_item")
            self.assertFalse(root.joinpath("Dest").exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _item(root, "O1", "one", "Nested")
            second = _item(root, "O2", "two", "Nested/two")
            with self.assertRaises(StrictExecutorError) as overlap:
                executor._execute_same_volume_identity_batch((first, second), root)
            self.assertEqual(overlap.exception.code, "invalid_strict_plan")
            self.assertFalse(root.joinpath("Nested").exists())

    def test_identity_replacements_symlink_and_missing_source_reject(self) -> None:
        mutations = ("replacement", "same_inode", "symlink", "missing")
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                item = _item(root, "O1", "source", "Dest/source")
                source = item.plan_item.source
                original = source.stat()
                if mutation == "replacement":
                    replacement = root / "replacement"
                    replacement.write_bytes(b"xxxxxx")
                    os.utime(
                        replacement,
                        ns=(original.st_atime_ns, original.st_mtime_ns),
                    )
                    os.replace(replacement, source)
                elif mutation == "same_inode":
                    source.write_bytes(b"xxxxxx")
                    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
                elif mutation == "symlink":
                    target = root / "target"
                    target.write_bytes(b"source")
                    source.unlink()
                    source.symlink_to(target)
                else:
                    source.unlink()

                with self.assertRaises(StrictExecutorError):
                    executor._execute_same_volume_identity_batch((item,), root)
                self.assertFalse(root.joinpath("Dest").exists())

    def test_destination_race_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            real_link = os.link

            def racing_link(source, destination, **kwargs):
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=kwargs["dst_dir_fd"],
                )
                os.write(descriptor, b"other")
                os.close(descriptor)
                return real_link(source, destination, **kwargs)

            with (
                mock.patch("organizer.executor.os.link", side_effect=racing_link),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertEqual(raised.exception.code, "destination_collision")
            self.assertFalse(raised.exception.recovery_required)
            self.assertTrue(item.plan_item.source.exists())
            self.assertEqual(item.plan_item.destination.read_bytes(), b"other")

    def test_destination_parent_substitution_before_link_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "Outside"
            outside.mkdir()
            item = _item(root, "O1", "source", "Dest/source")
            real_require = executor._require_directory_binding
            changed = False

            def substitute(path: Path, descriptor: int, checked_root: Path, **kwargs):
                nonlocal changed
                if (
                    path == item.plan_item.destination.parent.resolve(strict=False)
                    and not changed
                ):
                    changed = True
                    path.rename(root / "created-parent")
                    path.symlink_to(outside, target_is_directory=True)
                return real_require(path, descriptor, checked_root, **kwargs)

            with (
                mock.patch.object(
                    executor,
                    "_require_directory_binding",
                    side_effect=substitute,
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertEqual(raised.exception.code, "destination_publication_failed")
            self.assertFalse(raised.exception.recovery_required)
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse((outside / "source").exists())
            self.assertTrue(root.joinpath("created-parent").is_dir())

    def test_source_replacement_before_unlink_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            real_fsync = executor._fsync_after_publication
            calls = 0

            def replace_after_durable_link(descriptor: int) -> None:
                nonlocal calls
                real_fsync(descriptor)
                calls += 1
                if calls == 2:
                    replacement = root / "replacement"
                    replacement.write_bytes(b"other!")
                    os.replace(replacement, item.plan_item.source)

            with (
                mock.patch.object(
                    executor,
                    "_fsync_after_publication",
                    side_effect=replace_after_durable_link,
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertEqual(raised.exception.code, "source_path_changed")
            self.assertTrue(raised.exception.filesystem_changed)
            self.assertTrue(raised.exception.recovery_required)
            self.assertTrue(item.plan_item.source.exists())
            self.assertTrue(item.plan_item.destination.exists())

    def test_unexpected_link_count_before_unlink_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            real_fsync = executor._fsync_after_publication
            calls = 0

            def add_unexpected_link(descriptor: int) -> None:
                nonlocal calls
                real_fsync(descriptor)
                calls += 1
                if calls == 2:
                    os.link(item.plan_item.destination, root / "unexpected")

            with (
                mock.patch.object(
                    executor,
                    "_fsync_after_publication",
                    side_effect=add_unexpected_link,
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertEqual(raised.exception.code, "source_path_changed")
            self.assertTrue(raised.exception.recovery_required)
            self.assertTrue(item.plan_item.source.exists())
            self.assertTrue(item.plan_item.destination.exists())
            self.assertTrue(root.joinpath("unexpected").exists())

    def test_prepublication_interruption_propagates_without_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            with (
                mock.patch(
                    "organizer.executor.os.link",
                    side_effect=KeyboardInterrupt(),
                ),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertNotIsInstance(raised.exception, StrictExecutorInterrupted)
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse(item.plan_item.destination.exists())
            self.assertTrue(item.plan_item.destination.parent.exists())

    def test_source_disappearing_after_batch_prevalidation_fails_before_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            real_open_root = executor._open_root_directory

            def remove_after_prevalidation(resolved_root: Path) -> int:
                item.plan_item.source.unlink()
                return real_open_root(resolved_root)

            with (
                mock.patch.object(
                    executor,
                    "_open_root_directory",
                    side_effect=remove_after_prevalidation,
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertIn(
                raised.exception.code,
                {"source_identity_changed", "source_unstable"},
            )
            self.assertFalse(item.plan_item.destination.exists())

    def test_source_parent_symlink_substitution_after_prevalidation_rejects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "Inbox/source", "Dest/source")
            real_open_root = executor._open_root_directory

            def substitute_after_prevalidation(resolved_root: Path) -> int:
                (root / "Inbox").rename(root / "Actual")
                (root / "Inbox").symlink_to(
                    root / "Actual",
                    target_is_directory=True,
                )
                return real_open_root(resolved_root)

            with (
                mock.patch.object(
                    executor,
                    "_open_root_directory",
                    side_effect=substitute_after_prevalidation,
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertEqual(raised.exception.code, "source_unstable")
            self.assertTrue(root.joinpath("Actual/source").exists())
            self.assertFalse(item.plan_item.destination.exists())

    def test_postpublication_failures_remain_recovery_required_without_cleanup(
        self,
    ) -> None:
        cases = ("verify", "fsync", "unlink", "source_remains", "final_verify")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = _item(root, "O1", "source", "Dest/source")
                patches = []
                if case == "verify":
                    patches.append(
                        mock.patch.object(
                            executor,
                            "verify_open_regular_file_descriptor",
                            side_effect=StableFileIdentityError(
                                "source_identity_changed"
                            ),
                        )
                    )
                elif case == "fsync":
                    patches.append(
                        mock.patch.object(
                            executor,
                            "_fsync_after_publication",
                            side_effect=executor._recovery_failure(
                                "filesystem_changed_recovery_required"
                            ),
                        )
                    )
                elif case == "unlink":
                    patches.append(
                        mock.patch(
                            "organizer.executor.os.unlink",
                            side_effect=PermissionError(errno.EACCES, "private"),
                        )
                    )
                elif case == "source_remains":
                    patches.append(
                        mock.patch("organizer.executor.os.unlink", return_value=None)
                    )
                else:
                    real_verify = executor._verify_destination_binding
                    calls = 0

                    def fail_final(*args, **kwargs):
                        nonlocal calls
                        calls += 1
                        if calls == 3:
                            raise executor._recovery_failure(
                                "destination_verification_failed"
                            )
                        return real_verify(*args, **kwargs)

                    patches.append(
                        mock.patch.object(
                            executor,
                            "_verify_destination_binding",
                            side_effect=fail_final,
                        )
                    )

                with patches[0], self.assertRaises(StrictExecutorError) as raised:
                    executor._execute_same_volume_identity_batch((item,), root)

                self.assertTrue(raised.exception.filesystem_changed)
                self.assertTrue(raised.exception.recovery_required)
                self.assertTrue(item.plan_item.destination.exists())
                self.assertTrue(item.plan_item.destination.parent.exists())
                self.assertNotIn(str(root), str(raised.exception))
                self.assertNotIn(item.expected_digest, str(raised.exception))
                if case == "final_verify":
                    self.assertFalse(item.plan_item.source.exists())
                else:
                    self.assertTrue(item.plan_item.source.exists())

    def test_keyboard_interrupt_after_publication_is_annotated_and_closes_descriptors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            real_open = os.open
            descriptors: list[int] = []

            def observed_open(*args, **kwargs):
                descriptor = real_open(*args, **kwargs)
                descriptors.append(descriptor)
                return descriptor

            with (
                mock.patch("organizer.executor.os.open", side_effect=observed_open),
                mock.patch.object(
                    executor,
                    "_verify_destination_binding",
                    side_effect=KeyboardInterrupt(),
                ),
                self.assertRaises(StrictExecutorInterrupted) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertTrue(raised.exception.recovery_required)
            self.assertTrue(item.plan_item.source.exists())
            self.assertTrue(item.plan_item.destination.exists())
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_keyboard_interrupt_after_unlink_leaves_destination_only_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            real_verify = executor._verify_destination_binding
            calls = 0

            def interrupt_final_verification(*args, **kwargs) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise KeyboardInterrupt()
                real_verify(*args, **kwargs)

            with (
                mock.patch.object(
                    executor,
                    "_verify_destination_binding",
                    side_effect=interrupt_final_verification,
                ),
                self.assertRaises(StrictExecutorInterrupted) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)

            self.assertTrue(raised.exception.recovery_required)
            self.assertFalse(item.plan_item.source.exists())
            self.assertTrue(item.plan_item.destination.exists())

    def test_link_denial_and_unsupported_platform_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            with (
                mock.patch(
                    "organizer.executor.os.link",
                    side_effect=PermissionError(errno.EPERM, "private"),
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)
            self.assertEqual(raised.exception.code, "destination_publication_failed")
            self.assertFalse(raised.exception.recovery_required)
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse(item.plan_item.destination.exists())
            self.assertTrue(item.plan_item.destination.parent.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            with (
                mock.patch(
                    "organizer.executor.os.fsync",
                    side_effect=OSError(errno.EIO, "private"),
                ),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)
            self.assertEqual(raised.exception.code, "destination_publication_failed")
            self.assertTrue(raised.exception.filesystem_changed)
            self.assertFalse(raised.exception.recovery_required)
            self.assertTrue(item.plan_item.source.exists())
            self.assertFalse(item.plan_item.destination.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            with (
                mock.patch("organizer.executor.os.supports_dir_fd", frozenset()),
                self.assertRaises(StrictExecutorError) as raised,
            ):
                executor._execute_same_volume_identity_batch((item,), root)
            self.assertEqual(raised.exception.code, "unsupported_item")
            self.assertFalse(root.joinpath("Dest").exists())

    def test_private_engine_has_no_recorder_log_activity_or_production_caller(
        self,
    ) -> None:
        repository = Path(__file__).resolve().parents[1]
        for relative in (
            "src/organizer/application/execution_service.py",
            "src/organizer/application/organization_movement_service.py",
            "src/organizer/application/undo_service.py",
            "src/organizer/application/duplicate_bin_movement_service.py",
        ):
            text = (repository / relative).read_text()
            self.assertNotIn("apply_same_volume_identity_move_plan", text)
            self.assertNotIn("_execute_same_volume_identity", text)
            self.assertNotIn("IdentityMovePlanItem", text)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root, "O1", "source", "Dest/source")
            executor._execute_same_volume_identity_batch((item,), root)
            self.assertFalse(root.joinpath("AI_Review").exists())


def _item(
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
