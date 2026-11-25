from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_ROOT = REPOSITORY_ROOT / "packaging" / "macos"
sys.path.insert(0, str(PACKAGING_ROOT))

from sign_app import (  # noqa: E402
    SigningContractError,
    _validate_fingerprint,
    _validate_team_id,
    format_signed_result,
    main,
    sign_application,
)
from validate_app_bundle import (  # noqa: E402
    BundleValidationReport,
    BundleValidationResult,
    MachORecord,
)

FINGERPRINT = "A" * 40
TEAM_ID = "TEAMID1234"
DISPLAY_NAME = f"Developer ID Application: Example ({TEAM_ID})"


class MacOSSigningContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = _create_source_app(self.root / "dist")
        self.staging_root = self.root / "release-staging"
        self.staging = self.staging_root / "candidate"
        self.runner = _SigningRunner(self.source, self.staging)
        self.validator = _BundleValidator()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def sign(self):
        return sign_application(
            source_app=self.source,
            staging_directory=self.staging,
            identity_fingerprint=FINGERPRINT,
            expected_team_id=TEAM_ID,
            command_runner=self.runner,
            bundle_validator=self.validator,
            release_staging_root=self.staging_root,
        )

    def test_full_fingerprint_is_normalized_and_partial_or_name_rejects(
        self,
    ) -> None:
        self.assertEqual(_validate_fingerprint(FINGERPRINT.lower()), FINGERPRINT)
        for value in (
            FINGERPRINT[:-1],
            DISPLAY_NAME,
            f" {FINGERPRINT}",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaises(SigningContractError) as raised:
                    _validate_fingerprint(value)
                self.assertEqual(
                    raised.exception.code,
                    "invalid_identity_fingerprint",
                )

    def test_team_id_is_exact_uppercase_ten_character_value(self) -> None:
        self.assertEqual(_validate_team_id(TEAM_ID), TEAM_ID)
        for value in (TEAM_ID.lower(), TEAM_ID[:-1], f" {TEAM_ID}", ""):
            with self.subTest(value=value):
                with self.assertRaises(SigningContractError) as raised:
                    _validate_team_id(value)
                self.assertEqual(raised.exception.code, "invalid_team_id")

    def test_absent_or_private_keyless_identity_rejects_before_copy(
        self,
    ) -> None:
        self.runner.identity_output = "     0 valid identities found\n"

        with self.assertRaises(SigningContractError) as raised:
            self.sign()

        self.assertEqual(raised.exception.code, "identity_missing")
        self.assertFalse(self.staging.exists())
        self.assertFalse(self.runner.commands_for("/usr/bin/ditto"))

    def test_ambiguous_identity_rejects(self) -> None:
        line = f'  1) {FINGERPRINT} "{DISPLAY_NAME}"\n'
        self.runner.identity_output = line + line

        with self.assertRaises(SigningContractError) as raised:
            self.sign()

        self.assertEqual(raised.exception.code, "identity_ambiguous")

    def test_wrong_certificate_type_and_wrong_team_reject(self) -> None:
        cases = (
            (
                f'  1) {FINGERPRINT} "Apple Development: Example ({TEAM_ID})"\n',
                "identity_wrong_type",
            ),
            (
                f"  1) {FINGERPRINT} "
                '"Developer ID Application: Example (OTHERID123)"\n',
                "identity_wrong_team",
            ),
        )
        for output, expected in cases:
            with self.subTest(expected=expected):
                self.runner.identity_output = output
                with self.assertRaises(SigningContractError) as raised:
                    self.sign()
                self.assertEqual(raised.exception.code, expected)

    def test_certificate_team_and_expiry_are_validated(self) -> None:
        self.runner.openssl_output = _certificate_details("OTHERID123")
        with self.assertRaises(SigningContractError) as wrong_team:
            self.sign()
        self.assertEqual(
            wrong_team.exception.code,
            "certificate_wrong_team",
        )

        self.runner.openssl_output = _certificate_details(TEAM_ID)
        self.runner.openssl_returncode = 1
        with self.assertRaises(SigningContractError) as expired:
            self.sign()
        self.assertEqual(expired.exception.code, "certificate_expired")

    def test_not_yet_valid_certificate_rejects(self) -> None:
        self.runner.openssl_output = _certificate_details(
            TEAM_ID,
            not_before="Jan  1 00:00:00 2090 GMT",
            not_after="Jan  1 00:00:00 2099 GMT",
        )

        with self.assertRaises(SigningContractError) as raised:
            self.sign()

        self.assertEqual(
            raised.exception.code,
            "certificate_not_yet_valid",
        )

    def test_safe_errors_redact_fingerprint_and_raw_tool_output(self) -> None:
        self.runner.identity_returncode = 1
        self.runner.identity_output = f"{FINGERPRINT} /private/keychain secret-account"

        with self.assertRaises(SigningContractError) as raised:
            self.sign()

        rendered = str(raised.exception)
        self.assertEqual(rendered, "Loci Flow signing could not be completed.")
        self.assertNotIn(FINGERPRINT, rendered)
        self.assertNotIn("keychain", rendered)
        self.assertNotIn("secret-account", rendered)

    def test_source_destination_equivalence_and_staging_escape_reject(
        self,
    ) -> None:
        equivalent_root = self.source.parent
        with self.assertRaises(SigningContractError) as equivalent:
            sign_application(
                source_app=self.source,
                staging_directory=equivalent_root,
                identity_fingerprint=FINGERPRINT,
                expected_team_id=TEAM_ID,
                command_runner=self.runner,
                bundle_validator=self.validator,
                release_staging_root=equivalent_root,
            )
        self.assertEqual(
            equivalent.exception.code,
            "source_destination_equivalent",
        )

        with self.assertRaises(SigningContractError) as escape:
            sign_application(
                source_app=self.source,
                staging_directory=self.root / "outside",
                identity_fingerprint=FINGERPRINT,
                expected_team_id=TEAM_ID,
                command_runner=self.runner,
                bundle_validator=self.validator,
                release_staging_root=self.staging_root,
            )
        self.assertEqual(escape.exception.code, "invalid_staging_path")
        self.assertFalse(self.runner.commands)

    def test_symlink_source_is_rejected_before_identity_or_copy(self) -> None:
        linked = self.root / "linked.app"
        linked.symlink_to(self.source, target_is_directory=True)

        with self.assertRaises(SigningContractError) as raised:
            sign_application(
                source_app=linked,
                staging_directory=self.staging,
                identity_fingerprint=FINGERPRINT,
                expected_team_id=TEAM_ID,
                command_runner=self.runner,
                bundle_validator=self.validator,
                release_staging_root=self.staging_root,
            )

        self.assertEqual(raised.exception.code, "invalid_source_app")
        self.assertFalse(self.runner.commands)

    def test_owned_prior_destination_is_replaced_and_unrelated_path_remains(
        self,
    ) -> None:
        prior = self.staging / "Loci Flow.app"
        prior.mkdir(parents=True)
        (prior / "old").write_text("old", encoding="utf-8")
        unrelated = self.staging_root / "unrelated"
        unrelated.write_text("keep", encoding="utf-8")

        result = self.sign()

        self.assertFalse((prior / "old").exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertEqual(
            result.signed_staging_app_path,
            str(prior.resolve()),
        )

    def test_ditto_copy_grammar_and_canonical_input_preservation(self) -> None:
        original = _tree_snapshot(self.source)

        result = self.sign()

        self.assertEqual(
            self.runner.commands_for("/usr/bin/ditto"),
            [
                [
                    "/usr/bin/ditto",
                    str(self.source.resolve()),
                    result.signed_staging_app_path,
                ]
            ],
        )
        self.assertEqual(_tree_snapshot(self.source), original)

    def test_dynamic_nested_order_outer_last_and_signing_flags(self) -> None:
        result = self.sign()
        signing = [
            command
            for command in self.runner.commands_for("/usr/bin/codesign")
            if "--sign" in command
        ]
        expected_nested = [
            str(Path(result.signed_staging_app_path) / record.relative_path)
            for record in self.validator.result.macho_files
            if record.relative_path != "Contents/MacOS/Loci Flow"
        ]

        self.assertEqual(
            [command[-1] for command in signing[:-1]],
            expected_nested,
        )
        self.assertEqual(signing[-1][-1], result.signed_staging_app_path)
        self.assertEqual(len(signing), 12)
        for command in signing:
            self.assertEqual(
                command[:-1],
                [
                    "/usr/bin/codesign",
                    "--force",
                    "--sign",
                    FINGERPRINT,
                    "--options",
                    "runtime",
                    "--timestamp",
                ],
            )
            self.assertNotIn("--deep", command)
            self.assertNotIn("--entitlements", command)

    def test_signing_failure_stops_and_removes_staged_result(self) -> None:
        self.runner.fail_signing_number = 2

        with self.assertRaises(SigningContractError) as raised:
            self.sign()

        self.assertEqual(raised.exception.code, "signing_failed")
        self.assertEqual(len(self.runner.signing_commands), 2)
        self.assertFalse((self.staging / "Loci Flow.app").exists())
        self.assertFalse(self.runner.commands_for("/usr/sbin/spctl"))

    def test_copy_exception_cleans_only_partial_owned_destination(
        self,
    ) -> None:
        unrelated = self.staging_root / "unrelated"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("keep", encoding="utf-8")
        self.runner.raise_after_ditto_copy = True

        with self.assertRaises(SigningContractError) as raised:
            self.sign()

        self.assertEqual(raised.exception.code, "staging_copy_failed")
        self.assertFalse((self.staging / "Loci Flow.app").exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_no_signed_path_is_emitted_after_failure(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = main(
                [
                    "--source-app",
                    str(self.source),
                    "--staging-directory",
                    str(self.staging),
                    "--identity-fingerprint",
                    "not-a-fingerprint",
                    "--expected-team-id",
                    TEAM_ID,
                ]
            )

        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue().strip(),
            "Loci Flow signing could not be completed.",
        )

    def test_strict_item_entitlement_workflow_and_gatekeeper_contract(
        self,
    ) -> None:
        result = self.sign()
        codesign = self.runner.commands_for("/usr/bin/codesign")

        self.assertIn(
            [
                "/usr/bin/codesign",
                "--verify",
                "--deep",
                "--strict",
                "--verbose=4",
                result.signed_staging_app_path,
            ],
            codesign,
        )
        entitlement_commands = [
            command for command in codesign if "--entitlements" in command
        ]
        self.assertEqual(len(entitlement_commands), 13)
        self.assertTrue(
            all(
                command[1:4] == ["-d", "--entitlements", ":-"]
                for command in entitlement_commands
            )
        )
        self.assertEqual(self.validator.calls, 3)
        self.assertEqual(
            self.runner.workflow_commands,
            [
                [
                    str(
                        Path(result.signed_staging_app_path)
                        / "Contents"
                        / "MacOS"
                        / "Loci Flow"
                    ),
                    "--packaging-workflow-acceptance",
                ]
            ],
        )
        self.assertEqual(result.native_code_count, 12)
        self.assertEqual(result.hardened_runtime, "passed")
        self.assertEqual(result.secure_timestamp, "passed")
        self.assertEqual(result.strict_verification, "passed")
        self.assertEqual(result.smoke_workflow, "passed")
        self.assertEqual(result.gatekeeper_assessment, "rejected")
        safe_result = format_signed_result(result)
        self.assertNotIn(FINGERPRINT, safe_result)
        self.assertNotIn(TEAM_ID, safe_result)
        self.assertNotIn("certificate", safe_result.casefold())

    def test_team_runtime_timestamp_and_entitlements_fail_closed(self) -> None:
        cases = (
            ("signed_team_id", "OTHERID123", "signature_contract_failed"),
            ("include_runtime", False, "signature_contract_failed"),
            ("include_timestamp", False, "signature_contract_failed"),
            ("entitlements_present", True, "entitlements_present"),
        )
        for attribute, value, expected in cases:
            with self.subTest(attribute=attribute):
                runner = _SigningRunner(self.source, self.staging)
                setattr(runner, attribute, value)
                with self.assertRaises(SigningContractError) as raised:
                    sign_application(
                        source_app=self.source,
                        staging_directory=self.staging,
                        identity_fingerprint=FINGERPRINT,
                        expected_team_id=TEAM_ID,
                        command_runner=runner,
                        bundle_validator=_BundleValidator(),
                        release_staging_root=self.staging_root,
                    )
                self.assertEqual(raised.exception.code, expected)
                self.assertFalse((self.staging / "Loci Flow.app").exists())

    def test_ad_hoc_post_sign_state_cannot_satisfy_acceptance(self) -> None:
        self.runner.post_sign_ad_hoc = True

        with self.assertRaises(SigningContractError) as raised:
            self.sign()

        self.assertEqual(
            raised.exception.code,
            "signature_contract_failed",
        )

    def test_source_digest_change_during_signing_fails_and_cleans_staging(
        self,
    ) -> None:
        self.runner.mutate_source_during_signing = True

        with self.assertRaises(SigningContractError) as raised:
            self.sign()

        self.assertEqual(raised.exception.code, "source_changed")
        self.assertFalse((self.staging / "Loci Flow.app").exists())


class MacOSDeveloperIDAcceptanceTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("LOCI_FLOW_DEVELOPER_ID_FINGERPRINT")
        and os.environ.get("LOCI_FLOW_DEVELOPER_TEAM_ID")
        and (PACKAGING_ROOT / "dist" / "Loci Flow.app").is_dir(),
        "a valid Developer ID identity and fresh P2-B3 app are required",
    )
    def test_real_same_team_identity_with_no_entitlements(self) -> None:
        result = sign_application(
            source_app=PACKAGING_ROOT / "dist" / "Loci Flow.app",
            staging_directory=(
                PACKAGING_ROOT / "release-staging" / "developer-id-acceptance"
            ),
            identity_fingerprint=os.environ["LOCI_FLOW_DEVELOPER_ID_FINGERPRINT"],
            expected_team_id=os.environ["LOCI_FLOW_DEVELOPER_TEAM_ID"],
        )

        self.assertEqual(result.hardened_runtime, "passed")
        self.assertEqual(result.secure_timestamp, "passed")
        self.assertEqual(result.smoke_workflow, "passed")


class _BundleValidator:
    def __init__(self) -> None:
        records = tuple(
            [
                MachORecord(
                    f"Contents/Frameworks/native-{index:02d}.so",
                    ("arm64",),
                )
                for index in range(10)
            ]
            + [
                MachORecord(
                    "Contents/Frameworks/libpython3.13.dylib",
                    ("arm64",),
                ),
                MachORecord("Contents/MacOS/Loci Flow", ("arm64",)),
            ]
        )
        self.result = BundleValidationResult(
            report=BundleValidationReport(
                application_name="Loci Flow",
                bundle_identifier="com.yanqinwang.lociflow",
                version="0.1.0-beta.1",
                build_number=1,
                minimum_macos="13.0",
                architecture_claim="arm64",
                macho_count=12,
                architecture_sets=("arm64",),
                required_resources="passed",
                smoke_test="passed",
            ),
            macho_files=records,
        )
        self.calls = 0

    def __call__(self, _app, **_kwargs):
        self.calls += 1
        return self.result


class _SigningRunner:
    def __init__(self, source: Path, staging: Path) -> None:
        self.source = source
        self.staging = staging
        self.commands: list[list[str]] = []
        self.identity_output = (
            f'  1) {FINGERPRINT} "{DISPLAY_NAME}"\n     1 valid identities found\n'
        )
        self.identity_returncode = 0
        self.openssl_output = _certificate_details(TEAM_ID)
        self.openssl_returncode = 0
        self.fail_signing_number: int | None = None
        self.signed_team_id = TEAM_ID
        self.include_runtime = True
        self.include_timestamp = True
        self.entitlements_present = False
        self.post_sign_ad_hoc = False
        self.mutate_source_during_signing = False
        self.raise_after_ditto_copy = False
        self.signing_commands: list[list[str]] = []
        self.workflow_commands: list[list[str]] = []

    def commands_for(self, executable: str) -> list[list[str]]:
        return [command for command in self.commands if command[0] == executable]

    def __call__(self, command, **_kwargs):
        command = list(command)
        self.commands.append(command)
        if command[:4] == [
            "/usr/bin/security",
            "find-identity",
            "-p",
            "codesigning",
        ]:
            return _completed(
                command,
                self.identity_returncode,
                self.identity_output,
            )
        if command[:2] == ["/usr/bin/security", "find-certificate"]:
            return _completed(
                command,
                0,
                (
                    f"SHA-1 hash: {FINGERPRINT}\n"
                    "-----BEGIN CERTIFICATE-----\n"
                    "TEST\n"
                    "-----END CERTIFICATE-----\n"
                ),
            )
        if command[:2] == ["/usr/bin/openssl", "x509"]:
            return _completed(
                command,
                self.openssl_returncode,
                self.openssl_output,
            )
        if command[0] == "/usr/bin/ditto":
            shutil.copytree(command[1], command[2], symlinks=True)
            if self.raise_after_ditto_copy:
                raise OSError("private copy failure")
            return _completed(command)
        if command[0] == "/usr/bin/codesign":
            return self._codesign(command)
        if len(command) == 2 and command[1] == "--packaging-workflow-acceptance":
            self.workflow_commands.append(command)
            return _completed(command)
        if command[0] == "/usr/sbin/spctl":
            return _completed(command, 3, stderr="not notarized")
        raise AssertionError(command)

    def _codesign(self, command: list[str]):
        if "--sign" in command:
            self.signing_commands.append(command)
            if self.mutate_source_during_signing and len(self.signing_commands) == 1:
                (self.source / "Contents" / "Resources" / "mutable").write_text(
                    "changed", encoding="utf-8"
                )
            if self.fail_signing_number == len(self.signing_commands):
                return _completed(command, 1, stderr="private signing error")
            return _completed(command)
        if command[1:3] == ["-dv", "--verbose=4"]:
            return _completed(
                command,
                stderr=(
                    "Signature=adhoc\n"
                    "TeamIdentifier=not set\n"
                    "Format=Mach-O thin (arm64)\n"
                ),
            )
        if "--verify" in command:
            return _completed(command)
        if "--entitlements" in command:
            return _completed(
                command,
                stderr=(
                    "<plist><dict>"
                    "<key>com.apple.security.get-task-allow</key>"
                    "<true/></dict></plist>"
                    if self.entitlements_present
                    else "Executable=staged"
                ),
            )
        if command[1:3] == ["-d", "--verbose=4"]:
            if self.post_sign_ad_hoc:
                return _completed(
                    command,
                    stderr="Signature=adhoc\nTeamIdentifier=not set\n",
                )
            fields = [
                "Authority=Developer ID Application: Example",
                f"TeamIdentifier={self.signed_team_id}",
                (
                    "CodeDirectory flags=0x10000(runtime)"
                    if self.include_runtime
                    else "CodeDirectory flags=0x0"
                ),
            ]
            if self.include_timestamp:
                fields.append("Timestamp=Jul 27, 2026")
            return _completed(command, stderr="\n".join(fields))
        raise AssertionError(command)


def _completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _certificate_details(
    team_id: str,
    *,
    not_before: str = "Jan  1 00:00:00 2020 GMT",
    not_after: str = "Jan  1 00:00:00 2099 GMT",
) -> str:
    return (
        f"subject=CN = Example, OU = {team_id}, O = Example\n"
        f"notBefore={not_before}\n"
        f"notAfter={not_after}\n"
    )


def _create_source_app(parent: Path) -> Path:
    app = parent / "Loci Flow.app"
    frameworks = app / "Contents" / "Frameworks"
    executable = app / "Contents" / "MacOS" / "Loci Flow"
    resources = app / "Contents" / "Resources"
    frameworks.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    for index in range(10):
        (frameworks / f"native-{index:02d}.so").write_bytes(
            f"native-{index}".encode("ascii")
        )
    (frameworks / "libpython3.13.dylib").write_bytes(b"python")
    executable.write_bytes(b"executable")
    executable.chmod(0o755)
    (resources / "resource").write_text("resource", encoding="utf-8")
    return app


def _tree_snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


if __name__ == "__main__":
    unittest.main()
