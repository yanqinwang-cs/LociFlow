from __future__ import annotations

from html.parser import HTMLParser
import hashlib
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototype" / "loci-flow-ux-1-1-focused-policy-controls"
WEB = ROOT / "src" / "organizer" / "web"
LIVE = WEB / "static" / "loci_flow"

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


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[object, ...]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "meta" and any(
            ("name", name) in attrs
            for name in ("csrf-token", "recovery-attention")
        ):
            return
        normalized = []
        for name, value in attrs:
            if name in {"href", "src"} and tag in {"link", "img", "script"}:
                value = "<packaged-asset>"
            if tag == "meta" and ("name", "description") in attrs and name == "content":
                value = "<production-copy>"
            normalized.append((name, value))
        self.tokens.append(("start", tag, tuple(normalized)))

    def handle_endtag(self, tag: str) -> None:
        self.tokens.append(("end", tag))


def _structure(path: Path) -> list[tuple[object, ...]]:
    parser = _StructureParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.tokens


class LociFlowPrototypeFirstAssetTests(unittest.TestCase):
    def test_accepted_prototype_is_immutable(self) -> None:
        actual = {
            path.relative_to(PROTOTYPE).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(PROTOTYPE.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(actual, PROTOTYPE_SHA256)

    def test_immutable_core_assets_are_exact_copies(self) -> None:
        for relative in (
            "styles.css",
            "fixtures.json",
            "assets/file-icons.svg",
            "assets/loci-flow-mark.svg",
        ):
            with self.subTest(relative=relative):
                self.assertEqual(
                    (LIVE / relative).read_bytes(),
                    (PROTOTYPE / relative).read_bytes(),
                )

    def test_stage_b2_bootstrap_and_stage_a_tree_paths_remain_available(self) -> None:
        app = (LIVE / "app.js").read_text(encoding="utf-8")
        tree = (LIVE / "tree.js").read_text(encoding="utf-8")
        for required in (
            'import { createLociFlowApiClient } from "./api.js";',
            "createBackendDataSource,",
            '} from "./data_source.js";',
            "buildRouteViewModel(viewSource, state.route)",
            "buildDialogViewModel(viewSource)",
        ):
            self.assertIn(required, app)
        self.assertIn('new URL("./assets/file-icons.svg", import.meta.url)', tree)
        self.assertIn("export function buildMovementTree(changes)", tree)
        self.assertIn("export function renderSuppliedMovementTree", tree)

    def test_live_template_preserves_prototype_structure(self) -> None:
        self.assertEqual(
            _structure(WEB / "templates" / "loci_flow.html"),
            _structure(PROTOTYPE / "index.html"),
        )

    def test_fixture_boundary_has_distinct_page_view_models(self) -> None:
        data_source = (LIVE / "data_source.js").read_text(encoding="utf-8")
        view_models = (LIVE / "view_models.js").read_text(encoding="utf-8")
        self.assertIn("createFixtureDataSource", data_source)
        self.assertIn("clone(fixtureState)", data_source)
        for name in (
            "buildHomeViewModel",
            "buildOrganizeViewModel",
            "buildDuplicateBinViewModel",
            "buildActivityViewModel",
            "buildStandardsViewModel",
            "buildSettingsViewModel",
        ):
            self.assertIn(f"export function {name}", view_models)

    def test_page_roles_remain_distinct_and_complete(self) -> None:
        views = (LIVE / "views.js").read_text(encoding="utf-8")
        home = views.split("export function renderHome", 1)[1].split(
            "function styleControl", 1
        )[0]
        organize = views.split("export function renderOrganize", 1)[1].split(
            "function duplicateRow", 1
        )[0]
        self.assertIn("home-status", home)
        self.assertIn("summary-card", home)
        self.assertNotIn("organize-layout", home)
        self.assertIn("organize-layout", organize)
        self.assertNotIn("summary-card", organize)
        for required in (
            "bin-table",
            "Expand all",
            "Collapse all",
            "interpretation-panel",
            "Guided builder",
            "settings-layout",
            "Review and approve",
            "Where files will go",
        ):
            self.assertIn(required, views)

    def test_production_frontend_uses_backend_and_retains_explicit_test_fixtures(self) -> None:
        javascript = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(LIVE.glob("*.js"))
        )
        lower = javascript.lower()
        for forbidden in (
            "xmlhttprequest",
            "localstorage",
            "sessionstorage",
            "indexeddb",
            "document.cookie",
            "websocket",
            "eventsource",
            "serviceworker",
            "prototype/loci-flow",
        ):
            self.assertNotIn(forbidden, lower)
        self.assertTrue((LIVE / "api.js").exists())
        self.assertIn('const current_organization_url = "/api/organization/current"', lower)
        self.assertIn('const scan_url = "/scan?surface=home"', lower)
        data_source = (LIVE / "data_source.js").read_text(encoding="utf-8")
        self.assertIn('import("./fixtures.json"', data_source)
        self.assertNotIn('from "./fixtures.json"', data_source)
        self.assertNotIn("createFixtureDataSource", (LIVE / "app.js").read_text(encoding="utf-8"))

    def test_no_external_assets_or_prohibited_product_language(self) -> None:
        authored = [
            WEB / "templates" / "loci_flow.html",
            *sorted(LIVE.glob("*.js")),
            LIVE / "styles.css",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in authored)
        lower = combined.lower()
        for forbidden in (
            "http://",
            "https://",
            "backend verified movement",
            "backend-provided metadata",
            "backend-provided destinations",
            "organization styles are not connected yet",
            "sha-256",
            "/users/",
        ):
            self.assertNotIn(forbidden, lower)
        self.assertIsNone(re.search(r"floating.{0,20}(bot|assistant)", lower))


if __name__ == "__main__":
    unittest.main()
