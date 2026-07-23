from __future__ import annotations

from dataclasses import replace
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.organization_undo_identity_verification import (
    IdentityUndoMismatchCategory,
    IdentityUndoVerificationEligibilityError,
    IdentityUndoVerificationResult,
    IdentityUndoVerificationState,
    verify_identity_organization_undo,
)
from organizer.application.organization_undo_models import (
    UNDO_FAILED,
    UNDO_INTERRUPTED,
    UNDO_ITEM_ATTEMPTING,
    UNDO_ITEM_RESTORED,
    UNDO_PARTIAL,
    UNDO_PREPARED,
    UNDO_RESTORED,
    UNDO_RESTORED_UNVERIFIED,
    UNDO_UNDOING,
    UNDO_VERIFICATION_FAILED,
    UNDO_VERIFICATION_PASSED,
    UndoVerificationResult,
)
from tests.test_application_organization_undo_identity_execution import _intent


class IdentityAwareOrganizationUndoVerificationTests(unittest.TestCase):
    def test_one_and_multiple_item_restores_pass_with_bounded_reads(self) -> None:
        for payloads in ((b"a" * 41,), (b"a" * 41, b"b" * 37)):
            with self.subTest(items=len(payloads)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _operations, _undos, parent, undo = _restored_pair(root, payloads)
                read_sizes: list[int] = []
                real_read = os.read

                def observed_read(descriptor: int, size: int) -> bytes:
                    read_sizes.append(size)
                    return real_read(descriptor, size)

                with mock.patch(
                    "organizer.application.organization_undo_identity_verification.os.read",
                    side_effect=observed_read,
                ):
                    result = verify_identity_organization_undo(
                        undo,
                        parent,
                        root,
                        chunk_size=7,
                    )

                self.assertEqual(
                    result,
                    IdentityUndoVerificationResult(
                        state=IdentityUndoVerificationState.PASSED,
                        affected_items=len(payloads),
                        verified_count=len(payloads),
                        mismatch_categories=(),
                    ),
                )
                self.assertTrue(read_sizes)
                self.assertEqual(set(read_sizes), {7})

    def test_changed_ctime_and_link_count_do_not_false_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _operations, _undos, parent, undo = _restored_pair(
                root,
                (b"same-volume-content",),
            )
            restored = root / undo.expected_restores[0].restore_target
            before_ctime = restored.stat().st_ctime_ns
            os.link(restored, root / "extra-link")
            self.assertGreaterEqual(restored.stat().st_ctime_ns, before_ctime)

            result = verify_identity_organization_undo(undo, parent, root)

            self.assertEqual(result.state, IdentityUndoVerificationState.PASSED)

    def test_same_size_same_mtime_replacement_is_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _operations, _undos, parent, undo = _restored_pair(
                root,
                (b"recorded",),
            )
            restored = root / undo.expected_restores[0].restore_target
            baseline = restored.stat()
            restored.write_bytes(b"replaced")
            os.utime(restored, ns=(baseline.st_atime_ns, baseline.st_mtime_ns))

            result = verify_identity_organization_undo(undo, parent, root)

            self.assertEqual(result.state, IdentityUndoVerificationState.MISMATCH)
            self.assertEqual(result.verified_count, 0)
            self.assertEqual(
                result.mismatch_categories,
                (IdentityUndoMismatchCategory.RESTORED_IDENTITY_MISMATCH,),
            )

    def test_moved_from_presence_is_mismatch_even_with_matching_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _operations, _undos, parent, undo = _restored_pair(
                root,
                (b"recorded",),
            )
            moved_from = root / undo.expected_restores[0].moved_from
            moved_from.parent.mkdir(parents=True, exist_ok=True)
            moved_from.write_bytes(b"recorded")

            result = verify_identity_organization_undo(undo, parent, root)

            self.assertEqual(result.state, IdentityUndoVerificationState.MISMATCH)
            self.assertIn(
                IdentityUndoMismatchCategory.MOVED_FROM_PRESENT,
                result.mismatch_categories,
            )

    def test_missing_symlink_nonregular_and_unsafe_target_are_mismatches(self) -> None:
        cases = ("missing", "symlink", "directory", "unsafe_parent")
        expected = {
            "missing": IdentityUndoMismatchCategory.RESTORED_TARGET_MISSING,
            "symlink": IdentityUndoMismatchCategory.RESTORED_TARGET_SYMLINK,
            "directory": IdentityUndoMismatchCategory.RESTORED_TARGET_NOT_REGULAR,
            "unsafe_parent": IdentityUndoMismatchCategory.RESTORED_TARGET_UNSAFE,
        }
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                root = base / "root"
                root.mkdir()
                _operations, _undos, parent, undo = _restored_pair(
                    root,
                    (b"recorded",),
                )
                target = root / undo.expected_restores[0].restore_target
                if case == "missing":
                    target.unlink()
                elif case == "symlink":
                    target.unlink()
                    target.symlink_to(base / "outside")
                elif case == "directory":
                    target.unlink()
                    target.mkdir()
                else:
                    target.unlink()
                    target.parent.rmdir()
                    target.parent.symlink_to(base, target_is_directory=True)

                result = verify_identity_organization_undo(undo, parent, root)

                self.assertEqual(result.state, IdentityUndoVerificationState.MISMATCH)
                self.assertIn(expected[case], result.mismatch_categories)

    def test_unavailable_only_is_indeterminate_and_mismatch_dominates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _operations, _undos, parent, undo = _restored_pair(
                root,
                (b"first", b"second"),
            )
            real_read = os.read
            completed = 0

            def unavailable_second(descriptor: int, size: int) -> bytes:
                nonlocal completed
                if completed == 1:
                    raise OSError(errno.EIO, "private")
                block = real_read(descriptor, size)
                if not block:
                    completed += 1
                return block

            with mock.patch(
                "organizer.application.organization_undo_identity_verification.os.read",
                side_effect=unavailable_second,
            ):
                result = verify_identity_organization_undo(undo, parent, root)
            self.assertEqual(
                result.state,
                IdentityUndoVerificationState.INDETERMINATE,
            )
            self.assertEqual(result.verified_count, 1)

            changed = root / undo.expected_restores[0].restore_target
            changed.write_bytes(b"wrong")
            completed = 0
            with mock.patch(
                "organizer.application.organization_undo_identity_verification.os.read",
                side_effect=unavailable_second,
            ):
                result = verify_identity_organization_undo(undo, parent, root)
            self.assertEqual(result.state, IdentityUndoVerificationState.MISMATCH)
            self.assertEqual(result.verified_count, 0)
            self.assertEqual(
                result.mismatch_categories,
                (IdentityUndoMismatchCategory.RESTORED_IDENTITY_MISMATCH,),
            )

    def test_target_replaced_while_open_is_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _operations, _undos, parent, undo = _restored_pair(
                root,
                (b"abcdefgh",),
            )
            target = root / undo.expected_restores[0].restore_target
            replacement = target.with_suffix(".replacement")
            replacement.write_bytes(b"abcdefgh")
            real_read = os.read
            replaced = False

            def replacing_read(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                block = real_read(descriptor, size)
                if block and not replaced:
                    replaced = True
                    os.replace(replacement, target)
                return block

            with mock.patch(
                "organizer.application.organization_undo_identity_verification.os.read",
                side_effect=replacing_read,
            ):
                result = verify_identity_organization_undo(
                    undo,
                    parent,
                    root,
                    chunk_size=4,
                )

            self.assertEqual(
                result.state,
                IdentityUndoVerificationState.INDETERMINATE,
            )

    def test_ineligible_parent_undo_status_root_mapping_and_identity_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _operations, _undos, parent, undo = _restored_pair(
                root,
                (b"first", b"second"),
            )
            changed_identity = RegularFileContentIdentity(
                "regular_file",
                "sha256",
                "f" * 64,
            )
            candidates = (
                (replace(undo, schema_version=1), parent),
                (replace(undo, status=UNDO_PREPARED), parent),
                (replace(undo, status=UNDO_UNDOING), parent),
                (replace(undo, status=UNDO_INTERRUPTED), parent),
                (replace(undo, status=UNDO_PARTIAL), parent),
                (replace(undo, status=UNDO_FAILED), parent),
                (replace(undo, status=UNDO_RESTORED), parent),
                (replace(undo, status=UNDO_VERIFICATION_FAILED), parent),
                (
                    replace(
                        undo,
                        verification=UndoVerificationResult(
                            status=UNDO_VERIFICATION_PASSED,
                            verified_count=2,
                        ),
                    ),
                    parent,
                ),
                (replace(undo, configured_root_identity="f" * 64), parent),
                (
                    replace(
                        undo,
                        expected_restores=tuple(reversed(undo.expected_restores)),
                    ),
                    parent,
                ),
                (
                    replace(
                        undo,
                        expected_restores=(
                            replace(
                                undo.expected_restores[0],
                                content_identity=changed_identity,
                            ),
                            undo.expected_restores[1],
                        ),
                    ),
                    parent,
                ),
                (
                    replace(
                        undo,
                        expected_restores=(
                            replace(
                                undo.expected_restores[0],
                                content_identity=None,
                            ),
                            undo.expected_restores[1],
                        ),
                    ),
                    parent,
                ),
                (
                    replace(
                        undo,
                        expected_restores=(
                            replace(
                                undo.expected_restores[0],
                                change_id="changed",
                            ),
                            undo.expected_restores[1],
                        ),
                    ),
                    parent,
                ),
                (
                    replace(
                        undo,
                        expected_restores=(
                            replace(
                                undo.expected_restores[0],
                                result_state=UNDO_ITEM_ATTEMPTING,
                            ),
                            undo.expected_restores[1],
                        ),
                    ),
                    parent,
                ),
                (undo, replace(parent, status="verification_failed")),
            )
            for candidate_undo, candidate_parent in candidates:
                with self.subTest(
                    undo_status=candidate_undo.status,
                    parent_status=candidate_parent.status,
                ), self.assertRaises(IdentityUndoVerificationEligibilityError):
                    verify_identity_organization_undo(
                        candidate_undo,
                        candidate_parent,
                        root,
                    )

    def test_process_control_closes_descriptors_and_propagates(self) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit()):
            with (
                self.subTest(kind=type(interruption).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                _operations, _undos, parent, undo = _restored_pair(
                    root,
                    (b"recorded",),
                )
                real_open = os.open
                opened: list[int] = []

                def observed_open(*args, **kwargs) -> int:
                    descriptor = real_open(*args, **kwargs)
                    opened.append(descriptor)
                    return descriptor

                with (
                    mock.patch(
                        "organizer.application.organization_undo_identity_verification.os.open",
                        side_effect=observed_open,
                    ),
                    mock.patch(
                        "organizer.application.organization_undo_identity_verification.os.read",
                        side_effect=interruption,
                    ),
                    self.assertRaises(type(interruption)),
                ):
                    verify_identity_organization_undo(undo, parent, root)
                for descriptor in opened:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)

    def test_verifier_is_read_only_redacted_and_operation_log_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _operations, _undos, parent, undo = _restored_pair(
                root,
                (b"recorded",),
            )
            undo = replace(undo, legacy_undo_log="missing/undo-log.json")
            parent = replace(parent, legacy_operation_log="missing/move-log.json")
            before = _tree(root)

            result = verify_identity_organization_undo(undo, parent, root)

            self.assertEqual(result.state, IdentityUndoVerificationState.PASSED)
            self.assertEqual(_tree(root), before)
            rendered = repr(result)
            for secret in (
                str(root),
                "item-1",
                parent.operation_id,
                undo.undo_id,
                parent.expected_moves[0].content_identity.digest,
            ):
                self.assertNotIn(secret, rendered)

        repository_root = Path(__file__).resolve().parents[1]
        source = (
            repository_root
            / "src/organizer/application/organization_undo_identity_verification.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "OrganizationUndoRepository",
            "OrganizationOperationRepository",
            "executor",
            "MovementRecorder",
            "OperationLog",
            "activity",
            "mkdir(",
            "unlink(",
            "replace(",
            "rename(",
        ):
            self.assertNotIn(forbidden, source)


def _restored_pair(root: Path, payloads: tuple[bytes, ...]):
    operations, undos, parent, intent = _intent(root, payloads)
    restored_items = tuple(
        replace(item, result_state=UNDO_ITEM_RESTORED)
        for item in intent.record.expected_restores
    )
    undo = replace(
        intent.record,
        status=UNDO_RESTORED_UNVERIFIED,
        started_at="2026-07-23T08:00:00+00:00",
        expected_restores=restored_items,
    )
    for item, payload in zip(restored_items, payloads):
        target = root / item.restore_target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        (root / item.moved_from).unlink()
    return operations, undos, parent, undo


def _tree(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                "directory" if path.is_dir() else "file",
            )
            for path in root.rglob("*")
        )
    )


if __name__ == "__main__":
    unittest.main()
