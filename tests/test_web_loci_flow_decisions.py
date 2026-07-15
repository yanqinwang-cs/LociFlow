from __future__ import annotations

from pathlib import Path
import re
import subprocess
import textwrap
import tempfile
import unittest

import httpx

from organizer.web.app import create_app
from organizer.web.config import WebAppConfig


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "organizer" / "web"
LOCI = WEB / "static" / "loci_flow"


class WebLociFlowDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_renders_existing_csrf_token_without_inline_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            token = "l" * 32
            app = create_app(
                WebAppConfig(root, session_secret="s" * 32, launch_token=token, testing=True)
            )
            async with _client(app) as client:
                await client.get(f"/launch/{token}", follow_redirects=False)
                response = await client.get("/loci-flow")

        match = re.search(r'<meta name="csrf-token" content="([^"]+)">', response.text)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertGreaterEqual(len(match.group(1)), 32)
        self.assertNotIn(str(root), response.text)
        self.assertNotIn("<script>", response.text)
        self.assertIn("No files can move in this stage", response.text)

    def test_api_client_uses_fixed_urlencoded_mutation_contracts(self) -> None:
        script = textwrap.dedent(
            f"""
            import fs from 'node:fs';
            import assert from 'node:assert/strict';
            const source = fs.readFileSync({str(LOCI / 'api.js')!r}, 'utf8');
            const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
            const requests = [];
            const reject = async (url, options) => {{
              requests.push({{ url, options }});
              return {{ ok: false, status: 409, json: async () => ({{ error: {{ code: 'incomplete_decisions', message: 'safe' }} }}) }};
            }};
            await assert.rejects(
              api.updateOrganizationDecision({{ scanGeneration: 'g1', expectedRevision: 2, kind: 'recommendation', changeId: 'O1', decision: 'accept' }}, reject, 'csrf'),
              (error) => error.code === 'incomplete_decisions'
            );
            await assert.rejects(
              api.acceptRecommendedChanges({{ scanGeneration: 'g1', expectedRevision: 2, expectedChangeIds: ['O1', 'O2'] }}, reject, 'csrf'),
              (error) => error.code === 'incomplete_decisions'
            );
            await assert.rejects(
              api.approveOrganizationPlan({{ scanGeneration: 'g1', expectedRevision: 3 }}, reject, 'csrf'),
              (error) => error.code === 'incomplete_decisions'
            );
            assert.deepEqual(requests.map((item) => item.url), [
              '/api/organization/decisions', '/api/organization/decisions', '/api/organization/approval'
            ]);
            for (const request of requests) {{
              assert.equal(request.options.method, 'POST');
              assert.equal(request.options.credentials, 'same-origin');
              assert.equal(request.options.headers['Content-Type'], 'application/x-www-form-urlencoded');
              assert.ok(request.options.body instanceof URLSearchParams);
              assert.equal(request.options.body.get('csrf_token'), 'csrf');
              assert.equal(request.options.body.get('scan_generation'), 'g1');
            }}
            assert.deepEqual(JSON.parse(requests[1].options.body.get('expected_change_ids')), ['O1', 'O2']);
            assert.equal(requests[2].options.body.has('destination'), false);
            assert.equal(requests[2].options.body.has('summary'), false);
            """
        )
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frontend_contains_decision_approval_and_ready_states_without_persistence(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        data_source = (LOCI / "data_source.js").read_text(encoding="utf-8")
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        combined = (app + data_source + views).lower()
        for wording in (
            "Accept all recommended changes",
            "Keep here",
            "Choose copies for Duplicate Bin",
            "Review and approve",
            "Ready to organize",
            "Ready to move",
            "No files have moved yet.",
            "Move files — not connected yet",
        ):
            self.assertIn(wording, app + views)
        self.assertIn('decision === "undecided"', data_source)
        self.assertIn("selected.size >= backendChoice.copies.length", app)
        self.assertIn('options.disabled ? "disabled"', views)
        for forbidden in (
            "localstorage",
            "sessionstorage",
            "document.cookie",
            "activity.push",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertEqual(app.count("elements.app.innerHTML ="), 1)


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://127.0.0.1",
        follow_redirects=False,
    )


if __name__ == "__main__":
    unittest.main()
