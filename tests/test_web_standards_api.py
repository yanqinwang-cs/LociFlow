from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx

from organizer.application.standards_models import StandardsApplicationError
from organizer.organization_rules import (
    ORGANIZATION_RULES_RELATIVE_PATH,
    STANDARD_KEEP_MATCHING_FILES_TOGETHER,
)
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig


class WebStandardsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.token = "t" * 32
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

    def _saved_standard(self) -> tuple[str, int]:
        service = self.app.state.standards
        preview = service.preview(
            preset_type=STANDARD_KEEP_MATCHING_FILES_TOGETHER,
            scope=service.resolve_root_scope(),
            expected_revision=0,
            matcher_kind="project_code",
            matcher_value="EvoSim",
        )
        result = service.save_preview(preview.preview_id, expected_revision=0)
        return result.standard.id, result.catalog_revision

    async def test_authenticated_list_is_no_store_and_exposes_only_safe_projection(self) -> None:
        self._saved_standard()
        async with _client(self.app) as client:
            unauthenticated = await client.get("/api/standards")
            await _authenticate(client, self.token)
            response = await client.get("/api/standards")
            query = await client.get("/api/standards?path=/tmp/private")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            set(response.json()),
            {
                "revision", "items", "presets", "scope_choices",
                "creation_available", "creation_message",
            },
        )
        self.assertEqual(
            {item["id"] for item in response.json()["presets"]},
            {"keep_folder_unchanged", "keep_matching_files_together"},
        )
        self.assertFalse(response.json()["creation_available"])
        self.assertEqual(response.json()["scope_choices"], [])
        self.assertIn("Check files", response.json()["creation_message"])
        self.assertTrue(response.json()["items"])
        expected_fields = {
            "id",
            "name",
            "description",
            "active",
            "mutable",
            "scope_label",
            "effect_summary",
            "preset_type",
            "attention_message",
        }
        self.assertTrue(
            all(set(item) == expected_fields for item in response.json()["items"])
        )
        for forbidden in (
            str(self.root),
            "configured_root_identity",
            "relative_path",
            "parameters",
            "matcher_value",
            "record_revision",
            "created_at",
        ):
            self.assertNotIn(forbidden, response.text)
        self.assertEqual(query.status_code, 400)

    async def test_state_route_is_strict_authenticated_same_origin_urlencoded_form(self) -> None:
        standard_id, revision = self._saved_standard()
        path = "/api/standards/state"
        async with _client(self.app) as client:
            unauthenticated = await client.post(path)
            csrf = await _authenticate(client, self.token)
            valid = {
                "csrf_token": csrf,
                "expected_revision": str(revision),
                "standard_id": standard_id,
                "active": "false",
            }
            missing_origin = await client.post(path, data=valid)
            bad_csrf = await client.post(
                path,
                data={**valid, "csrf_token": "wrong"},
                headers={"Origin": "http://127.0.0.1"},
            )
            wrong_type = await client.post(
                path,
                json=valid,
                headers={"Origin": "http://127.0.0.1"},
            )
            extra = await client.post(
                path,
                data={**valid, "path": "/tmp/private"},
                headers={"Origin": "http://127.0.0.1"},
            )
            missing = await client.post(
                path,
                data={key: value for key, value in valid.items() if key != "active"},
                headers={"Origin": "http://127.0.0.1"},
            )
            duplicate = await client.post(
                path,
                content=(
                    f"csrf_token={csrf}&expected_revision={revision}"
                    f"&standard_id={standard_id}&active=false&active=true"
                ),
                headers={
                    "Origin": "http://127.0.0.1",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            uppercase = await client.post(
                path,
                data={**valid, "active": "False"},
                headers={"Origin": "http://127.0.0.1"},
            )
            query = await client.post(
                f"{path}?debug=true",
                data=valid,
                headers={"Origin": "http://127.0.0.1"},
            )
            success = await client.post(
                path,
                data=valid,
                headers={"Origin": "http://127.0.0.1"},
            )

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(missing_origin.status_code, 403)
        self.assertEqual(bad_csrf.status_code, 403)
        for response in (wrong_type, extra, missing, duplicate, uppercase, query):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.headers["cache-control"], "no-store")
        self.assertEqual(
            set(success.json()),
            {"revision", "items", "fresh_scan_required"},
        )
        self.assertTrue(success.json()["fresh_scan_required"])
        saved = next(item for item in success.json()["items"] if item["id"] == standard_id)
        self.assertFalse(saved["active"])

    async def test_typed_application_errors_are_safe_and_do_not_write_activity(self) -> None:
        standard_id, revision = self._saved_standard()
        cases = {
            "standard_revision_mismatch": (409, "Standards changed."),
            "standard_conflict": (409, "conflicts"),
            "active_plan_conflict": (409, "Finish or restart"),
            "standards_storage_unavailable": (409, "could not be loaded"),
            "unknown_standard": (404, "no longer available"),
            "immutable_standard": (409, "cannot be paused"),
        }
        activity_before = (
            self.app.state.organization_operations.list_records(),
            self.app.state.organization_undos.list_records(),
        )
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            for code, (status, message) in cases.items():
                with self.subTest(code=code), patch.object(
                    self.app.state.standards,
                    "set_active",
                    side_effect=StandardsApplicationError(code),
                ):
                    response = await client.post(
                        "/api/standards/state",
                        data={
                            "csrf_token": csrf,
                            "expected_revision": str(revision),
                            "standard_id": standard_id,
                            "active": "false",
                        },
                        headers={"Origin": "http://127.0.0.1"},
                    )
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json()["error"]["code"], code)
                    self.assertIn(message, response.json()["error"]["message"])
                    self.assertEqual(response.headers["cache-control"], "no-store")

        activity_after = (
            self.app.state.organization_operations.list_records(),
            self.app.state.organization_undos.list_records(),
        )
        self.assertEqual(activity_after, activity_before)

    async def test_built_in_is_immutable_and_stale_revision_is_rejected(self) -> None:
        standard_id, revision = self._saved_standard()
        built_in_id = next(
            item.id
            for item in self.app.state.standards.list_standards().items
            if not item.mutable
        )
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            common = {
                "csrf_token": csrf,
                "active": "false",
            }
            immutable = await client.post(
                "/api/standards/state",
                data={
                    **common,
                    "expected_revision": str(revision),
                    "standard_id": built_in_id,
                },
                headers={"Origin": "http://127.0.0.1"},
            )
            stale = await client.post(
                "/api/standards/state",
                data={
                    **common,
                    "expected_revision": str(revision - 1),
                    "standard_id": standard_id,
                },
                headers={"Origin": "http://127.0.0.1"},
            )

        self.assertEqual(immutable.status_code, 409)
        self.assertEqual(immutable.json()["error"]["code"], "immutable_standard")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "standard_revision_mismatch")

    async def test_v1_saved_standard_is_mutable_and_pause_state_survives_restart(self) -> None:
        path = self.root / ORGANIZATION_RULES_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"version": 1, "locked_anchors": ["CS1010X"]}',
            encoding="utf-8",
        )
        activity_before = (
            self.app.state.organization_operations.list_records(),
            self.app.state.organization_undos.list_records(),
        )
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            initial = await client.get("/api/standards")
            saved = next(
                item
                for item in initial.json()["items"]
                if item["preset_type"] == "legacy_locked_anchor"
            )
            built_ins = [
                item
                for item in initial.json()["items"]
                if item["preset_type"] in {"built_in_protection", "existing_folder_policy"}
            ]
            paused = await client.post(
                "/api/standards/state",
                data={
                    "csrf_token": csrf,
                    "expected_revision": str(initial.json()["revision"]),
                    "standard_id": saved["id"],
                    "active": "false",
                },
                headers={"Origin": "http://127.0.0.1"},
            )

        self.assertTrue(saved["active"])
        self.assertTrue(saved["mutable"])
        self.assertTrue(all(item["active"] and not item["mutable"] for item in built_ins))
        paused_saved = next(
            item for item in paused.json()["items"] if item["id"] == saved["id"]
        )
        self.assertFalse(paused_saved["active"])
        self.assertTrue(paused_saved["mutable"])
        self.assertTrue(paused.json()["fresh_scan_required"])

        restarted = create_app(
            WebAppConfig(
                self.root,
                session_secret="r" * 32,
                launch_token="u" * 32,
                testing=True,
            )
        )
        async with _client(restarted) as client:
            csrf = await _authenticate(client, "u" * 32)
            restored = await client.get("/api/standards")
            restored_saved = next(
                item for item in restored.json()["items"] if item["id"] == saved["id"]
            )
            reactivated = await client.post(
                "/api/standards/state",
                data={
                    "csrf_token": csrf,
                    "expected_revision": str(restored.json()["revision"]),
                    "standard_id": saved["id"],
                    "active": "true",
                },
                headers={"Origin": "http://127.0.0.1"},
            )

        self.assertFalse(restored_saved["active"])
        self.assertTrue(restored_saved["mutable"])
        reactivated_saved = next(
            item for item in reactivated.json()["items"] if item["id"] == saved["id"]
        )
        self.assertTrue(reactivated_saved["active"])
        self.assertTrue(reactivated_saved["mutable"])
        activity_after = (
            self.app.state.organization_operations.list_records(),
            self.app.state.organization_undos.list_records(),
        )
        self.assertEqual(activity_after, activity_before)


async def _authenticate(client: httpx.AsyncClient, token: str) -> str:
    await client.get(f"/launch/{token}", follow_redirects=False)
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
