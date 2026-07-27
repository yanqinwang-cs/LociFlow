from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPOSITORY_ROOT / "src" / "organizer" / "web"


class WebDependencyHygieneTests(unittest.TestCase):
    def test_optional_dependencies_are_exact_and_core_remains_empty(self) -> None:
        data = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = data["project"]

        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            project["optional-dependencies"]["web"],
            [
                "fastapi==0.139.0",
                "starlette==1.3.1",
                "uvicorn==0.50.2",
                "jinja2==3.1.6",
                "itsdangerous==2.2.0",
            ],
        )
        self.assertEqual(
            project["optional-dependencies"]["web-test"],
            ["httpx==0.28.1"],
        )
        self.assertEqual(
            project["optional-dependencies"]["macos-launcher"],
            [
                "pyobjc-core==12.2.1; sys_platform == 'darwin'",
                "pyobjc-framework-Cocoa==12.2.1; sys_platform == 'darwin'",
            ],
        )
        self.assertEqual(
            project["optional-dependencies"]["macos-build"],
            ["pillow==12.3.0", "pyinstaller==6.16.0"],
        )
        self.assertNotIn(
            "pyobjc",
            "\n".join(project["optional-dependencies"]["web"]).lower(),
        )

        all_requirements = "\n".join(
            requirement.lower()
            for requirements in project["optional-dependencies"].values()
            for requirement in requirements
        )
        for forbidden in (
            "python-multipart",
            "openai",
            "python-dotenv",
            "sqlalchemy",
            "fastapi[",
        ):
            self.assertNotIn(forbidden, all_requirements)

    def test_lockfile_contains_the_exact_web_versions(self) -> None:
        lock = (REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8")
        for name, version in {
            "fastapi": "0.139.0",
            "starlette": "1.3.1",
            "uvicorn": "0.50.2",
            "jinja2": "3.1.6",
            "itsdangerous": "2.2.0",
            "httpx": "0.28.1",
            "pyobjc-core": "12.2.1",
            "pyobjc-framework-cocoa": "12.2.1",
        }.items():
            self.assertIn(f'name = "{name}"\nversion = "{version}"', lock)

    def test_native_dependencies_and_imports_are_isolated(self) -> None:
        data = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        optional = data["project"]["optional-dependencies"]
        self.assertFalse(
            any("pyobjc" in item.lower() for item in optional["web"])
        )

        native_root = REPOSITORY_ROOT / "src" / "organizer" / "macos_launcher"
        restricted_import = re.compile(
            r"^\s*(?:from|import)\s+"
            r"(?:AppKit|Foundation|objc|PyObjCTools)(?:\s|\.|$)",
            re.MULTILINE,
        )
        for path in sorted(native_root.glob("*.py")):
            if path.name == "appkit_adapter.py":
                continue
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(restricted_import.search(source), path)

        permitted_native_import_owners = {"launcher.py", "server.py"}
        for path in sorted(WEB_ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "organizer.macos_launcher" not in source:
                continue
            self.assertIn(path.name, permitted_native_import_owners)
            self.assertNotIn(
                "organizer.macos_launcher.appkit_adapter",
                source,
            )
            self.assertNotIn(
                "organizer.macos_launcher.coordinator",
                source,
            )
            self.assertNotIn(
                "organizer.macos_launcher.shutdown_coordinator",
                source,
            )
            self.assertIsNone(restricted_import.search(source), path)

    def test_package_data_covers_all_web_resources(self) -> None:
        data = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        patterns = set(data["tool"]["setuptools"]["package-data"]["organizer.web"])
        self.assertEqual(
            patterns,
            {
                "templates/*.html",
                "static/THIRD_PARTY_NOTICES.txt",
                "static/css/*.css",
                "static/js/*.js",
                "static/loci_flow/*.css",
                "static/loci_flow/*.js",
                "static/loci_flow/*.json",
                "static/loci_flow/assets/*.svg",
                "static/vendor/*.css",
                "static/vendor/*.js",
            },
        )

    def test_vendor_assets_match_published_sha384_values(self) -> None:
        expected = {
            "htmx.min.js": (
                "H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/"
                "CeCpFReSfwBWDTKpkzPP8c+cLsK+V"
            ),
            "bootstrap.min.css": (
                "sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWL"
                "zeN8tE5YBujZqJLB"
            ),
            "bootstrap.bundle.min.js": (
                "FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/"
                "BmnVDxM+D2scQbITxI"
            ),
        }
        vendor = WEB_ROOT / "static" / "vendor"
        for filename, digest in expected.items():
            with self.subTest(filename=filename):
                actual = base64.b64encode(
                    hashlib.sha384((vendor / filename).read_bytes()).digest()
                ).decode("ascii")
                self.assertEqual(actual, digest)
        self.assertFalse(any(WEB_ROOT.rglob("*.map")))

    def test_runtime_pages_have_no_external_asset_or_inline_script_reference(self) -> None:
        authored_assets = [
            *sorted((WEB_ROOT / "templates").glob("*.html")),
            WEB_ROOT / "static" / "css" / "bootai.css",
            WEB_ROOT / "static" / "js" / "bootai.js",
            WEB_ROOT / "static" / "loci_flow" / "styles.css",
            *sorted((WEB_ROOT / "static" / "loci_flow").glob("*.js")),
        ]
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in authored_assets
        ).lower()
        for forbidden in (
            "http://",
            "https://",
            "cdn",
            "@import",
            "<script>",
            "onclick=",
            "onload=",
            "analytics",
        ):
            self.assertNotIn(forbidden, combined)

    def test_web_routes_use_application_services_not_domain_owners(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((WEB_ROOT / "routes").rglob("*.py"))
        ).lower()
        all_web_code = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(WEB_ROOT.rglob("*.py"))
        ).lower()
        for forbidden in (
            "organizer.executor",
            "organizer.scanner",
            "organizer.duplicates",
            "organizer.planner",
            "from organizer.review import",
            "from organizer.review_session import",
            "from organizer.review_state import",
            "import organizer.review",
            "import organizer.review_session",
            "import organizer.review_state",
            "organizer.grouping",
            "organizer.reports",
        ):
            self.assertNotIn(forbidden, combined)
        for forbidden in ("organizer.executor", "sqlite"):
            self.assertNotIn(forbidden, all_web_code)
        self.assertIn("organizer.application.scan_service", all_web_code)
        self.assertIn("organizer.application.review_service", combined)
        self.assertNotIn(
            "from organizer.application.review_service import create_review_session\n",
            combined,
        )
        self.assertIn(
            "from organizer.web.root_admission import admit_root",
            all_web_code,
        )

    def test_core_and_web_config_import_without_optional_dependencies(self) -> None:
        command = [
            sys.executable,
            "-S",
            "-c",
            "import organizer; import organizer.web; from organizer.web import WebAppConfig",
        ]
        environment = {"PYTHONPATH": str(REPOSITORY_ROOT / "src")}
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_web_imports_never_require_pyobjc(self) -> None:
        command = [
            sys.executable,
            "-c",
            """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split('.', 1)[0] in {'AppKit', 'Foundation', 'objc', 'PyObjCTools'}:
        raise AssertionError(f'unexpected native import: {name}')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import organizer
import organizer.web
import organizer.web.launcher
import organizer.web.server
""",
        ]
        environment = {"PYTHONPATH": str(REPOSITORY_ROOT / "src")}
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_only_organization_boundary_has_no_web_executor_or_rescan_dependency(self) -> None:
        service = (
            REPOSITORY_ROOT
            / "src"
            / "organizer"
            / "application"
            / "organization_slice_service.py"
        ).read_text(encoding="utf-8")
        route = (
            WEB_ROOT / "routes" / "organization_api.py"
        ).read_text(encoding="utf-8")

        for forbidden in (
            "organizer.web",
            "organizer.executor",
            "scan_directory",
            ".stat(",
            "os.stat",
        ):
            self.assertNotIn(forbidden, service)
        for forbidden in (
            "organizer.executor",
            "organizer.scanner",
            "organizer.planner",
            "organizer.grouping",
            "organizer.reports",
        ):
            self.assertNotIn(forbidden, route)

    def test_organization_decision_boundary_is_executor_and_persistence_free(self) -> None:
        decision_service = (
            REPOSITORY_ROOT
            / "src"
            / "organizer"
            / "application"
            / "organization_decision_service.py"
        ).read_text(encoding="utf-8").lower()
        store = (
            WEB_ROOT / "organization_decision_store.py"
        ).read_text(encoding="utf-8").lower()
        route = (
            WEB_ROOT / "routes" / "organization_api.py"
        ).read_text(encoding="utf-8").lower()
        combined = "\n".join((decision_service, store, route))
        for forbidden in (
            "organizer.executor",
            "apply_move_plan",
            "undo_operation_log",
            "organization_apply",
            "operation_log",
            "sqlite",
            "localstorage",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertNotIn("from organizer.models import moveplanitem", route)
        self.assertNotIn("from organizer.duplicates", route)

    def test_d1_execution_boundary_keeps_executor_out_of_web_routes(self) -> None:
        execution_service = (
            REPOSITORY_ROOT
            / "src"
            / "organizer"
            / "application"
            / "execution_service.py"
        ).read_text(encoding="utf-8")
        executor = (
            REPOSITORY_ROOT / "src" / "organizer" / "executor.py"
        ).read_text(encoding="utf-8")
        route_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (WEB_ROOT / "routes").glob("*.py")
        )

        self.assertIn("from organizer.executor import", execution_service)
        self.assertNotIn("organizer.executor", route_text)
        self.assertNotIn("apply_move_plan", route_text)
        self.assertNotIn("undo_operation_log", route_text)
        self.assertIn("shutil." + "move", executor)
        for path in (
            REPOSITORY_ROOT / "src" / "organizer"
        ).rglob("*.py"):
            if path.name != "executor.py":
                self.assertNotIn("shutil." + "move", path.read_text(encoding="utf-8"))

        loci_assets = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (WEB_ROOT / "static" / "loci_flow").glob("*.js")
        )
        self.assertIn("Approve test move", loci_assets)
        self.assertIn("/api/organization/current", loci_assets)
        self.assertIn("/scan?surface=home", loci_assets)
        self.assertIn("/api/activity", loci_assets)
        self.assertIn("/api/organization/move/preview", loci_assets)
        self.assertIn("/api/organization/move", loci_assets)
        self.assertIn("/api/organization/move", route_text)
        self.assertIn(
            "organizer.application.organization_movement_service",
            route_text,
        )
        self.assertIn("/api/activity", route_text)
        self.assertIn("organizer.application.undo_service", route_text)

    def test_launcher_reports_a_concise_missing_extra_error(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", "-m", "organizer.web", "--help"],
            cwd=REPOSITORY_ROOT,
            env={"PYTHONPATH": str(REPOSITORY_ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("bootai[web]", result.stderr)
        self.assertNotIn("traceback", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
