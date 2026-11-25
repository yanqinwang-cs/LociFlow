from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from organizer.application.content_identity import (
    REGULAR_FILE_IDENTITY_KIND,
    SHA256_IDENTITY_ALGORITHM,
    RegularFileContentIdentity,
)
from organizer.file_identity import (
    DEFAULT_HASH_CHUNK_SIZE,
    RegularFileStatFingerprint,
    StableFileIdentityError,
    StableFileIdentityInterrupted,
    capture_open_regular_file,
)

DEFAULT_IDENTITY_CAPTURE_CHUNK_SIZE = DEFAULT_HASH_CHUNK_SIZE
IDENTITY_CAPTURE_SAFE_MESSAGE = (
    "The file changed before it could be moved. Check the folder and try again."
)
_CAPTURE_ERROR_CODES = {
    "unsupported_item",
    "unsafe_source",
    "source_symlink",
    "source_missing",
    "source_unreadable",
    "source_changed_during_hash",
    "identity_capture_failed",
}


@dataclass(frozen=True)
class CapturedRegularFileIdentity:
    content_identity: RegularFileContentIdentity
    stat_fingerprint: RegularFileStatFingerprint


class FileIdentityCaptureError(Exception):
    """A classified failure whose rendered message contains no filesystem detail."""

    def __init__(self, code: str) -> None:
        if code not in _CAPTURE_ERROR_CODES:
            raise ValueError("identity capture error code is invalid")
        self.code = code
        self.safe_message = IDENTITY_CAPTURE_SAFE_MESSAGE
        super().__init__(self.safe_message)


class FileIdentityCaptureInterrupted(KeyboardInterrupt):
    """Typed interruption that retains normal KeyboardInterrupt semantics."""

    code = "identity_capture_interrupted"
    safe_message = IDENTITY_CAPTURE_SAFE_MESSAGE

    def __str__(self) -> str:
        return self.safe_message


def capture_regular_file_identity(
    path: Path,
    root: Path,
    *,
    chunk_size: int = DEFAULT_IDENTITY_CAPTURE_CHUNK_SIZE,
) -> CapturedRegularFileIdentity:
    """Capture stable SHA-256 identity from one safely revalidated descriptor.

    This proves continuity only from this explicit capture onward. A later strict
    executor must revalidate the recorded identity before moving the pathname.
    """

    try:
        with capture_open_regular_file(
            path,
            root,
            chunk_size=chunk_size,
        ) as verified:
            return CapturedRegularFileIdentity(
                content_identity=RegularFileContentIdentity(
                    kind=REGULAR_FILE_IDENTITY_KIND,
                    algorithm=SHA256_IDENTITY_ALGORITHM,
                    digest=verified.identity.digest,
                ),
                stat_fingerprint=verified.identity.stat_fingerprint,
            )
    except StableFileIdentityInterrupted:
        raise FileIdentityCaptureInterrupted() from None
    except StableFileIdentityError as error:
        mapped = (
            "source_changed_during_hash"
            if error.code in {"source_identity_changed", "source_unstable"}
            else error.code
        )
        raise FileIdentityCaptureError(mapped) from None
