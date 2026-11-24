from __future__ import annotations

import asyncio
from pathlib import Path
import re
import tempfile
import unittest

import httpx

from organizer.web.app import create_app
from organizer.web.config import WebAppConfig


class WebLociFlowStageB2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        destination = self.root / "Organized" / "EvoSim_project_slides" / "slides"
        destination.mkdir(parents=True)
        (self.root / "EvoSim_project_slides.pptx").write_text("a", encoding="utf-8")
        (self.root / "EvoSim_project_slides_final.pptx").write_text("b", encoding="utf-8")
        self.token = "q" * 32
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

    async def test_shell_bootstraps_backend_assets_without_fixture_copy(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.get("/loci-flow")
            launch = await client.get(f"/launch/{self.token}", follow_redirects=False)
            shell = await client.get("/loci-flow")
            app = await client.get("/static/loci_flow/app.js")
            api = await client.get("/static/loci_flow/api.js")
            data_source = await client.get("/static/loci_flow/data_source.js")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(launch.status_code, 303)
        self.assertEqual(launch.headers["location"], "/loci-flow")
        self.assertEqual(shell.status_code, 200)
        self.assertEqual(shell.headers["cache-control"], "no-store")
        self.assertTrue(all(response.status_code == 200 for response in (app, api, data_source)))
        self.assertIn("createBackendDataSource", app.text)
        self.assertNotIn("createFixtureDataSource", app.text)
        self.assertNotIn("fixtures.json", app.text)
        self.assertNotIn("Sample files only", shell.text)
        self.assertNotIn("Sample Downloads", shell.text)
        self.assertIn("No files can move in this stage", shell.text)
        self.assertNotIn(str(self.root), shell.text)

    async def test_existing_scan_stays_in_loci_flow_and_produces_real_recommendations(self) -> None:
        async with _client(self.app) as client:
            csrf = await self._authenticate(client)
            started = await client.post(
                "/scan?surface=home",
                data={"csrf_token": csrf},
                headers={"Origin": "http://127.0.0.1"},
                follow_redirects=False,
            )
            snapshot = await _wait_for_scan(self.app)
            root_redirect = await client.get(started.headers["location"], follow_redirects=False)
            shell = await client.get(root_redirect.headers["location"])
            organization = await client.get("/api/organization/current")

        self.assertEqual(started.status_code, 303)
        self.assertEqual(started.headers["location"], "/")
        self.assertEqual(root_redirect.status_code, 303)
        self.assertEqual(root_redirect.headers["location"], "/loci-flow")
        self.assertEqual(shell.status_code, 200)
        self.assertNotIn("legacy", shell.text.lower())
        self.assertEqual(snapshot.status, "completed")
        self.assertEqual(organization.status_code, 200)
        payload = organization.json()
        self.assertEqual(payload["scope"]["display_name"], self.root.name)
        self.assertEqual(len(payload["recommended_changes"]), 2)
        self.assertEqual(len(payload["choices_needed"]), 0)
        self.assertEqual(
            {item["reason_code"] for item in payload["recommended_changes"]},
            {"matched_existing_group"},
        )
        self.assertEqual(
            {item["destination_folder"] for item in payload["recommended_changes"]},
            {"Organized/EvoSim_project_slides/slides"},
        )
        self.assertFalse((self.root / "Organized" / "EvoSim_project_slides" / "slides" / "EvoSim_project_slides.pptx").exists())

    async def test_real_decisions_approval_and_reload_reach_ready_without_movement(self) -> None:
        first_source = self.root / "EvoSim_project_slides.pptx"
        second_source = self.root / "EvoSim_project_slides_final.pptx"
        before = (first_source.read_bytes(), second_source.read_bytes())

        async with _client(self.app) as client:
            csrf = await self._authenticate(client)
            await client.post(
                "/scan?surface=home",
                data={"csrf_token": csrf},
                headers={"Origin": "http://127.0.0.1"},
            )
            await _wait_for_scan(self.app)
            current = (await client.get("/api/organization/current")).json()
            first, second = current["recommended_changes"]
            accepted = await client.post(
                "/api/organization/decisions",
                data={
                    "csrf_token": csrf,
                    "scan_generation": current["freshness"]["scan_generation"],
                    "expected_revision": "0",
                    "kind": "recommendation",
                    "change_id": first["id"],
                    "decision": "accept",
                },
                headers={"Origin": "http://127.0.0.1"},
            )
            kept = await client.post(
                "/api/organization/decisions",
                data={
                    "csrf_token": csrf,
                    "scan_generation": current["freshness"]["scan_generation"],
                    "expected_revision": "1",
                    "kind": "recommendation",
                    "change_id": second["id"],
                    "decision": "keep",
                },
                headers={"Origin": "http://127.0.0.1"},
            )
            approved = await client.post(
                "/api/organization/approval",
                data={
                    "csrf_token": csrf,
                    "scan_generation": current["freshness"]["scan_generation"],
                    "expected_revision": "2",
                },
                headers={"Origin": "http://127.0.0.1"},
            )
            reloaded = await client.get("/api/organization/current")

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(kept.status_code, 200)
        self.assertTrue(kept.json()["review"]["summary"]["ready_for_approval"])
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["review"]["status"], "ready_to_move")
        self.assertEqual(reloaded.json()["review"]["status"], "ready_to_move")
        self.assertEqual(reloaded.json()["review"]["summary"]["files_to_move"], 1)
        self.assertTrue(first_source.exists())
        self.assertTrue(second_source.exists())
        self.assertEqual((first_source.read_bytes(), second_source.read_bytes()), before)
        self.assertFalse((self.root / "AI_Review" / "organization_operations").exists())

    async def test_backend_movement_is_post_only_and_executor_stays_out_of_routes(self) -> None:
        methods_by_path = {
            route.path: set(route.methods or set())
            for route in _application_routes(self.app.routes)
            if hasattr(route, "path") and hasattr(route, "methods")
        }
        self.assertEqual(methods_by_path["/loci-flow"], {"GET"})
        self.assertEqual(methods_by_path["/scan"], {"POST"})
        self.assertEqual(methods_by_path["/api/organization/current"], {"GET"})
        self.assertEqual(methods_by_path["/api/organization/decisions"], {"POST"})
        self.assertEqual(methods_by_path["/api/organization/approval"], {"POST"})
        self.assertEqual(methods_by_path["/api/organization/move/preview"], {"POST"})
        self.assertEqual(methods_by_path["/api/organization/move"], {"POST"})
        for forbidden in (
            "/api/organization/movement-preview",
            "/api/loci-flow/move",
        ):
            self.assertNotIn(forbidden, methods_by_path)
        routes_root = Path(__file__).resolve().parents[1] / "src" / "organizer" / "web" / "routes"
        route_code = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(routes_root.glob("*.py"))
        )
        self.assertNotRegex(route_code, r"(?:from|import)\s+organizer\.executor")

    async def _authenticate(self, client: httpx.AsyncClient) -> str:
        await client.get(f"/launch/{self.token}", follow_redirects=False)
        shell = await client.get("/loci-flow")
        match = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text)
        assert match is not None
        return match.group(1)


async def _wait_for_scan(app: object):
    for _ in range(200):
        snapshot = app.state.scan_jobs.snapshot()
        if snapshot.status != "scanning":
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError("scan did not settle")


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
