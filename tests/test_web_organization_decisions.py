from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest

import httpx

from organizer.application.scan_service import scan_root
from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from organizer.web.scan_jobs import ScanJobSnapshot


COMPLETED_AT = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


class WebOrganizationDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        destination = self.root / "Organized" / "EvoSim_project_slides" / "slides"
        destination.mkdir(parents=True)
        (self.root / "EvoSim_project_slides.pptx").write_text("a", encoding="utf-8")
        (self.root / "EvoSim_project_slides_final.pptx").write_text("b", encoding="utf-8")
        self.result = scan_root(self.root)
        self.token = "d" * 32
        self.app = create_app(
            WebAppConfig(
                self.root,
                session_secret="s" * 32,
                launch_token=self.token,
                testing=True,
            )
        )
        self._complete("scan-one")

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_get_has_implicit_revision_zero_and_post_survives_reload(self) -> None:
        async with _client(self.app) as client:
            csrf = await self._authenticate(client)
            initial = await client.get("/api/organization/current")
            repeated = await client.get("/api/organization/current")
            changed = await client.post(
                "/api/organization/decisions",
                data={
                    "csrf_token": csrf,
                    "scan_generation": "scan-one",
                    "expected_revision": "0",
                    "kind": "recommendation",
                    "change_id": "O1",
                    "decision": "accept",
                },
                headers=_origin(),
            )
            reloaded = await client.get("/api/organization/current")

        self.assertEqual(initial.json()["review"]["decision_revision"], 0)
        self.assertEqual(initial.json(), repeated.json())
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["review"]["decision_revision"], 1)
        self.assertEqual(
            reloaded.json()["review"]["recommendations"][0]["decision"],
            "accept",
        )
        self.assertEqual(reloaded.headers["cache-control"], "no-store")

    async def test_generation_revision_unknown_id_and_query_fail_without_mutation(self) -> None:
        async with _client(self.app) as client:
            csrf = await self._authenticate(client)
            base = {
                "csrf_token": csrf,
                "scan_generation": "scan-one",
                "expected_revision": "0",
                "kind": "recommendation",
                "change_id": "O1",
                "decision": "accept",
            }
            stale_generation = await client.post(
                "/api/organization/decisions",
                data={**base, "scan_generation": "old"},
                headers=_origin(),
            )
            generation_precedes_kind = await client.post(
                "/api/organization/decisions",
                data={**base, "scan_generation": "old", "kind": "unsupported"},
                headers=_origin(),
            )
            first = await client.post(
                "/api/organization/decisions",
                data=base,
                headers=_origin(),
            )
            stale_revision = await client.post(
                "/api/organization/decisions",
                data={**base, "change_id": "O2"},
                headers=_origin(),
            )
            revision_precedes_kind = await client.post(
                "/api/organization/decisions",
                data={**base, "kind": "unsupported"},
                headers=_origin(),
            )
            unknown = await client.post(
                "/api/organization/decisions",
                data={**base, "expected_revision": "1", "change_id": "O99"},
                headers=_origin(),
            )
            query = await client.post(
                "/api/organization/decisions?root=/tmp",
                data={**base, "expected_revision": "1"},
                headers=_origin(),
            )
            current = await client.get("/api/organization/current")

        self.assertEqual(stale_generation.json()["error"]["code"], "scan_generation_mismatch")
        self.assertEqual(
            generation_precedes_kind.json()["error"]["code"],
            "scan_generation_mismatch",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(stale_revision.json()["error"]["code"], "decision_revision_mismatch")
        self.assertEqual(
            revision_precedes_kind.json()["error"]["code"],
            "decision_revision_mismatch",
        )
        self.assertEqual(unknown.json()["error"]["code"], "unknown_change_id")
        self.assertEqual(query.json()["error"]["code"], "invalid_query")
        self.assertEqual(current.json()["review"]["decision_revision"], 1)

    async def test_bulk_accept_requires_exact_pending_set_and_increments_once(self) -> None:
        async with _client(self.app) as client:
            csrf = await self._authenticate(client)
            wrong = await client.post(
                "/api/organization/decisions",
                data={
                    "csrf_token": csrf,
                    "scan_generation": "scan-one",
                    "expected_revision": "0",
                    "kind": "bulk_recommendations",
                    "decision": "accept",
                    "expected_change_ids": json.dumps(["O1"]),
                },
                headers=_origin(),
            )
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

        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(wrong.json()["error"]["code"], "recommendation_set_changed")
        review = accepted.json()["review"]
        self.assertEqual(review["decision_revision"], 1)
        self.assertEqual(review["summary"]["files_to_move"], 2)
        self.assertTrue(review["summary"]["ready_for_approval"])

    async def test_approval_is_executor_free_ready_and_exact_replay_is_idempotent(self) -> None:
        async with _client(self.app) as client:
            csrf = await self._authenticate(client)
            await self._bulk(client, csrf)
            approved = await client.post(
                "/api/organization/approval",
                data={
                    "csrf_token": csrf,
                    "scan_generation": "scan-one",
                    "expected_revision": "1",
                },
                headers=_origin(),
            )
            replay = await client.post(
                "/api/organization/approval",
                data={
                    "csrf_token": csrf,
                    "scan_generation": "scan-one",
                    "expected_revision": "1",
                },
                headers=_origin(),
            )
            mutation = await client.post(
                "/api/organization/decisions",
                data={
                    "csrf_token": csrf,
                    "scan_generation": "scan-one",
                    "expected_revision": "1",
                    "kind": "recommendation",
                    "change_id": "O1",
                    "decision": "keep",
                },
                headers=_origin(),
            )

        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["review"]["status"], "ready_to_move")
        self.assertEqual(approved.json(), replay.json())
        self.assertEqual(mutation.json()["error"]["code"], "approval_already_completed")
        self.assertFalse((self.root / "AI_Review" / "operation_logs").exists())
        self.assertTrue((self.root / "EvoSim_project_slides.pptx").exists())

    async def test_approval_rejects_changed_source_and_existing_destination(self) -> None:
        async with _client(self.app) as client:
            csrf = await self._authenticate(client)
            await self._bulk(client, csrf)
            (self.root / "EvoSim_project_slides.pptx").write_text("changed", encoding="utf-8")
            changed = await client.post(
                "/api/organization/approval",
                data={
                    "csrf_token": csrf,
                    "scan_generation": "scan-one",
                    "expected_revision": "1",
                },
                headers=_origin(),
            )
        self.assertEqual(changed.json()["error"]["code"], "source_changed")

        self.directory.cleanup()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        destination = self.root / "Organized" / "EvoSim_project_slides" / "slides"
        destination.mkdir(parents=True)
        (self.root / "EvoSim_project_slides.pptx").write_text("a", encoding="utf-8")
        (self.root / "EvoSim_project_slides_final.pptx").write_text("b", encoding="utf-8")
        self.result = scan_root(self.root)
        self.token = "x" * 32
        self.app = create_app(WebAppConfig(self.root, "s" * 32, self.token, testing=True))
        self._complete("scan-one")
        async with _client(self.app) as client:
            csrf = await self._authenticate(client)
            await self._bulk(client, csrf)
            destination_file = destination / "EvoSim_project_slides.pptx"
            destination_file.write_text("collision", encoding="utf-8")
            collision = await client.post(
                "/api/organization/approval",
                data={
                    "csrf_token": csrf,
                    "scan_generation": "scan-one",
                    "expected_revision": "1",
                },
                headers=_origin(),
            )
        self.assertEqual(collision.json()["error"]["code"], "overwrite_detected")

    async def test_mutation_routes_enforce_auth_origin_csrf_and_strict_forms(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.post("/api/organization/decisions")
            unauthenticated_approval = await client.post("/api/organization/approval")
            csrf = await self._authenticate(client)
            valid = {
                "csrf_token": csrf,
                "scan_generation": "scan-one",
                "expected_revision": "0",
                "kind": "recommendation",
                "change_id": "O1",
                "decision": "accept",
            }
            no_origin = await client.post("/api/organization/decisions", data=valid)
            bad_csrf = await client.post(
                "/api/organization/decisions",
                data={**valid, "csrf_token": "wrong"},
                headers=_origin(),
            )
            unknown_field = await client.post(
                "/api/organization/decisions",
                data={**valid, "destination": "/tmp/attack"},
                headers=_origin(),
            )
            wrong_type = await client.post(
                "/api/organization/decisions",
                content=json.dumps(valid),
                headers={**_origin(), "Content-Type": "application/json"},
            )
            duplicate_field = await client.post(
                "/api/organization/decisions",
                content=(
                    "csrf_token=" + csrf
                    + "&csrf_token=" + csrf
                    + "&scan_generation=scan-one&expected_revision=0"
                    + "&kind=recommendation&change_id=O1&decision=accept"
                ),
                headers={**_origin(), "Content-Type": "application/x-www-form-urlencoded"},
            )
            oversized = await client.post(
                "/api/organization/decisions",
                content="kind=" + ("x" * 17_000),
                headers={**_origin(), "Content-Type": "application/x-www-form-urlencoded"},
            )
            approval = {
                "csrf_token": csrf,
                "scan_generation": "scan-one",
                "expected_revision": "0",
            }
            approval_no_origin = await client.post(
                "/api/organization/approval",
                data=approval,
            )
            approval_bad_csrf = await client.post(
                "/api/organization/approval",
                data={**approval, "csrf_token": "wrong"},
                headers=_origin(),
            )
            approval_unknown_field = await client.post(
                "/api/organization/approval",
                data={**approval, "summary": "forged"},
                headers=_origin(),
            )
            decision_get = await client.get("/api/organization/decisions")
            approval_get = await client.get("/api/organization/approval")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(unauthenticated_approval.status_code, 403)
        self.assertEqual(no_origin.status_code, 403)
        self.assertEqual(bad_csrf.status_code, 403)
        for response in (unknown_field, wrong_type, duplicate_field, oversized):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(approval_no_origin.status_code, 403)
        self.assertEqual(approval_bad_csrf.status_code, 403)
        self.assertEqual(approval_unknown_field.status_code, 400)
        self.assertEqual(decision_get.status_code, 405)
        self.assertEqual(approval_get.status_code, 405)

    async def test_duplicate_choice_uses_public_copy_ids_and_never_moves_or_exposes_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "Inbox").mkdir()
            (root / "Archive").mkdir()
            (root / "Inbox" / "copy.txt").write_text("same", encoding="utf-8")
            (root / "Archive" / "copy.txt").write_text("same", encoding="utf-8")
            result = scan_root(root)
            token = "z" * 32
            app = create_app(
                WebAppConfig(root, session_secret="s" * 32, launch_token=token, testing=True)
            )
            app.state.scan_jobs._snapshot = ScanJobSnapshot(
                status="completed",
                job_id="duplicates",
                completed_at=COMPLETED_AT,
                result=result,
            )
            async with _client(app) as client:
                await client.get(f"/launch/{token}", follow_redirects=False)
                shell = await client.get("/loci-flow")
                csrf = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text).group(1)  # type: ignore[union-attr]
                current = await client.get("/api/organization/current")
                choice = current.json()["choices_needed"][0]
                selected_id = choice["copies"][0]["id"]
                kept = await client.post(
                    "/api/organization/decisions",
                    data={
                        "csrf_token": csrf,
                        "scan_generation": "duplicates",
                        "expected_revision": "0",
                        "kind": "choice",
                        "choice_id": choice["id"],
                        "option_id": "keep-all",
                    },
                    headers=_origin(),
                )
                selected = await client.post(
                    "/api/organization/decisions",
                    data={
                        "csrf_token": csrf,
                        "scan_generation": "duplicates",
                        "expected_revision": "1",
                        "kind": "choice",
                        "choice_id": choice["id"],
                        "option_id": "choose-for-bin",
                        "copy_ids": json.dumps([selected_id]),
                    },
                    headers=_origin(),
                )
                approved = await client.post(
                    "/api/organization/approval",
                    data={
                        "csrf_token": csrf,
                        "scan_generation": "duplicates",
                        "expected_revision": "2",
                    },
                    headers=_origin(),
                )

            self.assertEqual(kept.status_code, 200)
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(approved.json()["review"]["status"], "ready_to_move")
            serialized = approved.text.lower()
            self.assertNotIn("sha256", serialized)
            self.assertNotIn("original", serialized)
            self.assertTrue((root / "Inbox" / "copy.txt").exists())
            self.assertTrue((root / "Archive" / "copy.txt").exists())

    async def _authenticate(self, client: httpx.AsyncClient) -> str:
        await client.get(f"/launch/{self.token}", follow_redirects=False)
        shell = await client.get("/loci-flow")
        match = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text)
        assert match is not None
        return match.group(1)

    async def _bulk(self, client: httpx.AsyncClient, csrf: str):
        return await client.post(
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

    def _complete(self, generation: str) -> None:
        self.app.state.scan_jobs._snapshot = ScanJobSnapshot(
            status="completed",
            job_id=generation,
            started_at=COMPLETED_AT,
            completed_at=COMPLETED_AT,
            result=self.result,
        )


def _origin() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1"}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        follow_redirects=False,
    )


if __name__ == "__main__":
    unittest.main()
