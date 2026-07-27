from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

from organizer.application.recovery_projection_service import BrowserRecoveryProjection
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig


class WebRecoveryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.token = "r" * 32
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

    async def test_authentication_query_method_and_no_store_contract(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.get("/api/recovery/current")
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            valid = await client.get("/api/recovery/current")
            query = await client.get("/api/recovery/current?root=/tmp")
            post = await client.post("/api/recovery/current")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(unauthenticated.json()["error"]["code"], "authentication_required")
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json()["state"], "clear")
        self.assertEqual(query.status_code, 400)
        self.assertEqual(post.status_code, 405)
        for response in (unauthenticated, valid, query, post):
            self.assertEqual(response.headers.get("cache-control"), "no-store")

    async def test_each_get_is_fresh_and_exposes_only_safe_fields(self) -> None:
        service = mock.Mock()
        service.current.side_effect = (
            BrowserRecoveryProjection("needs_attention", "First", "Review.", 1),
            BrowserRecoveryProjection("clear", "Clear", "Nothing to review.", 0),
        )
        self.app.state.recovery_projection_service = service
        lifecycle_before = self.app.state.runtime_lifecycle.snapshot()
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            first = await client.get("/api/recovery/current")
            second = await client.get("/api/recovery/current")

        self.assertEqual(first.json()["state"], "needs_attention")
        self.assertEqual(second.json()["state"], "clear")
        self.assertEqual(service.current.call_count, 2)
        self.assertEqual(self.app.state.runtime_lifecycle.snapshot(), lifecycle_before)
        self.assertEqual(
            set(first.json()), {"state", "title", "message", "affected_items"}
        )

    async def test_home_uses_current_coarse_status_without_detail_inspection(self) -> None:
        service = mock.Mock()
        service.current.return_value = BrowserRecoveryProjection(
            "clear", "Clear", "Nothing to review.", 0
        )
        self.app.state.recovery_projection_service = service
        with mock.patch.object(
            self.app.state.current_recovery_admission_status,
            "snapshot",
            return_value=SimpleNamespace(is_blocked=True),
        ) as snapshot:
            async with _client(self.app) as client:
                await client.get(f"/launch/{self.token}", follow_redirects=False)
                before = await client.get("/loci-flow")
                detail = await client.get("/api/recovery/current")
                after = await client.get("/loci-flow")

        self.assertIn('<meta name="recovery-attention" content="true">', before.text)
        self.assertEqual(detail.json()["state"], "clear")
        self.assertIn('<meta name="recovery-attention" content="true">', after.text)
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(service.current.call_count, 1)

    async def test_unexpected_service_error_is_bounded_json(self) -> None:
        self.app.state.recovery_projection_service = mock.Mock()
        self.app.state.recovery_projection_service.current.side_effect = RuntimeError(
            f"repository failed at {self.root}/secret.json"
        )
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            response = await client.get("/api/recovery/current")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "recovery_unavailable")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn("secret.json", response.text)

    async def test_actual_inspection_writes_nothing_and_calls_no_activity_or_executor(self) -> None:
        before = _root_files(self.root)
        with (
            mock.patch("organizer.executor.apply_move_plan") as executor,
            mock.patch(
                "organizer.application.activity_service.list_activity_entries"
            ) as activity,
        ):
            async with _client(self.app) as client:
                await client.get(f"/launch/{self.token}", follow_redirects=False)
                response = await client.get("/api/recovery/current")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_root_files(self.root), before)
        executor.assert_not_called()
        activity.assert_not_called()


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://127.0.0.1",
        follow_redirects=False,
    )


def _root_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
