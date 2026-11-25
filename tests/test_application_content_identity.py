from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from organizer.application.content_identity import (
    OrdinaryFileIdentityCompatibility,
    RegularFileContentIdentity,
    identity_compatibility,
)


class RegularFileContentIdentityTests(unittest.TestCase):
    def test_valid_identity_is_immutable_and_deterministic(self) -> None:
        identity = RegularFileContentIdentity(
            kind="regular_file",
            algorithm="sha256",
            digest="a" * 64,
        )

        self.assertEqual(
            identity.to_dict(),
            {
                "kind": "regular_file",
                "algorithm": "sha256",
                "digest": "a" * 64,
            },
        )
        self.assertEqual(
            RegularFileContentIdentity.from_dict(identity.to_dict()), identity
        )
        with self.assertRaises(FrozenInstanceError):
            identity.digest = "b" * 64

    def test_rejects_every_noncanonical_identity(self) -> None:
        valid = {
            "kind": "regular_file",
            "algorithm": "sha256",
            "digest": "a" * 64,
        }
        invalid = (
            {**valid, "digest": "A" * 64},
            {**valid, "digest": "a" * 63},
            {**valid, "digest": "a" * 65},
            {**valid, "digest": "g" * 64},
            {**valid, "digest": f"{'a' * 63} "},
            {**valid, "algorithm": "sha512"},
            {**valid, "kind": "directory"},
            {**valid, "unexpected": "value"},
            None,
        )

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                RegularFileContentIdentity.from_dict(value)

    def test_schema_compatibility_is_explicit_and_closed(self) -> None:
        self.assertEqual(
            identity_compatibility(1),
            OrdinaryFileIdentityCompatibility.HISTORICAL_V1,
        )
        self.assertEqual(
            identity_compatibility(2),
            OrdinaryFileIdentityCompatibility.IDENTITY_V2,
        )
        for invalid in (3, True, 2.0):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                identity_compatibility(invalid)


if __name__ == "__main__":
    unittest.main()
