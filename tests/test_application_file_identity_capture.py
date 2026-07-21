from __future__ import annotations

from dataclasses import FrozenInstanceError
import errno
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from organizer.application.file_identity_capture import (
    IDENTITY_CAPTURE_SAFE_MESSAGE,
    FileIdentityCaptureError,
    FileIdentityCaptureInterrupted,
    capture_regular_file_identity,
)


class FileIdentityCaptureTests(unittest.TestCase):
    def test_stable_regular_file_is_captured_deterministically_and_immutably(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "alpha.txt"
            source.write_bytes(b"stable bytes")

            captured = capture_regular_file_identity(source, root)

            self.assertEqual(captured.content_identity.kind, "regular_file")
            self.assertEqual(captured.content_identity.algorithm, "sha256")
            self.assertEqual(
                captured.content_identity.digest,
                hashlib.sha256(b"stable bytes").hexdigest(),
            )
            self.assertEqual(captured.stat_fingerprint.file_type, "regular_file")
            self.assertEqual(captured.stat_fingerprint.size_bytes, 12)
            with self.assertRaises(FrozenInstanceError):
                captured.stat_fingerprint.inode = 1

    def test_large_file_streams_in_configured_bounded_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "large.bin"
            payload = b"0123456789" * 50
            source.write_bytes(payload)
            real_read = os.read
            requested: list[int] = []

            def observed_read(descriptor: int, size: int) -> bytes:
                requested.append(size)
                return real_read(descriptor, size)

            with mock.patch(
                "organizer.application.file_identity_capture.os.read",
                side_effect=observed_read,
            ):
                captured = capture_regular_file_identity(source, root, chunk_size=17)

            self.assertEqual(captured.content_identity.digest, hashlib.sha256(payload).hexdigest())
            self.assertGreater(len(requested), 2)
            self.assertEqual(set(requested), {17})

    def test_chunk_size_must_be_a_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"x")
            for invalid in (0, -1, True, 1.0, None):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    capture_regular_file_identity(source, root, chunk_size=invalid)

    def test_rejects_symlink_directory_fifo_outside_and_missing_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            regular = root / "regular"
            regular.write_bytes(b"x")
            symlink = root / "link"
            symlink.symlink_to(regular)
            fifo = root / "pipe"
            if hasattr(os, "mkfifo"):
                os.mkfifo(fifo)

            cases = [
                (symlink, "source_symlink"),
                (root, "unsupported_item"),
                (outside / "file", "source_missing"),
                (root / "missing", "source_missing"),
            ]
            (outside / "file").write_bytes(b"outside")
            cases[2] = (outside / "file", "unsafe_source")
            if fifo.exists():
                cases.append((fifo, "unsupported_item"))

            for path, code in cases:
                with self.subTest(path=path.name), self.assertRaises(FileIdentityCaptureError) as raised:
                    capture_regular_file_identity(path, root)
                self.assertEqual(raised.exception.code, code)

    def test_unreadable_open_is_classified_without_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private-name.txt"
            source.write_bytes(b"private-content")
            with mock.patch(
                "organizer.application.file_identity_capture.os.open",
                side_effect=PermissionError(errno.EACCES, "private operating-system detail"),
            ):
                with self.assertRaises(FileIdentityCaptureError) as raised:
                    capture_regular_file_identity(source, root)

            self.assertEqual(raised.exception.code, "source_unreadable")
            self.assertEqual(str(raised.exception), IDENTITY_CAPTURE_SAFE_MESSAGE)
            rendered = str(raised.exception)
            for secret in (str(root), "private-name", "private-content", "operating-system"):
                self.assertNotIn(secret, rendered)

    def test_devices_and_sockets_are_rejected_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "special"
            for mode in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK):
                fake_stat = os.stat_result(
                    (mode | 0o600, 1, 1, 1, 0, 0, 0, 0, 0, 0)
                )
                with (
                    self.subTest(mode=mode),
                    mock.patch(
                        "organizer.application.file_identity_capture.os.lstat",
                        return_value=fake_stat,
                    ),
                    mock.patch(
                        "organizer.application.file_identity_capture.os.open"
                    ) as opened,
                    self.assertRaises(FileIdentityCaptureError) as raised,
                ):
                    capture_regular_file_identity(candidate, root)
                self.assertEqual(raised.exception.code, "unsupported_item")
                opened.assert_not_called()

    def test_removal_rename_replacement_and_same_inode_changes_fail_closed(self) -> None:
        mutations = ("remove", "rename", "replace", "rewrite", "truncate", "extend", "touch")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.bin"
                source.write_bytes(b"abcdefgh")
                real_read = os.read
                changed = False

                def mutating_read(descriptor: int, size: int) -> bytes:
                    nonlocal changed
                    block = real_read(descriptor, size)
                    if block and not changed:
                        changed = True
                        if mutation == "remove":
                            source.unlink()
                        elif mutation == "rename":
                            source.rename(root / "renamed.bin")
                        elif mutation == "replace":
                            replacement = root / "replacement.bin"
                            replacement.write_bytes(b"ABCDEFGH")
                            os.replace(replacement, source)
                        elif mutation == "rewrite":
                            source.write_bytes(b"ABCDEFGH")
                        elif mutation == "truncate":
                            source.write_bytes(b"abcd")
                        elif mutation == "extend":
                            source.write_bytes(b"abcdefghijkl")
                        else:
                            current = source.stat()
                            os.utime(source, ns=(current.st_atime_ns, current.st_mtime_ns + 1))
                    return block

                with mock.patch(
                    "organizer.application.file_identity_capture.os.read",
                    side_effect=mutating_read,
                ):
                    with self.assertRaises(FileIdentityCaptureError) as raised:
                        capture_regular_file_identity(source, root, chunk_size=4)
                self.assertEqual(raised.exception.code, "source_changed_during_hash")

    def test_read_failure_and_interruption_close_the_descriptor(self) -> None:
        for failure in (OSError(errno.EIO, "private read detail"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                source.write_bytes(b"bytes")
                real_open = os.open
                descriptor: list[int] = []

                def observed_open(*args, **kwargs) -> int:
                    value = real_open(*args, **kwargs)
                    descriptor.append(value)
                    return value

                with (
                    mock.patch(
                        "organizer.application.file_identity_capture.os.open",
                        side_effect=observed_open,
                    ),
                    mock.patch(
                        "organizer.application.file_identity_capture.os.read",
                        side_effect=failure,
                    ),
                ):
                    expected = FileIdentityCaptureInterrupted if isinstance(failure, KeyboardInterrupt) else FileIdentityCaptureError
                    with self.assertRaises(expected) as raised:
                        capture_regular_file_identity(source, root)

                self.assertEqual(len(descriptor), 1)
                with self.assertRaises(OSError) as closed:
                    os.fstat(descriptor[0])
                self.assertEqual(closed.exception.errno, errno.EBADF)
                self.assertIn(
                    raised.exception.code,
                    {"source_unreadable", "identity_capture_interrupted"},
                )

    def test_success_and_post_hash_failure_close_descriptors_without_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"sequential")
            real_open = os.open
            descriptors: list[int] = []

            def observed_open(*args, **kwargs) -> int:
                descriptor = real_open(*args, **kwargs)
                descriptors.append(descriptor)
                return descriptor

            with mock.patch(
                "organizer.application.file_identity_capture.os.open",
                side_effect=observed_open,
            ):
                for _ in range(40):
                    capture_regular_file_identity(source, root, chunk_size=2)

            self.assertEqual(len(descriptors), 40)
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

            with (
                mock.patch(
                    "organizer.application.file_identity_capture.os.open",
                    side_effect=observed_open,
                ),
                mock.patch(
                    "organizer.application.file_identity_capture._require_stable_regular_file",
                    side_effect=FileIdentityCaptureError("source_changed_during_hash"),
                ),
            ):
                with self.assertRaises(FileIdentityCaptureError):
                    capture_regular_file_identity(source, root)
            with self.assertRaises(OSError):
                os.fstat(descriptors[-1])


if __name__ == "__main__":
    unittest.main()
