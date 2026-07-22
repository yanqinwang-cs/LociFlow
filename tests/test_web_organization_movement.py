from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
import uuid
from unittest import mock

import httpx

from organizer.application.filesystem_operation_admission import (
    FilesystemOperationBlockedError,
    RecoveryAdmissionState,
    SAFE_RECOVERY_MESSAGE,
)
from organizer.application.scan_service import scan_root
from organizer.application.volume_admission import (
    SAFE_CROSS_VOLUME_MESSAGE,
    VolumeAdmissionError,
)
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from organizer.web.scan_jobs import ScanJobSnapshot
from organizer.web.security import LaunchTokenGate


COMPLETED_AT = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


class WebOrganizationMovementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        destination = self.root / "Organized" / "EvoSim_project_slides" / "slides"
        destination.mkdir(parents=True)
        (self.root / "EvoSim_project_slides.pptx").write_bytes(b"alpha")
        (self.root / "EvoSim_project_slides_final.pptx").write_bytes(b"beta")
        self.result = scan_root(self.root)
        self.token = "m" * 32
        self.app = create_app(
            WebAppConfig(
                self.root,
                session_secret="s" * 32,
                launch_token=self.token,
                testing=True,
            )
        )
        self.app.state.scan_jobs._snapshot = ScanJobSnapshot(
            status="completed",
            job_id="scan-one",
            started_at=COMPLETED_AT,
            completed_at=COMPLETED_AT,
            result=self.result,
        )

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_preview_recovery_block_creates_no_capability_after_security(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            await _approve(client, csrf)
            self.app.state.filesystem_operation_admission.inspect_and_require_clear = mock.Mock(
                side_effect=FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                )
            )
            wrong_origin = await client.post(
                "/api/organization/move/preview", data={"csrf_token": csrf}
            )
            blocked = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )

        self.assertEqual(wrong_origin.status_code, 403)
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
        self.assertEqual(self.app.state.organization_execution._authorizations, {})

    async def test_cross_volume_preview_is_safe_no_store_and_issues_no_capability(
        self,
    ) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            await _approve(client, csrf)
            with mock.patch(
                "organizer.application.organization_movement_service."
                "OrganizationVolumeAdmission.inspect_and_require_allowed",
                side_effect=VolumeAdmissionError("cross_volume_plan"),
            ):
                response = await client.post(
                    "/api/organization/move/preview",
                    data={"csrf_token": csrf},
                    headers=_origin(),
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            response.json(),
            {
                "error": {
                    "code": "cross_volume_plan",
                    "message": SAFE_CROSS_VOLUME_MESSAGE,
                }
            },
        )
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(self.app.state.organization_execution._authorizations, {})
        self.assertEqual(self.app.state.organization_operations.list_records(), ())

    async def test_topology_change_at_confirmation_is_safe_and_consumes_token(
        self,
    ) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            await _approve(client, csrf)
            preview = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            token = preview.json()["confirmation_token"]
            with mock.patch(
                "organizer.application.execution_service."
                "OrganizationVolumeAdmission.inspect_and_require_allowed",
                side_effect=VolumeAdmissionError("cross_volume_plan"),
            ):
                response = await client.post(
                    "/api/organization/move",
                    data={
                        "csrf_token": csrf,
                        "confirmation_token": token,
                        "confirmation": "MOVE APPROVED FILES",
                    },
                    headers=_origin(),
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["error"]["code"], "cross_volume_plan")
        self.assertEqual(
            response.json()["error"]["message"], SAFE_CROSS_VOLUME_MESSAGE
        )
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(
            self.app.state.organization_execution.authorization_snapshot(token).status,
            "consumed",
        )
        self.assertEqual(self.app.state.organization_operations.list_records(), ())
        self.assertTrue((self.root / "EvoSim_project_slides.pptx").is_file())

    async def test_recovery_block_returns_safe_conflict_after_security_and_token_validation(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            await _approve(client, csrf)
            preview = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            token = preview.json()["confirmation_token"]
            self.app.state.filesystem_operation_admission.inspect_and_require_clear = mock.Mock(
                side_effect=FilesystemOperationBlockedError(
                    RecoveryAdmissionState.BLOCKED_UNFINISHED_OPERATION
                )
            )
            wrong_origin = await client.post(
                "/api/organization/move",
                data={"csrf_token": csrf, "confirmation_token": token, "confirmation": "MOVE APPROVED FILES"},
            )
            invalid_token = await client.post(
                "/api/organization/move",
                data={"csrf_token": csrf, "confirmation_token": str(uuid.uuid4()), "confirmation": "MOVE APPROVED FILES"},
                headers=_origin(),
            )
            blocked = await client.post(
                "/api/organization/move",
                data={"csrf_token": csrf, "confirmation_token": token, "confirmation": "MOVE APPROVED FILES"},
                headers=_origin(),
            )

        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(invalid_token.json()["error"]["code"], "invalid_confirmation_token")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.headers["cache-control"], "no-store")
        self.assertEqual(blocked.json(), {"error": {"code": "filesystem_operation_blocked", "message": SAFE_RECOVERY_MESSAGE}})
        self.assertEqual(self.app.state.organization_execution.authorization_snapshot(token).status, "available")
        self.assertEqual(self.app.state.organization_operations.list_records(), ())

    async def test_preview_execute_lost_response_replay_activity_and_undo_are_durable(self) -> None:
        real_move = shutil.move
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            initial = await client.get("/api/organization/current")
            no_approval = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            approved = await _approve(client, csrf)
            preview = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            before_sources = (
                (self.root / "EvoSim_project_slides.pptx").read_bytes(),
                (self.root / "EvoSim_project_slides_final.pptx").read_bytes(),
            )
            with mock.patch(
                "organizer.executor.shutil.move",
                wraps=real_move,
            ) as movement:
                moved = await client.post(
                    "/api/organization/move",
                    data={
                        "csrf_token": csrf,
                        "confirmation_token": preview.json()["confirmation_token"],
                        "confirmation": "MOVE APPROVED FILES",
                    },
                    headers=_origin(),
                )
                replay = await client.post(
                    "/api/organization/move",
                    data={
                        "csrf_token": csrf,
                        "confirmation_token": preview.json()["confirmation_token"],
                        "confirmation": "MOVE APPROVED FILES",
                    },
                    headers=_origin(),
                )
            current = await client.get("/api/organization/current")
            activity = await client.get("/api/activity")
            operation_id = moved.json()["operation"]["id"]
            undo_preview = await client.post(
                f"/api/activity/{operation_id}/undo/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            undo = await client.post(
                f"/api/activity/{operation_id}/undo",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": undo_preview.json()["confirmation_token"],
                    "confirmation": "UNDO FILE MOVE",
                },
                headers=_origin(),
            )

        self.assertEqual(initial.json()["execution"]["status"], "not_ready")
        self.assertEqual(no_approval.json()["error"]["code"], "approved_plan_not_found")
        self.assertEqual(approved.json()["execution"]["status"], "ready")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(
            set(preview.json()),
            {
                "files_to_move",
                "duplicate_copies_deferred",
                "confirmation",
                "confirmation_token",
            },
        )
        self.assertEqual(preview.json()["files_to_move"], 2)
        self.assertEqual(preview.json()["confirmation"], "MOVE APPROVED FILES")
        self.assertEqual(movement.call_count, 2)
        operation = moved.json()["operation"]
        self.assertEqual(operation["status"], "verified")
        self.assertEqual(operation["files_moved"], 2)
        self.assertEqual(replay.json()["operation"], operation)
        self.assertEqual(len(self.app.state.organization_operations.list_records()), 1)
        self.assertEqual(current.json()["execution"]["status"], "verified")
        self.assertEqual(current.json()["execution"]["operation_id"], operation["id"])
        self.assertTrue(activity.json()["activity"][0]["undoable"])
        self.assertEqual(undo.json()["undo"]["status"], "restored")
        self.assertEqual(
            (self.root / "EvoSim_project_slides.pptx").read_bytes(),
            before_sources[0],
        )
        self.assertEqual(
            (self.root / "EvoSim_project_slides_final.pptx").read_bytes(),
            before_sources[1],
        )
        for response in (preview, moved, replay, current, activity, undo):
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(str(self.root), response.text)
        self.assertNotIn("operation_log", moved.text)

    async def test_stopping_execution_returns_safe_conflict_without_movement(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            await _approve(client, csrf)
            preview = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            authorizations = dict(
                self.app.state.organization_execution._authorizations
            )
            self.app.state.runtime_lifecycle.begin_shutdown()
            rejected_preview = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            response = await client.post(
                "/api/organization/move",
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
            rejected_preview.json()["error"]["code"], "application_stopping"
        )
        self.assertEqual(
            self.app.state.organization_execution._authorizations,
            authorizations,
        )
        self.assertEqual(self.app.state.organization_operations.list_records(), ())

    async def test_routes_enforce_auth_origin_csrf_post_forms_and_server_ids_only(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.post("/api/organization/move/preview")
            csrf = await _authenticate(client, self.token)
            await _approve(client, csrf)
            missing_origin = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
            )
            bad_csrf = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": "wrong"},
                headers=_origin(),
            )
            get_preview = await client.get("/api/organization/move/preview")
            get_move = await client.get("/api/organization/move")
            wrong_type = await client.post(
                "/api/organization/move/preview",
                content="{}",
                headers={**_origin(), "Content-Type": "application/json"},
            )
            missing = await client.post(
                "/api/organization/move/preview",
                content="",
                headers={
                    **_origin(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            duplicate = await client.post(
                "/api/organization/move/preview",
                content=f"csrf_token={csrf}&csrf_token={csrf}",
                headers={
                    **_origin(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            query = await client.post(
                "/api/organization/move/preview?root=/tmp",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            browser_owned = []
            for field, value in (
                ("root", "/tmp/evil"),
                ("path", "/tmp/evil"),
                ("source", "Inbox/a.txt"),
                ("destination", "Documents/a.txt"),
                ("scan_generation", "scan-one"),
                ("expected_revision", "1"),
                ("approved_plan_digest", "fake"),
                ("operation_result", "verified"),
            ):
                browser_owned.append(
                    await client.post(
                        "/api/organization/move/preview",
                        data={"csrf_token": csrf, field: value},
                        headers=_origin(),
                    )
                )

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(missing_origin.status_code, 403)
        self.assertEqual(bad_csrf.status_code, 403)
        self.assertEqual(get_preview.status_code, 405)
        self.assertEqual(get_move.status_code, 405)
        self.assertEqual(missing.status_code, 403)
        for response in (wrong_type, duplicate, query, *browser_owned):
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.app.state.organization_operations.list_records(), ())
        self.assertTrue((self.root / "EvoSim_project_slides.pptx").is_file())

    async def test_execute_rejects_fields_malformed_unknown_and_nonexact_confirmation(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            await _approve(client, csrf)
            preview = await client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            token = preview.json()["confirmation_token"]
            wrong_confirmation = []
            for confirmation in (
                "move approved files",
                " MOVE APPROVED FILES",
                "MOVE APPROVED FILES ",
                "MOVE APPROVED FILES\n",
            ):
                wrong_confirmation.append(
                    await client.post(
                        "/api/organization/move",
                        data={
                            "csrf_token": csrf,
                            "confirmation_token": token,
                            "confirmation": confirmation,
                        },
                        headers=_origin(),
                    )
                )
            missing = await client.post(
                "/api/organization/move",
                data={"csrf_token": csrf, "confirmation_token": token},
                headers=_origin(),
            )
            extra_path = await client.post(
                "/api/organization/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": token,
                    "confirmation": "MOVE APPROVED FILES",
                    "destination": "/tmp/evil",
                },
                headers=_origin(),
            )
            malformed = await client.post(
                "/api/organization/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": "not-a-uuid",
                    "confirmation": "MOVE APPROVED FILES",
                },
                headers=_origin(),
            )
            unknown = await client.post(
                "/api/organization/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": str(uuid.uuid4()),
                    "confirmation": "MOVE APPROVED FILES",
                },
                headers=_origin(),
            )

        for response in wrong_confirmation:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_confirmation")
        for response in (missing, extra_path, malformed):
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(unknown.status_code, 409)
        self.assertEqual(unknown.json()["error"]["code"], "invalid_confirmation_token")
        self.assertEqual(self.app.state.organization_operations.list_records(), ())
        self.assertEqual(
            self.app.state.organization_execution.authorization_snapshot(token).status,
            "available",
        )

    async def test_authorization_is_invalid_after_scan_change_and_in_another_session(self) -> None:
        async with _client(self.app) as first_client:
            csrf = await _authenticate(first_client, self.token)
            await _approve(first_client, csrf)
            preview = await first_client.post(
                "/api/organization/move/preview",
                data={"csrf_token": csrf},
                headers=_origin(),
            )
            token = preview.json()["confirmation_token"]
            self.app.state.scan_jobs._snapshot = ScanJobSnapshot(
                status="completed",
                job_id="scan-two",
                started_at=COMPLETED_AT,
                completed_at=COMPLETED_AT,
                result=self.result,
            )
            stale = await first_client.post(
                "/api/organization/move",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": token,
                    "confirmation": "MOVE APPROVED FILES",
                },
                headers=_origin(),
            )

        self.app.state.scan_jobs._snapshot = ScanJobSnapshot(
            status="completed",
            job_id="scan-one",
            started_at=COMPLETED_AT,
            completed_at=COMPLETED_AT,
            result=self.result,
        )
        self.app.state.launch_token_gate = LaunchTokenGate(self.token)
        async with _client(self.app) as second_client:
            other_csrf = await _authenticate(second_client, self.token)
            other_session = await second_client.post(
                "/api/organization/move",
                data={
                    "csrf_token": other_csrf,
                    "confirmation_token": token,
                    "confirmation": "MOVE APPROVED FILES",
                },
                headers=_origin(),
            )

        self.assertEqual(stale.status_code, 409)
        self.assertIn(
            stale.json()["error"]["code"],
            {"approved_plan_not_found", "scan_generation_mismatch"},
        )
        self.assertEqual(other_session.status_code, 409)
        self.assertEqual(other_session.json()["error"]["code"], "approved_plan_not_found")
        self.assertEqual(self.app.state.organization_operations.list_records(), ())
        self.assertTrue((self.root / "EvoSim_project_slides.pptx").is_file())


async def _approve(client: httpx.AsyncClient, csrf: str):
    accepted = await client.post(
        "/api/organization/decisions",
        data={
            "csrf_token": csrf,
            "scan_generation": "scan-one",
            "expected_revision": "0",
            "kind": "bulk_recommendations",
            "decision": "accept",
            "expected_change_ids": json.dumps(["O1", "O2"]),
        },
        headers=_origin(),
    )
    assert accepted.status_code == 200, accepted.text
    approved = await client.post(
        "/api/organization/approval",
        data={
            "csrf_token": csrf,
            "scan_generation": "scan-one",
            "expected_revision": "1",
        },
        headers=_origin(),
    )
    assert approved.status_code == 200, approved.text
    return approved


async def _authenticate(client: httpx.AsyncClient, token: str) -> str:
    await client.get(f"/launch/{token}", follow_redirects=False)
    shell = await client.get("/loci-flow")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text)
    assert match is not None
    return match.group(1)


def _origin() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1"}


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://127.0.0.1",
        follow_redirects=False,
    )


if __name__ == "__main__":
    unittest.main()
