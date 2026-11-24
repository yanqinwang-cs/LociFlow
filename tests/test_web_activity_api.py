from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import httpx

from organizer.application.filesystem_operation_admission import (
    FilesystemOperationBlockedError,
    RecoveryAdmissionState,
    SAFE_RECOVERY_MESSAGE,
)
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from tests.test_application_undo_service import _verified_operation


class WebActivityApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_recovery_block_creates_no_undo_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, _, _, record = _verified_operation(root, 1)
            app = _app(root)
            async with _client(app) as client:
                csrf = await _authenticate(client)
                app.state.filesystem_operation_admission.inspect_and_require_clear = mock.Mock(
                    side_effect=FilesystemOperationBlockedError(
                        RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                    )
                )
                blocked = await client.post(
                    f"/api/activity/{record.operation_id}/undo/preview",
                    data={"csrf_token": csrf},
                    headers={"Origin": "http://127.0.0.1"},
                )

            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.headers["cache-control"], "no-store")
            self.assertEqual(
                blocked.json(),
                {
                    "error": {
                        "code": "filesystem_operation_blocked",
                        "message": SAFE_RECOVERY_MESSAGE,
                    }
                },
            )
            self.assertEqual(app.state.organization_execution._undo_authorizations, {})
            self.assertEqual(app.state.organization_undos.list_records(), ())

    async def test_recovery_block_returns_safe_conflict_without_consuming_undo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, _, _, record = _verified_operation(root, 1)
            app = _app(root)
            async with _client(app) as client:
                csrf = await _authenticate(client)
                preview = await client.post(
                    f"/api/activity/{record.operation_id}/undo/preview",
                    data={"csrf_token": csrf},
                    headers={"Origin": "http://127.0.0.1"},
                )
                token = preview.json()["confirmation_token"]
                app.state.filesystem_operation_admission.inspect_and_require_clear = mock.Mock(
                    side_effect=FilesystemOperationBlockedError(
                        RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                    )
                )
                response = await client.post(
                    f"/api/activity/{record.operation_id}/undo",
                    data={"csrf_token": csrf, "confirmation_token": token, "confirmation": "UNDO FILE MOVE"},
                    headers={"Origin": "http://127.0.0.1"},
                )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json(), {"error": {"code": "filesystem_operation_blocked", "message": SAFE_RECOVERY_MESSAGE}})
            self.assertEqual(app.state.organization_execution.undo_authorization_snapshot(token).status, "available")
            self.assertEqual(app.state.organization_undos.list_records(), ())

    async def test_activity_requires_auth_rejects_queries_and_returns_empty_no_store_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app = _app(root)
            async with _client(app) as client:
                unauthenticated = await client.get("/api/activity")
                await client.get(f"/launch/{'t' * 32}")
                empty = await client.get("/api/activity")
                query = await client.get("/api/activity?root=/tmp")

            self.assertEqual(unauthenticated.status_code, 403)
            self.assertEqual(empty.json(), {"activity": []})
            self.assertEqual(empty.headers["cache-control"], "no-store")
            self.assertIn("application/json", empty.headers["content-type"])
            self.assertEqual(query.status_code, 400)
            self.assertNotIn(str(root), empty.text)

    async def test_preview_and_execute_restore_through_strict_same_origin_forms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, _, _, record = _verified_operation(root, 2)
            app = _app(root)
            async with _client(app) as client:
                csrf = await _authenticate(client)
                activity = await client.get("/api/activity")
                preview = await client.post(
                    f"/api/activity/{record.operation_id}/undo/preview",
                    data={"csrf_token": csrf},
                    headers={"Origin": "http://127.0.0.1"},
                )
                payload = preview.json()
                executed = await client.post(
                    f"/api/activity/{record.operation_id}/undo",
                    data={
                        "csrf_token": csrf,
                        "confirmation_token": payload["confirmation_token"],
                        "confirmation": "UNDO FILE MOVE",
                    },
                    headers={"Origin": "http://127.0.0.1"},
                )
                updated = await client.get("/api/activity")

            self.assertTrue(activity.json()["activity"][0]["undoable"])
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(payload["files_to_restore"], 2)
            self.assertEqual(executed.status_code, 200)
            self.assertEqual(executed.json()["undo"]["status"], "restored")
            self.assertIn("restore", {item["type"] for item in updated.json()["activity"]})
            self.assertFalse(any(item["undoable"] for item in updated.json()["activity"]))
            self.assertNotIn(str(root), updated.text)

    async def test_undo_routes_enforce_origin_csrf_fields_content_type_and_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, _, _, record = _verified_operation(root, 1)
            app = _app(root)
            path = f"/api/activity/{record.operation_id}/undo/preview"
            async with _client(app) as client:
                csrf = await _authenticate(client)
                missing_origin = await client.post(path, data={"csrf_token": csrf})
                bad_csrf = await client.post(
                    path,
                    data={"csrf_token": "wrong"},
                    headers={"Origin": "http://127.0.0.1"},
                )
                unknown = await client.post(
                    path,
                    data={"csrf_token": csrf, "log_path": "/tmp/evil"},
                    headers={"Origin": "http://127.0.0.1"},
                )
                duplicate = await client.post(
                    path,
                    content=f"csrf_token={csrf}&csrf_token={csrf}",
                    headers={
                        "Origin": "http://127.0.0.1",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                wrong_type = await client.post(
                    path,
                    content="{}",
                    headers={"Origin": "http://127.0.0.1", "Content-Type": "application/json"},
                )
                invalid_id = await client.post(
                    "/api/activity/NOT-A-UUID/undo/preview",
                    data={"csrf_token": csrf},
                    headers={"Origin": "http://127.0.0.1"},
                )
                query = await client.post(
                    f"{path}?path=/tmp",
                    data={"csrf_token": csrf},
                    headers={"Origin": "http://127.0.0.1"},
                )
                oversized = await client.post(
                    path,
                    content="csrf_token=" + ("x" * 17_000),
                    headers={
                        "Origin": "http://127.0.0.1",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )

            self.assertEqual(missing_origin.status_code, 403)
            self.assertEqual(bad_csrf.status_code, 403)
            for response in (unknown, duplicate, wrong_type, query, oversized):
                self.assertEqual(response.status_code, 400)
            self.assertEqual(invalid_id.status_code, 404)
            self.assertEqual(app.state.organization_undos.list_records(), ())

    async def test_stopping_execution_returns_safe_conflict_without_undo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, _, _, record = _verified_operation(root, 1)
            app = _app(root)
            async with _client(app) as client:
                csrf = await _authenticate(client)
                preview = await client.post(
                    f"/api/activity/{record.operation_id}/undo/preview",
                    data={"csrf_token": csrf},
                    headers={"Origin": "http://127.0.0.1"},
                )
                authorizations = dict(
                    app.state.organization_execution._undo_authorizations
                )
                app.state.runtime_lifecycle.begin_shutdown()
                rejected_preview = await client.post(
                    f"/api/activity/{record.operation_id}/undo/preview",
                    data={"csrf_token": csrf},
                    headers={"Origin": "http://127.0.0.1"},
                )
                response = await client.post(
                    f"/api/activity/{record.operation_id}/undo",
                    data={
                        "csrf_token": csrf,
                        "confirmation_token": preview.json()["confirmation_token"],
                        "confirmation": "UNDO FILE MOVE",
                    },
                    headers={"Origin": "http://127.0.0.1"},
                )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(
                response.json()["error"]["code"],
                "application_stopping",
            )
            self.assertEqual(rejected_preview.status_code, 409)
            self.assertEqual(rejected_preview.headers["cache-control"], "no-store")
            self.assertEqual(
                app.state.organization_execution._undo_authorizations,
                authorizations,
            )
            self.assertEqual(app.state.organization_undos.list_records(), ())


def _app(root: Path):
    return create_app(WebAppConfig(
        root,
        session_secret="s" * 32,
        launch_token="t" * 32,
        testing=True,
    ))


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


if __name__ == "__main__":
    unittest.main()
