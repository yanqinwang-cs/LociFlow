from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest

import httpx

from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from tests.test_application_duplicate_bin_movement_service import _approved_batch


class WebDuplicateBinRestoreTests(unittest.IsolatedAsyncioTestCase):
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
        self.batch = _approved_batch(
            self.root, self.app.state.duplicate_bin_batches, selected=2
        )
        self.app.state.duplicate_bin_batches.create_or_recover_approved_batch(
            self.batch
        )

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_projection_restore_and_replay_are_safe(self) -> None:
        async with _client(self.app) as client:
            csrf = await _authenticate(client, self.token)
            pending = await client.get("/api/duplicate-bin")
            await _move(client, csrf, self.batch.batch_id)
            listing = await client.get("/api/duplicate-bin")
            items = listing.json()["items"]
            preview = await client.post(
                "/api/duplicate-bin/restore/preview",
                data={
                    "csrf_token": csrf,
                    "item_ids": json.dumps([item["id"] for item in items]),
                },
                headers=_origin(),
            )
            payload = preview.json()
            restored = await client.post(
                "/api/duplicate-bin/restore",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": payload["confirmation_token"],
                    "confirmation": payload["confirmation"],
                },
                headers=_origin(),
            )
            replay = await client.post(
                "/api/duplicate-bin/restore",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": payload["confirmation_token"],
                    "confirmation": payload["confirmation"],
                },
                headers=_origin(),
            )
            empty = await client.get("/api/duplicate-bin")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(pending.json(), {"items": []})
        self.assertEqual(listing.headers["cache-control"], "no-store")
        self.assertEqual(len(items), 2)
        self.assertEqual(
            set(items[0]),
            {
                "id",
                "name",
                "previous_folder",
                "moved_at",
                "size",
                "kind",
                "restore_available",
                "attention_message",
            },
        )
        self.assertTrue(all(item["restore_available"] for item in items))
        self.assertEqual(payload["confirmation"], "RESTORE COPIES")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["restore"]["status"], "restored")
        self.assertEqual(
            replay.json()["restore"]["id"], restored.json()["restore"]["id"]
        )
        self.assertEqual(empty.json(), {"items": []})
        serialized = listing.text.lower() + preview.text.lower() + restored.text.lower()
        for forbidden in (
            "sha256",
            "protected_source",
            "previous_destination",
            "movement_operation_id",
            "batch_id",
            str(self.root).lower(),
            "original",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_restore_routes_enforce_security_and_exact_item_array(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.post("/api/duplicate-bin/restore/preview")
            csrf = await _authenticate(client, self.token)
            await _move(client, csrf, self.batch.batch_id)
            item_id = self.batch.items[0].bin_item_id
            no_origin = await client.post(
                "/api/duplicate-bin/restore/preview",
                data={"csrf_token": csrf, "item_ids": json.dumps([item_id])},
            )
            unsupported = await client.post(
                "/api/duplicate-bin/restore/preview",
                json={"csrf_token": csrf, "item_ids": [item_id]},
                headers=_origin(),
            )
            malformed = await client.post(
                "/api/duplicate-bin/restore/preview",
                data={"csrf_token": csrf, "item_ids": "not-json"},
                headers=_origin(),
            )
            duplicate_ids = await client.post(
                "/api/duplicate-bin/restore/preview",
                data={"csrf_token": csrf, "item_ids": json.dumps([item_id, item_id])},
                headers=_origin(),
            )
            browser_path = await client.post(
                "/api/duplicate-bin/restore/preview",
                data={
                    "csrf_token": csrf,
                    "item_ids": json.dumps([item_id]),
                    "destination": "Inbox/forged.txt",
                },
                headers=_origin(),
            )
            duplicate_field = await client.post(
                "/api/duplicate-bin/restore/preview",
                content=(
                    f"csrf_token={csrf}&item_ids=%5B%22{item_id}%22%5D"
                    f"&item_ids=%5B%22{item_id}%22%5D"
                ),
                headers={
                    **_origin(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            valid_preview = await client.post(
                "/api/duplicate-bin/restore/preview",
                data={"csrf_token": csrf, "item_ids": json.dumps([item_id])},
                headers=_origin(),
            )
            wrong_confirmation = await client.post(
                "/api/duplicate-bin/restore",
                data={
                    "csrf_token": csrf,
                    "confirmation_token": valid_preview.json()[
                        "confirmation_token"
                    ],
                    "confirmation": "RESTORE COPY",
                },
                headers=_origin(),
            )
            get_restore = await client.get("/api/duplicate-bin/restore")
            query = await client.get("/api/duplicate-bin?x=1")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(no_origin.status_code, 403)
        for response in (
            unsupported,
            malformed,
            duplicate_ids,
            browser_path,
            duplicate_field,
        ):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(get_restore.status_code, 405)
        self.assertEqual(query.status_code, 400)
        self.assertEqual(wrong_confirmation.status_code, 400)
        self.assertEqual(
            wrong_confirmation.json()["error"]["code"],
            "invalid_restore_confirmation",
        )
        self.assertEqual(self.app.state.duplicate_bin_restores.list_operations(), ())


async def _move(client, csrf, batch_id):
    preview = await client.post(
        "/api/duplicate-bin/move/preview",
        data={"csrf_token": csrf, "batch_id": batch_id},
        headers=_origin(),
    )
    payload = preview.json()
    return await client.post(
        "/api/duplicate-bin/move",
        data={
            "csrf_token": csrf,
            "confirmation_token": payload["confirmation_token"],
            "confirmation": payload["confirmation"],
        },
        headers=_origin(),
    )


async def _authenticate(client, token):
    await client.get(f"/launch/{token}", follow_redirects=False)
    shell = await client.get("/loci-flow")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', shell.text)
    assert match is not None
    return match.group(1)


def _origin():
    return {"Origin": "http://127.0.0.1"}


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    )
