from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"


def _file(
    item_id: str,
    name: str,
    path: str,
    folder: str,
    *,
    kind: str = "pdf",
    size: int = 2048,
) -> dict[str, object]:
    return {
        "id": item_id,
        "name": name,
        "entity_type": "file",
        "kind": kind,
        "extension": Path(name).suffix,
        "size_bytes": size,
        "modified_at": "2026-07-15T12:00:00+00:00",
        "relative_path": path,
        "current_folder": folder,
    }


def _payload() -> dict[str, object]:
    recommendation = _file(
        "g1:Inbox/report.pdf",
        "report.pdf",
        "Inbox/report.pdf",
        "Inbox",
    )
    destination = _file(
        "g1:Inbox/notes.md",
        "notes.md",
        "Inbox/notes.md",
        "Inbox",
        kind="markdown",
        size=512,
    )
    first_copy = _file(
        "g1:Inbox/copy.zip",
        "copy.zip",
        "Inbox/copy.zip",
        "Inbox",
        kind="archive",
        size=4096,
    )
    second_copy = _file(
        "g1:Archive/copy.zip",
        "copy.zip",
        "Archive/copy.zip",
        "Archive",
        kind="archive",
        size=4096,
    )
    protected = _file(
        "g1:Submissions/work.pdf",
        "work.pdf",
        "Submissions/work.pdf",
        "Submissions",
    )
    return {
        "scope": {"display_name": "Downloads", "root_label": "."},
        "freshness": {
            "scan_generation": "g1",
            "last_checked_at": "2026-07-15T12:00:00+00:00",
            "last_organized_at": None,
        },
        "planning": {
            "strategy_id": "current_deterministic",
            "organization_style": None,
        },
        "execution": {
            "status": "not_ready",
            "operation_id": None,
            "files_expected": 0,
            "files_moved": 0,
            "files_failed": 0,
            "files_unattempted": 0,
            "verification_status": None,
        },
        "recommended_changes": [
            {
                "id": "O1",
                "action": "move",
                "item": recommendation,
                "destination_folder": "Documents/Reports",
                "reason_code": "matched_existing_group",
            }
        ],
        "choices_needed": [
            {
                "id": "C1",
                "choice_type": "choose_destination",
                "reason_code": "multiple_safe_destinations",
                "item": destination,
                "options": [
                    {
                        "id": "destination-1",
                        "action": "move",
                        "destination_folder": "Projects/Notes",
                    },
                    {"id": "keep", "action": "keep"},
                ],
            },
            {
                "id": "D1",
                "choice_type": "exact_duplicate",
                "reason_code": "exact_duplicate_choice",
                "identical_copy_count": 2,
                "copies": [first_copy, second_copy],
                "options": [
                    {"id": "keep-all", "action": "keep_all_copies"},
                    {
                        "id": "choose-for-bin",
                        "action": "choose_copies_for_duplicate_bin",
                    },
                ],
            },
        ],
        "kept_by_standards": [
            {
                "item": protected,
                "standard": {"id": "submission", "name": "Protect submissions"},
                "reason_code": "submission_folder_protected",
            }
        ],
        "summary": {
            "recommended_changes": 1,
            "choices_needed": 2,
            "kept_by_standards": 1,
            "files_to_move": 1,
            "names_to_change": 0,
            "folders_to_create": 0,
            "duplicate_copies": 1,
            "movement_tree": [
                {"folder": "Documents/Reports", "files": ["Inbox/report.pdf"]}
            ],
        },
        "review": {
            "status": "in_progress",
            "scan_generation": "g1",
            "decision_revision": 0,
            "recommendations": [{"change_id": "O1", "decision": "undecided"}],
            "choices": [
                {"choice_id": "C1", "option_id": None, "selected_copy_ids": []},
                {"choice_id": "D1", "option_id": None, "selected_copy_ids": []},
            ],
            "summary": {
                "pending_recommendations": 1,
                "pending_choices": 2,
                "accepted_moves": 0,
                "kept_recommendations": 0,
                "duplicate_copies_for_future_bin": 0,
                "choices_remaining": 3,
                "ready_for_approval": False,
                "files_to_move": 0,
                "names_to_change": 0,
                "folders_to_create": 0,
            },
            "approval": None,
        },
    }


class LociFlowStageB2AdapterTests(unittest.TestCase):
    maxDiff = None

    def run_node(self, body: str) -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            import assert from "node:assert/strict";
            import * as api from {json.dumps((LOCI / 'api.js').as_uri())};
            import {{
              createBackendDataSource, isSafeRelativePath,
              validateOrganizationResponse
            }} from {json.dumps((LOCI / 'data_source.js').as_uri())};
            import {{
              buildProductionStageB1Contract, buildRouteViewModel
            }} from {json.dumps((LOCI / 'view_models.js').as_uri())};
            import {{ renderRoute }} from {json.dumps((LOCI / 'views.js').as_uri())};
            const original = {json.dumps(_payload())};
            const clone = (value) => JSON.parse(JSON.stringify(value));
            const payload = clone(original);
            {body}
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
        return json.loads(result.stdout)

    def test_locked_stage_b1_assets_keep_their_committed_hashes(self) -> None:
        expected = {
            "styles.css": "6288229571bb209624e00a2a82c639c93ea8eae1aca3dbad9022cb5203c637dd",
            "views.js": "0b26b9a25c07b6484a42584d957792b93aef1a16c81ddc97f1f9d50240748e0c",
            "tree.js": "5dc9db40114c78f1dd9b7d81dfb2a52c6c5b8f73670d8a6f2bed49cc7d385975",
            "store.js": "ac0876ec961fadd4b62115d1f372606fa536b2a00ac026029578a263bb5ad3be",
            "fixtures.json": "0d614d82bf3aa75b4ab102a77d47e77b3d3ef7ed390afd6119c1e4099e36fec1",
            "stage_b1_scenarios.js": "fdc12218e17521a15ffcb7e5aa85e64e80d177399db7a037d616388d580c4f08",
            "assets/file-icons.svg": "bfefdc18d03629036a5d1fe31b614ebcf70ab8acf7926169e6ef10e1926595c9",
            "assets/loci-flow-mark.svg": "ec0d01fa8e1f312e3233b0f7758f7192d8b40de1370cb48b0f95bce87686f439",
        }
        actual = {
            relative: hashlib.sha256((LOCI / relative).read_bytes()).hexdigest()
            for relative in expected
        }
        self.assertEqual(actual, expected)

    def test_api_client_uses_only_fixed_same_origin_contract_fields(self) -> None:
        result = self.run_node(
            """
            const requests=[];
            const fetchImpl=async(url,options)=>{
              requests.push({url,options});
              return {ok:true,status:200,json:async()=>clone(payload)};
            };
            await api.getCurrentOrganization(fetchImpl);
            await api.startScan({csrfToken:"csrf"},fetchImpl);
            await api.updateRecommendationDecision({scanGeneration:"g1",expectedRevision:1,changeId:"O1",decision:"accept"},fetchImpl,"csrf");
            await api.updateChoiceDecision({scanGeneration:"g1",expectedRevision:2,choiceId:"C1",optionId:"destination-1"},fetchImpl,"csrf");
            await api.updateChoiceDecision({scanGeneration:"g1",expectedRevision:3,choiceId:"D1",optionId:"choose-for-bin",copyIds:["g1:Inbox/copy.zip"]},fetchImpl,"csrf");
            await api.acceptRecommendedChanges({scanGeneration:"g1",expectedRevision:4,expectedChangeIds:["O1"]},fetchImpl,"csrf");
            await api.approveOrganizationPlan({scanGeneration:"g1",expectedRevision:5},fetchImpl,"csrf");
            const normalized=requests.map(({url,options})=>({
              url,method:options.method,credentials:options.credentials,
              accept:options.headers.Accept,
              contentType:options.headers["Content-Type"]||null,
              fields:options.body ? Object.fromEntries(options.body) : {},
            }));
            console.log(JSON.stringify(normalized));
            """
        )
        self.assertEqual(
            [item["url"] for item in result],
            [
                "/api/organization/current",
                "/scan?surface=home",
                "/api/organization/decisions",
                "/api/organization/decisions",
                "/api/organization/decisions",
                "/api/organization/decisions",
                "/api/organization/approval",
            ],
        )
        self.assertTrue(all(item["credentials"] == "same-origin" for item in result))
        self.assertTrue(all(item["accept"] == "application/json" for item in result))
        mutation_fields = set().union(*(item["fields"] for item in result[1:]))
        for forbidden in (
            "root",
            "source",
            "destination",
            "path",
            "summary",
            "plan",
            "sha256",
            "accept_all",
        ):
            self.assertNotIn(forbidden, mutation_fields)
        self.assertEqual(set(result[1]["fields"]), {"csrf_token"})

    def test_contract_validation_rejects_unsafe_and_inconsistent_data(self) -> None:
        result = self.run_node(
            """
            const valid=validateOrganizationResponse(payload);
            const unsafe=["/etc/passwd","../escape","C:/escape","https://example.test/x","\\\\server\\share","Inbox\\..\\escape","bad\\0path"];
            const rejected=[];
            for(const path of unsafe){
              const candidate=clone(original); candidate.recommended_changes[0].item.relative_path=path;
              try{validateOrganizationResponse(candidate);rejected.push(false);}catch{rejected.push(true);}
            }
            const malformed=[];
            for(const mutate of [
              (x)=>delete x.scope.display_name,
              (x)=>x.summary.recommended_changes=2,
              (x)=>x.kept_by_standards[0].item.id=x.recommended_changes[0].item.id,
              (x)=>x.summary.movement_tree[0].files=["Submissions/work.pdf"],
              (x)=>x.review.summary.ready_for_approval=true,
              (x)=>x.review.choices[0].option_id="invented",
            ]){
              const candidate=clone(original); mutate(candidate);
              try{validateOrganizationResponse(candidate);malformed.push(false);}catch{malformed.push(true);}
            }
            console.log(JSON.stringify({frozen:Object.isFrozen(valid),rejected,malformed,safe:[isSafeRelativePath("Inbox/file.txt"),isSafeRelativePath(".")]}));
            """
        )
        self.assertTrue(result["frozen"])
        self.assertTrue(all(result["rejected"]))
        self.assertTrue(all(result["malformed"]))
        self.assertEqual(result["safe"], [True, True])

    def test_real_view_model_maps_backend_values_without_inference_or_leaks(self) -> None:
        result = self.run_node(
            """
            payload.recommended_changes[0].item.kind="future_kind";
            const valid=validateOrganizationResponse(payload);
            const contract=buildProductionStageB1Contract({snapshot:{status:"completed",backendState:valid}});
            const source={getState:()=>({route:"organize",theme:"light",selectedItemId:"C1",stageB1:contract})};
            const model=buildRouteViewModel(source,"organize");
            const html=renderRoute(model,"organize");
            console.log(JSON.stringify({
              id:contract.id,workflow:contract.workflow,scope:contract.scope,
              recommendation:contract.organization.recommendations[0],
              destination:contract.organization.choicesNeeded[0],
              duplicate:contract.organization.choicesNeeded[1],
              kept:contract.organization.keptByStandards[0],
              tree:contract.organization.movementTree,
              disconnected:{bin:contract.duplicateBin,activity:contract.activity,standards:contract.standards},
              html,
            }));
            """
        )
        self.assertEqual(result["id"], "backend")
        self.assertEqual(result["workflow"]["status"], "3 changes are waiting")
        self.assertEqual(result["scope"]["displayName"], "Downloads")
        self.assertEqual(result["recommendation"]["kind"], "Document")
        self.assertEqual(result["recommendation"]["destinationFolderLabel"], "Documents › Reports")
        self.assertEqual(result["recommendation"]["reasonText"], "Clear folder match")
        self.assertEqual(
            [option["optionId"] for option in result["destination"]["options"]],
            ["destination-1", "keep"],
        )
        self.assertEqual(result["destination"]["options"][1]["label"], "Keep here")
        self.assertEqual(len(result["duplicate"]["copies"]), 2)
        self.assertEqual(result["kept"]["standardName"], "Protect submissions")
        self.assertEqual(result["tree"]["nodes"][0]["name"], "Documents › Reports")
        self.assertTrue(all(not value["connected"] for value in result["disconnected"].values()))
        for forbidden in (
            "matched_existing_group",
            "multiple_safe_destinations",
            "exact_duplicate_choice",
            "sha256",
            "scan_generation",
            "g1:Inbox",
        ):
            self.assertNotIn(forbidden, result["html"])

    def test_backend_data_source_polls_serializes_and_uses_current_revision(self) -> None:
        result = self.run_node(
            """
            const delays=[]; const calls=[];
            const accepted=clone(payload);
            accepted.review.decision_revision=1;
            accepted.review.recommendations[0].decision="accept";
            Object.assign(accepted.review.summary,{
              pending_recommendations:0,accepted_moves:1,files_to_move:1,
              choices_remaining:2,ready_for_approval:false,
            });
            const apiClient={
              getCurrentOrganization:async()=>clone(payload),
              startScan:async(value)=>{calls.push(["scan",value]);},
              updateRecommendationDecision:async(value,csrf)=>{calls.push(["recommendation",value,csrf]);return clone(accepted);},
              updateChoiceDecision:async(value,csrf)=>{calls.push(["choice",value,csrf]);return clone(payload);},
              acceptRecommendedChanges:async(value,csrf)=>{calls.push(["bulk",value,csrf]);return clone(payload);},
              approveOrganizationPlan:async(value,csrf)=>{calls.push(["approval",value,csrf]);return clone(payload);},
            };
            const source=createBackendDataSource({
              apiClient,csrfToken:"csrf",pollIntervalMs:1,
              schedule:(callback,delay)=>{delays.push(delay);queueMicrotask(callback);return 7;},
              cancel:()=>{},
            });
            await source.loadCurrentState();
            const decided=await source.decideRecommendation("O1","accept");
            const scanned=await source.startScan();
            let invalidSubset=false;
            try{await source.decideChoice("D1","choose-for-bin",[]);}catch(error){invalidSubset=error.code==="invalid_contract";}

            let release;
            const heldApi={...apiClient,updateRecommendationDecision:()=>new Promise((resolve)=>{release=resolve;})};
            const held=createBackendDataSource({apiClient:heldApi,csrfToken:"csrf"});
            await held.loadCurrentState();
            const first=held.decideRecommendation("O1","accept");
            let serialized=false;
            try{await held.decideRecommendation("O1","keep");}catch(error){serialized=error.code==="mutation_in_progress";}
            release(clone(accepted)); await first;
            source.dispose(); held.dispose();
            console.log(JSON.stringify({delays,calls,revision:decided.backendState.review.decision_revision,scanStatus:scanned.status,invalidSubset,serialized}));
            """
        )
        self.assertTrue(result["delays"] and min(result["delays"]) >= 1000)
        recommendation = next(call for call in result["calls"] if call[0] == "recommendation")
        self.assertEqual(recommendation[1]["scanGeneration"], "g1")
        self.assertEqual(recommendation[1]["expectedRevision"], 0)
        self.assertEqual(recommendation[2], "csrf")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["scanStatus"], "completed")
        self.assertTrue(result["invalidSubset"])
        self.assertTrue(result["serialized"])

    def test_production_bootstrap_has_no_fixture_fallback_or_future_requests(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        data_source = (LOCI / "data_source.js").read_text(encoding="utf-8")
        api = (LOCI / "api.js").read_text(encoding="utf-8")
        self.assertIn("createBackendDataSource", app)
        self.assertNotIn("createFixtureDataSource", app)
        self.assertNotIn("fixtures.json", app)
        self.assertNotIn("stage_b1_scenarios", app)
        self.assertIn('import("./fixtures.json"', data_source)
        self.assertNotIn('from "./fixtures.json"', data_source)
        self.assertNotIn("accepted_default", app)
        combined = (app + data_source + api).lower()
        for forbidden in (
            "localstorage",
            "sessionstorage",
            "indexeddb",
            "document.cookie",
            "xmlhttprequest",
            "websocket",
            "eventsource",
            "serviceworker",
            "/api/undo",
        ):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
