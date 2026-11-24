from __future__ import annotations

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


class WebLociFlowStageB3Tests(unittest.IsolatedAsyncioTestCase):
    def test_production_app_wires_activity_undo_and_confirmed_movement_without_fixture(self) -> None:
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
            "Move files",
            "MOVE APPROVED FILES",
        ):
            self.assertIn(required, combined)

        self.assertIn("entry.undoable", views)
        self.assertIn("item.undoable === true", app)
        self.assertIn("activeUndoPreview: null", app)
        self.assertIn("expandedIds: new Set()", app)
        self.assertIn("activityLoadPromise = dataSource.loadActivity()", app)
        self.assertIn("const payload = await activityLoadPromise", app)
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
            "movement-preview",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertNotIn("fixtures.json", app)
        self.assertNotIn("stage_b1_scenarios", app)
        self.assertIn('import("./fixtures.json"', data_source)
        self.assertNotIn('from "./fixtures.json"', data_source)

    def test_activity_is_loaded_on_startup_then_reused_by_activity_navigation(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        load_current = app[app.index("async function loadCurrent"):app.index("async function loadActivity")]
        check_files = app[app.index("async function checkFiles"):app.index("async function handleMutation")]
        self.assertNotIn("loadActivity", load_current)
        self.assertNotIn("loadActivity", check_files)
        self.assertIn('if (route === "activity" && previousRoute !== "activity") loadActivity();', app)
        self.assertIn('if (nextRoute === "activity" && previousRoute !== "activity") loadActivity();', app)
        self.assertIn("loadActivity({ force: true });", app)
        self.assertIn('state.activity.loadStatus === "ready" && !refresh', app)
        self.assertIn("if (activityLoadPromise)", app)
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
