from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"
PROTOTYPE = ROOT / "prototype" / "loci-flow-ux-1-1-focused-policy-controls"


class LociFlowStageB1RendererContractTests(unittest.TestCase):
    def run_node(self, body: str) -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            import fs from "node:fs";
            import {{ createFixtureDataSource }} from {json.dumps((LIVE / 'data_source.js').as_uri())};
            import {{ createStore }} from {json.dumps((LIVE / 'store.js').as_uri())};
            import {{
              buildRouteViewModel, buildDialogViewModel, buildHomeViewModel,
              buildOrganizeViewModel
            }} from {json.dumps((LIVE / 'view_models.js').as_uri())};
            import {{ renderRoute, renderDialog }} from {json.dumps((LIVE / 'views.js').as_uri())};
            import {{ renderMovementTree }} from {json.dumps((LIVE / 'tree.js').as_uri())};
            import {{
              STAGE_B1_SCENARIO_IDS, DEFAULT_STAGE_B1_SCENARIO_ID
            }} from {json.dumps((LIVE / 'stage_b1_scenarios.js').as_uri())};
            const make = async (scenarioId) => createStore(await createFixtureDataSource({{ scenarioId }}).load());
            const page = (store, route) => renderRoute(buildRouteViewModel(store, route), route);
            const dialog = (store, type, payload={{}}) => renderDialog(buildDialogViewModel(store), type, payload);
            {body}
            """
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_scenario_set_is_exact_and_accepted_default_is_the_default(self) -> None:
        result = self.run_node(
            """
            const source=createFixtureDataSource(); const store=createStore(await source.load());
            console.log(JSON.stringify({ids:STAGE_B1_SCENARIO_IDS,defaultId:DEFAULT_STAGE_B1_SCENARIO_ID,sourceId:source.scenarioId,contract:store.getState().stageB1}));
            """
        )
        self.assertEqual(
            result["ids"],
            [
                "accepted_default", "not_checked", "checking", "no_changes",
                "recommendations", "destination_choice", "duplicate_choice",
                "ready_for_approval", "ready_to_move", "stale_generation",
                "stale_revision", "disconnected_pages",
            ],
        )
        self.assertEqual(result["defaultId"], "accepted_default")
        self.assertEqual(result["sourceId"], "accepted_default")
        self.assertIsNone(result["contract"])

    def test_accepted_default_html_matches_the_accepted_prototype(self) -> None:
        result = self.run_node(
            f"""
            const current=await make('accepted_default');
            const fixture=JSON.parse(fs.readFileSync({json.dumps(str(PROTOTYPE / 'fixtures.json'))},'utf8'));
            const {{ createStore: createAcceptedStore }}=await import({json.dumps((PROTOTYPE / 'store.js').as_uri())});
            const {{ renderRoute: renderAccepted }}=await import({json.dumps((PROTOTYPE / 'views.js').as_uri())});
            const accepted=createAcceptedStore(fixture);
            const normalize=(html)=>html.replaceAll(/href="[^"]*file-icons[.]svg#/g,'href="file-icons.svg#');
            const routes=['home','organize','duplicate-bin','activity','standards','settings'];
            console.log(JSON.stringify(routes.map(route=>normalize(page(current,route))===normalize(renderAccepted(accepted,route)))));
            """
        )
        self.assertEqual(result, [True] * 6)

    def test_scope_freshness_and_home_workflow_states_are_data_driven(self) -> None:
        result = self.run_node(
            """
            const ids=['not_checked','checking','recommendations','ready_for_approval','ready_to_move','no_changes','stale_generation','stale_revision'];
            console.log(JSON.stringify(Object.fromEntries(await Promise.all(ids.map(async id=>{const store=await make(id);const model=buildHomeViewModel(store);return [id,{scope:model.scope.displayName,fresh:model.freshness.lastCheckedLabel,html:page(store,'home')}];})))));
            """
        )
        expected = {
            "not_checked": ("Files have not been checked yet", "Check files"),
            "checking": ("Checking files…", "Checking files…"),
            "recommendations": ("1 change is waiting", "Continue organizing"),
            "ready_for_approval": ("Ready to approve", "Review and approve"),
            "ready_to_move": ("Ready to move", "Move files — not connected yet"),
            "no_changes": ("Everything is organized", "Check again"),
            "stale_generation": ("Files have changed", "Check again"),
            "stale_revision": ("Organization choices were updated", "Reload choices"),
        }
        for scenario, labels in expected.items():
            with self.subTest(scenario=scenario):
                self.assertEqual(result[scenario]["scope"], "Downloads")
                self.assertNotEqual(result[scenario]["fresh"], "Today, 6:52 PM")
                for label in labels:
                    self.assertIn(label, result[scenario]["html"])
        self.assertIn("disabled", result["checking"]["html"])
        self.assertIn("disabled", result["ready_to_move"]["html"])
        self.assertNotIn("summary-card", result["recommendations"]["html"].split("Organization", 1)[0])

    def test_structured_organize_rows_details_and_kept_items_render_separately(self) -> None:
        result = self.run_node(
            """
            const store=await make('recommendations'); const model=buildOrganizeViewModel(store); const before=page(store,'organize');
            store.selectContractItem('recommend-budget'); const selected=page(store,'organize');
            store.decideContractRecommendation('recommend-budget','keep'); const kept=page(store,'organize');
            console.log(JSON.stringify({before,selected,kept,model:{name:model.organization.recommendations[0].name,size:model.organization.recommendations[0].size,current:model.organization.recommendations[0].currentFolder}}));
            """
        )
        self.assertEqual(result["model"]["name"], "Quarterly report.pdf")
        self.assertEqual(result["model"]["size"], "284 KB")
        self.assertEqual(result["model"]["current"], "Downloads")
        self.assertIn("Recommended changes", result["before"])
        self.assertIn("Choices needed", result["before"])
        self.assertIn("Kept by Standards", result["before"])
        self.assertIn("Budget 2026.xlsx", result["selected"])
        self.assertIn("Current folder", result["selected"])
        self.assertIn("Kept here", result["kept"])
        self.assertNotIn("home-summaries", result["before"])
        self.assertNotIn("implementation-status", result["before"])

    def test_destination_choice_uses_supplied_options_and_stores_only_the_selected_id(self) -> None:
        result = self.run_node(
            """
            const store=await make('destination_choice'); const before=dialog(store,'destination',{itemId:'choose-notes-folder'});
            const selected=store.selectDestinationOption('choose-notes-folder','notes-keep');
            const after=dialog(store,'destination',{itemId:'choose-notes-folder'});
            const confirmed=store.confirmDestinationChoice('choose-notes-folder');
            const choice=store.getState().stageB1.organization.choicesNeeded[0];
            console.log(JSON.stringify({before,after,selected,confirmed,stored:choice.selectedOptionId,done:choice.confirmed}));
            """
        )
        self.assertIn("Projects › Elarian › Notes", result["before"]["content"])
        self.assertIn("Keep in Downloads", result["before"]["content"])
        self.assertNotIn("type=\"text\"", result["before"]["content"])
        self.assertTrue(result["selected"] and result["confirmed"] and result["done"])
        self.assertEqual(result["stored"], "notes-keep")
        self.assertIn("checked", result["after"]["content"])

    def test_duplicate_choice_enforces_a_nonempty_proper_subset(self) -> None:
        result = self.run_node(
            """
            const store=await make('duplicate_choice');
            const zeroSet=store.setDuplicateCopies('choose-brand-copies',[]); const zero=store.confirmDuplicateChoice('choose-brand-copies');
            store.setDuplicateCopies('choose-brand-copies',['brand-assets','brand-downloads','brand-shared']); const all=store.confirmDuplicateChoice('choose-brand-copies');
            const duplicateIds=store.setDuplicateCopies('choose-brand-copies',['brand-assets','brand-assets']);
            store.setDuplicateCopies('choose-brand-copies',['brand-downloads']); const proper=store.confirmDuplicateChoice('choose-brand-copies');
            const html=dialog(store,'duplicate',{itemId:'choose-brand-copies'}).content;
            store.setDuplicateDecision('choose-brand-copies','keep_all'); const keepAll=store.confirmDuplicateChoice('choose-brand-copies');
            console.log(JSON.stringify({zeroSet,zero,all,duplicateIds,proper,keepAll,html}));
            """
        )
        self.assertTrue(result["zeroSet"])
        self.assertFalse(result["zero"])
        self.assertFalse(result["all"])
        self.assertFalse(result["duplicateIds"])
        self.assertTrue(result["proper"])
        self.assertTrue(result["keepAll"])
        self.assertIn("Keep all copies", result["html"])
        self.assertIn("Choose copies for Duplicate Bin", result["html"])
        for forbidden in ("SHA-256", "reference copy", "original"):
            self.assertNotIn(forbidden, result["html"])

    def test_supplied_tree_is_rendered_without_rebuilding_or_forbidden_items(self) -> None:
        result = self.run_node(
            """
            const store=await make('recommendations'); const model=buildOrganizeViewModel(store);
            const before=renderMovementTree(model.organization.movementTree);
            store.decideContractRecommendation('recommend-report','keep');
            const after=renderMovementTree(buildOrganizeViewModel(store).organization.movementTree);
            const choiceTree=page(await make('destination_choice'),'organize');
            console.log(JSON.stringify({before,after,choiceTree}));
            """
        )
        self.assertEqual(result["before"], result["after"])
        self.assertIn("Quarterly report.pdf", result["before"])
        self.assertNotIn("Budget 2026.xlsx", result["before"])
        self.assertNotIn("submission.pdf", result["before"])
        self.assertNotIn("Project notes.md", result["choiceTree"].split("Where files will go", 1)[1])
        self.assertIn('role="tree"', result["before"])
        self.assertNotIn("data-action", result["before"])

    def test_approval_ready_to_move_and_stale_states_have_product_language_only(self) -> None:
        result = self.run_node(
            """
            const store=await make('ready_for_approval'); const activityBefore=store.getState().activityMoves.length;
            const approval=dialog(store,'approval'); const approved=store.approveScenario();
            const ready=page(store,'organize'); const activityAfter=store.getState().activityMoves.length;
            const staleGeneration=page(await make('stale_generation'),'organize'); const staleRevision=page(await make('stale_revision'),'organize');
            console.log(JSON.stringify({approval,approved,ready,activityBefore,activityAfter,staleGeneration,staleRevision}));
            """
        )
        self.assertEqual(result["approval"]["title"], "Ready to organize")
        self.assertIn("2", result["approval"]["content"])
        self.assertIn("files will move", result["approval"]["content"])
        self.assertIn("duplicate copy is set aside for a later step", result["approval"]["content"])
        self.assertEqual(result["activityBefore"], result["activityAfter"])
        self.assertIsNotNone(result["approved"])
        self.assertIn("Ready to move", result["ready"])
        self.assertIn("No files have moved yet.", result["ready"])
        self.assertIn("Move files — not connected yet", result["ready"])
        self.assertIn("disabled>Move files — not connected yet", result["ready"])
        self.assertIn("Files have changed", result["staleGeneration"])
        self.assertIn("Check the folder again before continuing.", result["staleGeneration"])
        self.assertIn("Organization choices were updated", result["staleRevision"])
        self.assertIn("Reload the latest choices.", result["staleRevision"])

    def test_disconnected_pages_show_no_accepted_fixture_records_or_actions(self) -> None:
        result = self.run_node(
            """
            const store=await make('disconnected_pages'); console.log(JSON.stringify({bin:page(store,'duplicate-bin'),activity:page(store,'activity'),standards:page(store,'standards'),settings:page(store,'settings')}));
            """
        )
        self.assertIn("Duplicate Bin is not connected yet.", result["bin"])
        self.assertNotIn("course-outline.pdf", result["bin"])
        self.assertIn("No file moves recorded.", result["activity"])
        self.assertNotIn("Undo move", result["activity"])
        self.assertIn("Standards editing is not connected yet.", result["standards"])
        self.assertNotIn("Keep Submissions unchanged", result["standards"])
        self.assertIn("Appearance", result["settings"])

    def test_fixture_boundary_remains_explicit_without_persistence_or_dom_bypass(self) -> None:
        fixture_source = (LIVE / "data_source.js").read_text(encoding="utf-8")
        lower = fixture_source.lower()
        for forbidden in (
            "fetch(", "xmlhttprequest", "websocket", "eventsource",
            "localstorage", "sessionstorage", "indexeddb",
            "document.cookie", "shutil", "os.rename", "path.rename",
        ):
            self.assertNotIn(forbidden, lower)
        self.assertIn('import("./fixtures.json"', fixture_source)
        self.assertIn('import("./stage_b1_scenarios.js")', fixture_source)
        self.assertNotRegex(lower, r"[?&](fixture|scenario|debug)=")
        app = (LIVE / "app.js").read_text(encoding="utf-8")
        self.assertEqual(app.count("elements.app.innerHTML ="), 1)
        self.assertIn("renderRoute(buildRouteViewModel(viewSource, state.route), state.route)", app)
        self.assertNotIn("createFixtureDataSource", app)

    def test_prohibited_visible_language_is_absent(self) -> None:
        authored = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [*sorted(LIVE.glob("*.js")), LIVE / "fixtures.json"]
        )
        for phrase in (
            "BACKEND VERIFIED MOVEMENT", "backend-provided metadata",
            "backend-provided destinations", "Organization styles are not connected yet",
            "scan generation", "decision revision", "option ID", "copy ID",
            "fixture operation", "immutable derived structure", "structural plan",
            "simulation",
        ):
            self.assertNotIn(phrase.lower(), authored.lower())


if __name__ == "__main__":
    unittest.main()
