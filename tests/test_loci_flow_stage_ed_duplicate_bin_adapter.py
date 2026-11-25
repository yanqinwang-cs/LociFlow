from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from tests.test_loci_flow_stage_b2_adapter import _payload

ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"
BATCH_ID = "2d7d7186-53ec-4767-885f-c9a52c4edb5d"
ITEM_A = "112022c2-5f44-44be-93d8-53a9cb724659"
ITEM_B = "1addad6f-9877-4dce-892c-439c9d0483a5"
TOKEN = "530af841-39c9-44ea-9f7a-bdf91d6f9195"
OPERATION = "b6fddda2-2c87-4891-8774-372b7aac71de"


def _ready_payload() -> dict[str, object]:
    payload = _payload()
    review = payload["review"]
    assert isinstance(review, dict)
    review["status"] = "ready_to_move"
    review["recommendations"] = [{"change_id": "O1", "decision": "accept"}]
    review["choices"] = [
        {"choice_id": "C1", "option_id": "destination-1", "selected_copy_ids": []},
        {
            "choice_id": "D1",
            "option_id": "choose-for-bin",
            "selected_copy_ids": ["g1:Inbox/copy.zip"],
        },
    ]
    review["summary"] = {
        "pending_recommendations": 0,
        "pending_choices": 0,
        "accepted_moves": 2,
        "kept_recommendations": 0,
        "duplicate_copies_for_future_bin": 1,
        "choices_remaining": 0,
        "ready_for_approval": True,
        "files_to_move": 2,
        "names_to_change": 0,
        "folders_to_create": 0,
    }
    review["approval"] = {
        "approved_at": "2026-07-17T08:00:00+00:00",
        "summary": {},
    }
    payload["execution"] = {
        "status": "ready",
        "operation_id": None,
        "files_expected": 2,
        "files_moved": 0,
        "files_failed": 0,
        "files_unattempted": 2,
        "verification_status": None,
    }
    payload["duplicate_batch"] = {
        "id": BATCH_ID,
        "status": "approved",
        "copies": 1,
    }
    return payload


class LociFlowStageEDDuplicateBinAdapterTests(unittest.TestCase):
    maxDiff = None

    def run_node(self, body: str) -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            import * as api from {json.dumps((LOCI / 'api.js').as_uri())};
            import {{
              createBackendDataSource, createFixtureDataSource,
              validateDuplicateBinResponse, validateOrganizationResponse,
              validateActivityResponse, validateUndoPreview,
            }} from {json.dumps((LOCI / 'data_source.js').as_uri())};
            import {{
              buildProductionStageB1Contract, buildRouteViewModel,
            }} from {json.dumps((LOCI / 'view_models.js').as_uri())};
            import {{ renderRoute }} from {json.dumps((LOCI / 'views.js').as_uri())};
            const ready={json.dumps(_ready_payload())};
            const batchId={json.dumps(BATCH_ID)};
            const itemA={json.dumps(ITEM_A)};
            const itemB={json.dumps(ITEM_B)};
            const token={json.dumps(TOKEN)};
            const operationId={json.dumps(OPERATION)};
            const movedAt="2026-07-17T09:00:00+00:00";
            const listing={{items:[
              {{id:itemA,name:"copy-a.txt",previous_folder:"Inbox",moved_at:movedAt,size:12,kind:"text",restore_available:true,attention_message:null}},
              {{id:itemB,name:"copy-b.txt",previous_folder:"Archive",moved_at:movedAt,size:12,kind:"text",restore_available:false,attention_message:"This copy needs attention."}},
            ]}};
            const movePreview={{copies_to_move:1,confirmation:"MOVE COPIES TO DUPLICATE BIN",confirmation_token:token,expires_at:"2026-07-17T09:15:00+00:00"}};
            const restorePreview={{copies_to_restore:1,confirmation:"RESTORE COPIES",confirmation_token:token,expires_at:"2026-07-17T09:15:00+00:00"}};
            const moveResult={{operation:{{id:operationId,status:"moved",copies_expected:1,copies_moved:1,copies_failed:0,copies_unattempted:0,verification_status:"passed",started_at:movedAt,completed_at:movedAt,recovery_required:false}}}};
            const restoreResult={{restore:{{id:operationId,status:"restored",copies_expected:1,copies_restored:1,copies_failed:0,copies_unattempted:0,verification_status:"passed",started_at:movedAt,completed_at:movedAt,recovery_required:false}}}};
            {body}
            """
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_api_uses_exact_routes_and_fields_without_internal_data(self) -> None:
        result = self.run_node(
            """
            const calls=[];
            const fetcher=async(url,options)=>{
              calls.push({url,options});
              const payload=url.endsWith("/move/preview")?movePreview
                :url.endsWith("/move")?moveResult
                :url.endsWith("/restore/preview")?restorePreview
                :url.endsWith("/restore")?restoreResult:listing;
              return {ok:true,status:200,json:async()=>payload};
            };
            await api.previewDuplicateBinMove(batchId,fetcher,"csrf");
            await api.confirmDuplicateBinMove({confirmationToken:token,confirmation:"MOVE COPIES TO DUPLICATE BIN"},fetcher,"csrf");
            await api.getDuplicateBin(fetcher);
            await api.previewDuplicateBinRestore([itemA,itemB],fetcher,"csrf");
            await api.confirmDuplicateBinRestore({confirmationToken:token,confirmation:"RESTORE COPIES"},fetcher,"csrf");
            console.log(JSON.stringify(calls.map(({url,options})=>({
              url,method:options.method,credentials:options.credentials,
              fields:options.body?Object.fromEntries(options.body):{},
            }))));
            """
        )
        self.assertEqual([call["url"] for call in result], [
            "/api/duplicate-bin/move/preview",
            "/api/duplicate-bin/move",
            "/api/duplicate-bin",
            "/api/duplicate-bin/restore/preview",
            "/api/duplicate-bin/restore",
        ])
        self.assertEqual(result[0]["fields"], {"csrf_token": "csrf", "batch_id": BATCH_ID})
        self.assertEqual(result[1]["fields"], {
            "csrf_token": "csrf",
            "confirmation_token": TOKEN,
            "confirmation": "MOVE COPIES TO DUPLICATE BIN",
        })
        self.assertEqual(result[2]["method"], "GET")
        self.assertEqual(result[3]["fields"], {
            "csrf_token": "csrf",
            "item_ids": json.dumps([ITEM_A, ITEM_B], separators=(",", ":")),
        })
        self.assertEqual(result[4]["fields"], {
            "csrf_token": "csrf",
            "confirmation_token": TOKEN,
            "confirmation": "RESTORE COPIES",
        })
        serialized = json.dumps(result).lower()
        for forbidden in ("source", "destination", "sha256", "batch_version", "scan_generation"):
            self.assertNotIn(forbidden, serialized)

    def test_data_source_delegates_serializes_and_keeps_fixture_inert(self) -> None:
        result = self.run_node(
            """
            const calls=[];
            const client={
              getCurrentOrganization:async()=>ready,
              previewDuplicateBinMove:async(id,csrf)=>{calls.push(["move-preview",id,csrf]);return movePreview;},
              confirmDuplicateBinMove:async(payload,csrf)=>{calls.push(["move",payload,csrf]);return moveResult;},
              getDuplicateBin:async()=>{calls.push(["bin"]);return listing;},
              previewDuplicateBinRestore:async(ids,csrf)=>{calls.push(["restore-preview",ids,csrf]);return restorePreview;},
              confirmDuplicateBinRestore:async(payload,csrf)=>{calls.push(["restore",payload,csrf]);return restoreResult;},
            };
            const source=createBackendDataSource({apiClient:client,csrfToken:"csrf"});
            await source.loadCurrentState();
            await source.previewDuplicateBinMove(batchId);
            await source.confirmDuplicateBinMove(token,"MOVE COPIES TO DUPLICATE BIN");
            const bin=await source.loadDuplicateBin();
            let attentionRejected=false;
            try{await source.previewDuplicateBinRestore([itemB]);}catch{attentionRejected=true;}
            await source.previewDuplicateBinRestore([itemA]);
            await source.confirmDuplicateBinRestore(token,"RESTORE COPIES");
            const fixture=createFixtureDataSource();
            const blocked=[];
            for(const call of [
              ()=>fixture.previewDuplicateBinMove(batchId),
              ()=>fixture.confirmDuplicateBinMove(token,"MOVE COPIES TO DUPLICATE BIN"),
              ()=>fixture.previewDuplicateBinRestore([itemA]),
              ()=>fixture.confirmDuplicateBinRestore(token,"RESTORE COPIES"),
            ]){try{await call();blocked.push(false);}catch(error){blocked.push(error.code==="movement_unavailable");}}
            console.log(JSON.stringify({calls,bin,attentionRejected,blocked}));
            """
        )
        self.assertTrue(result["attentionRejected"])
        self.assertTrue(all(result["blocked"]))
        self.assertEqual(result["calls"][0], ["move-preview", BATCH_ID, "csrf"])
        self.assertEqual(result["calls"][1][1], {
            "confirmationToken": TOKEN,
            "confirmation": "MOVE COPIES TO DUPLICATE BIN",
        })
        self.assertEqual(result["calls"][3], ["restore-preview", [ITEM_A], "csrf"])
        self.assertEqual(result["calls"][4][1], {
            "confirmationToken": TOKEN,
            "confirmation": "RESTORE COPIES",
        })

    def test_duplicate_actions_are_serialized_and_restore_selection_is_literal(self) -> None:
        result = self.run_node(
            """
            let releaseMove;
            let releaseRestore;
            const movePending=new Promise((resolve)=>{releaseMove=resolve;});
            const restorePending=new Promise((resolve)=>{releaseRestore=resolve;});
            const calls=[];
            const both={items:listing.items.map((item)=>({...item,restore_available:true,attention_message:null}))};
            const client={
              getCurrentOrganization:async()=>ready,
              getDuplicateBin:async()=>both,
              confirmDuplicateBinMove:async(payload)=>{calls.push(["move",payload]);await movePending;return moveResult;},
              previewDuplicateBinRestore:async(ids)=>{calls.push(["restore-preview",ids]);return restorePreview;},
              confirmDuplicateBinRestore:async(payload)=>{calls.push(["restore",payload]);await restorePending;return restoreResult;},
            };
            const source=createBackendDataSource({apiClient:client,csrfToken:"csrf"});
            await source.loadCurrentState();
            await source.loadDuplicateBin();
            const firstMove=source.confirmDuplicateBinMove(token,"MOVE COPIES TO DUPLICATE BIN");
            let moveBlocked=false;
            try{await source.confirmDuplicateBinMove(token,"MOVE COPIES TO DUPLICATE BIN");}
            catch(error){moveBlocked=error.code==="movement_or_undo_in_progress";}
            releaseMove();await firstMove;
            await source.previewDuplicateBinRestore([itemB,itemA]);
            const firstRestore=source.confirmDuplicateBinRestore(token,"RESTORE COPIES");
            let restoreBlocked=false;
            try{await source.confirmDuplicateBinRestore(token,"RESTORE COPIES");}
            catch(error){restoreBlocked=error.code==="movement_or_undo_in_progress";}
            releaseRestore();await firstRestore;
            console.log(JSON.stringify({calls,moveBlocked,restoreBlocked}));
            """
        )
        self.assertTrue(result["moveBlocked"])
        self.assertTrue(result["restoreBlocked"])
        self.assertEqual(result["calls"][1], ["restore-preview", [ITEM_B, ITEM_A]])
        self.assertEqual(sum(call[0] == "move" for call in result["calls"]), 1)
        self.assertEqual(sum(call[0] == "restore" for call in result["calls"]), 1)

    def test_view_models_keep_actions_separate_and_render_safe_bin_states(self) -> None:
        result = self.run_node(
            """
            const valid=validateOrganizationResponse(ready);
            const duplicateBin={loadStatus:"ready",items:validateDuplicateBinResponse(listing).items,selectedIds:[itemA],restorePending:false};
            const contract=buildProductionStageB1Contract({snapshot:{status:"completed",backendState:valid},duplicateBin});
            const source={getState:()=>({route:"organize",theme:"light",selectedItemId:null,stageB1:contract})};
            const organize=renderRoute(buildRouteViewModel(source,"organize"),"organize");
            const bin=renderRoute(buildRouteViewModel(source,"duplicate-bin"),"duplicate-bin");
            const emptyContract=buildProductionStageB1Contract({snapshot:{status:"completed",backendState:valid},duplicateBin:{loadStatus:"ready",items:[],selectedIds:[]}});
            const emptySource={getState:()=>({route:"duplicate-bin",theme:"light",selectedItemId:null,stageB1:emptyContract})};
            const empty=renderRoute(buildRouteViewModel(emptySource,"duplicate-bin"),"duplicate-bin");
            console.log(JSON.stringify({organize,bin,empty,action:contract.organization.duplicateMovement}));
            """
        )
        self.assertIn('data-action="preview-move"', result["organize"])
        self.assertIn('data-action="preview-duplicate-bin-move"', result["organize"])
        self.assertEqual(result["action"]["action"]["label"], "Move copies to Duplicate Bin")
        self.assertIn("Previous folder", result["bin"])
        self.assertIn("This copy needs attention.", result["bin"])
        self.assertIn('data-bin-id="' + ITEM_A + '"', result["bin"])
        self.assertIn("Duplicate Bin is empty", result["empty"])
        serialized = (result["organize"] + result["bin"] + result["empty"]).lower()
        for forbidden in ("sha256", "batch_version", "empty bin", "permanent", "safe to delete"):
            self.assertNotIn(forbidden, serialized)

    def test_organize_and_duplicate_bin_cover_recovered_and_empty_ordinary_states(self) -> None:
        result = self.run_node(
            """
            const make=(payload,duplicateMovement=null,duplicateBin=null)=>buildProductionStageB1Contract({
              snapshot:{status:"completed",backendState:validateOrganizationResponse(payload)},
              duplicateMovement,duplicateBin,
            });
            const noBatch=JSON.parse(JSON.stringify(ready));noBatch.duplicate_batch=null;
            const duplicateOnly=JSON.parse(JSON.stringify(ready));
            duplicateOnly.review.recommendations[0].decision="keep";
            duplicateOnly.review.choices[0].option_id="keep";
            duplicateOnly.execution={status:"no_supported_moves",operation_id:null,files_expected:0,files_moved:0,files_failed:0,files_unattempted:0,verification_status:null};
            duplicateOnly.review.summary.files_to_move=0;
            duplicateOnly.review.summary.accepted_moves=0;
            const recovered=JSON.parse(JSON.stringify(duplicateOnly));recovered.duplicate_batch.status="moved";
            const noBatchContract=make(noBatch);
            const duplicateOnlyContract=make(duplicateOnly);
            const recoveredContract=make(recovered);
            const loading=make(ready,null,{loadStatus:"loading",items:[],selectedIds:[]});
            const error=make(ready,null,{loadStatus:"error",items:[],selectedIds:[],errorMessage:"Try again."});
            const source=(contract,route)=>({getState:()=>({route,theme:"light",selectedItemId:null,stageB1:contract})});
            console.log(JSON.stringify({
              noBatch:noBatchContract.organization.duplicateMovement,
              duplicateOnly:duplicateOnlyContract.organization.duplicateMovement,
              duplicateOnlyHtml:renderRoute(buildRouteViewModel(source(duplicateOnlyContract,"organize"),"organize"),"organize"),
              recovered:recoveredContract.organization.duplicateMovement,
              loading:renderRoute(buildRouteViewModel(source(loading,"duplicate-bin"),"duplicate-bin"),"duplicate-bin"),
              error:renderRoute(buildRouteViewModel(source(error,"duplicate-bin"),"duplicate-bin"),"duplicate-bin"),
            }));
            """
        )
        self.assertEqual(result["noBatch"]["stage"], "unavailable")
        self.assertEqual(result["duplicateOnly"]["stage"], "ready")
        self.assertIn('data-action="preview-duplicate-bin-move"', result["duplicateOnlyHtml"])
        self.assertNotIn('data-action="preview-move"', result["duplicateOnlyHtml"])
        self.assertEqual(result["recovered"]["stage"], "completed")
        self.assertIn("Loading Duplicate Bin", result["loading"])
        self.assertIn("Duplicate Bin could not be loaded", result["error"])

    def test_duplicate_activity_undo_uses_restore_phrase_and_multiple_entries(self) -> None:
        result = self.run_node(
            """
            const entry=(id)=>({id,type:"duplicate_bin_move",title:"Moved 1 copy to Duplicate Bin",status:"moved",started_at:movedAt,completed_at:movedAt,files_expected:1,files_moved:1,files_restored:0,files_failed:0,files_unattempted:0,verification_status:"passed",undoable:true,related_operation_id:null,details:[{source:"Inbox",destination:"Duplicate Bin",status:"verified"}]});
            const activity=validateActivityResponse({activity:[entry(operationId),entry("cfb22b92-f69f-422e-a206-2c44e6b46227")]});
            const preview=validateUndoPreview(restorePreview,operationId);
            const calls=[];
            const client={
              getCurrentOrganization:async()=>ready,
              getActivity:async()=>activity,
              previewActivityUndo:async()=>restorePreview,
              undoActivityOperation:async(...args)=>{calls.push(args);return {undo:restoreResult.restore};},
            };
            const source=createBackendDataSource({apiClient:client,csrfToken:"csrf"});
            await source.loadActivity();
            const loaded=await source.previewUndo(operationId);
            await source.confirmUndo(operationId,loaded.confirmation_token,loaded.confirmation);
            console.log(JSON.stringify({preview,calls,count:activity.activity.length}));
            """
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["preview"]["copies_to_restore"], 1)
        self.assertEqual(result["preview"]["confirmation"], "RESTORE COPIES")
        self.assertEqual(result["calls"][0], [OPERATION, TOKEN, "csrf", "RESTORE COPIES"])

    def test_app_orchestration_refreshes_authoritative_owners_and_clears_confirmations(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        move = app[
            app.index("async function confirmDuplicateBinMove"):
            app.index("function setDuplicateBinSelection")
        ]
        restore = app[
            app.index("async function confirmDuplicateBinRestore"):
            app.index("async function checkFiles")
        ]
        listing = app[
            app.index("async function loadDuplicateBin"):
            app.index("function toggleActivity")
        ]
        self.assertEqual(app.count("dataSource.confirmDuplicateBinMove("), 1)
        self.assertEqual(app.count("dataSource.confirmDuplicateBinRestore("), 1)
        self.assertIn("loadCurrent()", move)
        self.assertIn("loadDuplicateBin({ refresh: true })", move)
        self.assertIn("loadActivity({ force: true, refresh: true })", move)
        self.assertIn("isUncertainDuplicateBinError", move)
        self.assertIn("loadDuplicateBin({ noticeMessage: message, refresh: true })", restore)
        self.assertIn("loadActivity({ force: true, refresh: true })", restore)
        self.assertIn("await duplicateBinLoadPromise.catch", listing)
        self.assertIn("return loadDuplicateBin({ noticeMessage, refresh: true })", listing)
        self.assertIn("discardDuplicateMovementConfirmation()", app)
        self.assertIn("discardDuplicateRestoreConfirmation()", app)
        self.assertNotIn("localStorage", app)
        self.assertNotIn("sessionStorage", app)
        self.assertNotIn("Empty bin", views)
        self.assertNotIn("Trash", app + views)


if __name__ == "__main__":
    unittest.main()
