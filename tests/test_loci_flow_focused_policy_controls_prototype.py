from __future__ import annotations

from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototype" / "loci-flow-ux-1-1-focused-policy-controls"
ORIGINAL = Path("/Users/yanqinwang/bootAI")
EXPECTED_FILES = {
    "README.md", "app.js", "fixtures.json", "index.html", "store.js",
    "styles.css", "tree.js", "views.js",
}
EXPECTED_ROUTES = {
    "#/home": "Home",
    "#/organize": "Organize",
    "#/duplicate-bin": "Duplicate Bin",
    "#/activity": "Activity",
    "#/standards": "Standards",
    "#/settings": "Settings",
}
PRESERVED_SHA256 = {
    "prototype/loci-flow-ux-1-1-focused/README.md": "f8442315c667fbd4b5a4819b23824515c46dc00edb4b9b0b02e9c9ead23f4846",
    "prototype/loci-flow-ux-1-1-focused/app.js": "43ef016431d8017ecc5a257e26f124d71a88ba6c46a50b5a48e1886126ca2a86",
    "prototype/loci-flow-ux-1-1-focused/fixtures.json": "2df909236130f1465630def7171b5dd0841bf796a362895bbcb7f14a6e4d9e66",
    "prototype/loci-flow-ux-1-1-focused/index.html": "2bbc767f2190ee2e27e8516e54b31c3abdb5419ad0b3c86d900a51024c162d38",
    "prototype/loci-flow-ux-1-1-focused/store.js": "dc9d209a7268d911004c883c3dae5ca3925790fc904c71ba64e10f9ddd8bc5a7",
    "prototype/loci-flow-ux-1-1-focused/styles.css": "702a3a0252c1735fb1bc49728a129dd6e731dcf30886b2cd449046b6d273795a",
    "prototype/loci-flow-ux-1-1-focused/views.js": "7ffcd9a802e992e009c5ee393b7563ce0c284c2eb2d89e7a853e62f1bcfa8774",
    "tests/test_loci_flow_focused_prototype.py": "09eab1b19fb2550089342cfd102e8be5e0a8a0d57e58c9ef31e43abf942f506f",
}
ORIGINAL_POLICY_SHA256 = {
    "prototype/loci-flow-ux-1-1-policy-controls/README.md": "8a9df5cdf1007097bd4e585ba8c91b1d04f220c320f491f0c3d10dcacc07ca0a",
    "prototype/loci-flow-ux-1-1-policy-controls/app.js": "88b10b92e27500a9b2c8de436facd8079961e4ce1fb36082dd473dd214616149",
    "prototype/loci-flow-ux-1-1-policy-controls/fixtures.json": "b7da5421da6ff65bffc38c1f35f1c08a3d883276bd3b4c1f21ad7d0aa2efabe7",
    "prototype/loci-flow-ux-1-1-policy-controls/index.html": "231895d76e39233763851f6e5aa7ddbd7582b282e304e32bc3e2ab21e28504c2",
    "prototype/loci-flow-ux-1-1-policy-controls/store.js": "ff0b78129df6e79e0efd7df6103cf63231a696be6d39de266083d144a8d8b7cb",
    "prototype/loci-flow-ux-1-1-policy-controls/styles.css": "c62175873b0b2edfb77b4e4993c81a04dec60c2c9aa5f014af39be711791e3f1",
    "prototype/loci-flow-ux-1-1-policy-controls/views.js": "0964b405c18ea5fe190ecc82b5d1fa9d6fb9ef8278b65fe8c41d75d95da23314",
    "prototype/loci-flow-ux-1-1-policy-controls/assets/loci-flow-mark.svg": "ec0d01fa8e1f312e3233b0f7758f7192d8b40de1370cb48b0f95bce87686f439",
    "tests/test_loci_flow_policy_controls_prototype.py": "2557b5f1901ebfc9988f0700e9e4ecfe6d3520d840183ab7eedf7d2e002d55b6",
}


class NavigationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_primary = False
        self.in_link = False
        self.current_href: str | None = None
        self.current_text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "nav" and "primary" in (values.get("aria-label") or "").lower():
            self.in_primary = True
        if self.in_primary and tag == "a" and values.get("href"):
            self.in_link = True
            self.current_href = values["href"]
            self.current_text = []

    def handle_data(self, data):
        if self.in_link:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.in_link:
            self.links.append((self.current_href or "", " ".join("".join(self.current_text).split())))
            self.in_link = False
            self.current_href = None
        if tag == "nav":
            self.in_primary = False


class FocusedPolicyControlsPrototypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_text = (PROTOTYPE / "fixtures.json").read_text(encoding="utf-8")
        cls.fixture = json.loads(cls.fixture_text)
        cls.sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in PROTOTYPE.iterdir() if path.is_file()
        }
        cls.runtime = "\n".join(
            cls.sources[name] for name in
            ("index.html", "styles.css", "fixtures.json", "tree.js", "store.js", "views.js", "app.js")
        )
        cls.visible_sources = "\n".join(
            cls.sources[name] for name in ("index.html", "fixtures.json", "views.js")
        )
        cls.parser = NavigationParser()
        cls.parser.feed(cls.sources["index.html"])

    def run_node(self, body: str):
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(f"""
            import fs from 'node:fs';
            import {{
              createStore, deriveActionCounts, recommendedChanges, choiceChanges,
              compatibleChanges, authoredCandidateChanges, applyStandardsToChanges,
              buildChangeCollections, WORKFLOW_STAGES
            }} from {json.dumps((PROTOTYPE / 'store.js').as_uri())};
            import {{ buildMovementTree, FILE_ICON_MAP }} from {json.dumps((PROTOTYPE / 'tree.js').as_uri())};
            import {{ renderRoute, renderDialog }} from {json.dumps((PROTOTYPE / 'views.js').as_uri())};
            const sample = JSON.parse(fs.readFileSync({json.dumps(str(PROTOTYPE / 'fixtures.json'))}, 'utf8'));
            const finishAll = (store) => {{
              store.acceptRecommended();
              for (const change of [...store.choiceChanges()]) {{
                store.decideChange(change.id, change.type === 'duplicate' ? 'move-bin' : 'accept');
              }}
            }};
            const approveMove = (store) => {{
              finishAll(store); store.beginApproval(); return store.approveTestMove();
            }};
            const verifyMove = (store) => {{ approveMove(store); return store.verifyTestMove(); }};
            {body}
        """)
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            check=True, capture_output=True, text=True,
        )
        return json.loads(completed.stdout)

    def test_active_prototype_file_set_is_bounded(self) -> None:
        self.assertEqual({path.name for path in PROTOTYPE.iterdir() if path.is_file()}, EXPECTED_FILES)
        self.assertEqual({path.name for path in (PROTOTYPE / "assets").iterdir()}, {"loci-flow-mark.svg", "file-icons.svg"})
        self.assertFalse((PROTOTYPE / "package.json").exists())

    def test_previous_focused_prototype_and_test_are_byte_identical(self) -> None:
        for relative, expected in PRESERVED_SHA256.items():
            with self.subTest(path=relative):
                self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), expected)

    def test_original_policy_comparison_is_byte_identical_when_present(self) -> None:
        if not ORIGINAL.exists():
            self.skipTest("Original comparison worktree unavailable")
        for relative, expected in ORIGINAL_POLICY_SHA256.items():
            with self.subTest(path=relative):
                self.assertEqual(hashlib.sha256((ORIGINAL / relative).read_bytes()).hexdigest(), expected)

    def test_primary_navigation_has_exact_six_pages(self) -> None:
        self.assertEqual({href for href, _ in self.parser.links}, set(EXPECTED_ROUTES))
        self.assertEqual(len(self.parser.links), 6)

    def test_organization_styles_are_exact_and_create_no_activity(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().activityMoves.length;
            store.setOrganizationStyle('standard'); store.setOrganizationStyle('proactive');
            console.log(JSON.stringify({styles:Object.keys(sample.organizationStyles),delta:store.getState().activityMoves.length-before}));
        """)
        self.assertEqual(result, {"styles": ["cautious", "standard", "proactive"], "delta": 0})

    def test_light_and_dark_share_one_theme_state(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); store.setTheme('dark'); console.log(JSON.stringify([store.getState().theme,store.getState().settings.theme]));
        """)
        self.assertEqual(result, ["dark", "dark"])

    def test_workflow_stage_enum_is_exact(self) -> None:
        result = self.run_node("console.log(JSON.stringify(WORKFLOW_STAGES));")
        self.assertEqual(result, ["idle", "checking", "organizing", "approving", "moving", "verifying"])

    def test_home_workflow_labels_are_exact(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const html=renderRoute(store,'home');
            console.log(JSON.stringify([...html.matchAll(/<li[^>]*><span>\\d<\\/span>([^<]+)<\\/li>/g)].map(match=>match[1])));
        """)
        self.assertEqual(result, ["Check", "Organize", "Approve", "Move", "Verify"])

    def test_recommended_and_choices_are_separate_visible_sections(self) -> None:
        views = self.sources["views.js"]
        self.assertIn("Recommended changes", views)
        self.assertIn("Choices needed", views)
        self.assertLess(views.index("Recommended changes"), views.index("Choices needed"))

    def test_visible_counts_derive_from_change_records(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); console.log(JSON.stringify({counts:store.changeCounts(),recommended:store.recommendedChanges().length,choices:store.choiceChanges().length,total:store.getState().changes.length}));
        """)
        self.assertEqual(result["counts"]["total"], result["total"])
        self.assertEqual(result["recommended"] + result["choices"], result["total"])

    def test_active_standards_are_applied_before_visible_classification(self) -> None:
        result = self.run_node("""
            const candidates=authoredCandidateChanges(sample,'cautious'); const active=sample.standards.filter(item=>item.active);
            const filtered=applyStandardsToChanges(candidates,active); const collections=buildChangeCollections(sample,'cautious',sample.standards);
            console.log(JSON.stringify({candidate:candidates.length,allowed:filtered.allowedChanges.length,visible:collections.changes.length,kept:filtered.keptByStandards.length}));
        """)
        self.assertEqual(result["candidate"], result["allowed"] + result["kept"])
        self.assertEqual(result["allowed"], result["visible"])

    def test_submission_is_absent_when_keep_submissions_is_active(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); console.log(JSON.stringify({visible:store.getState().changes.some(item=>item.name==='submission.pdf'),kept:store.getState().keptByStandards.some(item=>item.name==='submission.pdf')}));
        """)
        self.assertEqual(result, {"visible": False, "kept": True})

    def test_submission_is_absent_from_where_files_will_go(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); store.acceptRecommended(); const tree=JSON.stringify(buildMovementTree(store.getState().changes)); console.log(JSON.stringify(tree.includes('submission.pdf')));
        """)
        self.assertFalse(result)

    def test_kept_by_standards_summary_is_informational(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const html=renderRoute(store,'organize'); const kept=html.match(/<details class="kept-summary[\\s\\S]*?<\\/details>/)?.[0]||'';
            console.log(JSON.stringify({label:kept.includes('Kept by Standards'),summary:kept.includes('files kept in place'),action:kept.includes('data-action')}));
        """)
        self.assertEqual(result, {"label": True, "summary": True, "action": False})

    def assert_style_filters_submission(self, style: str) -> None:
        result = self.run_node(f"""
            const store=createStore(sample); {'store.setOrganizationStyle(' + json.dumps(style) + ');' if style != 'cautious' else ''}
            console.log(JSON.stringify({{visible:store.getState().changes.some(item=>item.name==='submission.pdf'),kept:store.getState().keptByStandards.map(item=>item.name)}}));
        """)
        self.assertFalse(result["visible"])
        self.assertIn("submission.pdf", result["kept"])

    def test_cautious_applies_active_standards(self) -> None:
        self.assert_style_filters_submission("cautious")

    def test_standard_applies_active_standards(self) -> None:
        self.assert_style_filters_submission("standard")

    def test_proactive_applies_active_standards(self) -> None:
        self.assert_style_filters_submission("proactive")

    def test_accept_all_count_is_derived_from_pending_recommendations(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const count=store.recommendedChanges().length; const html=renderRoute(store,'organize'); console.log(JSON.stringify({count,shown:html.includes(`Accept all recommended changes (${count})`)}));
        """)
        self.assertTrue(result["shown"])

    def test_accept_all_accepts_every_pending_recommendation(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const pending=store.recommendedChanges().map(item=>item.id); const accepted=store.acceptRecommended();
            console.log(JSON.stringify({pending,accepted,remaining:store.recommendedChanges().length}));
        """)
        self.assertEqual(result["pending"], result["accepted"])
        self.assertEqual(result["remaining"], 0)

    def test_no_recommended_item_is_excluded_from_accept_all(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const recommended=store.getState().changes.filter(item=>item.classification==='recommended').map(item=>item.id); const accepted=store.acceptRecommended();
            console.log(JSON.stringify({recommended,accepted}));
        """)
        self.assertEqual(set(result["recommended"]), set(result["accepted"]))

    def test_choices_are_not_accepted_by_accept_all(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const choices=store.choiceChanges().map(item=>item.id); store.acceptRecommended(); console.log(JSON.stringify(store.getState().changes.filter(item=>choices.includes(item.id)).map(item=>item.decision)));
        """)
        self.assertTrue(result)
        self.assertEqual(set(result), {"pending"})

    def test_accept_all_selects_the_next_choice(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); store.acceptRecommended(); console.log(JSON.stringify({active:store.getState().activeChangeId,first:store.choiceChanges()[0].id}));
        """)
        self.assertEqual(result["active"], result["first"])

    def test_organization_choices_create_no_activity(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().activityMoves.length; finishAll(store); console.log(JSON.stringify(store.getState().activityMoves.length-before));
        """)
        self.assertEqual(result, 0)

    def test_compatible_bulk_actions_remain_available(self) -> None:
        views = self.sources["views.js"]
        for label in ("Accept selected", "Keep selected", "Change folder", "Remove changes"):
            self.assertIn(label, views)

    def test_review_and_approve_appears_only_after_all_choices(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=renderRoute(store,'organize').includes('Review and approve'); finishAll(store); const after=renderRoute(store,'organize').includes('Review and approve'); console.log(JSON.stringify({before,after}));
        """)
        self.assertEqual(result, {"before": False, "after": True})

    def test_direct_test_move_is_not_available_before_approval(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const html=renderRoute(store,'organize'); console.log(JSON.stringify({approve:html.includes('Approve test move'),testAction:html.includes('data-action="test-move"')}));
        """)
        self.assertEqual(result, {"approve": False, "testAction": False})

    def test_approval_summary_contains_all_four_movement_categories(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); finishAll(store); store.beginApproval(); const html=renderDialog(store,'approval').content;
            console.log(JSON.stringify(['files will move','names will change','folders will be created','copies will move','Duplicate Bin'].every(label=>html.includes(label))));
        """)
        self.assertTrue(result)

    def test_approve_test_move_stores_complete_pre_move_snapshot(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const batch=approveMove(store); console.log(JSON.stringify(Object.keys(batch.preMoveSnapshot).sort()));
        """)
        for key in ("sampleFiles", "createdFolders", "duplicateBinItems", "changes", "candidateChanges", "keptByStandards"):
            self.assertIn(key, result)

    def test_approve_test_move_applies_locations_names_folders_and_duplicates(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=JSON.stringify(store.getState().sampleFiles); const bin=store.getState().duplicateBinItems.length; approveMove(store);
            console.log(JSON.stringify({filesChanged:before!==JSON.stringify(store.getState().sampleFiles),folders:store.getState().createdFolders.length,binDelta:store.getState().duplicateBinItems.length-bin}));
        """)
        self.assertTrue(result["filesChanged"])
        self.assertGreater(result["folders"], 0)
        self.assertGreater(result["binDelta"], 0)

    def test_approve_test_move_enters_move_stage(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); approveMove(store); console.log(JSON.stringify(store.getState().workflowStage));
        """)
        self.assertEqual(result, "moving")

    def test_move_and_verify_are_distinct_actions(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().activityMoves.length; approveMove(store); const html=renderRoute(store,'organize'); console.log(JSON.stringify({activityDelta:store.getState().activityMoves.length-before,verify:html.includes('Verify test move')}));
        """)
        self.assertEqual(result, {"activityDelta": 0, "verify": True})

    def test_verify_test_move_checks_expected_sample_results(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const result=verifyMove(store); console.log(JSON.stringify({passed:result.passed,checks:result.checks.length,all:result.checks.every(item=>item.passed),standards:result.checks.some(item=>item.name.includes('Standards'))}));
        """)
        self.assertTrue(result["passed"] and result["all"] and result["standards"])
        self.assertGreater(result["checks"], 4)

    def test_verify_creates_exactly_one_activity_movement_batch(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().activityMoves.length; verifyMove(store); const first=store.getState().activityMoves.length; store.verifyTestMove();
            console.log(JSON.stringify({first:first-before,second:store.getState().activityMoves.length-before,type:store.getState().activityMoves[0].type}));
        """)
        self.assertEqual(result, {"first": 1, "second": 1, "type": "organization_move"})

    def test_verify_enters_verify_stage(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); verifyMove(store); console.log(JSON.stringify(store.getState().workflowStage));
        """)
        self.assertEqual(result, "verifying")

    def test_done_sets_idle_and_navigates_home(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); verifyMove(store); store.completeWorkflow(); console.log(JSON.stringify({stage:store.getState().workflowStage,route:store.getState().route}));
        """)
        self.assertEqual(result, {"stage": "idle", "route": "home"})

    def test_idle_home_highlights_no_workflow_stage(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); verifyMove(store); store.completeWorkflow(); const html=renderRoute(store,'home'); console.log(JSON.stringify((html.match(/class="current"/g)||[]).length));
        """)
        self.assertEqual(result, 0)

    def test_home_shows_organized_status_and_check_again_after_done(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); verifyMove(store); store.completeWorkflow(); const html=renderRoute(store,'home'); console.log(JSON.stringify({organized:html.includes('Everything is organized'),check:html.includes('Check again'),last:html.includes('Last move completed successfully')}));
        """)
        self.assertEqual(result, {"organized": True, "check": True, "last": True})

    def test_latest_reversible_activity_shows_undo_when_collapsed(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); verifyMove(store); const html=renderRoute(store,'activity'); console.log(JSON.stringify({undo:html.includes('Undo move'),details:html.includes('activity-details')}));
        """)
        self.assertEqual(result, {"undo": True, "details": False})

    def test_latest_reversible_activity_keeps_undo_when_expanded(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); verifyMove(store); store.toggleActivity('move-today'); const html=renderRoute(store,'activity'); console.log(JSON.stringify({undo:html.includes('Undo move'),details:html.includes('activity-details')}));
        """)
        self.assertEqual(result, {"undo": True, "details": True})

    def test_undo_restores_file_locations(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().sampleFiles.map(item=>item.folder); verifyMove(store); store.completeWorkflow(); store.undoLatestMove(); console.log(JSON.stringify({before,after:store.getState().sampleFiles.map(item=>item.folder)}));
        """)
        self.assertEqual(result["before"], result["after"])

    def test_undo_restores_file_names(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().sampleFiles.map(item=>item.name); verifyMove(store); store.completeWorkflow(); store.undoLatestMove(); console.log(JSON.stringify({before,after:store.getState().sampleFiles.map(item=>item.name)}));
        """)
        self.assertEqual(result["before"], result["after"])

    def test_undo_restores_created_folder_state(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().createdFolders.length; verifyMove(store); const moved=store.getState().createdFolders.length; store.completeWorkflow(); store.undoLatestMove(); console.log(JSON.stringify({before,moved,after:store.getState().createdFolders.length}));
        """)
        self.assertGreater(result["moved"], result["before"])
        self.assertEqual(result["after"], result["before"])

    def test_undo_removes_related_duplicate_bin_entries(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().duplicateBinItems.length; verifyMove(store); const moved=store.getState().duplicateBinItems.length; store.completeWorkflow(); store.undoLatestMove(); console.log(JSON.stringify({before,moved,after:store.getState().duplicateBinItems.length}));
        """)
        self.assertGreater(result["moved"], result["before"])
        self.assertEqual(result["after"], result["before"])

    def test_undo_adds_one_restore_activity_entry(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); verifyMove(store); const before=store.getState().activityMoves.length; store.completeWorkflow(); store.undoLatestMove(); const entry=store.getState().activityMoves[0]; console.log(JSON.stringify({delta:store.getState().activityMoves.length-before,title:entry.title,summary:entry.summary}));
        """)
        self.assertEqual(result, {"delta": 1, "title": "Restored previous organization", "summary": "12 files restored"})

    def test_undo_disables_original_batch_and_returns_home_idle(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); verifyMove(store); store.completeWorkflow(); store.undoLatestMove(); const original=store.getState().activityMoves.find(item=>item.id==='move-today'); console.log(JSON.stringify({undoable:original.undoable,route:store.getState().route,stage:store.getState().workflowStage}));
        """)
        self.assertEqual(result, {"undoable": False, "route": "home", "stage": "idle"})

    def test_standards_page_has_compact_interpretation_entry_point(self) -> None:
        for text in ("Describe an organization preference", "Loci Flow turns your description into Standards for you to review.", "Interpret preference"):
            self.assertIn(text, self.sources["views.js"])
        self.assertIn("<textarea", self.sources["views.js"])

    def test_clear_coding_intent_yields_four_inactive_suggested_standards(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().standards.length; const value=store.interpretPreference(sample.interpretations.codingProjects.intent);
            console.log(JSON.stringify({status:value.status,count:value.suggestions.length,active:value.suggestions.some(item=>item.active),standardDelta:store.getState().standards.length-before,names:value.suggestions.map(item=>item.name)}));
        """)
        self.assertEqual(result["status"], "suggested")
        self.assertEqual(result["count"], 4)
        self.assertFalse(result["active"])
        self.assertEqual(result["standardDelta"], 0)
        self.assertIn("Keep code projects together", result["names"])

    def test_confirmed_suggested_standards_join_ordinary_standards(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().standards.length; store.interpretPreference(sample.interpretations.codingProjects.intent); const used=store.confirmSuggestedStandards();
            console.log(JSON.stringify({used,delta:store.getState().standards.length-before,names:store.getState().standards.slice(before).map(item=>item.name),active:store.getState().standards.slice(before).every(item=>item.active)}));
        """)
        self.assertEqual(result["used"], result["delta"])
        self.assertEqual(result["used"], 4)
        self.assertTrue(result["active"])

    def test_interpretation_and_confirmation_create_no_activity(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const before=store.getState().activityMoves.length; store.interpretPreference(sample.interpretations.codingProjects.intent); store.confirmSuggestedStandards(); console.log(JSON.stringify(store.getState().activityMoves.length-before));
        """)
        self.assertEqual(result, 0)

    def test_ambiguous_name_intent_requires_clarification(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const value=store.interpretPreference('Organize files by name'); console.log(JSON.stringify({status:value.status,question:value.clarification.question,choices:value.clarification.choices.map(item=>item.label)}));
        """)
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["question"], "What kind of name do you mean?")
        self.assertEqual(result["choices"], ["Person name", "Project code", "Course code", "Common filename prefix"])

    def test_clarification_choice_yields_editable_suggested_standards(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); store.interpretPreference('Organize files by name'); const suggestions=store.clarifyInterpretation('Project code'); const html=renderRoute(store,'standards'); console.log(JSON.stringify({count:suggestions.length,edit:html.includes('Edit'),remove:html.includes('Remove'),use:html.includes('Use these Standards')}));
        """)
        self.assertEqual(result, {"count": 1, "edit": True, "remove": True, "use": True})

    def test_editing_suggested_standard_preserves_or_updates_its_authored_values(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); store.interpretPreference(sample.interpretations.codingProjects.intent);
            const original=store.getState().interpretation.suggestions[0];
            const authoredName=original.name; const authoredDescription=original.description;
            const dialog=renderDialog(store,'standard-builder',{suggestedStandardId:original.id});
            store.updateSuggestedStandard(original.id,{name:original.name,description:original.description,appliesTo:original.appliesTo});
            const preserved=store.getState().interpretation.suggestions[0];
            const preservedAuthoredValues=preserved.name===authoredName&&preserved.description===authoredDescription;
            store.updateSuggestedStandard(original.id,{name:'Keep reviewed code projects together',description:original.description,appliesTo:['Projects','Reviewed']});
            const updated=store.getState().interpretation.suggestions[0];
            console.log(JSON.stringify({prefilled:dialog.content.includes(authoredName)&&dialog.content.includes(authoredDescription),preserved:preservedAuthoredValues,updated:{name:updated.name,appliesTo:updated.appliesTo}}));
        """)
        self.assertTrue(result["prefilled"])
        self.assertTrue(result["preserved"])
        self.assertEqual(result["updated"], {"name": "Keep reviewed code projects together", "appliesTo": ["Projects", "Reviewed"]})

    def test_unsafe_deletion_intent_is_rejected_with_supported_alternatives(self) -> None:
        result = self.run_node("""
            const store=createStore(sample); const value=store.interpretPreference('Remove files I probably do not need'); console.log(JSON.stringify({status:value.status,alternatives:value.alternatives,standards:store.getState().standards.length}));
        """)
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["alternatives"], ["Find exact duplicate copies", "Organize files into folders", "Keep selected folders unchanged"])
        self.assertEqual(result["standards"], len(self.fixture["standards"]))

    def test_interpretation_is_fixture_driven_and_has_no_llm_or_chat_ui(self) -> None:
        self.assertIn('fixtures.interpretations', self.sources["store.js"])
        for forbidden in ("assistant avatar", "message history", "typing animation", "chat-message", "openai", "anthropic"):
            self.assertNotIn(forbidden, self.runtime.lower())

    def test_activity_contains_no_planning_records_or_undo_plan(self) -> None:
        lower = self.runtime.lower()
        self.assertNotIn("undo plan", lower)
        self.assertNotIn("planning activity", lower)
        self.assertTrue(all(entry["status"] == "Completed" for entry in self.fixture["activityMoves"]))

    def test_duplicate_bin_and_activity_detail_grid_are_preserved(self) -> None:
        views = self.sources["views.js"]
        for label in ("Duplicate Bin", "Original folder", "Restore selected", "Files moved", "Names changed", "Folders created", "Copies moved to Duplicate Bin", "Files restored", "Problems"):
            self.assertIn(label, views)

    def test_settings_remain_the_five_minimal_sections(self) -> None:
        match = re.search(r"export const SETTINGS_SECTIONS = \[([\s\S]*?)\];", self.sources["views.js"])
        self.assertIsNotNone(match)
        for label in ("Appearance", "Notifications", "Local AI", "Privacy", "Advanced"):
            self.assertIn(label, match.group(1))

    def test_no_network_filesystem_backend_or_persistence_api_is_introduced(self) -> None:
        lower = self.runtime.lower()
        for forbidden in (
            "xmlhttprequest", "websocket", "eventsource", "serviceworker", "showdirectorypicker",
            "showopenfilepicker", "executor.py", "localstorage", "sessionstorage", "indexeddb",
            "document.cookie", "fetch(", "http://", "https://", "//cdn", "@import",
        ):
            self.assertNotIn(forbidden, lower)

    def test_no_drag_drop_sliders_or_substantial_animation(self) -> None:
        lower = self.runtime.lower()
        for forbidden in ('type="range"', "draggable", "dragstart", "dropzone", "ondrop", "slider"):
            self.assertNotIn(forbidden, lower)
        self.assertIn("prefers-reduced-motion", self.sources["styles.css"])

    def test_readme_documents_final_correction_boundaries(self) -> None:
        readme = self.sources["README.md"]
        for text in (
            "Recommended changes", "Choices needed", "Standards", "fixture-driven", "No LLM",
            "approval", "verification", "Undo move", "idle", "no backend", "no persistence",
        ):
            self.assertIn(text.lower(), readme.lower())

    def test_accessibility_and_safety_hooks_remain(self) -> None:
        self.assertIn('aria-live="polite"', self.sources["index.html"])
        self.assertIn("skip-link", self.sources["index.html"])
        self.assertIn('role="tree"', self.sources["tree.js"])
        self.assertIn('role="table"', self.sources["views.js"])
        self.assertIn('event.key === "Escape"', self.sources["app.js"])
        self.assertIn('event.key === "Tab"', self.sources["app.js"])
        self.assertIn("Sample files only. Your files are not changed.", self.sources["index.html"])


if __name__ == "__main__":
    unittest.main()
