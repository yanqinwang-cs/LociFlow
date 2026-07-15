from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import unittest

import httpx

from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from tests.test_application_undo_service import _verified_operation


ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"
PROTOTYPE = ROOT / "prototype" / "loci-flow-ux-1-1-focused-policy-controls"


class WebLociFlowStageB3Tests(unittest.IsolatedAsyncioTestCase):
    def test_fidelity_locked_files_prototype_and_template_are_unchanged(self) -> None:
        expected_live = {
            "styles.css": "6288229571bb209624e00a2a82c639c93ea8eae1aca3dbad9022cb5203c637dd",
            "views.js": "b07a7b55e6f4571ade59f04439660952ff4c2582d8f0fea0e78f7ea3b61aa96a",
            "tree.js": "5dc9db40114c78f1dd9b7d81dfb2a52c6c5b8f73670d8a6f2bed49cc7d385975",
            "store.js": "ac0876ec961fadd4b62115d1f372606fa536b2a00ac026029578a263bb5ad3be",
            "fixtures.json": "0d614d82bf3aa75b4ab102a77d47e77b3d3ef7ed390afd6119c1e4099e36fec1",
            "stage_b1_scenarios.js": "fdc12218e17521a15ffcb7e5aa85e64e80d177399db7a037d616388d580c4f08",
            "assets/file-icons.svg": "bfefdc18d03629036a5d1fe31b614ebcf70ab8acf7926169e6ef10e1926595c9",
            "assets/loci-flow-mark.svg": "ec0d01fa8e1f312e3233b0f7758f7192d8b40de1370cb48b0f95bce87686f439",
        }
        expected_prototype = {
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
        self.assertEqual(
            {relative: _sha256(LOCI / relative) for relative in expected_live},
            expected_live,
        )
        self.assertEqual(
            {relative: _sha256(PROTOTYPE / relative) for relative in expected_prototype},
            expected_prototype,
        )
        self.assertEqual(
            _sha256(ROOT / "src" / "organizer" / "web" / "templates" / "loci_flow.html"),
            "4f775f4f624e6d22a7e7ef7bb86bf2908430224b7940dc802b22c73312551f09",
        )

    def test_production_app_wires_activity_states_and_undo_without_fixture_or_movement(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        api = (LOCI / "api.js").read_text(encoding="utf-8")
        data_source = (LOCI / "data_source.js").read_text(encoding="utf-8")
        view_models = (LOCI / "view_models.js").read_text(encoding="utf-8")
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        combined = app + api + data_source + view_models + views

        for required in (
            "Loading Activity…",
            "Activity could not be loaded.",
            "No file moves recorded.",
            "Try again",
            "toggle-activity",
            "expand-all-activity",
            "collapse-all-activity",
            "Undo this move?",
            "Undo move",
            "Restoring files…",
            "Files restored.",
            "The result could not be loaded. Reload Activity before trying again.",
            "Move files — not connected yet",
        ):
            self.assertIn(required, combined)

        self.assertIn("entry.undoable", views)
        self.assertIn("item.undoable === true", app)
        self.assertIn("activeUndoPreview: null", app)
        self.assertIn("expandedIds: new Set()", app)
        self.assertIn("await dataSource.loadActivity()", app)
        self.assertIn("await dataSource.previewUndo(entry.id)", app)
        self.assertEqual(app.count("await dataSource.confirmUndo("), 1)
        self.assertIn("textContent", app)
        self.assertNotIn("confirmation_token}`", app)

        for forbidden in (
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "XMLHttpRequest",
            "WebSocket",
            "EventSource",
            "/api/organization/move",
            "movement-preview",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertNotIn("fixtures.json", app)
        self.assertNotIn("stage_b1_scenarios", app)
        self.assertIn('import("./fixtures.json"', data_source)
        self.assertNotIn('from "./fixtures.json"', data_source)

    def test_activity_is_loaded_on_activity_navigation_and_reload_only(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        load_current = app[app.index("async function loadCurrent"):app.index("async function loadActivity")]
        check_files = app[app.index("async function checkFiles"):app.index("async function handleMutation")]
        self.assertNotIn("loadActivity", load_current)
        self.assertNotIn("loadActivity", check_files)
        self.assertIn('if (route === "activity" && previousRoute !== "activity") loadActivity();', app)
        self.assertIn('if (nextRoute === "activity" && previousRoute !== "activity") loadActivity();', app)
        self.assertIn('if (state.route === "activity") loadActivity();', app)
        self.assertIn('if (previousRoute === "activity" && nextRoute !== "activity") discardActivityPreview();', app)
        self.assertIn('if (action === "retry-activity") return loadActivity();', app)
        self.assertIn('routeLink?.dataset.route === "activity" && state.route === "activity"', app)

    async def test_real_durable_activity_and_undo_are_visible_through_existing_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, _, _, record = _verified_operation(root, 2)
            moved_bytes = [
                (root / "Documents" / f"file-{index}.txt").read_bytes()
                for index in range(2)
            ]
            app = create_app(WebAppConfig(
                root,
                session_secret="s" * 32,
                launch_token="t" * 32,
                testing=True,
            ))
            async with _client(app) as client:
                csrf = await _authenticate(client)
                initial = await client.get("/api/activity")
                preview = await client.post(
                    f"/api/activity/{record.operation_id}/undo/preview",
                    data={"csrf_token": csrf},
                    headers={"Origin": "http://127.0.0.1"},
                )
                final = await client.post(
                    f"/api/activity/{record.operation_id}/undo",
                    data={
                        "csrf_token": csrf,
                        "confirmation_token": preview.json()["confirmation_token"],
                        "confirmation": "UNDO FILE MOVE",
                    },
                    headers={"Origin": "http://127.0.0.1"},
                )
                updated = await client.get("/api/activity")
                reloaded = await client.get("/api/activity")

            self.assertEqual(initial.status_code, 200)
            self.assertTrue(initial.json()["activity"][0]["undoable"])
            self.assertEqual(preview.json()["files_to_restore"], 2)
            self.assertEqual(final.json()["undo"]["status"], "restored")
            self.assertEqual(updated.json(), reloaded.json())
            self.assertEqual(updated.headers["cache-control"], "no-store")
            self.assertEqual(updated.json()["activity"][0]["type"], "restore")
            self.assertFalse(any(entry["undoable"] for entry in updated.json()["activity"]))
            for index, expected in enumerate(moved_bytes):
                source = root / "Inbox" / f"file-{index}.txt"
                destination = root / "Documents" / f"file-{index}.txt"
                self.assertEqual(source.read_bytes(), expected)
                self.assertFalse(destination.exists())
            serialized = updated.text
            for forbidden in (str(root), "sha256", "authorization_id", "legacy_operation_log"):
                self.assertNotIn(forbidden, serialized)

    async def test_backend_movement_is_post_only_and_executor_out_of_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app = create_app(WebAppConfig(
                root,
                session_secret="s" * 32,
                launch_token="t" * 32,
                testing=True,
            ))
            methods_by_path = {
                route.path: set(route.methods or set())
                for route in _application_routes(app.routes)
                if hasattr(route, "path") and hasattr(route, "methods")
            }
        self.assertEqual(methods_by_path["/api/activity"], {"GET"})
        self.assertEqual(
            methods_by_path["/api/activity/{operation_id}/undo/preview"],
            {"POST"},
        )
        self.assertEqual(methods_by_path["/api/activity/{operation_id}/undo"], {"POST"})
        self.assertEqual(methods_by_path["/api/organization/move/preview"], {"POST"})
        self.assertEqual(methods_by_path["/api/organization/move"], {"POST"})
        for forbidden in (
            "/api/organization/movement-preview",
            "/api/loci-flow/move",
        ):
            self.assertNotIn(forbidden, methods_by_path)
        route_code = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "src" / "organizer" / "web" / "routes").glob("*.py"))
        )
        self.assertNotRegex(route_code, r"(?:from|import)\s+organizer\.executor")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _authenticate(client: httpx.AsyncClient) -> str:
    await client.get(f"/launch/{'t' * 32}", follow_redirects=False)
    shell = await client.get("/loci-flow")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text)
    assert match is not None
    return match.group(1)


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://127.0.0.1",
        follow_redirects=False,
    )


def _application_routes(routes: list[object]) -> list[object]:
    flattened: list[object] = []
    for route in routes:
        included_router = getattr(route, "original_router", None)
        if included_router is not None:
            flattened.extend(_application_routes(included_router.routes))
        else:
            flattened.append(route)
    return flattened


if __name__ == "__main__":
    unittest.main()
