from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from organizer.application.recovery_check_service import (
    RecoveryCheckContextResponse,
    RecoveryCheckError,
    RecoveryCheckResult,
    RecoveryCheckResultState,
)
from organizer.application.recovery_projection_service import BrowserRecoveryProjection
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from tests.test_application_identity_verification_publication import _operation


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
        self.assertEqual(
            unauthenticated.json()["error"]["code"], "authentication_required"
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json()["state"], "clear")
        self.assertEqual(query.status_code, 400)
        self.assertEqual(post.status_code, 405)
        for response in (unauthenticated, valid, query, post):
            self.assertEqual(response.headers.get("cache-control"), "no-store")

    async def test_each_get_is_fresh_and_exposes_only_safe_fields(self) -> None:
        service = mock.Mock()
        service.current.side_effect = (
            BrowserRecoveryProjection("needs_attention", "First", "Review.", 1, True),
            BrowserRecoveryProjection("clear", "Clear", "Nothing to review.", 0, False),
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
            set(first.json()),
            {"state", "title", "message", "affected_items", "can_check_result"},
        )
        self.assertTrue(first.json()["can_check_result"])

    async def test_home_uses_current_coarse_status_without_detail_inspection(
        self,
    ) -> None:
        service = mock.Mock()
        service.current.return_value = BrowserRecoveryProjection(
            "clear", "Clear", "Nothing to review.", 0, False
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

    async def test_actual_inspection_writes_nothing_and_calls_no_activity_or_executor(
        self,
    ) -> None:
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

    async def test_context_and_result_posts_use_exact_safe_contracts(self) -> None:
        _operation(self.root, (b"recorded",))
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            eligible = await client.get("/api/recovery/current")
            context = await client.post(
                "/api/recovery/check-context",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            result = await client.post(
                "/api/recovery/check-result",
                data={
                    "csrf_token": csrf,
                    "case_id": context.json()["case_id"],
                    "action_id": context.json()["action_id"],
                },
                headers=_origin(),
            )
            current = await client.get("/api/recovery/current")

        self.assertEqual(eligible.status_code, 200)
        self.assertTrue(eligible.json()["can_check_result"])
        self.assertEqual(
            eligible.json()["message"],
            "Loci Flow can check what happened before you continue.",
        )
        self.assertEqual(context.status_code, 200)
        self.assertEqual(
            set(context.json()),
            {"case_id", "action_id", "expires_at", "label"},
        )
        self.assertEqual(context.json()["label"], "Check result")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            set(result.json()),
            {"state", "title", "message", "recovery_attention"},
        )
        self.assertEqual(result.json()["state"], "confirmed")
        self.assertFalse(result.json()["recovery_attention"])
        self.assertEqual(
            set(current.json()),
            {"state", "title", "message", "affected_items", "can_check_result"},
        )
        self.assertFalse(current.json()["can_check_result"])
        for response in (eligible, context, result, current):
            self.assertEqual(response.headers["cache-control"], "no-store")
        serialized = context.text + result.text
        for secret in (
            str(self.root),
            "Documents/item-1.txt",
            "organization_movement",
            "moved_unverified",
            "sha256",
            "schema_version",
        ):
            self.assertNotIn(secret, serialized)

    async def test_post_security_fields_methods_and_stopping_precede_service(
        self,
    ) -> None:
        service = mock.Mock()
        service.create_context.return_value = RecoveryCheckContextResponse(
            case_id="11111111-1111-4111-8111-111111111111",
            action_id="22222222-2222-4222-8222-222222222222",
            expires_at="2026-07-27T10:00:00+00:00",
        )
        service.check_result.return_value = RecoveryCheckResult(
            RecoveryCheckResultState.CONFIRMED,
            "Check complete",
            "The file move was confirmed.",
            False,
        )
        self.app.state.recovery_check_service = service
        async with _client(self.app) as client:
            unauthenticated = await client.post(
                "/api/recovery/check-context",
                data={"csrf_token": "wrong"},
                headers=_origin(),
            )
            csrf = await _authenticate(client, self.token)
            wrong_origin = await client.post(
                "/api/recovery/check-context",
                data={"csrf_token": csrf},
                headers={"Origin": "http://evil.invalid"},
            )
            bad_csrf = await client.post(
                "/api/recovery/check-context",
                data={"csrf_token": "wrong"},
                headers=_origin(),
            )
            extra = await client.post(
                "/api/recovery/check-context",
                data={"csrf_token": csrf, "operation_id": "private"},
                headers=_origin(),
            )
            missing = await client.post(
                "/api/recovery/check-result",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            get_context = await client.get("/api/recovery/check-context")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(bad_csrf.status_code, 403)
        self.assertEqual(extra.status_code, 400)
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(get_context.status_code, 405)
        service.create_context.assert_not_called()
        service.check_result.assert_not_called()
        for response in (
            unauthenticated,
            wrong_origin,
            bad_csrf,
            extra,
            missing,
            get_context,
        ):
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            self.assertNotIn("private", response.text)

    async def test_stale_and_shutdown_are_bounded_conflicts(self) -> None:
        service = mock.Mock()
        service.check_result.side_effect = RecoveryCheckError(
            "recovery_details_changed"
        )
        self.app.state.recovery_check_service = service
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            stale = await client.post(
                "/api/recovery/check-result",
                data={
                    "csrf_token": csrf,
                    "case_id": "11111111-1111-4111-8111-111111111111",
                    "action_id": "22222222-2222-4222-8222-222222222222",
                },
                headers=_origin(),
            )

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(
            stale.json()["error"]["code"],
            "recovery_details_changed",
        )
        self.assertEqual(stale.headers["cache-control"], "no-store")

        self.app = create_app(
            WebAppConfig(
                self.root,
                session_secret="s" * 32,
                launch_token=self.token,
                testing=True,
            )
        )
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            self.app.state.runtime_lifecycle.begin_shutdown()
            stopping = await client.post(
                "/api/recovery/check-context",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
        self.assertEqual(stopping.status_code, 409)
        self.assertEqual(
            stopping.json()["error"]["code"],
            "application_stopping",
        )
        self.assertEqual(stopping.headers["cache-control"], "no-store")


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://127.0.0.1",
        follow_redirects=False,
    )


async def _authenticate(client: httpx.AsyncClient, token: str) -> str:
    await client.get(f"/launch/{token}", follow_redirects=False)
    shell = await client.get("/loci-flow")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text)
    assert match is not None
    return match.group(1)


def _origin() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1"}


def _root_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
