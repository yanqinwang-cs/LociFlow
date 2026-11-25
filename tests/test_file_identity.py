from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organizer.file_identity import (
    RequiredRegularFileIdentity,
    StableFileIdentityError,
    StableFileIdentityInterrupted,
    capture_open_regular_file,
    open_verified_regular_file,
    verify_open_regular_file_descriptor,
)


class NeutralFileIdentityTests(unittest.TestCase):
    def test_retained_descriptor_verification_does_not_take_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"identity")
            descriptor = os.open(source, os.O_RDONLY)
            try:
                observed = verify_open_regular_file_descriptor(
                    descriptor,
                    hashlib.sha256(b"identity").hexdigest(),
                )
                self.assertEqual(observed.inode, os.fstat(descriptor).st_ino)
                with self.assertRaises(StableFileIdentityError) as raised:
                    verify_open_regular_file_descriptor(descriptor, "0" * 64)
                self.assertEqual(raised.exception.code, "source_identity_changed")
                self.assertEqual(os.fstat(descriptor).st_size, len(b"identity"))
            finally:
                os.close(descriptor)

    def test_capture_retains_verified_descriptor_until_context_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            payload = b"bounded-stream" * 20
            source.write_bytes(payload)

            with capture_open_regular_file(source, root, chunk_size=7) as verified:
                descriptor = verified.fileno()
                self.assertEqual(os.fstat(descriptor).st_size, len(payload))
                self.assertEqual(
                    verified.identity.digest,
                    hashlib.sha256(payload).hexdigest(),
                )
                verified.rewind()
                self.assertEqual(os.read(descriptor, 7), payload[:7])
                self.assertNotIn(str(source), repr(verified))
                self.assertNotIn(verified.identity.digest, repr(verified))

            with self.assertRaises(OSError) as raised:
                os.fstat(descriptor)
            self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_enforcement_requires_and_accepts_exact_digest_and_stat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"identity")
            with capture_open_regular_file(source, root) as captured:
                required = RequiredRegularFileIdentity(
                    captured.identity.digest,
                    captured.identity.stat_fingerprint,
                )

            with open_verified_regular_file(source, root, required) as verified:
                self.assertEqual(verified.identity, captured.identity)

            with self.assertRaises(StableFileIdentityError) as missing:
                with open_verified_regular_file(source, root, None):
                    pass
            self.assertEqual(missing.exception.code, "missing_identity")
            with self.assertRaises(StableFileIdentityError) as malformed:
                with open_verified_regular_file(source, root, object()):
                    pass
            self.assertEqual(malformed.exception.code, "malformed_identity")

    def test_same_size_same_mtime_replacement_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"first")
            with capture_open_regular_file(source, root) as captured:
                required = RequiredRegularFileIdentity(
                    captured.identity.digest,
                    captured.identity.stat_fingerprint,
                )
                original_stat = source.stat()

            replacement = root / "replacement"
            replacement.write_bytes(b"other")
            os.utime(
                replacement,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            os.replace(replacement, source)

            with self.assertRaises(StableFileIdentityError) as raised:
                with open_verified_regular_file(source, root, required):
                    pass
            self.assertEqual(raised.exception.code, "source_identity_changed")

    def test_digest_mismatch_closes_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"identity")
            with capture_open_regular_file(source, root) as captured:
                required = RequiredRegularFileIdentity(
                    "0" * 64,
                    captured.identity.stat_fingerprint,
                )
            real_open = os.open
            descriptors: list[int] = []

            def observed_open(*args, **kwargs) -> int:
                descriptor = real_open(*args, **kwargs)
                descriptors.append(descriptor)
                return descriptor

            with mock.patch(
                "organizer.file_identity.os.open",
                side_effect=observed_open,
            ):
                with self.assertRaises(StableFileIdentityError) as raised:
                    with open_verified_regular_file(source, root, required):
                        pass

            self.assertEqual(raised.exception.code, "source_identity_changed")
            with self.assertRaises(OSError) as closed:
                os.fstat(descriptors[0])
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_same_inode_change_and_path_substitution_during_hash_reject(self) -> None:
        mutations = ("rewrite", "remove", "rename", "symlink")
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = root / "source"
                source.write_bytes(b"abcdefgh")
                target = root / "target"
                target.write_bytes(b"abcdefgh")
                real_read = os.read
                changed = False

                def mutating_read(descriptor: int, size: int) -> bytes:
                    nonlocal changed
                    block = real_read(descriptor, size)
                    if block and not changed:
                        changed = True
                        if mutation == "rewrite":
                            source.write_bytes(b"ABCDEFGH")
                        elif mutation == "remove":
                            source.unlink()
                        elif mutation == "rename":
                            source.rename(root / "renamed")
                        else:
                            source.unlink()
                            source.symlink_to(target)
                    return block

                with mock.patch(
                    "organizer.file_identity.os.read",
                    side_effect=mutating_read,
                ):
                    with self.assertRaises(StableFileIdentityError) as raised:
                        with capture_open_regular_file(source, root, chunk_size=4):
                            pass
                self.assertEqual(raised.exception.code, "source_unstable")

    def test_missing_outside_symlink_and_nonregular_sources_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.write_bytes(b"outside")
            target = root / "target"
            target.write_bytes(b"target")
            link = root / "link"
            link.symlink_to(target)
            cases = (
                (root / "missing", "source_missing"),
                (outside, "unsafe_source"),
                (link, "source_symlink"),
                (root, "unsupported_item"),
            )
            for path, code in cases:
                with (
                    self.subTest(code=code),
                    self.assertRaises(StableFileIdentityError) as raised,
                ):
                    with capture_open_regular_file(path, root):
                        pass
                self.assertEqual(raised.exception.code, code)

    def test_read_failure_and_interruption_close_without_sensitive_copy(self) -> None:
        failures = (
            OSError(errno.EIO, "private operating-system detail"),
            KeyboardInterrupt(),
        )
        for failure in failures:
            with (
                self.subTest(kind=type(failure).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = root / "private-name"
                source.write_bytes(b"private-content")
                real_open = os.open
                descriptors: list[int] = []

                def observed_open(*args, **kwargs) -> int:
                    descriptor = real_open(*args, **kwargs)
                    descriptors.append(descriptor)
                    return descriptor

                with (
                    mock.patch(
                        "organizer.file_identity.os.open",
                        side_effect=observed_open,
                    ),
                    mock.patch(
                        "organizer.file_identity.os.read",
                        side_effect=failure,
                    ),
                ):
                    expected = (
                        StableFileIdentityInterrupted
                        if isinstance(failure, KeyboardInterrupt)
                        else StableFileIdentityError
                    )
                    with self.assertRaises(expected) as raised:
                        with capture_open_regular_file(source, root):
                            pass

                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])
                rendered = str(raised.exception)
                for secret in (
                    str(root),
                    "private-name",
                    "private-content",
                    "operating-system",
                ):
                    self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
