from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGING_ROOT = (
    Path(__file__).resolve().parents[1] / "packaging" / "macos"
)
sys.path.insert(0, str(PACKAGING_ROOT))

from validate_app_bundle import (  # noqa: E402
    BundleValidationError,
    format_validation_report,
    validate_app_bundle,
)


class MacOSBundleArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.app = _create_bundle(Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_exact_bundle_metadata_resources_and_arm64_closure_pass(self) -> None:
        result = validate_app_bundle(
            self.app,
            command_runner=_bundle_runner,
            host_architecture="arm64",
        )

        self.assertEqual(result.report.application_name, "Loci Flow")
        self.assertEqual(
            result.report.bundle_identifier,
            "com.yanqinwang.lociflow",
        )
        self.assertEqual(result.report.version, "0.1.0-beta.1")
        self.assertEqual(result.report.build_number, 1)
        self.assertEqual(result.report.minimum_macos, "13.0")
        self.assertEqual(result.report.architecture_claim, "arm64")
        self.assertEqual(result.report.macho_count, 3)
        self.assertEqual(result.report.architecture_sets, ("arm64",))
        self.assertEqual(result.report.required_resources, "passed")
        self.assertEqual(result.report.smoke_test, "passed")

        report = format_validation_report(result)
        self.assertNotIn(str(self.app), report)
        self.assertNotIn(str(Path.home()), report)
        self.assertEqual(json.loads(report)["architecture_claim"], "arm64")

    def test_every_native_file_is_discovered_in_deterministic_order(self) -> None:
        result = validate_app_bundle(
            self.app,
            command_runner=_bundle_runner,
            host_architecture="arm64",
            run_smoke=False,
        )

        self.assertEqual(
            [record.relative_path for record in result.macho_files],
            [
                "Contents/Frameworks/a-native.so",
                "Contents/Frameworks/z-native.dylib",
                "Contents/MacOS/Loci Flow",
            ],
        )
        self.assertTrue(
            all(record.architectures == ("arm64",) for record in result.macho_files)
        )

    def test_x86_only_and_universal_native_files_are_rejected(self) -> None:
        native = self.app / "Contents" / "Frameworks" / "a-native.so"
        for slices in ("x86_64", "arm64 x86_64"):
            with self.subTest(slices=slices):
                native.write_text(f"MACHO:{slices}", encoding="ascii")
                with self.assertRaises(BundleValidationError) as raised:
                    validate_app_bundle(
                        self.app,
                        command_runner=_bundle_runner,
                        host_architecture="arm64",
                        run_smoke=False,
                    )
                self.assertEqual(raised.exception.code, "architecture_mismatch")

    def test_non_arm64_host_and_non_native_primary_are_rejected(self) -> None:
        with self.assertRaises(BundleValidationError) as wrong_host:
            validate_app_bundle(
                self.app,
                command_runner=_bundle_runner,
                host_architecture="x86_64",
                run_smoke=False,
            )
        self.assertEqual(
            wrong_host.exception.code,
            "wrong_host_architecture",
        )

        executable = self.app / "Contents" / "MacOS" / "Loci Flow"
        executable.write_text("not native", encoding="ascii")
        with self.assertRaises(BundleValidationError) as nonnative:
            validate_app_bundle(
                self.app,
                command_runner=_bundle_runner,
                host_architecture="arm64",
                run_smoke=False,
            )
        self.assertEqual(
            nonnative.exception.code,
            "indeterminate_native_file",
        )

    def test_indeterminate_native_file_fails_closed(self) -> None:
        def unavailable_file(command, **kwargs):
            if command[0] == "file" and command[-1].endswith("a-native.so"):
                return subprocess.CompletedProcess(command, 1, "", "private path")
            return _bundle_runner(command, **kwargs)

        with self.assertRaises(BundleValidationError) as raised:
            validate_app_bundle(
                self.app,
                command_runner=unavailable_file,
                host_architecture="arm64",
                run_smoke=False,
            )
        self.assertEqual(raised.exception.code, "file_classification_failed")
        self.assertEqual(
            str(raised.exception),
            "The Loci Flow application bundle is unavailable.",
        )
        self.assertNotIn(str(self.app), str(raised.exception))

    def test_malformed_metadata_icon_identity_and_resources_reject(self) -> None:
        cases = {
            "metadata": (
                self.app / "Contents" / "Info.plist",
                b"not a plist",
                "invalid_info_plist",
            ),
            "icon": (
                self.app / "Contents" / "Resources" / "LociFlow.icns",
                None,
                "icon_missing",
            ),
            "identity": (
                self.app
                / "Contents"
                / "Resources"
                / "organizer"
                / "macos_launcher"
                / "release_identity.json",
                b'{"application_version":"9.9.9","build_number":9}',
                "release_identity_mismatch",
            ),
            "resource": (
                self.app
                / "Contents"
                / "Resources"
                / "organizer"
                / "web"
                / "templates"
                / "loci_flow.html",
                b"",
                "resource_missing",
            ),
        }
        for name, (target, replacement, expected) in cases.items():
            with self.subTest(name=name):
                original = target.read_bytes()
                if replacement is None:
                    target.unlink()
                else:
                    target.write_bytes(replacement)
                try:
                    with self.assertRaises(BundleValidationError) as raised:
                        validate_app_bundle(
                            self.app,
                            command_runner=_bundle_runner,
                            host_architecture="arm64",
                            run_smoke=False,
                        )
                    self.assertEqual(raised.exception.code, expected)
                finally:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original)

    def test_unexpected_executable_forbidden_tree_and_outside_symlink_reject(
        self,
    ) -> None:
        extra = self.app / "Contents" / "MacOS" / "helper"
        extra.write_text("MACHO:arm64", encoding="ascii")
        extra.chmod(0o755)
        with self.assertRaises(BundleValidationError) as executable:
            validate_app_bundle(
                self.app,
                command_runner=_bundle_runner,
                host_architecture="arm64",
                run_smoke=False,
            )
        self.assertEqual(
            executable.exception.code,
            "unexpected_launch_executable",
        )
        extra.unlink()

        forbidden = self.app / "Contents" / "Resources" / "tests"
        forbidden.mkdir()
        with self.assertRaises(BundleValidationError) as content:
            validate_app_bundle(
                self.app,
                command_runner=_bundle_runner,
                host_architecture="arm64",
                run_smoke=False,
            )
        self.assertEqual(content.exception.code, "forbidden_content")
        forbidden.rmdir()

        outside = Path(self.directory.name) / "outside"
        outside.write_text("private", encoding="utf-8")
        link = self.app / "Contents" / "Resources" / "outside-link"
        link.symlink_to(outside)
        with self.assertRaises(BundleValidationError) as symlink:
            validate_app_bundle(
                self.app,
                command_runner=_bundle_runner,
                host_architecture="arm64",
                run_smoke=False,
            )
        self.assertEqual(symlink.exception.code, "unsafe_bundle_symlink")

    def test_smoke_failure_is_bounded(self) -> None:
        def smoke_failure(command, **kwargs):
            if command[0] not in {"file", "lipo"}:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    "private repository path",
                    "private traceback",
                )
            return _bundle_runner(command, **kwargs)

        with self.assertRaises(BundleValidationError) as raised:
            validate_app_bundle(
                self.app,
                command_runner=smoke_failure,
                host_architecture="arm64",
            )
        self.assertEqual(raised.exception.code, "packaging_smoke_failed")
        self.assertNotIn("private", str(raised.exception))


def _create_bundle(parent: Path) -> Path:
    app = parent / "Loci Flow.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    frameworks = contents / "Frameworks"
    macos.mkdir(parents=True)
    resources.mkdir()
    frameworks.mkdir()

    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleExecutable": "Loci Flow",
                "CFBundleIconFile": "LociFlow.icns",
                "CFBundleIdentifier": "com.yanqinwang.lociflow",
                "CFBundleName": "Loci Flow",
                "CFBundleShortVersionString": "0.1.0-beta.1",
                "CFBundleVersion": "1",
                "LSMinimumSystemVersion": "13.0",
            },
            stream,
        )
    executable = macos / "Loci Flow"
    executable.write_text("MACHO:arm64", encoding="ascii")
    executable.chmod(0o755)
    (frameworks / "z-native.dylib").write_text(
        "MACHO:arm64",
        encoding="ascii",
    )
    (frameworks / "a-native.so").write_text(
        "MACHO:arm64",
        encoding="ascii",
    )
    (resources / "LociFlow.icns").write_bytes(b"icns")
    required = (
        "organizer/web/templates/loci_flow.html",
        "organizer/web/static/css/bootai.css",
        "organizer/web/static/js/bootai.js",
        "organizer/web/static/loci_flow/app.js",
        "organizer/web/static/loci_flow/assets/loci-flow-mark.svg",
        "organizer/web/static/vendor/bootstrap.min.css",
        "organizer/web/static/THIRD_PARTY_NOTICES.txt",
    )
    for relative in required:
        target = resources / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("resource", encoding="utf-8")
    identity = (
        resources
        / "organizer"
        / "macos_launcher"
        / "release_identity.json"
    )
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text(
        '{"application_version":"0.1.0-beta.1","build_number":1}',
        encoding="ascii",
    )
    return app


def _bundle_runner(command, **_kwargs):
    if command[0] == "file":
        content = Path(command[-1]).read_bytes().decode(
            "ascii",
            errors="ignore",
        )
        description = (
            "Mach-O 64-bit binary"
            if content.startswith("MACHO:")
            else "data"
        )
        return subprocess.CompletedProcess(command, 0, description, "")
    if command[0] == "lipo":
        content = Path(command[-1]).read_text(encoding="ascii")
        return subprocess.CompletedProcess(
            command,
            0,
            content.removeprefix("MACHO:"),
            "",
        )
    return subprocess.CompletedProcess(command, 0, "", "")


if __name__ == "__main__":
    unittest.main()
