from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import subprocess
import tempfile
import tomllib
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPOSITORY_ROOT / "packaging" / "macos" / "loci_flow.spec"
BUILD_SCRIPT = (
    REPOSITORY_ROOT / "packaging" / "macos" / "build_unsigned_app.py"
)
METADATA = REPOSITORY_ROOT / "packaging" / "macos" / "release_metadata.py"


def _release_metadata():
    specification = importlib.util.spec_from_file_location(
        "loci_flow_release_metadata_test",
        METADATA,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class MacOSPackagingContractTests(unittest.TestCase):
    def test_build_dependency_is_optional_and_exact(self) -> None:
        metadata = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = metadata["project"]

        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            project["optional-dependencies"]["macos-build"],
            ["pillow==12.3.0", "pyinstaller==6.16.0"],
        )
        self.assertNotIn(
            "pyinstaller",
            "\n".join(project["optional-dependencies"]["web"]).lower(),
        )
        self.assertNotIn(
            "pyinstaller",
            "\n".join(
                project["optional-dependencies"]["macos-launcher"]
            ).lower(),
        )

    def test_spec_is_onedir_windowed_native_and_unsigned(self) -> None:
        source = SPEC.read_text(encoding="utf-8")

        self.assertIn("COLLECT(", source)
        self.assertIn("BUNDLE(", source)
        self.assertIn('name=f"{APP_NAME}.app"', source)
        self.assertIn("console=False", source)
        self.assertIn("exclude_binaries=True", source)
        self.assertIn("upx=False", source)
        self.assertIn("target_arch=SUPPORTED_ARCHITECTURE", source)
        self.assertIn("codesign_identity=None", source)
        self.assertNotIn("--onefile", source)

    def test_release_metadata_is_centralized_valid_and_exact(self) -> None:
        metadata = _release_metadata()
        self.assertEqual(metadata.APP_NAME, "Loci Flow")
        self.assertEqual(
            metadata.BUNDLE_IDENTIFIER,
            "com.yanqinwang.lociflow",
        )
        self.assertEqual(metadata.SEMANTIC_VERSION, "0.1.0-beta.1")
        self.assertEqual(metadata.BUILD_NUMBER, 1)
        self.assertIsInstance(metadata.BUILD_NUMBER, int)
        self.assertEqual(metadata.MINIMUM_MACOS_VERSION, "13.0")
        self.assertEqual(metadata.SUPPORTED_ARCHITECTURE, "arm64")
        metadata.validate_release_metadata()

    def test_bundle_spec_consumes_centralized_metadata(self) -> None:
        source = SPEC.read_text(encoding="utf-8")
        for expected in (
            "bundle_identifier=BUNDLE_IDENTIFIER",
            '"CFBundleName": APP_NAME',
            '"CFBundleDisplayName": APP_NAME',
            '"CFBundleShortVersionString": SEMANTIC_VERSION',
            '"CFBundleVersion": str(BUILD_NUMBER)',
            '"LSMinimumSystemVersion": MINIMUM_MACOS_VERSION',
            '"NSHighResolutionCapable": True',
        ):
            self.assertIn(expected, source)
        for duplicated in (
            "com.yanqinwang.lociflow",
            "0.1.0-beta.1",
            '"13.0"',
        ):
            self.assertNotIn(duplicated, source)

    def test_runtime_identity_is_generated_from_centralized_metadata(self) -> None:
        metadata = _release_metadata()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "release_identity.json"

            metadata.write_runtime_identity(destination)

            self.assertEqual(
                json.loads(destination.read_text(encoding="ascii")),
                {
                    "application_version": metadata.SEMANTIC_VERSION,
                    "build_number": metadata.BUILD_NUMBER,
                },
            )
        build_source = BUILD_SCRIPT.read_text(encoding="utf-8")
        spec_source = SPEC.read_text(encoding="utf-8")
        self.assertIn("write_runtime_identity", build_source)
        self.assertIn("RUNTIME_IDENTITY_PATH", build_source)
        self.assertIn("RUNTIME_IDENTITY_PATH", spec_source)
        self.assertIn('"organizer/macos_launcher"', spec_source)

    def test_spec_uses_generated_lf_icon_not_pyinstaller_default(self) -> None:
        source = SPEC.read_text(encoding="utf-8")
        self.assertIn('"build" / "LociFlow.icns"', source)
        self.assertIn("icon=str(ICON_PATH)", source)
        self.assertIn('"CFBundleIconFile": ICON_PATH.name', source)
        self.assertNotIn("icon=None", source)
        self.assertNotIn("icon-windowed.icns", source)

    def test_spec_collects_production_closure_and_required_resources(
        self,
    ) -> None:
        source = SPEC.read_text(encoding="utf-8")
        self.assertIn('collect_submodules("organizer")', source)
        for hidden_import in ("AppKit", "Foundation", "objc", "PyObjCTools"):
            self.assertIn(f'"{hidden_import}"', source)
        for resource in (
            '"templates"',
            '"static" / "css"',
            '"static" / "js"',
            '"static" / "loci_flow"',
            '"static" / "vendor"',
            '"static" / "THIRD_PARTY_NOTICES.txt"',
            '"organizer/web/static/loci_flow"',
        ):
            self.assertIn(resource, source)

    def test_spec_excludes_nonproduction_trees(self) -> None:
        source = SPEC.read_text(encoding="utf-8")
        self.assertIn(
            'excludes=["tests", "prototype", "research"]',
            source,
        )
        for forbidden in (
            "AI_Review",
            "test_scan",
            ".env",
            ".git",
            "screenshots",
        ):
            self.assertNotIn(forbidden, source)

    def test_build_script_owns_only_known_ignored_outputs(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('PACKAGING_ROOT / "build"', source)
        self.assertIn('PACKAGING_ROOT / "dist"', source)
        self.assertIn('"PyInstaller"', source)
        self.assertIn("generate_icon()", source)
        self.assertLess(
            source.index("generate_icon()"),
            source.index('"PyInstaller"', source.index("generate_icon()")),
        )
        self.assertNotIn('"--clean"', source)
        self.assertNotIn("git clean", source)
        self.assertNotIn("codesign", source)
        self.assertNotIn("notary", source)
        self.assertNotIn("hdiutil", source)

        for relative in (
            "packaging/macos/build/example",
            "packaging/macos/build/LociFlow.iconset/icon_16x16.png",
            "packaging/macos/build/LociFlow.icns",
            "packaging/macos/dist/Loci Flow.app",
        ):
            checked = subprocess.run(
                ["git", "check-ignore", "-q", relative],
                cwd=REPOSITORY_ROOT,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, relative)

    def test_build_outputs_are_not_tracked(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertFalse(
            any(
                path.startswith("packaging/macos/build/")
                or path.startswith("packaging/macos/dist/")
                for path in tracked
            )
        )
        source_icon = "packaging/macos/icon/loci-flow-app-icon-1024.png"
        self.assertTrue((REPOSITORY_ROOT / source_icon).is_file())
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", source_icon],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        self.assertNotEqual(ignored.returncode, 0)


if __name__ == "__main__":
    unittest.main()
