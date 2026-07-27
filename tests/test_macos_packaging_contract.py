from __future__ import annotations

from pathlib import Path
import subprocess
import tomllib
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPOSITORY_ROOT / "packaging" / "macos" / "loci_flow.spec"
BUILD_SCRIPT = (
    REPOSITORY_ROOT / "packaging" / "macos" / "build_unsigned_app.py"
)


class MacOSPackagingContractTests(unittest.TestCase):
    def test_build_dependency_is_optional_and_exact(self) -> None:
        metadata = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = metadata["project"]

        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            project["optional-dependencies"]["macos-build"],
            ["pyinstaller==6.16.0"],
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
        self.assertIn('name="Loci Flow.app"', source)
        self.assertIn("console=False", source)
        self.assertIn("exclude_binaries=True", source)
        self.assertIn("upx=False", source)
        self.assertIn("target_arch=None", source)
        self.assertIn("codesign_identity=None", source)
        self.assertNotIn("--onefile", source)

    def test_bundle_metadata_is_exact(self) -> None:
        source = SPEC.read_text(encoding="utf-8")
        for expected in (
            'bundle_identifier="com.yanqinwang.lociflow"',
            '"CFBundleName": "Loci Flow"',
            '"CFBundleDisplayName": "Loci Flow"',
            '"CFBundleShortVersionString": "0.1.0-beta.1"',
            '"CFBundleVersion": "1"',
            '"LSMinimumSystemVersion": "13.0"',
            '"NSHighResolutionCapable": True',
        ):
            self.assertIn(expected, source)

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
        self.assertNotIn('"--clean"', source)
        self.assertNotIn("git clean", source)
        self.assertNotIn("codesign", source)
        self.assertNotIn("notary", source)
        self.assertNotIn("hdiutil", source)

        for relative in (
            "packaging/macos/build/example",
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


if __name__ == "__main__":
    unittest.main()
