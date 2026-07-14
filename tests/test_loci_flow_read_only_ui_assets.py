from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "organizer" / "web"
LOCI = WEB / "static" / "loci_flow"
PROTOTYPE = ROOT / "prototype" / "loci-flow-ux-1-1-focused-policy-controls"
PROTOTYPE_SHA256 = {
    "README.md": "d146f921a49b65223a8438972f2bec167bd7f9be8721bb12bf8e2e7abc84f54d",
    "app.js": "f17163cd2929cf7b8c14cfb23a396204efc5bd204b2205c60c291842666d77e5",
    "assets/file-icons.svg": "bfefdc18d03629036a5d1fe31b614ebcf70ab8acf7926169e6ef10e1926595c9",
    "assets/loci-flow-mark.svg": "ec0d01fa8e1f312e3233b0f7758f7192d8b40de1370cb48b0f95bce87686f439",
    "fixtures.json": "0d614d82bf3aa75b4ab102a77d47e77b3d3ef7ed390afd6119c1e4099e36fec1",
    "index.html": "1e7ad3659eeaa35424e4cf09f265542d4cde55bf6fbe7672b77a6f54c01115b2",
    "store.js": "a6b6a811a5cedac0ffe3b21b0906df00640dfb1b224beb97b7aacbc5c2dca0ff",
    "styles.css": "6288229571bb209624e00a2a82c639c93ea8eae1aca3dbad9022cb5203c637dd",
    "tree.js": "6f48d5cd34372deb2ec6407845310dba930dbf3fbc79a516eedd87f6c3ebf704",
    "views.js": "02b2f1078dd772da51274645d7938ce84c6585db4e97d0f0bbf0f160485e0f6f",
}


class LociFlowReadOnlyUiAssetTests(unittest.TestCase):
    def test_accepted_prototype_is_byte_unchanged(self) -> None:
        actual = {
            path.relative_to(PROTOTYPE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(PROTOTYPE.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(actual, PROTOTYPE_SHA256)

    def test_integrated_shell_has_exact_navigation_and_local_assets(self) -> None:
        template = (WEB / "templates" / "loci_flow.html").read_text(encoding="utf-8")
        for route in ("home", "organize", "duplicate-bin", "activity", "standards", "settings"):
            self.assertEqual(template.count(f'data-route="{route}"'), 1)
        self.assertNotIn("prototype/", template)
        self.assertNotIn("http://", template)
        self.assertNotIn("https://", template)
        self.assertNotIn("<script>", template)
        self.assertNotIn("style=", template)

    def test_frontend_is_dom_safe_and_has_no_fixture_or_mutation_boundary(self) -> None:
        javascript = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(LOCI.glob("*.js"))
        )
        lower = javascript.lower()
        for forbidden in (
            "innerhtml", "insertadjacenthtml", "fixtures.json", "localstorage",
            "sessionstorage", "indexeddb", "xmlhttprequest", "websocket",
            "eventsource", "serviceworker", "document.cookie", "sha-256", "sha256",
            "accept all", "test move", "apply changes", "approve changes",
        ):
            self.assertNotIn(forbidden, lower)
        self.assertEqual(javascript.count('fetchImpl(ORGANIZATION_ENDPOINT'), 1)
        self.assertNotIn('method: "POST"', javascript)
        self.assertNotIn('method: "PUT"', javascript)
        self.assertNotIn('method: "DELETE"', javascript)
        self.assertIn('source: "backend_read_only"', javascript)
        self.assertIn("textContent", javascript)
        self.assertIn("replaceChildren", javascript)

    def test_presenter_uses_backend_categories_without_reclassification(self) -> None:
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        for field in (
            "recommended_changes", "choices_needed", "kept_by_standards",
            "summary.movement_tree", "scope.display_name", "freshness.last_checked_at",
        ):
            self.assertIn(field, views + app)
        for kind in ("folder", "pdf", "text", "markdown", "word", "spreadsheet", "image", "presentation", "source_code", "archive", "document"):
            self.assertIn(f'{kind}:', views)
        self.assertNotIn(".extension", views)
        self.assertIn('const knownKind = Object.hasOwn(KIND_LABELS, kind) ? kind : "document"', views)
        self.assertIn("Organization information is available for review.", views)
        self.assertNotIn("entry.reason_code, text", views)

    def test_non_integrated_pages_are_honest_and_theme_is_memory_only(self) -> None:
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        for wording in (
            "Duplicate Bin is not connected yet.",
            "No file moves recorded.",
            "Standards editing is not connected yet.",
            "Organization styles are not connected yet.",
        ):
            self.assertIn(wording, views)
        self.assertIn('theme: "light"', app)
        self.assertNotIn("Storage", app)

    def test_api_client_uses_fixed_get_and_rejects_unsafe_contracts(self) -> None:
        script = textwrap.dedent(
            f"""
            import fs from 'node:fs';
            import assert from 'node:assert/strict';
            const source = fs.readFileSync({str(LOCI / 'api.js')!r}, 'utf8');
            const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
            const snapshot = (path = 'Inbox/file.txt') => ({{
              id: 'F1', name: 'file.txt', entity_type: 'file', kind: 'text', extension: '.txt',
              size_bytes: 4, modified_at: '2026-07-14T10:00:00+00:00', relative_path: path, current_folder: 'Inbox'
            }});
            const payload = () => ({{
              scope: {{ display_name: 'sample', root_label: '.' }},
              freshness: {{ scan_generation: 'scan-1', last_checked_at: '2026-07-14T10:00:00+00:00', last_organized_at: null }},
              planning: {{ strategy_id: 'current_deterministic', organization_style: null }},
              recommended_changes: [{{ id: 'O1', action: 'move', item: snapshot(), destination_folder: 'Text', reason_code: 'matched_existing_group' }}],
              choices_needed: [], kept_by_standards: [],
              summary: {{ recommended_changes: 1, choices_needed: 0, kept_by_standards: 0, files_to_move: 1, names_to_change: 0, folders_to_create: 0, duplicate_copies: 0, movement_tree: [{{ folder: 'Text', files: ['Inbox/file.txt'] }}] }}
            }});
            let request;
            const result = await api.fetchOrganizationSlice(async (url, options) => {{
              request = {{ url, options }};
              return {{ ok: true, status: 200, json: async () => payload() }};
            }});
            assert.equal(result.scope.display_name, 'sample');
            assert.equal(request.url, '/api/organization/current');
            assert.equal(request.options.method, 'GET');
            assert.equal(request.options.credentials, 'same-origin');
            assert.deepEqual(request.options.headers, {{ Accept: 'application/json' }});
            const unsafe = payload();
            unsafe.recommended_changes[0].item.relative_path = '../secret.txt';
            assert.throws(() => api.validateOrganizationSlice(unsafe), (error) => error.code === 'contract_error');
            for (const code of ['scan_not_ready', 'scan_in_progress', 'scan_failed']) {{
              await assert.rejects(
                api.fetchOrganizationSlice(async () => ({{ ok: false, status: 409, json: async () => ({{ error: {{ code, message: 'safe message' }} }}) }})),
                (error) => error.code === code
              );
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

    def test_route_has_no_domain_owner_or_executor_dependency(self) -> None:
        route = (WEB / "routes" / "loci_flow.py").read_text(encoding="utf-8")
        for forbidden in ("executor", "scanner", "planner", "duplicates", "grouping"):
            self.assertNotIn(forbidden, route.lower())


if __name__ == "__main__":
    unittest.main()
