from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

REGULAR_FILE_IDENTITY_KIND = "regular_file"
SHA256_IDENTITY_ALGORITHM = "sha256"

_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}")


class OrdinaryFileIdentityCompatibility(str, Enum):
    HISTORICAL_V1 = "historical_v1"
    IDENTITY_V2 = "identity_v2"


@dataclass(frozen=True)
class RegularFileContentIdentity:
    kind: str
    algorithm: str
    digest: str

    def __post_init__(self) -> None:
        if self.kind != REGULAR_FILE_IDENTITY_KIND:
            raise ValueError("content identity kind is unsupported")
        if self.algorithm != SHA256_IDENTITY_ALGORITHM:
            raise ValueError("content identity algorithm is unsupported")
        if not isinstance(self.digest, str) or _SHA256_DIGEST.fullmatch(
            self.digest
        ) is None:
            raise ValueError("content identity digest is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "algorithm": self.algorithm,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: object) -> RegularFileContentIdentity:
        if not isinstance(data, dict) or set(data) != {
            "kind",
            "algorithm",
            "digest",
        }:
            raise ValueError("content identity fields are invalid")
        return cls(
            kind=data["kind"],
            algorithm=data["algorithm"],
            digest=data["digest"],
        )


def identity_compatibility(schema_version: int) -> OrdinaryFileIdentityCompatibility:
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValueError("ordinary file identity schema version is unsupported")
    if schema_version == 1:
        return OrdinaryFileIdentityCompatibility.HISTORICAL_V1
    if schema_version == 2:
        return OrdinaryFileIdentityCompatibility.IDENTITY_V2
    raise ValueError("ordinary file identity schema version is unsupported")
