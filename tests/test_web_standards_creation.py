from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import httpx

from organizer.application.organization_slice_service import (
    build_read_only_organization_slice,
)
from organizer.application.standards_models import StandardsApplicationError
from organizer.application.standards_repository import StandardsRepository
from organizer.application.standards_service import StandardsService
from organizer.application.scan_service import scan_root
from organizer.models import FileMetadata
from organizer.organization_rules import ORGANIZATION_RULES_RELATIVE_PATH
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from organizer.web.scan_jobs import ScanJobSnapshot
from organizer.web.standards_creation_store import StandardsCreationStore


ORIGIN = {"Origin": "http://127.0.0.1"}


class WebStandardsCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.token = "g" * 32
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

    def _complete_scan(self, generation: str) -> None:
        self.app.state.scan_jobs._snapshot = ScanJobSnapshot(
            status="completed",
            job_id=generation,
            completed_at=datetime.now(timezone.utc),
            result=scan_root(self.root),
        )

    async def _issue_context(
        self,
        client: httpx.AsyncClient,
        csrf: str,
        revision: int = 0,
    ) -> dict[str, object]:
        response = await client.post(
            "/api/standards/creation-context",
            data={"csrf_token": csrf, "expected_revision": str(revision)},
            headers=ORIGIN,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_standards_list_is_read_only_and_available_while_stopping(self) -> None:
        self._complete_scan("scan-read-only")
        async with _client(self.app) as client:
            await _authenticate(client, self.token)
            with patch.object(
                self.app.state.standards_creation,
                "issue_creation_context",
                wraps=self.app.state.standards_creation.issue_creation_context,
            ) as issue:
                first = await client.get("/api/standards")
                second = await client.get("/api/standards")
                self.app.state.runtime_lifecycle.begin_shutdown()
                stopping = await client.get("/api/standards")

        self.assertEqual(issue.call_count, 0)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(stopping.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertNotIn("scope_choices", first.json())
        self.assertTrue(first.json()["creation_available"])
        self.assertFalse(stopping.json()["creation_available"])
        self.assertIn("closing", stopping.json()["creation_message"])
        self.assertEqual(stopping.headers["cache-control"], "no-store")
        self.assertEqual(self.app.state.runtime_lifecycle.snapshot().active_count, 0)

    async def test_shutdown_rejects_context_and_preview_after_security(self) -> None:
        self._complete_scan("scan-stopping")
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            context = await self._issue_context(client, csrf)
            scope_id = context["scope_choices"][0]["id"]
            self.app.state.runtime_lifecycle.begin_shutdown()
            valid_context = {
                "csrf_token": csrf,
                "expected_revision": "0",
            }
            wrong_origin = await client.post(
                "/api/standards/creation-context",
                data=valid_context,
                headers={"Origin": "http://example.invalid"},
            )
            wrong_csrf = await client.post(
                "/api/standards/creation-context",
                data={**valid_context, "csrf_token": "wrong"},
                headers=ORIGIN,
            )
            rejected_context = await client.post(
                "/api/standards/creation-context",
                data=valid_context,
                headers=ORIGIN,
            )
            rejected_preview = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_folder_unchanged",
                    "scope_id": scope_id,
                },
                headers=ORIGIN,
            )
            invalid_matcher = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_matching_files_together",
                    "scope_id": scope_id,
                    "matcher_kind": "project_code",
                    "matcher_value": "bad path",
                },
                headers=ORIGIN,
            )

        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(wrong_csrf.status_code, 403)
        for response in (rejected_context, rejected_preview):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "application_stopping")
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(invalid_matcher.status_code, 400)
        self.assertEqual(invalid_matcher.json()["error"]["code"], "invalid_matcher")
        self.app.state.standards_creation.validate_scope_reference(
            scope_id=scope_id,
            session_id=next(iter(self.app.state.standards_creation._session_contexts)),
            root_identity=self.app.state.standards.repository.configured_root_identity,
            scan_generation="scan-stopping",
            expected_revision=0,
        )
        self.assertEqual(self.app.state.runtime_lifecycle.snapshot().active_count, 0)

    async def test_shutdown_rejected_save_preserves_reusable_preview_reference(self) -> None:
        self._complete_scan("scan-save-stopping")
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            context = await self._issue_context(client, csrf)
            preview = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_folder_unchanged",
                    "scope_id": context["scope_choices"][0]["id"],
                },
                headers=ORIGIN,
            )
            self.app.state.runtime_lifecycle.begin_shutdown()
            rejected = await client.post(
                "/api/standards",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preview_token": preview.json()["preview_token"],
                },
                headers=ORIGIN,
            )

        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["error"]["code"], "application_stopping")
        self.assertFalse((self.root / ORGANIZATION_RULES_RELATIVE_PATH).exists())
        session_id = next(iter(self.app.state.standards_creation._session_contexts))
        application_preview_id = self.app.state.standards_creation.resolve_preview_token(
            token=preview.json()["preview_token"],
            session_id=session_id,
            root_identity=self.app.state.standards.repository.configured_root_identity,
            scan_generation="scan-save-stopping",
            expected_revision=0,
        )
        self.assertTrue(application_preview_id)

    async def test_server_owned_folder_preview_save_and_fresh_scan_planning(self) -> None:
        documents = self.root / "Documents"
        documents.mkdir()
        for folder in ("lectures", "recitations", "exams"):
            (self.root / "Organized" / "CS1010X" / folder).mkdir(parents=True)
        for name in (
            "CS1010X lecture 1.pdf",
            "CS1010X recitation 1.pdf",
            "CS1010X finals 2026.pdf",
        ):
            (documents / name).write_text(name, encoding="utf-8")
        self._complete_scan("scan-before")
        rules_path = self.root / ORGANIZATION_RULES_RELATIVE_PATH
        activity_before = (
            self.app.state.organization_operations.list_records(),
            self.app.state.organization_undos.list_records(),
        )

        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            listed = await client.get("/api/standards")
            context = await self._issue_context(client, csrf)
            folder = next(
                choice
                for choice in context["scope_choices"]
                if choice["label"] == "Documents"
            )
            self.assertFalse(rules_path.exists())
            preview = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_folder_unchanged",
                    "scope_id": folder["id"],
                },
                headers=ORIGIN,
            )
            self.assertFalse(rules_path.exists())
            saved = await client.post(
                "/api/standards",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preview_token": preview.json()["preview_token"],
                },
                headers=ORIGIN,
            )

        self.assertEqual(listed.status_code, 200)
        self.assertTrue(listed.json()["creation_available"])
        self.assertEqual(
            {item["kind"] for item in context["scope_choices"]},
            {"root", "folder"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["cache-control"], "no-store")
        self.assertEqual(
            set(preview.json()), {"preview_token", "expires_at", "preview"}
        )
        self.assertEqual(preview.json()["preview"]["matching_files"], 3)
        self.assertEqual(preview.json()["preview"]["scope_label"], "Documents")
        self.assertNotIn(str(self.root), preview.text)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["revision"], 1)
        self.assertTrue(saved.json()["fresh_scan_required"])
        self.assertEqual(saved.json()["save_message"], "Standard saved.")

        fresh = scan_root(self.root)
        projection = build_read_only_organization_slice(
            scan_result=fresh,
            scan_generation="scan-after",
            completed_at=datetime.now(timezone.utc),
        )
        self.assertFalse(projection.recommended_changes)
        self.assertEqual(
            len(
                [
                    item
                    for item in projection.kept_by_standards
                    if item.reason_code == "saved_standard_kept"
                ]
            ),
            3,
        )
        self.assertEqual(
            activity_before,
            (
                self.app.state.organization_operations.list_records(),
                self.app.state.organization_undos.list_records(),
            ),
        )

    async def test_matching_preview_uses_exact_fields_and_shapes_fresh_grouping(self) -> None:
        documents = self.root / "Documents"
        documents.mkdir()
        for name in ("CS1010X notes.txt", "CS1010X slides.pdf"):
            (documents / name).write_text(name, encoding="utf-8")
        self._complete_scan("scan-one")

        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            listed = await client.get("/api/standards")
            context = await self._issue_context(client, csrf)
            root_choice = next(
                item for item in context["scope_choices"] if item["kind"] == "root"
            )
            zero = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_matching_files_together",
                    "scope_id": root_choice["id"],
                    "matcher_kind": "project_code",
                    "matcher_value": "NoCurrentMatch",
                },
                headers=ORIGIN,
            )
            preview = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_matching_files_together",
                    "scope_id": root_choice["id"],
                    "matcher_kind": "course_code",
                    "matcher_value": "CS1010X",
                },
                headers=ORIGIN,
            )
            saved = await client.post(
                "/api/standards",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preview_token": preview.json()["preview_token"],
                },
                headers=ORIGIN,
            )

        self.assertEqual(zero.status_code, 200)
        self.assertEqual(zero.json()["preview"]["matching_files"], 0)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["preview"]["matching_files"], 2)
        self.assertNotIn("matcher", preview.text.lower())
        self.assertEqual(saved.status_code, 200)
        fresh = scan_root(self.root)
        group = next(
            item
            for item in fresh.report["project_groups"]
            if item["group_name"] == "CS1010X"
        )
        self.assertEqual(
            set(group["files"]),
            {"Documents/CS1010X notes.txt", "Documents/CS1010X slides.pdf"},
        )

    async def test_preview_and_save_are_strict_authenticated_and_same_origin(self) -> None:
        (self.root / "Documents").mkdir()
        self._complete_scan("scan-strict")
        async with _client(self.app) as client:
            unauthenticated = await client.post("/api/standards/preview")
            csrf = await _authenticate(client, self.token)
            listed = await client.get("/api/standards")
            context = await self._issue_context(client, csrf)
            scope_id = context["scope_choices"][0]["id"]
            valid = {
                "csrf_token": csrf,
                "expected_revision": "0",
                "preset_type": "keep_matching_files_together",
                "scope_id": scope_id,
                "matcher_kind": "project_code",
                "matcher_value": "BootAI",
            }
            no_origin = await client.post("/api/standards/preview", data=valid)
            json_request = await client.post(
                "/api/standards/preview", json=valid, headers=ORIGIN
            )
            path_field = await client.post(
                "/api/standards/preview",
                data={**valid, "path": "/tmp/private"},
                headers=ORIGIN,
            )
            duplicate = await client.post(
                "/api/standards/preview",
                content=urlencode(valid) + "&matcher_value=Other",
                headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
            )
            glob = await client.post(
                "/api/standards/preview",
                data={**valid, "matcher_value": "Boot*"},
                headers=ORIGIN,
            )
            regex = await client.post(
                "/api/standards/preview",
                data={**valid, "matcher_value": "Boot.+"},
                headers=ORIGIN,
            )
            valid_preview = await client.post(
                "/api/standards/preview", data=valid, headers=ORIGIN
            )
            newer_preview = await client.post(
                "/api/standards/preview",
                data={**valid, "matcher_value": "EvoSim"},
                headers=ORIGIN,
            )
            superseded = await client.post(
                "/api/standards",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preview_token": valid_preview.json()["preview_token"],
                },
                headers=ORIGIN,
            )
            bad_save = await client.post(
                "/api/standards",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preview_token": valid_preview.json()["preview_token"],
                    "preset_type": "keep_folder_unchanged",
                },
                headers=ORIGIN,
            )

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(no_origin.status_code, 403)
        for response in (json_request, path_field, duplicate, bad_save):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["cache-control"], "no-store")
        for response in (glob, regex):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_matcher")
        self.assertEqual(valid_preview.status_code, 200)
        self.assertEqual(newer_preview.status_code, 200)
        self.assertEqual(superseded.status_code, 409)
        self.assertEqual(superseded.json()["error"]["code"], "preview_superseded")

    async def test_scope_and_preview_tokens_are_scan_and_session_bound(self) -> None:
        (self.root / "Documents").mkdir()
        self._complete_scan("scan-old")
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            listed = await client.get("/api/standards")
            context = await self._issue_context(client, csrf)
            scope_id = context["scope_choices"][0]["id"]
            first = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_folder_unchanged",
                    "scope_id": scope_id,
                },
                headers=ORIGIN,
            )
            self._complete_scan("scan-new")
            stale_scope = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_folder_unchanged",
                    "scope_id": scope_id,
                },
                headers=ORIGIN,
            )
            stale_preview = await client.post(
                "/api/standards",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preview_token": first.json()["preview_token"],
                },
                headers=ORIGIN,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(stale_scope.status_code, 409)
        self.assertEqual(stale_scope.json()["error"]["code"], "folder_scope_stale")
        self.assertEqual(stale_preview.status_code, 409)
        self.assertEqual(stale_preview.json()["error"]["code"], "preview_expired")

    async def test_active_plan_makes_creation_unavailable_and_preview_rejects(self) -> None:
        (self.root / "Documents").mkdir()
        self._complete_scan("scan-blocked")
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            with patch.object(
                self.app.state.standards,
                "ensure_creation_available",
                side_effect=StandardsApplicationError("active_plan_conflict"),
            ):
                listed = await client.get("/api/standards")
                context = await client.post(
                    "/api/standards/creation-context",
                    data={
                        "csrf_token": csrf,
                        "expected_revision": "0",
                    },
                    headers=ORIGIN,
                )

        self.assertEqual(listed.status_code, 200)
        self.assertFalse(listed.json()["creation_available"])
        self.assertIn("Finish or restart", listed.json()["creation_message"])
        self.assertEqual(context.status_code, 409)
        self.assertEqual(context.json()["error"]["code"], "active_plan_conflict")

    async def test_scope_registry_filters_unsafe_folders_and_revalidates_ownership(self) -> None:
        safe = self.root / "Safe"
        protected = self.root / "AI_Review"
        safe.mkdir()
        protected.mkdir()
        missing = self.root / "Missing"
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory)
            symlink = self.root / "Linked"
            symlink.symlink_to(outside, target_is_directory=True)
            result = scan_root(self.root)
            extras = (
                FileMetadata(protected, Path("AI_Review"), "AI_Review", "", 0, 0, True),
                FileMetadata(symlink, Path("Linked"), "Linked", "", 0, 0, True),
                FileMetadata(missing, Path("Missing"), "Missing", "", 0, 0, True),
            )
            result = replace(result, metadata_items=(*result.metadata_items, *extras))
            store = StandardsCreationStore(self.root)
            context = store.issue_creation_context(
                session_id="session-one",
                root_identity="root-one",
                scan_generation="scan-owned",
                expected_revision=4,
                metadata_items=result.metadata_items,
            )
            choices = context.scope_choices

            self.assertIn("Safe", {choice.label for choice in choices})
            self.assertFalse(
                {"AI_Review", "Linked", "Missing"} & {choice.label for choice in choices}
            )
            safe_choice = next(choice for choice in choices if choice.label == "Safe")
            for session_id, root_identity in (
                ("session-two", "root-one"),
                ("session-one", "root-two"),
            ):
                with self.subTest(
                    session_id=session_id, root_identity=root_identity
                ), self.assertRaisesRegex(
                    StandardsApplicationError, "folder_scope_stale"
                ):
                    store.resolve_scope(
                        scope_id=safe_choice.id,
                        session_id=session_id,
                        root_identity=root_identity,
                        scan_generation="scan-owned",
                        expected_revision=4,
                        metadata_items=result.metadata_items,
                    )
            safe.rmdir()
            with self.assertRaisesRegex(
                StandardsApplicationError, "folder_scope_stale"
            ):
                store.resolve_scope(
                    scope_id=safe_choice.id,
                    session_id="session-one",
                    root_identity="root-one",
                    scan_generation="scan-owned",
                    expected_revision=4,
                    metadata_items=result.metadata_items,
                )

    async def test_v1_preview_is_read_only_and_first_save_migrates_once(self) -> None:
        rules_path = self.root / ORGANIZATION_RULES_RELATIVE_PATH
        rules_path.parent.mkdir(parents=True)
        rules_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locked_anchors": ["CS1010X"],
                    "ignored_terms": ["summary"],
                }
            ),
            encoding="utf-8",
        )
        before = rules_path.read_bytes()
        self._complete_scan("scan-v1")
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            listed = await client.get("/api/standards")
            context = await self._issue_context(client, csrf)
            scope_id = next(
                item["id"]
                for item in context["scope_choices"]
                if item["kind"] == "root"
            )
            preview = await client.post(
                "/api/standards/preview",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preset_type": "keep_matching_files_together",
                    "scope_id": scope_id,
                    "matcher_kind": "project_code",
                    "matcher_value": "BootAI",
                },
                headers=ORIGIN,
            )
            self.assertEqual(rules_path.read_bytes(), before)
            saved = await client.post(
                "/api/standards",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preview_token": preview.json()["preview_token"],
                },
                headers=ORIGIN,
            )
            repeated = await client.post(
                "/api/standards",
                data={
                    "csrf_token": csrf,
                    "expected_revision": "0",
                    "preview_token": preview.json()["preview_token"],
                },
                headers=ORIGIN,
            )

        catalog = json.loads(rules_path.read_text(encoding="utf-8"))
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(saved.json()["revision"], 1)
        self.assertEqual(repeated.json()["revision"], 1)
        self.assertEqual(catalog["version"], 2)
        self.assertEqual(catalog["revision"], 1)
        self.assertEqual(len(catalog["standards"]), 2)
        self.assertNotIn('"summary"', rules_path.read_text(encoding="utf-8"))
        restarted = StandardsService(StandardsRepository(self.root)).list_standards()
        self.assertEqual(restarted.revision, 1)
        self.assertEqual(len([item for item in restarted.items if item.mutable]), 2)


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
