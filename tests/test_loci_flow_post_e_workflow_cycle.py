from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

from tests.test_loci_flow_stage_b2_adapter import _payload
from tests.test_loci_flow_stage_b4b_movement_adapter import _ready_payload


ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"
OPERATION_ID = "77249be2-06f0-41a4-a7c4-6c25b97a3ab1"
BIN_ITEM_ID = "112022c2-5f44-44be-93d8-53a9cb724659"


class LociFlowPostEWorkflowCycleTests(unittest.TestCase):
    maxDiff = None

    def run_node(self, body: str) -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            import {{ validateOrganizationResponse }} from {json.dumps((LOCI / 'data_source.js').as_uri())};
            import {{
              buildProductionStageB1Contract,buildRouteViewModel,buildShellViewModel,
            }} from {json.dumps((LOCI / 'view_models.js').as_uri())};
            import {{ renderRoute }} from {json.dumps((LOCI / 'views.js').as_uri())};
            const clone=(value)=>JSON.parse(JSON.stringify(value));
            const base={json.dumps(_payload())};
            const ready={json.dumps(_ready_payload())};
            const operationId={json.dumps(OPERATION_ID)};
            const binItemId={json.dumps(BIN_ITEM_ID)};
            const verified={{
              status:"verified",operation_id:operationId,
              files_expected:2,files_moved:2,files_failed:0,files_unattempted:0,
              verification_status:"passed",
              started_at:"2026-07-17T08:00:00+00:00",
              completed_at:"2026-07-17T08:00:01+00:00",
            }};
            const history={{
              loadStatus:"ready",expandedIds:new Set(),errorMessage:null,noticeMessage:null,
              entries:[{{
                id:operationId,type:"organization",title:"Organized 2 files",status:"verified",
                started_at:verified.started_at,completed_at:verified.completed_at,
                files_expected:2,files_moved:2,files_restored:0,files_failed:0,
                files_unattempted:0,verification_status:"passed",undoable:true,
                related_operation_id:null,details:[],
              }}],
            }};
            const duplicateBin={{
              loadStatus:"ready",selectedIds:new Set([binItemId]),restorePending:false,
              items:[{{
                id:binItemId,name:"copy.txt",previous_folder:"Inbox",
                moved_at:"2026-07-17T08:05:00+00:00",size:12,kind:"text",
                restore_available:true,attention_message:null,
              }}],
            }};
            const project=(snapshot,options={{}})=>{{
              const contract=buildProductionStageB1Contract({{
                snapshot,activity:options.activity||history,
                movement:options.movement||null,duplicateBin:options.duplicateBin||duplicateBin,
              }});
              const source={{getState:()=>({{
                route:"home",theme:"light",selectedItemId:null,settingsSection:"appearance",
                settings:{{notifications:{{}}}},stageB1:contract,
              }})}};
              const home=buildRouteViewModel(source,"home");
              const organize=buildRouteViewModel(source,"organize");
              return {{
                workflow:home.workflow,freshness:home.freshness,
                lastMoveSummary:home.activity.lastMoveSummary,
                activityConnected:home.activity.connected,
                duplicateItems:home.duplicateBin.items,
                homeHtml:renderRoute(home,"home"),
                organizeHtml:renderRoute(organize,"organize"),
                shell:buildShellViewModel(source),
              }};
            }};
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

    def test_verified_history_yields_check_again_and_new_cycle_uses_new_scan(self) -> None:
        result = self.run_node(
            """
            const completedPayload=clone(ready);completedPayload.execution=verified;
            const completed=project({status:"completed",backendState:validateOrganizationResponse(completedPayload)});
            const checking=project({status:"checking",backendState:null});
            const newPayload=clone(base);newPayload.execution=verified;
            const newScan=project({status:"completed",backendState:validateOrganizationResponse(newPayload)});
            const reloaded=project({status:"completed",backendState:validateOrganizationResponse(completedPayload)});
            console.log(JSON.stringify({completed,checking,newScan,reloaded}));
            """
        )
        completed = result["completed"]
        self.assertEqual(completed["workflow"]["stage"], "ready_for_check")
        self.assertEqual(completed["workflow"]["primaryAction"], {
            "label": "Check again", "action": "check-again", "enabled": True,
        })
        self.assertIn("Files moved", completed["homeHtml"])
        self.assertIn("2 files moved.", completed["lastMoveSummary"])
        self.assertNotEqual(completed["freshness"]["lastOrganizedLabel"], "Not organized yet")
        self.assertTrue(completed["activityConnected"])
        self.assertIn("View Activity", completed["homeHtml"])
        self.assertIn('<li class="current"><span>1</span>Check</li>', completed["homeHtml"])
        self.assertIn('data-action="check-again"', completed["organizeHtml"])
        self.assertEqual(completed["shell"]["movementStatusLabel"], "Move verified")

        checking = result["checking"]
        self.assertEqual(checking["workflow"]["stage"], "checking")
        self.assertEqual(checking["lastMoveSummary"], "2 files moved.")
        self.assertNotEqual(checking["freshness"]["lastOrganizedLabel"], "Not organized yet")
        self.assertEqual(len(checking["duplicateItems"]), 1)

        self.assertEqual(result["newScan"]["workflow"]["stage"], "organizing")
        self.assertNotEqual(result["newScan"]["workflow"]["stage"], "move_completed")
        self.assertEqual(result["newScan"]["lastMoveSummary"], "2 files moved.")
        self.assertEqual(result["reloaded"]["workflow"]["stage"], "ready_for_check")

    def test_unsafe_operation_states_never_offer_check_again(self) -> None:
        result = self.run_node(
            """
            const state=(status,overrides={})=>{
              const payload=clone(ready);
              payload.execution={
                status,operation_id:operationId,files_expected:2,files_moved:0,
                files_failed:0,files_unattempted:2,verification_status:"not_run",...overrides,
              };
              return project({status:"completed",backendState:validateOrganizationResponse(payload)}).workflow;
            };
            console.log(JSON.stringify({
              moving:state("moving"),
              interrupted:state("interrupted"),
              recovery:state("recovery_required"),
              failed:state("movement_failed",{files_failed:1,files_unattempted:1}),
              verification:state("verification_failed",{
                files_moved:2,files_unattempted:0,verification_status:"failed",
              }),
            }));
            """
        )
        for key, workflow in result.items():
            self.assertNotEqual(workflow["primaryAction"]["action"], "check-again", key)
        self.assertEqual(result["moving"]["stage"], "move_in_progress")
        for key in ("interrupted", "recovery", "failed", "verification"):
            self.assertEqual(result[key]["stage"], "move_failed", key)

    def test_check_again_starts_one_scan_and_resets_only_cycle_local_state(self) -> None:
        app_path = LOCI / "app.js"
        script = textwrap.dedent(
            f"""
            import fs from "node:fs";
            const source=fs.readFileSync({json.dumps(str(app_path))},"utf8");
            const start=source.indexOf("async function checkFiles");
            const end=source.indexOf("async function handleMutation",start);
            const state={{
              scanPolling:false,pendingMutation:false,workflowOverride:{{old:true}},
              backendSnapshot:{{status:"completed",backendState:{{approval:"old"}}}},
              movement:{{execution:{{status:"verified"}},errorCode:"old",errorMessage:"old"}},
              duplicateMovement:{{execution:{{status:"moved"}},errorCode:"old",errorMessage:"old"}},
              duplicateBin:{{items:[{{id:"item"}}],selectedIds:new Set(["item"]),preview:{{token:"old"}}}},
              destinationDrafts:{{old:"choice"}},duplicateDrafts:{{old:"choice"}},standards:{{freshScanRequired:true}},
            }};
            const discarded={{movement:0,duplicateMovement:0,restore:0}};
            const discardMovementConfirmation=()=>{{discarded.movement+=1;}};
            const discardDuplicateMovementConfirmation=()=>{{discarded.duplicateMovement+=1;}};
            const discardDuplicateRestoreConfirmation=()=>{{
              discarded.restore+=1;state.duplicateBin.preview=null;
            }};
            const snapshots=[];
            const applySnapshot=(snapshot)=>{{state.backendSnapshot=snapshot;snapshots.push(snapshot.status);}};
            let renders=0;const render=()=>{{renders+=1;}};const announce=()=>{{}};
            let requests=0;let release;
            const held=new Promise((resolve)=>{{release=resolve;}});
            const settled={{status:"completed",backendState:{{review:{{status:"in_progress"}}}}}};
            const dataSource={{startScan:async(onStatus)=>{{
              requests+=1;onStatus({{status:"checking",backendState:null}});await held;return settled;
            }}}};
            const make=new Function(
              "state","discardMovementConfirmation","discardDuplicateMovementConfirmation",
              "discardDuplicateRestoreConfirmation","discardStandardBuilder","loadStandards","applySnapshot","render","announce","dataSource",
              `${{source.slice(start,end)}};return checkFiles;`,
            );
            const checkFiles=make(
              state,discardMovementConfirmation,discardDuplicateMovementConfirmation,
              discardDuplicateRestoreConfirmation,()=>{{}},async()=>{{}},applySnapshot,render,announce,dataSource,
            );
            const first=checkFiles();const second=checkFiles();
            const during={{
              requests,scanPolling:state.scanPolling,snapshot:state.backendSnapshot.status,
              drafts:[Object.keys(state.destinationDrafts).length,Object.keys(state.duplicateDrafts).length],
              ordinaryExecution:state.movement.execution,duplicateExecution:state.duplicateMovement.execution,
              binItems:state.duplicateBin.items.length,binSelected:state.duplicateBin.selectedIds.size,
              restorePreview:state.duplicateBin.preview,
            }};
            release();await Promise.all([first,second]);
            const startup=source.slice(source.lastIndexOf("state.route = routeFromHash();"));
            console.log(JSON.stringify({{
              during,after:{{requests,scanPolling:state.scanPolling,snapshot:state.backendSnapshot.status}},
              discarded,snapshots,autoScan:startup.includes("checkFiles()"),renders,
            }}));
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
        result = json.loads(completed.stdout)
        self.assertEqual(result["during"]["requests"], 1)
        self.assertTrue(result["during"]["scanPolling"])
        self.assertEqual(result["during"]["snapshot"], "checking")
        self.assertEqual(result["during"]["drafts"], [0, 0])
        self.assertIsNone(result["during"]["ordinaryExecution"])
        self.assertIsNone(result["during"]["duplicateExecution"])
        self.assertEqual(result["during"]["binItems"], 1)
        self.assertEqual(result["during"]["binSelected"], 1)
        self.assertIsNone(result["during"]["restorePreview"])
        self.assertEqual(result["after"], {
            "requests": 1, "scanPolling": False, "snapshot": "completed",
        })
        self.assertEqual(result["discarded"], {
            "movement": 1, "duplicateMovement": 1, "restore": 1,
        })
        self.assertFalse(result["autoScan"])

    def test_unresolved_duplicate_approval_conflict_reloads_and_closes_dialog(self) -> None:
        app_path = LOCI / "app.js"
        script = textwrap.dedent(
            f"""
            import fs from "node:fs";
            const source=fs.readFileSync({json.dumps(str(app_path))},"utf8");
            const start=source.indexOf("async function handleMutation");
            const end=source.indexOf("function recommendationDecision",start);
            const state={{
              pendingMutation:false,activeDialog:"approval",route:"organize",
              workflowOverride:null,selectedItemId:"old",selectedRowIds:["old"],
              destinationDrafts:{{old:"choice"}},duplicateDrafts:{{old:"choice"}},
              backendSnapshot:{{status:"completed",backendState:{{old:true}}}},
            }};
            const pending=[];const announcements=[];
            const setPending=(value)=>{{state.pendingMutation=value;pending.push(value);}};
            const announce=(message)=>{{announcements.push(message);}};
            let reloads=0;let applies=0;let closes=0;let renders=0;let attempts=0;
            const authoritative={{status:"completed",backendState:{{current:true}}}};
            const dataSource={{loadCurrentState:async()=>{{reloads+=1;return authoritative;}}}};
            const applySnapshot=(snapshot)=>{{applies+=1;state.backendSnapshot=snapshot;}};
            const closeDialog=()=>{{closes+=1;state.activeDialog=null;}};
            const render=()=>{{renders+=1;}};
            const workflowOverride=(status,message,action,label,kind=null)=>({{
              status,message,primaryAction:{{action,label}},kind,
            }});
            const make=new Function(
              "state","setPending","announce","applySnapshot","closeDialog","render",
              "workflowOverride","dataSource",
              `${{source.slice(start,end)}};return handleMutation;`,
            );
            const handleMutation=make(
              state,setPending,announce,applySnapshot,closeDialog,render,
              workflowOverride,dataSource,
            );
            await handleMutation(async()=>{{
              attempts+=1;
              throw Object.assign(new Error("conflict"),{{code:"duplicate_batch_conflict"}});
            }},"Approved",{{close:true}});
            console.log(JSON.stringify({{
              pending,announcements,reloads,applies,closes,renders,attempts,
              activeDialog:state.activeDialog,snapshot:state.backendSnapshot,
              override:state.workflowOverride,
            }}));
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
        result = json.loads(completed.stdout)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual((result["reloads"], result["applies"]), (1, 1))
        self.assertEqual(result["closes"], 1)
        self.assertIsNone(result["activeDialog"])
        self.assertEqual(result["pending"], [True, False])
        self.assertEqual(result["snapshot"]["backendState"], {"current": True})
        self.assertEqual(
            result["override"],
            {
                "status": "These copies are already part of another approved action.",
                "message": "Review the latest duplicate choices.",
                "primaryAction": {
                    "action": "reload-choices",
                    "label": "Review latest choices",
                },
                "kind": None,
            },
        )
        visible = " ".join(result["announcements"] + list(result["override"].values())[:2])
        self.assertNotIn("information could not be loaded", visible)
        for internal_term in ("catalog", "claim", "batch", "digest"):
            self.assertNotIn(internal_term, visible.lower())


if __name__ == "__main__":
    unittest.main()
