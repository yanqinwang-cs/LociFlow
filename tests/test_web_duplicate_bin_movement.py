import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from organizer.application.filesystem_operation_admission import (
    SAFE_RECOVERY_MESSAGE,
    FilesystemOperationBlockedError,
    RecoveryAdmissionState,
)
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from tests.test_application_duplicate_bin_movement_service import _approved_batch


class WebDuplicateBinMovementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.token = "e" * 32
        self.app = create_app(
            WebAppConfig(
                self.root,
                session_secret="s" * 32,
                launch_token=self.token,
                testing=True,
            )
        )
        self.batch = _approved_batch(
            self.root,
            self.app.state.duplicate_bin_batches,
            selected=1,
        )
        self.app.state.duplicate_bin_batches.create_or_recover_approved_batch(
            self.batch
        )

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_preview_recovery_block_does_not_supersede_existing_token(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            existing = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            before = self.app.state.duplicate_bin_authorizations.path.read_bytes()
            self.app.state.filesystem_operation_admission.inspect_and_require_clear = mock.Mock(
                side_effect=FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_INCONSISTENT_OPERATION_STATE
                )
            )
            blocked = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )

        self.assertEqual(existing.status_code, 200)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.headers["cache-control"], "no-store")
        self.assertEqual(blocked.json()["error"]["code"], "filesystem_operation_blocked")
        self.assertEqual(blocked.json()["error"]["message"], SAFE_RECOVERY_MESSAGE)
        self.assertEqual(
            self.app.state.duplicate_bin_authorizations.path.read_bytes(), before
        )

    async def test_recovery_block_is_safe_and_does_not_consume_confirmation(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            preview = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            payload = preview.json()
            before = self.app.state.duplicate_bin_authorizations.path.read_bytes()
            self.app.state.filesystem_operation_admission.inspect_and_require_clear = mock.Mock(
                side_effect=FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_MALFORMED_OPERATION_STATE
                )
            )
            response = await client.post(
                "/api/duplicate-bin/move",
                data={"csrf_token": csrf, "confirmation_token": payload["confirmation_token"], "confirmation": payload["confirmation"]},
                headers=_origin(),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"error": {"code": "filesystem_operation_blocked", "message": SAFE_RECOVERY_MESSAGE}})
        self.assertEqual(self.app.state.duplicate_bin_authorizations.path.read_bytes(), before)
        self.assertEqual(self.app.state.duplicate_bin_operations.list_records(), ())

    async def test_preview_and_execution_are_safe_no_store_and_replay(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            preview = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            payload = preview.json()
            wrong_confirmation = await client.post(
                "/api/duplicate-bin/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": payload["confirmation_token"],
                    "confirmation": "MOVE FILES",
                },
                headers=_origin(),
            )
            moved = await client.post(
                "/api/duplicate-bin/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": payload["confirmation_token"],
                    "confirmation": payload["confirmation"],
                },
                headers=_origin(),
            )
            replay = await client.post(
                "/api/duplicate-bin/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": payload["confirmation_token"],
                    "confirmation": payload["confirmation"],
                },
                headers=_origin(),
            )
            get_preview = await client.get("/api/duplicate-bin/move/preview")
            get_move = await client.get("/api/duplicate-bin/move")

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["cache-control"], "no-store")
        self.assertEqual(
            set(payload),
            {"copies_to_move", "confirmation", "confirmation_token", "expires_at"},
        )
        self.assertEqual(payload["confirmation"], "MOVE COPIES TO DUPLICATE BIN")
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(wrong_confirmation.status_code, 400)
        self.assertEqual(
            wrong_confirmation.json()["error"]["code"],
            "invalid_confirmation",
        )
        self.assertEqual(moved.headers["cache-control"], "no-store")
        self.assertEqual(
            replay.json()["operation"]["id"],
            moved.json()["operation"]["id"],
        )
        self.assertEqual(len(self.app.state.duplicate_bin_operations.list_records()), 1)
        self.assertEqual(get_preview.status_code, 405)
        self.assertEqual(get_move.status_code, 405)
        serialized = preview.text.lower() + moved.text.lower()
        for forbidden in (
            "sha256",
            "source",
            "destination",
            "batch_version",
            str(self.root).lower(),
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_stopping_execution_returns_safe_conflict_without_movement(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            preview = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            authorizations = (
                self.app.state.duplicate_bin_authorizations.path.read_bytes()
            )
            self.app.state.runtime_lifecycle.begin_shutdown()
            rejected_preview = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            response = await client.post(
                "/api/duplicate-bin/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": preview.json()["confirmation_token"],
                    "confirmation": preview.json()["confirmation"],
                },
                headers=_origin(),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["error"]["code"], "application_stopping")
        self.assertEqual(rejected_preview.status_code, 409)
        self.assertEqual(rejected_preview.headers["cache-control"], "no-store")
        self.assertEqual(
            self.app.state.duplicate_bin_authorizations.path.read_bytes(),
            authorizations,
        )
        self.assertEqual(self.app.state.duplicate_bin_operations.list_records(), ())

    async def test_routes_enforce_auth_origin_csrf_content_type_and_exact_fields(
        self,
    ) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.post("/api/duplicate-bin/move/preview")
            csrf = await _authenticate(client, self.token)
            no_origin = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
            )
            bad_csrf = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": "bad", "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            unsupported = await client.post(
                "/api/duplicate-bin/move/preview",
                json={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            extra_path = await client.post(
                "/api/duplicate-bin/move/preview",
                data={
                    "csrf_token": csrf,
                    "batch_id": self.batch.batch_id,
                    "source": "Inbox/copy.txt",
                },
                headers=_origin(),
            )
            duplicate = await client.post(
                "/api/duplicate-bin/move/preview",
                content=(
                    f"csrf_token={csrf}&batch_id={self.batch.batch_id}"
                    f"&batch_id={self.batch.batch_id}"
                ),
                headers={
                    **_origin(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            valid_preview = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            confirmation_token = valid_preview.json()["confirmation_token"]
            missing_execution_field = await client.post(
                "/api/duplicate-bin/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": confirmation_token,
                },
                headers=_origin(),
            )
            execution_path = await client.post(
                "/api/duplicate-bin/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": confirmation_token,
                    "confirmation": "MOVE COPIES TO DUPLICATE BIN",
                    "destination": "AI_Review/duplicate_bin/items/forged",
                },
                headers=_origin(),
            )
            duplicate_execution_field = await client.post(
                "/api/duplicate-bin/move",
                content=(
                    f"csrf_token={csrf}&confirmation_token={confirmation_token}"
                    "&confirmation=MOVE+COPIES+TO+DUPLICATE+BIN"
                    "&confirmation=MOVE+COPIES+TO+DUPLICATE+BIN"
                ),
                headers={
                    **_origin(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(no_origin.status_code, 403)
        self.assertEqual(bad_csrf.status_code, 403)
        for response in (
            unsupported,
            extra_path,
            duplicate,
            missing_execution_field,
            execution_path,
            duplicate_execution_field,
        ):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.app.state.duplicate_bin_operations.list_records(), ())

    def test_route_keeps_executor_behind_application_service(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        route = (
            repository_root / "src/organizer/web/routes/duplicate_bin_api.py"
        ).read_text(encoding="utf-8")
        service = (
            repository_root
            / "src/organizer/application/duplicate_bin_movement_service.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("organizer.executor", route)
        self.assertNotIn("apply_move_plan", route)
        self.assertIn(
            "from organizer.executor import MovementRecordingError, apply_move_plan",
            service,
        )
        for forbidden in ("shutil.move", "os.rename", "Path.rename"):
            self.assertNotIn(forbidden, service)

    async def test_activity_undo_routes_use_duplicate_restore_not_ordinary_undo(
        self,
    ) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            preview = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            moved = await client.post(
                "/api/duplicate-bin/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": preview.json()["confirmation_token"],
                    "confirmation": preview.json()["confirmation"],
                },
                headers=_origin(),
            )
            operation_id = moved.json()["operation"]["id"]

            with mock.patch(
                "organizer.web.routes.activity_api.preview_organization_undo"
            ) as ordinary_preview:
                undo_preview = await client.post(
                    f"/api/activity/{operation_id}/undo/preview",
                    data={"csrf_token": csrf},
                    headers=_origin(),
                )
            restore_preview = undo_preview.json()
            with mock.patch(
                "organizer.web.routes.activity_api.undo_organization_operation"
            ) as ordinary_undo:
                undo = await client.post(
                    f"/api/activity/{operation_id}/undo",
                    data={
                        "csrf_token": csrf,
                        "confirmation_token": restore_preview["confirmation_token"],
                        "confirmation": restore_preview["confirmation"],
                    },
                    headers=_origin(),
                )

        self.assertEqual(undo_preview.status_code, 200)
        self.assertEqual(restore_preview["confirmation"], "RESTORE COPIES")
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(undo.json()["undo"]["status"], "restored")
        ordinary_preview.assert_not_called()
        ordinary_undo.assert_not_called()
        self.assertEqual(self.app.state.organization_undos.list_records(), ())

    async def test_stopping_rejects_duplicate_activity_undo_preview(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            preview = await client.post(
                "/api/duplicate-bin/move/preview",
                data={"csrf_token": csrf, "batch_id": self.batch.batch_id},
                headers=_origin(),
            )
            moved = await client.post(
                "/api/duplicate-bin/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": preview.json()["confirmation_token"],
                    "confirmation": preview.json()["confirmation"],
                },
                headers=_origin(),
            )
            operation_id = moved.json()["operation"]["id"]
            self.app.state.runtime_lifecycle.begin_shutdown()
            with mock.patch(
                "organizer.web.routes.activity_api.preview_organization_undo"
            ) as ordinary_preview:
                rejected = await client.post(
                    f"/api/activity/{operation_id}/undo/preview",
                    data={"csrf_token": csrf},
                    headers=_origin(),
                )

        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.headers["cache-control"], "no-store")
        self.assertEqual(rejected.json()["error"]["code"], "application_stopping")
        ordinary_preview.assert_not_called()
        self.assertFalse(
            self.app.state.duplicate_bin_restores.authorization_path.exists()
        )
        self.assertEqual(self.app.state.duplicate_bin_restores.list_operations(), ())


async def _authenticate(client: httpx.AsyncClient, token: str) -> str:
    await client.get(f"/launch/{token}", follow_redirects=False)
    shell = await client.get("/loci-flow")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text)
    assert match is not None
    return match.group(1)


def _origin() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1"}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    )


if __name__ == "__main__":
    unittest.main()
