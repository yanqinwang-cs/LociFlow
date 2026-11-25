from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"


class LociFlowStageB3ActivityAdapterTests(unittest.TestCase):
    maxDiff = None

    def run_node(self, body: str) -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            import assert from "node:assert/strict";
            import * as api from {json.dumps((LOCI / "api.js").as_uri())};
            import {{
              ActivityDataError, activityErrorMessage, createBackendDataSource,
              isCanonicalActivityId, isUncertainUndoError, validateActivityResponse,
              validateUndoPreview
            }} from {json.dumps((LOCI / "data_source.js").as_uri())};
            import {{
              buildProductionActivityEntries, buildProductionStageB1Contract,
              buildRouteViewModel
            }} from {json.dumps((LOCI / "view_models.js").as_uri())};
            import {{ renderRoute }} from {json.dumps((LOCI / "views.js").as_uri())};

            const movementId="4d7b4e92-14c8-4bb9-8328-3d271f355574";
            const restoreId="77249be2-06f0-41a4-a7c4-6c25b97a3ab1";
            const movement={{
              id:movementId,type:"organization",title:"Organized 2 files",status:"verified",
              started_at:"2026-07-15T08:00:00+00:00",completed_at:"2026-07-15T08:00:01+00:00",
              files_expected:2,files_moved:2,files_restored:0,files_failed:0,files_unattempted:0,
              verification_status:"passed",undoable:true,related_operation_id:null,
              details:[
                {{source:"Inbox/alpha.txt",destination:"Documents/alpha.txt",status:"moved"}},
                {{source:"Inbox/beta.txt",destination:"Documents/beta.txt",status:"moved"}},
              ],
            }};
            const restore={{
              id:restoreId,type:"restore",title:"Restored 2 files",status:"restored",
              started_at:"2026-07-15T09:00:00Z",completed_at:"2026-07-15T09:00:01Z",
              files_expected:2,files_moved:0,files_restored:2,files_failed:0,files_unattempted:0,
              verification_status:"passed",undoable:false,related_operation_id:movementId,
              details:[
                {{restored_to:"Inbox/alpha.txt",moved_from:"Documents/alpha.txt",status:"restored"}},
                {{restored_to:"Inbox/beta.txt",moved_from:"Documents/beta.txt",status:"restored"}},
              ],
            }};
            const clone=(value)=>JSON.parse(JSON.stringify(value));
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

    def test_activity_client_uses_only_fixed_same_origin_routes_and_fields(
        self,
    ) -> None:
        result = self.run_node(
            """
            const requests=[];
            const fetcher=async(url,options)=>{
              requests.push({url,options});
              if(url==="/api/activity") return {ok:true,status:200,json:async()=>({activity:[clone(movement)]})};
              if(url.endsWith("/preview")) return {ok:true,status:200,json:async()=>({
                operation_id:movementId,files_to_restore:2,confirmation:"UNDO FILE MOVE",confirmation_token:"opaque-token",
              })};
              return {ok:true,status:200,json:async()=>({undo:{status:"restored"}})};
            };
            await api.getActivity(fetcher);
            await api.previewActivityUndo(movementId,fetcher,"csrf");
            await api.undoActivityOperation(movementId,"opaque-token",fetcher,"csrf");
            let malformed=false;
            try{await api.previewActivityUndo("../escape",fetcher,"csrf");}catch(error){malformed=error.code==="invalid_activity_contract";}
            console.log(JSON.stringify({
              malformed,canonical:isCanonicalActivityId(movementId),uppercase:isCanonicalActivityId(movementId.toUpperCase()),
              requests:requests.map(({url,options})=>({
                url,method:options.method,credentials:options.credentials,accept:options.headers.Accept,
                contentType:options.headers["Content-Type"]||null,
                fields:options.body?Object.fromEntries(options.body):{},
              })),
            }));
            """
        )
        self.assertTrue(result["malformed"])
        self.assertTrue(result["canonical"])
        self.assertFalse(result["uppercase"])
        requests = result["requests"]
        self.assertEqual(
            [item["url"] for item in requests],
            [
                "/api/activity",
                "/api/activity/4d7b4e92-14c8-4bb9-8328-3d271f355574/undo/preview",
                "/api/activity/4d7b4e92-14c8-4bb9-8328-3d271f355574/undo",
            ],
        )
        self.assertEqual(requests[0]["method"], "GET")
        self.assertNotIn("?", requests[0]["url"])
        self.assertTrue(all(item["credentials"] == "same-origin" for item in requests))
        self.assertEqual(requests[1]["fields"], {"csrf_token": "csrf"})
        self.assertEqual(
            requests[2]["fields"],
            {
                "csrf_token": "csrf",
                "confirmation_token": "opaque-token",
                "confirmation": "UNDO FILE MOVE",
            },
        )
        all_fields = set(requests[1]["fields"]) | set(requests[2]["fields"])
        for forbidden in (
            "path",
            "root",
            "source",
            "destination",
            "log_path",
            "file_id",
            "count",
        ):
            self.assertNotIn(forbidden, all_fields)

    def test_activity_validation_accepts_safe_records_and_strips_unknown_fields(
        self,
    ) -> None:
        result = self.run_node(
            """
            const payload={activity:[{...clone(movement),log_path:"/private/secret",details:[
              {...clone(movement.details[0]),sha256:"secret"},clone(movement.details[1]),
            ]},clone(restore)],artifact_path:"/private/artifact"};
            const valid=validateActivityResponse(payload);
            const preview=validateUndoPreview({
              operation_id:movementId,files_to_restore:2,confirmation:"UNDO FILE MOVE",
              confirmation_token:"opaque",log_path:"/private/log",
            },movementId);
            console.log(JSON.stringify({
              empty:validateActivityResponse({activity:[]}),valid,preview,
              frozen:Object.isFrozen(valid)&&Object.isFrozen(valid.activity[0]),
              order:valid.activity.map((entry)=>entry.id),
            }));
            """
        )
        self.assertEqual(result["empty"], {"activity": []})
        self.assertTrue(result["frozen"])
        self.assertEqual(
            result["order"],
            [
                "4d7b4e92-14c8-4bb9-8328-3d271f355574",
                "77249be2-06f0-41a4-a7c4-6c25b97a3ab1",
            ],
        )
        serialized = json.dumps(result)
        for forbidden in ("log_path", "artifact_path", "sha256", "/private"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            set(result["preview"]),
            {
                "operation_id",
                "files_to_restore",
                "confirmation",
                "confirmation_token",
            },
        )

    def test_activity_validation_rejects_every_unsafe_contract_class(self) -> None:
        result = self.run_node(
            """
            const cases=[
              (x)=>x.activity[0].type="future",
              (x)=>x.activity[0].id="NOT-A-UUID",
              (x)=>x.activity[0].id=movementId.toUpperCase(),
              (x)=>x.activity[0].files_moved=-1,
              (x)=>x.activity[0].files_moved=1.5,
              (x)=>x.activity.push({...clone(x.activity[0]),id:"a2a5b23f-a112-4332-bf3a-5a1d61526fa8"}),
              (x)=>{x.activity[0]=clone(restore);x.activity[0].undoable=true;},
              (x)=>{x.activity[0].status="partial_movement";x.activity[0].undoable=true;},
              (x)=>x.activity[0].details[0].source="/etc/passwd",
              (x)=>x.activity[0].details[0].source="Inbox/../escape",
              (x)=>x.activity[0].details[0].source=`bad${String.fromCharCode(0)}path`,
              (x)=>x.activity[0].details[0].source="https://example.test/file",
              (x)=>x.activity[0].completed_at="15 July sometime",
              (x)=>x.activity[0].files_expected=1,
            ];
            const rejected=cases.map((mutate)=>{
              const candidate={activity:[clone(movement)]};mutate(candidate);
              try{validateActivityResponse(candidate);return false;}catch{return true;}
            });
            const previews=[
              {operation_id:restoreId,files_to_restore:2,confirmation:"UNDO FILE MOVE",confirmation_token:"opaque"},
              {operation_id:movementId,files_to_restore:0,confirmation:"UNDO FILE MOVE",confirmation_token:"opaque"},
              {operation_id:movementId,files_to_restore:2,confirmation:"wrong",confirmation_token:"opaque"},
              {operation_id:movementId,files_to_restore:2,confirmation:"UNDO FILE MOVE",confirmation_token:""},
            ].map((candidate)=>{try{validateUndoPreview(candidate,movementId);return false;}catch{return true;}});
            console.log(JSON.stringify({rejected,previews}));
            """
        )
        self.assertTrue(all(result["rejected"]))
        self.assertTrue(all(result["previews"]))

    def test_activity_view_model_maps_statuses_counts_details_and_order(self) -> None:
        result = self.run_node(
            """
            const partialMove={...clone(movement),id:"105f4827-d0f9-4ccf-929d-3b95db6f1260",title:"Move partly completed",status:"partial_movement",files_moved:1,files_failed:1,undoable:false,verification_status:"failed",details:[clone(movement.details[0]),{...clone(movement.details[1]),status:"failed"}]};
            const partialRestore={...clone(restore),id:"58b61703-fe5d-4ef1-8c91-95a3936ff31d",title:"Restore partly completed",status:"partial_undo",files_restored:1,files_failed:1,details:[clone(restore.details[0]),{...clone(restore.details[1]),status:"failed"}]};
            const machine=[clone(movement),partialMove,clone(restore),partialRestore];
            const entries=buildProductionActivityEntries(machine);
            const contract=buildProductionStageB1Contract({
              snapshot:{status:"not_checked",backendState:null},
              activity:{loadStatus:"ready",entries:machine,expandedIds:[movementId]},
            });
            const source={getState:()=>({route:"activity",theme:"light",stageB1:contract})};
            const html=renderRoute(buildRouteViewModel(source,"activity"),"activity");
            console.log(JSON.stringify({entries,html,order:entries.map((entry)=>entry.id)}));
            """
        )
        self.assertEqual(
            result["order"],
            [
                "4d7b4e92-14c8-4bb9-8328-3d271f355574",
                "105f4827-d0f9-4ccf-929d-3b95db6f1260",
                "77249be2-06f0-41a4-a7c4-6c25b97a3ab1",
                "58b61703-fe5d-4ef1-8c91-95a3936ff31d",
            ],
        )
        self.assertEqual(
            [entry["status"] for entry in result["entries"]],
            [
                "Completed",
                "Partly completed",
                "Restored",
                "Partly completed",
            ],
        )
        self.assertEqual(result["entries"][0]["summary"], "2 files moved")
        self.assertEqual(
            result["entries"][1]["summary"], "1 file moved · 1 file did not move"
        )
        self.assertEqual(result["entries"][2]["summary"], "2 files restored")
        self.assertEqual(
            result["entries"][3]["summary"], "1 file restored · 1 file did not restore"
        )
        self.assertEqual(
            result["entries"][0]["details"]["filesMoved"][0],
            "Inbox/alpha.txt → Documents/alpha.txt",
        )
        self.assertIn("Did not restore", result["entries"][3]["details"]["problems"][0])
        html = result["html"]
        self.assertEqual(html.count("Undo move"), 1)
        self.assertIn("activity-details", html)
        for machine_status in (
            "partial_movement",
            "partial_undo",
            "verification_failed",
        ):
            self.assertNotIn(machine_status, html)

    def test_data_source_uses_backend_owned_eligibility_serializes_undo_and_maps_errors(
        self,
    ) -> None:
        result = self.run_node(
            """
            const calls=[];
            const apiClient={
              getCurrentOrganization:async()=>({}),
              getActivity:async()=>{calls.push("activity");return {activity:[clone(movement)]};},
              previewActivityUndo:async(id,csrf)=>{calls.push(["preview",id,csrf]);return {operation_id:id,files_to_restore:2,confirmation:"UNDO FILE MOVE",confirmation_token:"opaque"};},
              undoActivityOperation:async(id,token,csrf)=>{calls.push(["undo",id,token,csrf]);return {undo:{status:"restored"}};},
            };
            const source=createBackendDataSource({apiClient,csrfToken:"csrf"});
            await source.loadActivity();
            const preview=await source.previewUndo(movementId);
            await source.confirmUndo(movementId,preview.confirmation_token);
            let olderRejected=false;
            try{await source.previewUndo(restoreId);}catch(error){olderRejected=error.code==="operation_not_undoable";}

            let release;
            const heldClient={...apiClient,previewActivityUndo:()=>new Promise((resolve)=>{release=resolve;})};
            const held=createBackendDataSource({apiClient:heldClient,csrfToken:"csrf"});
            await held.loadActivity();
            const first=held.previewUndo(movementId);
            let serialized=false;
            try{await held.previewUndo(movementId);}catch(error){serialized=error.code==="undo_in_progress";}
            release({operation_id:movementId,files_to_restore:2,confirmation:"UNDO FILE MOVE",confirmation_token:"opaque"});
            await first;
            source.dispose();held.dispose();
            const codes=["operation_not_found","operation_not_undoable","operation_superseded","undo_already_started","undo_already_completed","undo_destination_occupied","undo_source_missing","undo_source_changed","unsafe_undo_path","movement_or_undo_in_progress"];
            console.log(JSON.stringify({calls,olderRejected,serialized,messages:codes.map((code)=>activityErrorMessage({code})),uncertain:[isUncertainUndoError({code:"request_failed"}),isUncertainUndoError({code:"http_503"}),isUncertainUndoError({code:"operation_not_undoable"})]}));
            """
        )
        self.assertTrue(result["olderRejected"])
        self.assertTrue(result["serialized"])
        self.assertEqual(result["calls"].count("activity"), 2)
        self.assertEqual(
            [call[0] for call in result["calls"] if isinstance(call, list)],
            ["preview", "undo"],
        )
        self.assertEqual(result["uncertain"], [True, True, False])
        self.assertEqual(
            result["messages"],
            [
                "This move is no longer available.",
                "This move can no longer be undone.",
                "A newer file move must be handled first.",
                "This move is already being restored.",
                "This move has already been restored.",
                "A file now exists in one of the previous folders.",
                "A moved file could not be found.",
                "A moved file changed after it was moved.",
                "This move cannot be undone safely.",
                "Another file action is running.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
