from __future__ import annotations

from pathlib import Path
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"


class WebLociFlowActivityTests(unittest.TestCase):
    def test_activity_client_uses_fixed_same_origin_contract_and_exact_confirmation(self) -> None:
        script = textwrap.dedent(
            f"""
            import fs from 'node:fs';
            import assert from 'node:assert/strict';
            const source = fs.readFileSync({str(LOCI / 'api.js')!r}, 'utf8');
            const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
            const id = '4d7b4e92-14c8-4bb9-8328-3d271f355574';
            const entry = {{
              id, type: 'organization', title: 'Organized 1 file', status: 'verified',
              started_at: '2026-07-15T08:00:00+00:00', completed_at: '2026-07-15T08:00:01+00:00',
              files_expected: 1, files_moved: 1, files_restored: 0, files_failed: 0,
              files_unattempted: 0, verification_status: 'passed', undoable: true,
              related_operation_id: null,
              details: [{{ source: 'Inbox/a.txt', destination: 'Documents/a.txt', status: 'moved' }}],
            }};
            const requests = [];
            const fetcher = async (url, options) => {{
              requests.push({{ url, options }});
              if (url === '/api/activity') return {{ ok: true, status: 200, json: async () => ({{ activity: [entry] }}) }};
              if (url.endsWith('/preview')) return {{ ok: true, status: 200, json: async () => ({{ operation_id: id, files_to_restore: 1, confirmation: 'UNDO FILE MOVE', confirmation_token: 'one-use' }}) }};
              return {{ ok: true, status: 200, json: async () => ({{ undo: {{ undo_id: id }}, activity: {{ ...entry, type: 'restore', title: 'Restored 1 file', status: 'restored', files_moved: 0, files_restored: 1, undoable: false, related_operation_id: id, details: [{{ restored_to: 'Inbox/a.txt', moved_from: 'Documents/a.txt', status: 'restored' }}] }} }}) }};
            }};
            const activity = await api.fetchActivity(fetcher);
            const preview = await api.previewActivityUndo(id, fetcher, 'csrf');
            const result = await api.executeActivityUndo({{ operationId: id, confirmationToken: preview.confirmation_token }}, fetcher, 'csrf');
            assert.equal(activity.activity[0].undoable, true);
            assert.equal(result.activity.undoable, false);
            assert.deepEqual(requests.map((request) => request.url), [
              '/api/activity', `/api/activity/${{id}}/undo/preview`, `/api/activity/${{id}}/undo`,
            ]);
            assert.equal(requests[0].options.method, 'GET');
            for (const request of requests.slice(1)) {{
              assert.equal(request.options.method, 'POST');
              assert.equal(request.options.credentials, 'same-origin');
              assert.equal(request.options.body.get('csrf_token'), 'csrf');
            }}
            assert.equal(requests[2].options.body.get('confirmation'), 'UNDO FILE MOVE');
            for (const forbidden of ['path', 'root', 'log_path', 'file_id']) {{
              assert.equal(requests[2].options.body.has(forbidden), false);
            }}
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

    def test_activity_ui_has_real_controls_confirmation_and_no_fixture_persistence(self) -> None:
        api = (LOCI / "api.js").read_text(encoding="utf-8")
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        combined = api + app + views
        for wording in (
            "No file moves recorded.", "Details", "Expand all", "Collapse all",
            "Undo move", "Undo this move?", "will return to", "UNDO FILE MOVE",
            "Move files — not connected yet",
        ):
            self.assertIn(wording, combined)
        self.assertIn("expandedActivityIds: new Set()", app)
        self.assertIn("entry.undoable", views)
        self.assertIn("await loadActivity()", app)
        self.assertIn("announce(\"File move undone.", app)
        self.assertIn('disabled: "disabled"', views)
        for forbidden in ("fixtures.json", "localStorage", "sessionStorage", "/api/organization/move"):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
