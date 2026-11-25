from __future__ import annotations

import hashlib
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "organizer" / "web"
LOCI = WEB / "static" / "loci_flow"
PROTOTYPE = ROOT / "prototype" / "loci-flow-ux-1-1-focused-policy-controls"
PROTOTYPE_SHA256 = {
    "README.md": "d146f921a49b65223a8438972f2bec167bd7f9be8721bb12bf8e2e7abc84f54d",
    "app.js": "f17163cd2929cf7b8c14cfb23a396204efc5bd204b2205c60c291842666d77e5",
    "assets/file-icons.svg": "bfefdc18d03629036a5d1fe31b614ebcf70ab8acf7926169e6ef10e1926595c9",
    "assets/loci-flow-mark.svg": "ec0d01fa8e1f312e3233b0f7758f7192d8b40de1370cb48b0f95bce87686f439",
    "fixtures.json": "0d614d82bf3aa75b4ab102a77d47e77b3d3ef7ed390afd6119c1e4099e36fec1",
    "index.html": "1e7ad3659eeaa35424e4cf09f265542d4cde55bf6fbe7672b77a6f54c01115b2",
    "store.js": "a6b6a811a5cedac0ffe3b21b0906df00640dfb1b224beb97b7aacbc5c2dca0ff",
    "styles.css": "6288229571bb209624e00a2a82c639c93ea8eae1aca3dbad9022cb5203c637dd",
    "tree.js": "6f48d5cd34372deb2ec6407845310dba930dbf3fbc79a516eedd87f6c3ebf704",
    "views.js": "02b2f1078dd772da51274645d7938ce84c6585db4e97d0f0bbf0f160485e0f6f",
}


class LociFlowReadOnlyUiAssetTests(unittest.TestCase):
    def test_accepted_prototype_is_byte_unchanged(self) -> None:
        actual = {
            path.relative_to(PROTOTYPE).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(PROTOTYPE.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(actual, PROTOTYPE_SHA256)

    def test_integrated_shell_has_exact_navigation_and_local_assets(self) -> None:
        template = (WEB / "templates" / "loci_flow.html").read_text(
            encoding="utf-8"
        )
        for route in (
            "home",
            "organize",
            "duplicate-bin",
            "activity",
            "standards",
            "settings",
        ):
            self.assertEqual(template.count(f'data-route="{route}"'), 1)
        self.assertNotIn("prototype/", template)
        self.assertNotIn("http://", template)
        self.assertNotIn("https://", template)
        self.assertNotIn("<script>", template)
        self.assertNotIn("style=", template)

    def test_frontend_uses_backend_with_explicit_test_fixture_boundary(self) -> None:
        javascript = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(LOCI.glob("*.js"))
        )
        lower = javascript.lower()
        for forbidden in (
            "localstorage",
            "sessionstorage",
            "indexeddb",
            "document.cookie",
            "xmlhttprequest",
            "websocket",
            "eventsource",
            "serviceworker",
        ):
            self.assertNotIn(forbidden, lower)
        self.assertIn("createFixtureDataSource", javascript)
        self.assertIn("createBackendDataSource", javascript)
        self.assertIn("buildHomeViewModel", javascript)
        self.assertIn("buildOrganizeViewModel", javascript)
        self.assertTrue((LOCI / "api.js").exists())
        self.assertNotIn("createFixtureDataSource", (LOCI / "app.js").read_text(encoding="utf-8"))

    def test_all_six_prototype_pages_are_live(self) -> None:
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        for renderer in (
            "renderHome",
            "renderOrganize",
            "renderDuplicateBin",
            "renderActivity",
            "renderStandards",
            "renderSettings",
        ):
            self.assertIn(f"export function {renderer}", views)
        for wording in (
            "Recommended changes",
            "Choices needed",
            "Where files will go",
            "Expand all",
            "Guided builder",
            "Appearance",
        ):
            self.assertIn(wording, views)

    def test_fixture_data_source_returns_fresh_sample_state(self) -> None:
        script = textwrap.dedent(
            f"""
            import assert from 'node:assert/strict';
            const module = await import({(LOCI / 'data_source.js').as_uri()!r});
            const source = module.createFixtureDataSource();
            const first = await source.load();
            first.meta.currentFolder = 'Changed';
            first.baseChanges.length = 0;
            const second = await source.load();
            assert.equal(source.kind, 'fixture');
            assert.equal(second.meta.currentFolder, 'Sample Downloads');
            assert.ok(second.baseChanges.length > 0);
            """
        )
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_route_has_no_domain_owner_or_executor_dependency(self) -> None:
        route = (WEB / "routes" / "loci_flow.py").read_text(encoding="utf-8")
        for forbidden in ("executor", "scanner", "planner", "duplicates", "grouping"):
            self.assertNotIn(forbidden, route.lower())


if __name__ == "__main__":
    unittest.main()
