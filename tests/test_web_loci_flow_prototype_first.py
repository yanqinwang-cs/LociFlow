from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

import httpx

from organizer.web.app import create_app
from organizer.web.config import WebAppConfig


class _NavigationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.routes: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        if values.get("data-route"):
            self.routes.append(values["data-route"] or "")


class WebLociFlowPrototypeFirstTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.token = "p" * 32
        self.app = create_app(
            WebAppConfig(
                self.root,
                session_secret="s" * 32,
                launch_token=self.token,
                testing=True,
            )
        )

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_launch_and_root_enter_authenticated_loci_flow(self) -> None:
        async with _client(self.app) as client:
            unauthenticated_root = await client.get("/")
            unauthenticated_shell = await client.get("/loci-flow")
            launch = await client.get(
                f"/launch/{self.token}",
                follow_redirects=False,
            )
            shell = await client.get(launch.headers["location"])
            root = await client.get("/", follow_redirects=False)
            replay = await client.get(
                f"/launch/{self.token}",
                follow_redirects=False,
            )

        self.assertEqual(unauthenticated_root.status_code, 403)
        self.assertEqual(unauthenticated_shell.status_code, 403)
        self.assertEqual(launch.status_code, 303)
        self.assertEqual(launch.headers["location"], "/loci-flow")
        self.assertIn("bootai_session=", launch.headers["set-cookie"].lower())
        self.assertEqual(shell.status_code, 200)
        self.assertEqual(shell.headers["cache-control"], "no-store")
        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/loci-flow")
        self.assertEqual(root.headers["cache-control"], "no-store")
        self.assertEqual(replay.status_code, 403)

    async def test_shell_is_the_prototype_and_has_exact_navigation(self) -> None:
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            shell = await client.get("/loci-flow")

        parser = _NavigationParser()
        parser.feed(shell.text)
        self.assertEqual(
            parser.routes,
            ["home", "organize", "duplicate-bin", "activity", "standards", "settings"],
        )
        self.assertEqual(shell.text.count("<strong>Loci Flow</strong>"), 1)
        self.assertIn("No files can move in this stage.", shell.text)
        self.assertNotIn("Sample files only", shell.text)
        self.assertIn('data-theme-value="light"', shell.text)
        self.assertIn('data-theme-value="dark"', shell.text)
        self.assertNotIn("site-header", shell.text)
        self.assertNotIn("Open existing scan workflow", shell.text)
        self.assertNotIn(str(self.root), shell.text)

    async def test_all_fixture_assets_are_local_and_packaged(self) -> None:
        paths = (
            "/static/loci_flow/styles.css",
            "/static/loci_flow/app.js",
            "/static/loci_flow/data_source.js",
            "/static/loci_flow/view_models.js",
            "/static/loci_flow/fixtures.json",
            "/static/loci_flow/store.js",
            "/static/loci_flow/views.js",
            "/static/loci_flow/tree.js",
            "/static/loci_flow/assets/loci-flow-mark.svg",
            "/static/loci_flow/assets/file-icons.svg",
        )
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            shell = await client.get("/loci-flow")
            responses = [await client.get(path) for path in paths]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertIn("/static/loci_flow/styles.css", shell.text)
        self.assertIn("/static/loci_flow/app.js", shell.text)
        self.assertNotIn("prototype/", shell.text)
        self.assertNotIn("http://", shell.text)
        self.assertNotIn("https://", shell.text)

    async def test_frontend_connects_to_movement_without_changing_fixture_scenarios(self) -> None:
        methods_by_path = {
            route.path: set(route.methods or set())
            for route in _application_routes(self.app.routes)
            if hasattr(route, "path") and hasattr(route, "methods")
        }
        self.assertEqual(methods_by_path["/loci-flow"], {"GET"})
        self.assertEqual(methods_by_path["/api/organization/move/preview"], {"POST"})
        self.assertEqual(methods_by_path["/api/organization/move"], {"POST"})
        loci = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "organizer"
            / "web"
            / "static"
            / "loci_flow"
        )
        production_assets = "\n".join(
            (loci / name).read_text(encoding="utf-8")
            for name in ("api.js", "data_source.js", "view_models.js", "app.js", "views.js")
        )
        fixture_assets = "\n".join(
            (loci / name).read_text(encoding="utf-8")
            for name in ("store.js", "stage_b1_scenarios.js")
        )
        self.assertIn("/api/organization/move/preview", production_assets)
        self.assertIn("/api/organization/move", production_assets)
        self.assertNotIn("Move files — not connected yet", production_assets)
        self.assertIn("Move files — not connected yet", fixture_assets)


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
