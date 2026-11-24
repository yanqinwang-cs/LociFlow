from __future__ import annotations

from dataclasses import replace
import errno
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

from organizer.application.content_identity import RegularFileContentIdentity
from organizer.application.organization_identity_verification import (
    IdentityMismatchCategory,
    IdentityOrganizationVerificationResult,
    IdentityVerificationEligibilityError,
    IdentityVerificationState,
    verify_identity_organization_operation,
)
from organizer.application.organization_operation_models import (
    MOVE_ATTEMPTING,
    MOVE_FAILED,
    MOVE_MOVED,
    MOVE_PENDING,
    ORGANIZATION_OPERATION_SCHEMA_VERSION,
    STATUS_INTERRUPTED,
    STATUS_MOVED_UNVERIFIED,
    STATUS_MOVEMENT_FAILED,
    STATUS_MOVING,
    STATUS_PARTIAL_MOVEMENT,
    STATUS_PREPARED,
    STATUS_VERIFICATION_FAILED,
    STATUS_VERIFIED,
    VERIFICATION_FAILED,
    OrganizationExpectedMove,
    OrganizationOperationRecord,
    OrganizationOperationVerification,
)


class IdentityAwareOrganizationVerificationTests(unittest.TestCase):
    def test_valid_one_and_multiple_item_operations_pass_with_bounded_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payloads = (b"a" * 41, b"b" * 37)
            record = _record(root, payloads)
            read_sizes: list[int] = []
            real_read = os.read

            def observed_read(descriptor: int, size: int) -> bytes:
                read_sizes.append(size)
                return real_read(descriptor, size)

            with mock.patch(
                "organizer.application.organization_identity_verification.os.read",
                side_effect=observed_read,
            ):
                result = verify_identity_organization_operation(
                    record,
                    root,
                    chunk_size=7,
                )

            self.assertEqual(
                result,
                IdentityOrganizationVerificationResult(
                    state=IdentityVerificationState.PASSED,
                    affected_items=2,
                    verified_count=2,
                    mismatch_categories=(),
                ),
            )
            self.assertTrue(read_sizes)
            self.assertEqual(set(read_sizes), {7})

    def test_hard_link_move_and_changed_ctime_do_not_false_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"same-volume-content"
            source = root / "Inbox" / "item-1.txt"
            destination = root / "Documents" / "item-1.txt"
            source.parent.mkdir()
            destination.parent.mkdir()
            source.write_bytes(payload)
            before = source.stat().st_ctime_ns
            os.link(source, destination)
            source.unlink()
            self.assertGreaterEqual(destination.stat().st_ctime_ns, before)

            result = verify_identity_organization_operation(
                _record(root, (payload,), create_files=False),
                root,
            )
            self.assertEqual(result.state, IdentityVerificationState.PASSED)

    def test_same_size_same_mtime_replacement_is_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b"recorded"
            record = _record(root, (original,))
            destination = root / record.expected_moves[0].destination
            baseline = destination.stat()
            destination.write_bytes(b"replaced")
            os.utime(
                destination,
                ns=(baseline.st_atime_ns, baseline.st_mtime_ns),
            )

            result = verify_identity_organization_operation(record, root)
            self.assertEqual(result.state, IdentityVerificationState.MISMATCH)
            self.assertEqual(result.verified_count, 0)
            self.assertEqual(
                result.mismatch_categories,
                (IdentityMismatchCategory.DESTINATION_IDENTITY_MISMATCH,),
            )

    def test_source_presence_and_any_occupant_are_mismatches(self) -> None:
        for occupant in (b"recorded", b"another-entry"):
            with self.subTest(occupant=occupant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = _record(root, (b"recorded",))
                source = root / record.expected_moves[0].source
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(occupant)
                result = verify_identity_organization_operation(record, root)
                self.assertEqual(result.state, IdentityVerificationState.MISMATCH)
                self.assertIn(
                    IdentityMismatchCategory.SOURCE_PRESENT,
                    result.mismatch_categories,
                )

    def test_destination_missing_symlink_nonregular_and_unsafe_ancestry(self) -> None:
        cases = ("missing", "symlink", "directory", "unsafe_parent")
        expected = {
            "missing": IdentityMismatchCategory.DESTINATION_MISSING,
            "symlink": IdentityMismatchCategory.DESTINATION_SYMLINK,
            "directory": IdentityMismatchCategory.DESTINATION_NOT_REGULAR,
            "unsafe_parent": IdentityMismatchCategory.DESTINATION_UNSAFE,
        }
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "root"
                root.mkdir()
                record = _record(root, (b"recorded",))
                destination = root / record.expected_moves[0].destination
                if case == "missing":
                    destination.unlink()
                elif case == "symlink":
                    destination.unlink()
                    destination.symlink_to(base / "outside")
                elif case == "directory":
                    destination.unlink()
                    destination.mkdir()
                else:
                    destination.unlink()
                    destination.parent.rmdir()
                    destination.parent.symlink_to(base)
                result = verify_identity_organization_operation(record, root)
                self.assertEqual(result.state, IdentityVerificationState.MISMATCH)
                self.assertIn(expected[case], result.mismatch_categories)

    def test_unavailable_inspection_is_indeterminate_and_mismatch_dominates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = _record(root, (b"first", b"second"))
            real_verify = __import__(
                "organizer.application.organization_identity_verification",
                fromlist=["_verify_destination"],
            )._verify_destination
            calls = 0

            def unavailable_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(errno.EIO, "private detail")
                return real_verify(*args, **kwargs)

            with mock.patch(
                "organizer.application.organization_identity_verification._verify_destination",
                side_effect=unavailable_second,
            ):
                # Unexpected helper errors remain at the application boundary.
                with self.assertRaises(OSError):
                    verify_identity_organization_operation(record, root)

            destination = root / record.expected_moves[0].destination
            destination.write_bytes(b"wrong")
            real_read = os.read
            completed_files = 0

            def fail_second_descriptor(descriptor: int, size: int) -> bytes:
                nonlocal completed_files
                if completed_files == 1:
                    raise OSError(errno.EIO, "private detail")
                block = real_read(descriptor, size)
                if not block:
                    completed_files += 1
                return block

            with mock.patch(
                "organizer.application.organization_identity_verification.os.read",
                side_effect=fail_second_descriptor,
            ):
                result = verify_identity_organization_operation(record, root)
            self.assertEqual(completed_files, 1)
            self.assertEqual(result.state, IdentityVerificationState.MISMATCH)
            self.assertEqual(result.verified_count, 0)
            self.assertEqual(
                result.mismatch_categories,
                (IdentityMismatchCategory.DESTINATION_IDENTITY_MISMATCH,),
            )

    def test_read_failure_without_mismatch_is_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = _record(root, (b"recorded",))
            with mock.patch(
                "organizer.application.organization_identity_verification.os.read",
                side_effect=OSError(errno.EIO, "private path detail"),
            ):
                result = verify_identity_organization_operation(record, root)
            self.assertEqual(result.state, IdentityVerificationState.INDETERMINATE)
            self.assertEqual(result.verified_count, 0)
            self.assertEqual(result.mismatch_categories, ())

    def test_destination_replaced_while_open_is_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = _record(root, (b"abcdefgh",))
            destination = root / record.expected_moves[0].destination
            replacement = destination.with_suffix(".replacement")
            replacement.write_bytes(b"abcdefgh")
            real_read = os.read
            replaced = False

            def replacing_read(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                block = real_read(descriptor, size)
                if block and not replaced:
                    replaced = True
                    os.replace(replacement, destination)
                return block

            with mock.patch(
                "organizer.application.organization_identity_verification.os.read",
                side_effect=replacing_read,
            ):
                result = verify_identity_organization_operation(
                    record,
                    root,
                    chunk_size=4,
                )
            self.assertEqual(result.state, IdentityVerificationState.INDETERMINATE)

    def test_descriptors_close_on_all_outcomes_and_process_control(self) -> None:
        failures = (
            None,
            OSError(errno.EIO, "private detail"),
            KeyboardInterrupt(),
            SystemExit(),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = _record(root, (b"recorded",))
                real_open = os.open
                opened: list[int] = []

                def observed_open(*args, **kwargs) -> int:
                    descriptor = real_open(*args, **kwargs)
                    opened.append(descriptor)
                    return descriptor

                patches = [
                    mock.patch(
                        "organizer.application.organization_identity_verification.os.open",
                        side_effect=observed_open,
                    )
                ]
                if failure is not None:
                    patches.append(
                        mock.patch(
                            "organizer.application.organization_identity_verification.os.read",
                            side_effect=failure,
                        )
                    )
                with patches[0]:
                    if len(patches) == 2:
                        with patches[1]:
                            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                                with self.assertRaises(type(failure)):
                                    verify_identity_organization_operation(record, root)
                            else:
                                verify_identity_organization_operation(record, root)
                    else:
                        verify_identity_organization_operation(record, root)
                for descriptor in opened:
                    with self.assertRaises(OSError) as raised:
                        os.fstat(descriptor)
                    self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_strict_eligibility_rejects_incomplete_and_cross_field_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = _record(root, (b"recorded",))
            item = valid.expected_moves[0]
            cases = (
                replace(valid, schema_version=ORGANIZATION_OPERATION_SCHEMA_VERSION),
                replace(valid, status=STATUS_PREPARED),
                replace(valid, status=STATUS_MOVING),
                replace(valid, status=STATUS_INTERRUPTED),
                replace(valid, status=STATUS_PARTIAL_MOVEMENT),
                replace(valid, status=STATUS_MOVEMENT_FAILED),
                replace(valid, status=STATUS_VERIFIED),
                replace(valid, status=STATUS_VERIFICATION_FAILED),
                replace(valid, expected_moves=()),
                replace(valid, expected_moves=(replace(item, result_state=MOVE_PENDING),)),
                replace(valid, expected_moves=(replace(item, result_state=MOVE_ATTEMPTING),)),
                replace(valid, expected_moves=(replace(item, result_state=MOVE_FAILED),)),
                replace(valid, expected_moves=(replace(item, content_identity=None),)),
                replace(valid, expected_moves=(replace(item, content_identity=object()),)),
                replace(valid, configured_root_identity="0" * 64),
                replace(valid, started_at=None),
                replace(valid, completed_at="2026-07-20T10:02:00+00:00"),
                replace(valid, failure_code="private_failure"),
                replace(
                    valid,
                    verification=OrganizationOperationVerification(
                        status=VERIFICATION_FAILED,
                        verified_count=0,
                        mismatch_codes=("private",),
                    ),
                ),
                replace(valid, expected_moves=(replace(item, failure_code="failed"),)),
                replace(valid, expected_moves=(replace(item, recovery_state="retry"),)),
                replace(valid, expected_moves=(replace(item, destination=item.source),)),
                replace(valid, expected_moves=(item, item)),
            )
            for candidate in cases:
                with self.subTest(status=candidate.status), self.assertRaises(
                    IdentityVerificationEligibilityError
                ) as raised:
                    verify_identity_organization_operation(candidate, root)
                self.assertNotIn(str(root), str(raised.exception))
                self.assertNotIn("item-1", str(raised.exception))

    def test_result_and_error_copy_are_private_and_operation_log_is_irrelevant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = replace(
                _record(root, (b"private-content",)),
                legacy_operation_log="malformed/private-log.json",
            )
            destination = root / record.expected_moves[0].destination
            destination.write_bytes(b"changed-content!")
            result = verify_identity_organization_operation(record, root)
            rendered = repr(result)
            for secret in (
                str(root),
                "item-1",
                record.operation_id,
                record.expected_moves[0].content_identity.digest,
                "private-log",
            ):
                self.assertNotIn(secret, rendered)

    def test_verifier_has_no_repository_executor_recorder_or_operation_log_dependency(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "src"
            / "organizer"
            / "application"
            / "organization_identity_verification.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
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


def _record(
    root: Path,
    payloads: tuple[bytes, ...],
    *,
    create_files: bool = True,
) -> OrganizationOperationRecord:
    moves: list[OrganizationExpectedMove] = []
    for index, payload in enumerate(payloads, start=1):
        source = Path("Inbox") / f"item-{index}.txt"
        destination = Path("Documents") / f"item-{index}.txt"
        if create_files:
            path = root / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        moves.append(
            OrganizationExpectedMove(
                change_id=f"O{index}",
                source=source.as_posix(),
                destination=destination.as_posix(),
                source_size_bytes=len(payload),
                source_modified_time=100.0,
                reason="test",
                confidence=100,
                operation="move",
                overwrite_risk=False,
                result_state=MOVE_MOVED,
                content_identity=RegularFileContentIdentity(
                    kind="regular_file",
                    algorithm="sha256",
                    digest=hashlib.sha256(payload).hexdigest(),
                ),
            )
        )
    root_identity = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    return OrganizationOperationRecord(
        operation_id=str(uuid.uuid4()),
        authorization_id=str(uuid.uuid4()),
        status=STATUS_MOVED_UNVERIFIED,
        scan_generation="scan-one",
        decision_revision=1,
        approved_at="2026-07-20T10:00:00+00:00",
        created_at="2026-07-20T10:00:00+00:00",
        started_at="2026-07-20T10:01:00+00:00",
        completed_at=None,
        configured_root_identity=root_identity,
        expected_moves=tuple(moves),
        verification=OrganizationOperationVerification(),
        legacy_operation_log=None,
        failure_code=None,
        duplicate_actions_deferred=0,
        schema_version=2,
    )


if __name__ == "__main__":
    unittest.main()
