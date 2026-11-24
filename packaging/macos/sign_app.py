from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Final

from release_metadata import (
    APP_NAME,
    BUILD_NUMBER,
    BUNDLE_IDENTIFIER,
    SEMANTIC_VERSION,
    SUPPORTED_ARCHITECTURE,
)
from validate_app_bundle import (
    BundleValidationError,
    BundleValidationResult,
    validate_app_bundle,
)


PACKAGING_ROOT: Final = Path(__file__).resolve().parent
RELEASE_STAGING_ROOT: Final = PACKAGING_ROOT / "release-staging"
EXPECTED_NATIVE_CODE_COUNT: Final = 12

_CODESIGN: Final = "/usr/bin/codesign"
_SECURITY: Final = "/usr/bin/security"
_DITTO: Final = "/usr/bin/ditto"
_OPENSSL: Final = "/usr/bin/openssl"
_SPCTL: Final = "/usr/sbin/spctl"
_SAFE_FAILURE: Final = "Loci Flow signing could not be completed."
_FINGERPRINT = re.compile(r"[0-9A-Fa-f]{40}")
_TEAM_ID = re.compile(r"[A-Z0-9]{10}")
_IDENTITY_LINE = re.compile(
    r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"]+)"\s*$',
    re.MULTILINE,
)
_DEVELOPER_ID_NAME = re.compile(
    r"^Developer ID Application: .+ \(([A-Z0-9]{10})\)$"
)
_CERTIFICATE_BLOCK = re.compile(
    r"SHA-1 hash:\s*([0-9A-Fa-f]{40}).*?"
    r"(-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----)",
    re.DOTALL,
)
_SUBJECT_TEAM_ID = re.compile(
    r"(?:^|[,/]\s*)OU\s*=\s*([A-Z0-9]{10})(?:[,/]|$)"
)
_CERTIFICATE_DATE = re.compile(
    r"^(notBefore|notAfter)=(.+ GMT)$",
    re.MULTILINE,
)


class SigningContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(_SAFE_FAILURE)
        self.code = code
        self.safe_message = _SAFE_FAILURE


@dataclass(frozen=True)
class SigningIdentity:
    display_name: str
    team_id: str


@dataclass(frozen=True)
class SignedApplicationResult:
    signed_staging_app_path: str
    application_name: str
    bundle_identifier: str
    version: str
    build_number: int
    architecture: str
    native_code_count: int
    hardened_runtime: str
    secure_timestamp: str
    strict_verification: str
    smoke_workflow: str
    gatekeeper_assessment: str

    def as_safe_dict(self) -> dict[str, object]:
        return asdict(self)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
BundleValidator = Callable[..., BundleValidationResult]


def sign_application(
    *,
    source_app: Path,
    staging_directory: Path,
    identity_fingerprint: str,
    expected_team_id: str,
    command_runner: CommandRunner = subprocess.run,
    bundle_validator: BundleValidator = validate_app_bundle,
    release_staging_root: Path = RELEASE_STAGING_ROOT,
) -> SignedApplicationResult:
    fingerprint = _validate_fingerprint(identity_fingerprint)
    team_id = _validate_team_id(expected_team_id)
    source, destination = _resolve_staging_paths(
        source_app,
        staging_directory,
        release_staging_root,
    )
    _validate_identity(
        fingerprint,
        team_id,
        command_runner=command_runner,
    )
    source_validation = _validate_bundle(
        source,
        bundle_validator=bundle_validator,
        command_runner=command_runner,
    )
    _require_expected_native_closure(source_validation)
    _require_unsigned_input(
        source,
        source_validation,
        command_runner=command_runner,
    )
    source_digest = _bundle_digest(source)

    _prepare_owned_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        copied = _run_command(
            command_runner,
            [_DITTO, str(source), str(destination)],
            code="staging_copy_failed",
            timeout=120,
        )
        if copied.returncode != 0 or not destination.is_dir():
            raise SigningContractError("staging_copy_failed")

        staged_validation = _validate_bundle(
            destination,
            bundle_validator=bundle_validator,
            command_runner=command_runner,
        )
        _require_expected_native_closure(staged_validation)
        if _bundle_digest(destination) != source_digest:
            raise SigningContractError("staging_copy_mismatch")

        executable_relative = f"Contents/MacOS/{APP_NAME}"
        nested_records = tuple(
            record
            for record in staged_validation.macho_files
            if record.relative_path != executable_relative
        )
        if len(nested_records) != EXPECTED_NATIVE_CODE_COUNT - 1:
            raise SigningContractError("native_closure_mismatch")
        for record in nested_records:
            _sign_code(
                destination / record.relative_path,
                fingerprint,
                command_runner=command_runner,
            )
        _sign_code(
            destination,
            fingerprint,
            command_runner=command_runner,
        )

        _verify_signed_closure(
            destination,
            staged_validation,
            team_id,
            command_runner=command_runner,
        )
        signed_validation = _validate_bundle(
            destination,
            bundle_validator=bundle_validator,
            command_runner=command_runner,
        )
        _require_expected_native_closure(signed_validation)
        workflow = _run_command(
            command_runner,
            [
                str(destination / executable_relative),
                "--packaging-workflow-acceptance",
            ],
            code="workflow_failed",
            timeout=90,
        )
        if workflow.returncode != 0:
            raise SigningContractError("workflow_failed")
        if _bundle_digest(source) != source_digest:
            raise SigningContractError("source_changed")

        assessment = _run_command(
            command_runner,
            [
                _SPCTL,
                "--assess",
                "--type",
                "execute",
                "--verbose=4",
                str(destination),
            ],
            code="gatekeeper_tool_failed",
            timeout=30,
        )
        return SignedApplicationResult(
            signed_staging_app_path=str(destination),
            application_name=APP_NAME,
            bundle_identifier=BUNDLE_IDENTIFIER,
            version=SEMANTIC_VERSION,
            build_number=BUILD_NUMBER,
            architecture=SUPPORTED_ARCHITECTURE,
            native_code_count=len(signed_validation.macho_files),
            hardened_runtime="passed",
            secure_timestamp="passed",
            strict_verification="passed",
            smoke_workflow="passed",
            gatekeeper_assessment=(
                "accepted" if assessment.returncode == 0 else "rejected"
            ),
        )
    except BaseException:
        _remove_owned_destination(destination)
        raise


def format_signed_result(result: SignedApplicationResult) -> str:
    return json.dumps(
        result.as_safe_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_fingerprint(value: str) -> str:
    if _FINGERPRINT.fullmatch(value) is None:
        raise SigningContractError("invalid_identity_fingerprint")
    return value.upper()


def _validate_team_id(value: str) -> str:
    if _TEAM_ID.fullmatch(value) is None:
        raise SigningContractError("invalid_team_id")
    return value


def _resolve_staging_paths(
    source_app: Path,
    staging_directory: Path,
    release_staging_root: Path,
) -> tuple[Path, Path]:
    source_candidate = Path(source_app)
    try:
        if source_candidate.is_symlink():
            raise SigningContractError("invalid_source_app")
        source = source_candidate.resolve(strict=True)
        owned_root = Path(release_staging_root).resolve(strict=False)
        staging = Path(staging_directory).resolve(strict=False)
        staging.relative_to(owned_root)
    except SigningContractError:
        raise
    except (OSError, ValueError) as error:
        raise SigningContractError("invalid_staging_path") from error
    if source.suffix != ".app" or not source.is_dir():
        raise SigningContractError("invalid_source_app")
    destination = staging / f"{APP_NAME}.app"
    try:
        destination.relative_to(owned_root)
    except ValueError as error:
        raise SigningContractError("staging_escape") from error
    if destination == source:
        raise SigningContractError("source_destination_equivalent")
    try:
        if destination.exists() and os.path.samefile(source, destination):
            raise SigningContractError("source_destination_equivalent")
    except OSError as error:
        raise SigningContractError("invalid_staging_path") from error
    return source, destination


def _validate_identity(
    fingerprint: str,
    expected_team_id: str,
    *,
    command_runner: CommandRunner,
) -> SigningIdentity:
    identities = _run_command(
        command_runner,
        [_SECURITY, "find-identity", "-p", "codesigning", "-v"],
        code="identity_inspection_failed",
    )
    if identities.returncode != 0:
        raise SigningContractError("identity_inspection_failed")
    matches = [
        (found.upper(), display_name)
        for found, display_name in _IDENTITY_LINE.findall(identities.stdout)
        if found.upper() == fingerprint
    ]
    if not matches:
        raise SigningContractError("identity_missing")
    if len(matches) != 1:
        raise SigningContractError("identity_ambiguous")
    display_name = matches[0][1]
    name_match = _DEVELOPER_ID_NAME.fullmatch(display_name)
    if name_match is None:
        raise SigningContractError("identity_wrong_type")
    if name_match.group(1) != expected_team_id:
        raise SigningContractError("identity_wrong_team")

    certificates = _run_command(
        command_runner,
        [
            _SECURITY,
            "find-certificate",
            "-a",
            "-Z",
            "-p",
            "-c",
            display_name,
        ],
        code="certificate_inspection_failed",
    )
    if certificates.returncode != 0:
        raise SigningContractError("certificate_inspection_failed")
    certificate_matches = [
        pem
        for found, pem in _CERTIFICATE_BLOCK.findall(certificates.stdout)
        if found.upper() == fingerprint
    ]
    if len(certificate_matches) != 1:
        raise SigningContractError(
            "certificate_missing"
            if not certificate_matches
            else "certificate_ambiguous"
        )
    validity = _run_command(
        command_runner,
        [
            _OPENSSL,
            "x509",
            "-checkend",
            "0",
            "-noout",
            "-subject",
            "-dates",
        ],
        code="certificate_inspection_failed",
        input_text=certificate_matches[0] + "\n",
    )
    if validity.returncode != 0:
        raise SigningContractError("certificate_expired")
    subject_match = _SUBJECT_TEAM_ID.search(validity.stdout)
    if subject_match is None or subject_match.group(1) != expected_team_id:
        raise SigningContractError("certificate_wrong_team")
    dates = {
        name: _parse_certificate_date(value)
        for name, value in _CERTIFICATE_DATE.findall(validity.stdout)
    }
    if set(dates) != {"notBefore", "notAfter"}:
        raise SigningContractError("certificate_invalid_dates")
    now = datetime.now(UTC)
    if dates["notBefore"] > now:
        raise SigningContractError("certificate_not_yet_valid")
    if dates["notAfter"] <= now:
        raise SigningContractError("certificate_expired")
    return SigningIdentity(display_name=display_name, team_id=expected_team_id)


def _parse_certificate_date(value: str) -> datetime:
    try:
        return datetime.strptime(
            value,
            "%b %d %H:%M:%S %Y GMT",
        ).replace(tzinfo=UTC)
    except ValueError as error:
        raise SigningContractError("certificate_invalid_dates") from error


def _validate_bundle(
    app_path: Path,
    *,
    bundle_validator: BundleValidator,
    command_runner: CommandRunner,
) -> BundleValidationResult:
    try:
        return bundle_validator(app_path, command_runner=command_runner)
    except BundleValidationError as error:
        raise SigningContractError("bundle_validation_failed") from error


def _require_expected_native_closure(
    result: BundleValidationResult,
) -> None:
    executable = f"Contents/MacOS/{APP_NAME}"
    if (
        len(result.macho_files) != EXPECTED_NATIVE_CODE_COUNT
        or executable not in {
            record.relative_path for record in result.macho_files
        }
        or any(
            record.architectures != (SUPPORTED_ARCHITECTURE,)
            for record in result.macho_files
        )
    ):
        raise SigningContractError("native_closure_mismatch")


def _require_unsigned_input(
    source: Path,
    validation: BundleValidationResult,
    *,
    command_runner: CommandRunner,
) -> None:
    inspected = [
        source / record.relative_path
        for record in validation.macho_files
    ]
    inspected.append(source)
    for item in inspected:
        signature = _run_command(
            command_runner,
            [_CODESIGN, "-dv", "--verbose=4", str(item)],
            code="unsigned_signature_inspection_failed",
        )
        details = f"{signature.stdout}\n{signature.stderr}"
        if signature.returncode != 0:
            if "code object is not signed at all" in details:
                continue
            raise SigningContractError("unsigned_signature_inspection_failed")
        if (
            "Signature=adhoc" not in details
            or "Authority=" in details
            or "TeamIdentifier=not set" not in details
        ):
            raise SigningContractError("input_already_release_signed")


def _prepare_owned_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise SigningContractError("unsafe_prior_staging")
    if destination.exists():
        if not destination.is_dir():
            raise SigningContractError("unsafe_prior_staging")
        _remove_owned_destination(destination)


def _remove_owned_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise SigningContractError("unsafe_prior_staging")
    if destination.exists():
        if not destination.is_dir():
            raise SigningContractError("unsafe_prior_staging")
        shutil.rmtree(destination)


def _sign_code(
    item: Path,
    fingerprint: str,
    *,
    command_runner: CommandRunner,
) -> None:
    signed = _run_command(
        command_runner,
        [
            _CODESIGN,
            "--force",
            "--sign",
            fingerprint,
            "--options",
            "runtime",
            "--timestamp",
            str(item),
        ],
        code="signing_failed",
        timeout=120,
    )
    if signed.returncode != 0:
        raise SigningContractError("signing_failed")


def _verify_signed_closure(
    app: Path,
    validation: BundleValidationResult,
    expected_team_id: str,
    *,
    command_runner: CommandRunner,
) -> None:
    strict = _run_command(
        command_runner,
        [
            _CODESIGN,
            "--verify",
            "--deep",
            "--strict",
            "--verbose=4",
            str(app),
        ],
        code="strict_verification_failed",
    )
    if strict.returncode != 0:
        raise SigningContractError("strict_verification_failed")

    inspected = [app / record.relative_path for record in validation.macho_files]
    inspected.append(app)
    for item in inspected:
        verified = _run_command(
            command_runner,
            [_CODESIGN, "--verify", "--strict", "--verbose=4", str(item)],
            code="item_verification_failed",
        )
        if verified.returncode != 0:
            raise SigningContractError("item_verification_failed")
        details = _run_command(
            command_runner,
            [_CODESIGN, "-d", "--verbose=4", str(item)],
            code="signature_detail_failed",
        )
        if details.returncode != 0:
            raise SigningContractError("signature_detail_failed")
        output = f"{details.stdout}\n{details.stderr}"
        if (
            "Authority=Developer ID Application:" not in output
            or f"TeamIdentifier={expected_team_id}" not in output
            or "runtime" not in output
            or "Timestamp=" not in output
        ):
            raise SigningContractError("signature_contract_failed")
        entitlements = _run_command(
            command_runner,
            [_CODESIGN, "-d", "--entitlements", ":-", str(item)],
            code="entitlement_inspection_failed",
        )
        if entitlements.returncode != 0:
            raise SigningContractError("entitlement_inspection_failed")
        entitlement_output = (
            f"{entitlements.stdout}\n{entitlements.stderr}"
        )
        if (
            "<plist" in entitlement_output
            or "com.apple.security." in entitlement_output
        ):
            raise SigningContractError("entitlements_present")


def _bundle_digest(bundle: Path) -> str:
    digest = hashlib.sha256()
    root = bundle.resolve(strict=True)
    entries = [root]
    for current, directories, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directories.sort()
        filenames.sort()
        current_path = Path(current)
        entries.extend(current_path / name for name in directories)
        entries.extend(current_path / name for name in filenames)
    for entry in sorted(entries, key=lambda item: item.relative_to(root).as_posix()):
        relative = entry.relative_to(root).as_posix().encode("utf-8")
        details = entry.lstat()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(stat.S_IMODE(details.st_mode).to_bytes(4, "big"))
        if stat.S_ISLNK(details.st_mode):
            digest.update(b"L")
            digest.update(os.readlink(entry).encode("utf-8"))
        elif stat.S_ISDIR(details.st_mode):
            digest.update(b"D")
        elif stat.S_ISREG(details.st_mode):
            digest.update(b"F")
            with entry.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        else:
            raise SigningContractError("unsupported_bundle_entry")
    return digest.hexdigest()


def _run_command(
    command_runner: CommandRunner,
    command: Sequence[str],
    *,
    code: str,
    timeout: int = 30,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return command_runner(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SigningContractError(code) from error


def _parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sign a validated Loci Flow staging application."
    )
    parser.add_argument("--source-app", required=True, type=Path)
    parser.add_argument("--staging-directory", required=True, type=Path)
    parser.add_argument("--identity-fingerprint", required=True)
    parser.add_argument("--expected-team-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(
        tuple(sys.argv[1:] if argv is None else argv)
    )
    try:
        result = sign_application(
            source_app=arguments.source_app,
            staging_directory=arguments.staging_directory,
            identity_fingerprint=arguments.identity_fingerprint,
            expected_team_id=arguments.expected_team_id,
        )
    except SigningContractError as error:
        print(error.safe_message, file=sys.stderr)
        return 1
    print(format_signed_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
