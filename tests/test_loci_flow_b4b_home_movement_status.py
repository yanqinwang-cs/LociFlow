from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from tests.test_loci_flow_stage_b2_adapter import _payload
from tests.test_loci_flow_stage_b4b_movement_adapter import _ready_payload

ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"
TEMPLATE = ROOT / "src" / "organizer" / "web" / "templates" / "loci_flow.html"
OPERATION_ID = "77249be2-06f0-41a4-a7c4-6c25b97a3ab1"


class LociFlowB4BHomeMovementStatusTests(unittest.TestCase):
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
            const project=(raw,options={{}})=>{{
              const backend=validateOrganizationResponse(clone(raw));
              const contract=buildProductionStageB1Contract({{
                snapshot:{{status:"completed",backendState:backend}},
                movement:options.movement||null,
                activity:options.activity||null,
              }});
              const source={{getState:()=>({{
                route:"home",theme:"light",selectedItemId:null,settingsSection:"appearance",
                settings:{{notifications:{{}}}},stageB1:contract,
              }})}};
              const home=buildRouteViewModel(source,"home");
              return {{
                freshness:home.freshness,workflow:home.workflow,
                homeHtml:renderRoute(home,"home"),shell:buildShellViewModel(source),
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

    def test_startup_activity_load_is_backgrounded_coalesced_and_renders_home(self) -> None:
        app_path = LOCI / "app.js"
        script = textwrap.dedent(
            f"""
            import fs from "node:fs";
            const source=fs.readFileSync({json.dumps(str(app_path))},"utf8");
            const start=source.indexOf("async function loadActivity");
            const end=source.indexOf("function toggleActivity",start);
            if(start<0||end<0) throw new Error("loadActivity source not found");
            const state={{
              route:"home",
              activity:{{
                loadStatus:"idle",entries:[],expandedIds:new Set(),
                errorMessage:null,noticeMessage:null,loadVersion:0,
              }},
            }};
            let requests=0;
            let release;
            const held=new Promise((resolve)=>{{release=resolve;}});
            const dataSource={{loadActivity:()=>{{requests+=1;return held;}}}};
            let renders=0;
            let shellSyncs=0;
            const render=()=>{{renders+=1;}};
            const syncShell=()=>{{shellSyncs+=1;}};
            const makeLoader=new Function(
              "state","dataSource","render","syncShell",
              `let activityLoadPromise=null;${{source.slice(start,end)}};return loadActivity;`,
            );
            const loadActivity=makeLoader(state,dataSource,render,syncShell);
            const first=loadActivity({{force:true}});
            const second=loadActivity({{force:true}});
            const during={{requests,renders,status:state.activity.loadStatus}};
            release({{activity:[{{
              id:{json.dumps(OPERATION_ID)},type:"organization",status:"verified",
              title:"Organized 2 files",started_at:"2026-07-15T12:06:00+00:00",
              completed_at:"2026-07-15T12:06:01+00:00",files_expected:2,
              files_moved:2,files_restored:0,files_failed:0,files_unattempted:0,
              verification_status:"passed",undoable:true,related_operation_id:null,details:[],
            }}]}});
            await Promise.all([first,second]);
            const after={{requests,renders,status:state.activity.loadStatus,entries:state.activity.entries.length}};
            await loadActivity({{force:true}});
            state.route="activity";
            await loadActivity();
            console.log(JSON.stringify({{
              during,after,final:{{requests,renders,shellSyncs,status:state.activity.loadStatus}},
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
        self.assertEqual(result["during"], {"requests": 1, "renders": 0, "status": "loading"})
        self.assertEqual(
            result["after"],
            {"requests": 1, "renders": 1, "status": "ready", "entries": 1},
        )
        self.assertEqual(result["final"]["requests"], 1)
        self.assertEqual(result["final"]["status"], "ready")
        self.assertEqual(result["final"]["renders"], 3)

        startup = app_path.read_text(encoding="utf-8").rsplit("state.route = routeFromHash();", 1)[1]
        self.assertLess(startup.index("render();"), startup.index("loadCurrent();"))
        self.assertLess(startup.index("loadCurrent();"), startup.index("loadActivity({ force: true });"))
        self.assertNotIn('if (state.route === "activity") loadActivity()', startup)

    def test_no_history_and_verified_completed_movement_are_truthful(self) -> None:
        result = self.run_node(
            """
            const noHistory=project(base);
            const verifiedOperation={
              status:"verified",operation_id:operationId,
              files_expected:2,files_moved:2,files_failed:0,files_unattempted:0,
              verification_status:"passed",
              started_at:"2026-07-15T12:06:00+00:00",
              completed_at:"2026-07-15T12:06:01+00:00",
            };
            const verified=project(ready,{movement:{execution:verifiedOperation}});
            const recoveredPayload=clone(ready);
            recoveredPayload.execution={
              status:"verified",operation_id:operationId,
              files_expected:2,files_moved:2,files_failed:0,files_unattempted:0,
              verification_status:"passed",
            };
            const recovered=project(recoveredPayload);
            console.log(JSON.stringify({noHistory,verified,recovered}));
            """
        )
        self.assertEqual(
            result["noHistory"]["freshness"]["lastOrganizedLabel"],
            "Not organized yet",
        )
        self.assertEqual(result["noHistory"]["shell"]["movementStatusLabel"], "No move ready")
        self.assertEqual(result["noHistory"]["shell"]["bannerStatusLabel"], "Review changes")
        self.assertNotIn("Not organized yet", result["verified"]["homeHtml"])
        self.assertIn("2 files moved and checked.", result["verified"]["homeHtml"])
        self.assertEqual(result["verified"]["shell"]["movementStatusLabel"], "Move verified")
        self.assertEqual(result["verified"]["shell"]["bannerStatusLabel"], "Move verified")
        self.assertNotEqual(
            result["verified"]["freshness"]["lastOrganizedLabel"],
            "Completed",
        )
        self.assertEqual(result["recovered"]["freshness"]["lastOrganizedLabel"], "Completed")
        self.assertEqual(result["recovered"]["shell"]["movementStatusLabel"], "Move verified")
        self.assertNotIn("Movement off", json.dumps(result))

    def test_production_banner_maps_only_truthful_user_states(self) -> None:
        result = self.run_node(
            """
            const shellFor=(snapshot,movement=null)=>{
              const contract=buildProductionStageB1Contract({snapshot,movement});
              return buildShellViewModel({getState:()=>({
                route:"home",theme:"light",selectedItemId:null,settingsSection:"appearance",
                settings:{notifications:{}},stageB1:contract,
              })});
            };
            const loading=shellFor({status:"loading",backendState:null});
            const readyState=clone(ready);
            readyState.execution={
              status:"ready",operation_id:null,files_expected:2,files_moved:0,
              files_failed:0,files_unattempted:2,verification_status:null,
            };
            const movingState=clone(readyState);
            movingState.execution={
              ...movingState.execution,status:"moving",operation_id:operationId,
              verification_status:"not_run",
            };
            const verifiedState=clone(readyState);
            verifiedState.execution={
              ...verifiedState.execution,status:"verified",operation_id:operationId,
              files_moved:2,files_unattempted:0,verification_status:"passed",
            };
            const failedState=clone(readyState);
            failedState.execution={
              ...failedState.execution,status:"movement_failed",operation_id:operationId,
              files_failed:1,files_unattempted:1,verification_status:"not_run",
            };
            console.log(JSON.stringify({
              loading:loading.bannerStatusLabel,
              ready:shellFor({status:"completed",backendState:validateOrganizationResponse(readyState)}).bannerStatusLabel,
              moving:shellFor({status:"completed",backendState:validateOrganizationResponse(movingState)}).bannerStatusLabel,
              verified:shellFor({status:"completed",backendState:validateOrganizationResponse(verifiedState)}).bannerStatusLabel,
              failed:shellFor({status:"completed",backendState:validateOrganizationResponse(failedState)}).bannerStatusLabel,
            }));
            """
        )
        self.assertEqual(result, {
            "loading": "Checking files",
            "ready": "Ready to move",
            "moving": "Moving files",
            "verified": "Move verified",
            "failed": "Move needs attention",
        })
        self.assertNotIn("No files can move in this stage", json.dumps(result))

    def test_in_progress_failed_and_activity_history_do_not_claim_false_completion(self) -> None:
        result = self.run_node(
            """
            const execution=(status,overrides={})=>({
              status,operation_id:operationId,
              files_expected:2,files_moved:0,files_failed:0,files_unattempted:2,
              verification_status:"not_run",...overrides,
            });
            const movingPayload=clone(ready);
            movingPayload.execution=execution("moving");
            const failedPayload=clone(ready);
            failedPayload.execution=execution("movement_failed",{files_failed:1,files_unattempted:1});
            const history={
              loadStatus:"ready",expandedIds:new Set(),errorMessage:null,noticeMessage:null,
              entries:[{
                id:"restore-operation",type:"restore",title:"Restored 2 files",status:"restored",
                started_at:"2026-07-15T13:00:00+00:00",completed_at:"2026-07-15T13:00:01+00:00",
                files_expected:2,files_moved:0,files_restored:2,files_failed:0,files_unattempted:0,
                verification_status:"passed",undoable:false,related_operation_id:operationId,
                details:[
                  {restored_to:"Inbox/alpha.txt",moved_from:"Documents/alpha.txt",status:"restored"},
                  {restored_to:"Inbox/beta.txt",moved_from:"Documents/beta.txt",status:"restored"},
                ],
              },{
                id:operationId,type:"organization",title:"Organized 2 files",status:"verified",
                started_at:"2026-07-15T12:06:00+00:00",completed_at:"2026-07-15T12:06:01+00:00",
                files_expected:2,files_moved:2,files_restored:0,files_failed:0,files_unattempted:0,
                verification_status:"passed",undoable:true,related_operation_id:null,
                details:[
                  {source:"Inbox/alpha.txt",destination:"Documents/alpha.txt",status:"moved"},
                  {source:"Inbox/beta.txt",destination:"Documents/beta.txt",status:"moved"},
                ],
              }],
            };
            const laterScan=project(base,{activity:history});
            console.log(JSON.stringify({
              moving:project(movingPayload),failed:project(failedPayload),laterScan,
            }));
            """
        )
        self.assertEqual(result["moving"]["shell"]["movementStatusLabel"], "Moving files")
        self.assertEqual(result["failed"]["shell"]["movementStatusLabel"], "Move needs attention")
        self.assertNotEqual(result["failed"]["shell"]["movementStatusLabel"], "Move verified")
        self.assertNotEqual(
            result["laterScan"]["freshness"]["lastOrganizedLabel"],
            "Not organized yet",
        )

    def test_shell_wiring_and_product_boundaries_remain_intact(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('document.querySelector(".sample-label")', app)
        self.assertIn("movementStatus.textContent = shell.movementStatusLabel", app)
        self.assertIn('document.querySelector(".prototype-notice span:last-child")', app)
        self.assertIn("bannerStatus.textContent = shell.bannerStatusLabel", app)
        self.assertIn('<span class="sample-label">Movement off</span>', template)
        home = views[views.index("function renderContractHome"):views.index("export function renderHome")]
        organize = views[views.index("function renderContractOrganize"):views.index("export function renderOrganize")]
        self.assertIn("home-status", home)
        self.assertNotIn("organize-layout", home)
        self.assertIn("organize-layout", organize)
        self.assertNotIn("undo-move", organize)
        combined = app + views + template
        for forbidden in (
            "authorization token", "plan digest", "decision revision", "scan generation",
            "durable operation", "executor", "safe to delete", "Trash",
        ):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
