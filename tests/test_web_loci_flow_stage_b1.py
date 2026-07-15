from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import httpx

from organizer.web.app import create_app
from organizer.web.config import WebAppConfig


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"


class WebLociFlowStageB1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.token = "b" * 32
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

    async def test_authenticated_shell_packages_the_stage_b1_scenario_contract(self) -> None:
        paths = (
            "/static/loci_flow/stage_b1_scenarios.js",
            "/static/loci_flow/data_source.js",
            "/static/loci_flow/view_models.js",
            "/static/loci_flow/views.js",
            "/static/loci_flow/store.js",
            "/static/loci_flow/tree.js",
            "/static/loci_flow/app.js",
        )
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            shell = await client.get("/loci-flow")
            assets = [await client.get(path) for path in paths]

        self.assertEqual(shell.status_code, 200)
        self.assertEqual(shell.headers["cache-control"], "no-store")
        self.assertTrue(all(response.status_code == 200 for response in assets))
        self.assertIn("accepted_default", assets[0].text)
        self.assertIn('src="/static/loci_flow/app.js"', shell.text)
        self.assertNotIn("stage_b1_scenarios.js", shell.text)
        self.assertNotIn(str(self.root), shell.text)

    async def test_stage_b1_adds_no_route_or_state_changing_request_surface(self) -> None:
        methods_by_path = {
            route.path: set(route.methods or set())
            for route in _application_routes(self.app.routes)
            if hasattr(route, "path") and hasattr(route, "methods")
        }
        self.assertEqual(methods_by_path["/loci-flow"], {"GET"})
        self.assertFalse(any("scenario" in path or "fixture" in path for path in methods_by_path))
        self.assertNotIn("/api/organization/move", methods_by_path)
        self.assertNotIn("/api/loci-flow/stage-b1", methods_by_path)

    async def test_launch_and_root_behavior_remain_stage_a_compatible(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.get("/loci-flow")
            launch = await client.get(f"/launch/{self.token}", follow_redirects=False)
            root = await client.get("/", follow_redirects=False)
            shell = await client.get("/loci-flow")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(launch.status_code, 303)
        self.assertEqual(launch.headers["location"], "/loci-flow")
        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/loci-flow")
        self.assertEqual(shell.status_code, 200)

    def test_new_asset_is_covered_by_existing_package_data_glob(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"static/loci_flow/*.js"', pyproject)
        self.assertTrue((LIVE / "stage_b1_scenarios.js").is_file())


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
