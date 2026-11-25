from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import httpx

from organizer.application.scan_service import scan_root
from organizer.organization_rules import ORGANIZATION_RULES_RELATIVE_PATH
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from organizer.web.scan_jobs import ScanJobSnapshot


class WebOrganizationSliceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        (self.root / "Inbox").mkdir()
        (self.root / "Archive").mkdir()
        (self.root / "Submissions").mkdir()
        (self.root / "Inbox" / "copy.txt").write_text("same", encoding="utf-8")
        (self.root / "Archive" / "copy.txt").write_text("same", encoding="utf-8")
        (self.root / "Submissions" / "work.pdf").write_text("work", encoding="utf-8")
        self.result = scan_root(self.root)
        self.token = "o" * 32
        self.app = create_app(
            WebAppConfig(
                self.root,
                session_secret="s" * 32,
                launch_token=self.token,
                testing=True,
            )
        )
        self.completed_at = datetime(2026, 7, 14, 18, 52, tzinfo=UTC)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_endpoint_requires_authentication_and_completed_scan(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.get("/api/organization/current")
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            not_ready = await client.get("/api/organization/current")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(
            unauthenticated.json()["error"]["code"],
            "authentication_required",
        )
        self.assertEqual(not_ready.status_code, 409)
        self.assertEqual(not_ready.json()["error"]["code"], "scan_not_ready")
        self.assertEqual(unauthenticated.headers["cache-control"], "no-store")
        self.assertEqual(not_ready.headers["cache-control"], "no-store")

    async def test_scan_progress_and_failure_have_stable_errors(self) -> None:
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            self.app.state.scan_jobs._snapshot = ScanJobSnapshot(
                status="scanning",
                job_id="scan-progress",
            )
            scanning = await client.get("/api/organization/current")
            self.app.state.scan_jobs._snapshot = ScanJobSnapshot(
                status="failed",
                job_id="scan-failed",
            )
            failed = await client.get("/api/organization/current")

        self.assertEqual(scanning.status_code, 409)
        self.assertEqual(scanning.json()["error"]["code"], "scan_in_progress")
        self.assertEqual(failed.status_code, 409)
        self.assertEqual(failed.json()["error"]["code"], "scan_failed")

    async def test_completed_scan_returns_read_only_root_relative_json(self) -> None:
        self._complete_scan()
        before = self.app.state.scan_jobs.snapshot()
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            first = await client.get("/api/organization/current")
            second = await client.get("/api/organization/current")
        after = self.app.state.scan_jobs.snapshot()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(first.headers["content-type"], "application/json")
        self.assertEqual(first.json(), second.json())
        self.assertEqual(before, after)
        payload = first.json()
        self.assertEqual(payload["scope"]["display_name"], self.root.name)
        self.assertEqual(payload["scope"]["root_label"], ".")
        self.assertEqual(payload["freshness"]["scan_generation"], "scan-current")
        self.assertEqual(
            payload["freshness"]["last_checked_at"],
            self.completed_at.isoformat(),
        )
        self.assertIsNone(payload["planning"]["organization_style"])
        self.assertTrue(payload["choices_needed"])
        self.assertTrue(payload["kept_by_standards"])
        serialized = json.dumps(payload).lower()
        self.assertNotIn(str(self.root).lower(), serialized)
        self.assertNotIn("sha256", serialized)
        self.assertNotIn("executor", serialized)

    async def test_query_parameters_and_non_get_method_are_rejected(self) -> None:
        self._complete_scan()
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            unknown = await client.get("/api/organization/current?unknown=1")
            root = await client.get("/api/organization/current?root=/tmp")
            duplicate = await client.get("/api/organization/current?style=a&style=b")
            post = await client.post("/api/organization/current")

        for response in (unknown, root, duplicate):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_query")
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(post.status_code, 405)

    async def test_unavailable_standards_block_review_approval_and_movement_then_recover(
        self,
    ) -> None:
        config = self.root / ORGANIZATION_RULES_RELATIVE_PATH
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("{invalid", encoding="utf-8")
        self.result = scan_root(self.root)
        self._complete_scan()

        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            shell = await client.get("/loci-flow")
            match = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text)
            assert match is not None
            csrf = match.group(1)
            current = await client.get("/api/organization/current")
            approval = await client.post(
                "/api/organization/approval",
                data={
                    "csrf_token": csrf,
                    "scan_generation": "scan-current",
                    "expected_revision": "0",
                },
                headers={"Origin": "http://127.0.0.1"},
            )
            preview = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers={"Origin": "http://127.0.0.1"},
            )

            config.write_text('{"version": 1}', encoding="utf-8")
            self.result = scan_root(self.root)
            self._complete_scan()
            recovered = await client.get("/api/organization/current")

        for response in (current, approval, preview):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "standards_unavailable")
            self.assertEqual(
                response.json()["error"]["message"],
                "Standards could not be loaded. Fix them before organizing.",
            )
        self.assertEqual(self.app.state.organization_decisions._sessions, {})
        self.assertEqual(recovered.status_code, 200)

    def _complete_scan(self) -> None:
        self.app.state.scan_jobs._snapshot = ScanJobSnapshot(
            status="completed",
            job_id="scan-current",
            started_at=self.completed_at,
            completed_at=self.completed_at,
            result=self.result,
            report_path=self.root / "AI_Review" / "reports" / "scan.json",
        )


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        follow_redirects=False,
    )


if __name__ == "__main__":
    unittest.main()
