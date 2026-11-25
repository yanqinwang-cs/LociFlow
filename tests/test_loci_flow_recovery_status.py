from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"


class LociFlowRecoveryStatusTests(unittest.TestCase):
    def run_node(self, body: str) -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", textwrap.dedent(body)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_home_only_override_and_read_only_dialog(self) -> None:
        result = self.run_node(
            f"""
            import {{buildProductionStageB1Contract,buildHomeViewModel,buildOrganizeViewModel,buildRecoveryDialogViewModel}} from {json.dumps((LOCI / 'view_models.js').as_uri())};
            import {{renderRoute}} from {json.dumps((LOCI / 'views.js').as_uri())};
            const contract=buildProductionStageB1Contract({{
              snapshot:{{status:"not_checked",backendState:null}},recoveryAttention:true,
            }});
            const source={{getState:()=>({{
              route:"home",theme:"light",selectedItemId:null,settingsSection:"appearance",
              settings:{{notifications:{{}}}},stageB1:contract,
            }})}};
            source.duplicateChoiceStatus=()=>({{valid:false}});
            const home=buildHomeViewModel(source);
            const organize=buildOrganizeViewModel(source);
            const projection={{state:"needs_attention",title:"File changes need attention.",message:"Review what happened before continuing.",affected_items:2,can_check_result:true}};
            const dialog=buildRecoveryDialogViewModel(projection);
            const readOnly=buildRecoveryDialogViewModel({{...projection,can_check_result:false}});
            console.log(JSON.stringify({{
              home:home.workflow,organize:organize.workflow,
              homeHtml:renderRoute(home,"home"),organizeHtml:renderRoute(organize,"organize"),dialog,readOnly,
            }}));
            """
        )
        self.assertEqual(result["home"]["status"], "File changes need attention.")
        self.assertEqual(result["home"]["primaryAction"]["action"], "review-recovery")
        self.assertEqual(result["organize"]["status"], "Files have not been checked yet")
        self.assertIn("Review unfinished file work", result["homeHtml"])
        self.assertNotIn("Review unfinished file work", result["organizeHtml"])
        self.assertEqual(result["dialog"]["eyebrow"], "Needs attention")
        self.assertEqual(result["dialog"]["closeLabel"], "Close")
        self.assertTrue(result["dialog"]["canCheckResult"])
        self.assertEqual(result["dialog"]["checkLabel"], "Check result")
        self.assertFalse(result["readOnly"]["canCheckResult"])
        for forbidden in ("Continue", "input", "file tree"):
            self.assertNotIn(forbidden, json.dumps(result["dialog"]))

    def test_api_and_data_source_validate_exact_five_field_contract(self) -> None:
        result = self.run_node(
            f"""
            import {{getCurrentRecovery}} from {json.dumps((LOCI / 'api.js').as_uri())};
            import {{createBackendDataSource,validateRecoveryResponse}} from {json.dumps((LOCI / 'data_source.js').as_uri())};
            const payload={{state:"needs_attention",title:"File changes need attention.",message:"Review what happened before continuing.",affected_items:1,can_check_result:true}};
            const calls=[];
            const fetcher=async(url,options)=>{{calls.push([url,options.method]);return {{ok:true,status:200,json:async()=>payload}};}};
            const direct=await getCurrentRecovery(fetcher);
            const source=createBackendDataSource({{apiClient:{{getCurrentOrganization:async()=>({{}}),getCurrentRecovery:async()=>payload}},csrfToken:"csrf"}});
            const loaded=await source.loadRecoveryProjection();
            let extraRejected=false;
            try{{validateRecoveryResponse({{...payload,path:"Inbox/secret.txt"}});}}catch(error){{extraRejected=error.code==="invalid_recovery_contract";}}
            let missingRejected=false;
            const {{can_check_result,...missing}}=payload;
            try{{validateRecoveryResponse(missing);}}catch(error){{missingRejected=error.code==="invalid_recovery_contract";}}
            let typeRejected=false;
            try{{validateRecoveryResponse({{...payload,can_check_result:"yes"}});}}catch(error){{typeRejected=error.code==="invalid_recovery_contract";}}
            console.log(JSON.stringify({{calls,direct,loaded,extraRejected,missingRejected,typeRejected}}));
            """
        )
        self.assertEqual(result["calls"], [["/api/recovery/current", "GET"]])
        self.assertEqual(
            set(result["loaded"]),
            {"state", "title", "message", "affected_items", "can_check_result"},
        )
        self.assertTrue(result["extraRejected"])
        self.assertTrue(result["missingRejected"])
        self.assertTrue(result["typeRejected"])

    def test_explicit_opening_deduplicates_and_reopening_fetches_fresh_data(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        start = app.index("async function reviewRecovery")
        end = app.index("function openRecoveryDialog", start)
        function_source = app[start:end]
        result = self.run_node(
            f"""
            const state={{route:"home"}};
            let calls=0;let release;
            let held=new Promise(resolve=>{{release=resolve;}});
            const dataSource={{loadRecoveryProjection:()=>{{calls+=1;return held;}}}};
            const opened=[];
            const openRecoveryDialog=(...args)=>opened.push(args);
            const SAFE_RECOVERY_FAILURE={{state:"cannot_continue_automatically",title:"Safe",message:"Safe",affected_items:0,can_check_result:false}};
            const make=new Function("state","dataSource","openRecoveryDialog","SAFE_RECOVERY_FAILURE",`let recoveryLoadPending=false;let recoveryCheckPending=false;let recoveryDialogGeneration=0;{function_source};return reviewRecovery;`);
            const review=make(state,dataSource,openRecoveryDialog,SAFE_RECOVERY_FAILURE);
            const firstTrigger={{disabled:false}};
            const first=review(firstTrigger);const duplicate=review(firstTrigger);
            const during={{calls,disabled:firstTrigger.disabled}};
            release({{state:"clear",title:"Clear",message:"Clear",affected_items:0,can_check_result:false}});
            await Promise.all([first,duplicate]);
            held=Promise.resolve({{state:"needs_attention",title:"New",message:"New",affected_items:1,can_check_result:true}});
            const secondTrigger={{disabled:false}};await review(secondTrigger);
            held=Promise.reject(new Error("/private/root/secret.json"));
            await review({{disabled:false}});
            let routeRelease;
            held=new Promise(resolve=>{{routeRelease=resolve;}});
            const routed=review({{disabled:false}});state.route="activity";
            routeRelease({{state:"needs_attention",title:"Stale",message:"Stale",affected_items:9,can_check_result:true}});
            await routed;
            console.log(JSON.stringify({{during,calls,opened:opened.length,last:opened.at(-1)[0],firstDisabled:firstTrigger.disabled,secondDisabled:secondTrigger.disabled}}));
            """
        )
        self.assertEqual(result["during"], {"calls": 1, "disabled": True})
        self.assertEqual(result["calls"], 4)
        self.assertEqual(result["opened"], 3)
        self.assertEqual(result["last"]["state"], "cannot_continue_automatically")
        self.assertNotIn("secret", json.dumps(result["last"]).lower())
        self.assertFalse(result["firstDisabled"])
        self.assertFalse(result["secondDisabled"])

    def test_check_adapters_validate_exact_context_and_result_contracts(self) -> None:
        result = self.run_node(
            f"""
            import {{createLociFlowApiClient}} from {json.dumps((LOCI / 'api.js').as_uri())};
            import {{createBackendDataSource,validateRecoveryCheckContext,validateRecoveryCheckResult}} from {json.dumps((LOCI / 'data_source.js').as_uri())};
            const context={{case_id:"11111111-1111-4111-8111-111111111111",action_id:"22222222-2222-4222-8222-222222222222",expires_at:"2026-07-27T10:00:00Z",label:"Check result"}};
            const checked={{state:"confirmed",title:"Check complete",message:"The file move was confirmed.",recovery_attention:false}};
            const calls=[];
            const fetcher=async(url,options)=>{{
              calls.push([url,options.method,Object.fromEntries(options.body)]);
              return {{ok:true,status:200,json:async()=>url.endsWith("check-context")?context:checked}};
            }};
            const api=createLociFlowApiClient(fetcher);
            const source=createBackendDataSource({{apiClient:{{...api,getCurrentOrganization:async()=>({{}})}},csrfToken:"csrf"}});
            const issued=await source.createRecoveryCheckContext();
            const outcome=await source.checkRecoveryResult(issued.case_id,issued.action_id);
            let badContext=false;let badResult=false;
            try{{validateRecoveryCheckContext({{...context,path:"private"}});}}catch(error){{badContext=error.code==="invalid_recovery_contract";}}
            try{{validateRecoveryCheckResult({{...checked,family:"organization"}});}}catch(error){{badResult=error.code==="invalid_recovery_contract";}}
            console.log(JSON.stringify({{calls,issued,outcome,badContext,badResult}}));
            """
        )
        self.assertEqual(result["calls"][0][0], "/api/recovery/check-context")
        self.assertEqual(
            result["calls"][0][2],
            {"csrf_token": "csrf"},
        )
        self.assertEqual(result["calls"][1][0], "/api/recovery/check-result")
        self.assertEqual(
            result["calls"][1][2],
            {
                "csrf_token": "csrf",
                "case_id": "11111111-1111-4111-8111-111111111111",
                "action_id": "22222222-2222-4222-8222-222222222222",
            },
        )
        self.assertTrue(result["badContext"])
        self.assertTrue(result["badResult"])

    def test_check_request_chain_is_one_use_and_refreshes_existing_owners(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        start = app.index("const SAFE_RECOVERY_CHECK_FAILURE")
        end = app.index("function resetStandardBuilder", start)
        function_source = app[start:end]
        result = self.run_node(
            f"""
            let contextCalls=0;let resultCalls=0;let projectionLoads=0;let homeLoads=0;let activityLoads=0;
            let releaseContext;
            const held=new Promise(resolve=>{{releaseContext=resolve;}});
            const dataSource={{
              createRecoveryCheckContext:async()=>{{contextCalls+=1;return held;}},
              checkRecoveryResult:async(caseId,actionId)=>{{resultCalls+=1;return {{state:"confirmed",title:"Check complete",message:"The file move was confirmed.",recovery_attention:false,caseId,actionId}};}},
              loadRecoveryProjection:async()=>{{projectionLoads+=1;return {{}};}},
            }};
            const state={{activeDialog:"recovery"}};
            let dialogPayload={{can_check_result:true}};
            let dialogTrigger={{id:"trigger"}};
            const opened=[];const announced=[];
            const elements={{dialogActions:{{querySelector:()=>({{disabled:false,textContent:"Check result"}})}}}};
            const openRecoveryDialog=(...args)=>opened.push(args);
            const announce=(message)=>announced.push(message);
            const loadCurrent=async()=>{{homeLoads+=1;}};
            const loadActivity=async()=>{{activityLoads+=1;}};
            const functionSource={json.dumps(function_source)};
            const make=new Function(
              "dataSource","state","elements","openRecoveryDialog","announce",
              "loadCurrent","loadActivity","dialogPayload","dialogTrigger",
              "let recoveryAttention=true;"
              +"let recoveryCheckPending=false;"
              +"let recoveryDialogGeneration=4;"
              +"let recoveryCheckContext=null;"
              +functionSource
              +"return {{run:checkRecoveryResult,snapshot:()=>({{recoveryAttention,recoveryCheckPending,recoveryCheckContext}})}};",
            );
            const flow=make(dataSource,state,elements,openRecoveryDialog,announce,loadCurrent,loadActivity,dialogPayload,dialogTrigger);
            const first=flow.run();const duplicate=flow.run();
            releaseContext({{case_id:"11111111-1111-4111-8111-111111111111",action_id:"22222222-2222-4222-8222-222222222222",expires_at:"2026-07-27T10:00:00Z",label:"Check result"}});
            await Promise.all([first,duplicate]);
            console.log(JSON.stringify({{contextCalls,resultCalls,projectionLoads,homeLoads,activityLoads,opened,announced,snapshot:flow.snapshot()}}));
            """
        )
        self.assertEqual(result["contextCalls"], 1)
        self.assertEqual(result["resultCalls"], 1)
        self.assertEqual(result["projectionLoads"], 1)
        self.assertEqual(result["homeLoads"], 1)
        self.assertEqual(result["activityLoads"], 1)
        self.assertFalse(result["snapshot"]["recoveryAttention"])
        self.assertFalse(result["snapshot"]["recoveryCheckPending"])
        self.assertIsNone(result["snapshot"]["recoveryCheckContext"])
        self.assertTrue(result["opened"][0][2]["resultOnly"])
        self.assertNotIn("11111111", json.dumps(result["opened"]))

    def test_check_context_failure_and_closed_generation_are_safe(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        start = app.index("const SAFE_RECOVERY_CHECK_FAILURE")
        end = app.index("function resetStandardBuilder", start)
        function_source = app[start:end]
        result = self.run_node(
            f"""
            const functionSource={json.dumps(function_source)};
            const build=(dataSource)=>{{
              const state={{activeDialog:"recovery"}};
              const elements={{dialogActions:{{querySelector:()=>({{disabled:false,textContent:"Check result"}})}}}};
              const opened=[];const announced=[];
              const make=new Function(
                "dataSource","state","elements","openRecoveryDialog","announce",
                "loadCurrent","loadActivity","dialogPayload","dialogTrigger",
                "let recoveryAttention=true;"
                +"let recoveryCheckPending=false;"
                +"let recoveryDialogGeneration=8;"
                +"let recoveryCheckContext=null;"
                +functionSource
                +"return {{run:checkRecoveryResult,invalidate:()=>{{recoveryDialogGeneration+=1;recoveryCheckContext=null;}},snapshot:()=>({{recoveryCheckPending,recoveryCheckContext}})}};",
              );
              const flow=make(
                dataSource,state,elements,(...args)=>opened.push(args),
                (message)=>announced.push(message),async()=>{{}},async()=>{{}},
                {{can_check_result:true}},{{}},
              );
              return {{flow,opened,announced}};
            }};
            let failedResultCalls=0;let failedRefreshes=0;
            const failed=build({{
              createRecoveryCheckContext:async()=>{{throw Object.assign(new Error("private"),{{code:"recovery_details_changed"}});}},
              checkRecoveryResult:async()=>{{failedResultCalls+=1;}},
              loadRecoveryProjection:async()=>{{failedRefreshes+=1;}},
            }});
            await failed.flow.run();
            let release;let lateResultCalls=0;let lateRefreshes=0;
            const held=new Promise(resolve=>{{release=resolve;}});
            const late=build({{
              createRecoveryCheckContext:async()=>held,
              checkRecoveryResult:async()=>{{lateResultCalls+=1;}},
              loadRecoveryProjection:async()=>{{lateRefreshes+=1;}},
            }});
            const pending=late.flow.run();
            late.flow.invalidate();
            release({{case_id:"11111111-1111-4111-8111-111111111111",action_id:"22222222-2222-4222-8222-222222222222"}});
            await pending;
            console.log(JSON.stringify({{
              failedResultCalls,failedRefreshes,failedOpened:failed.opened,
              lateResultCalls,lateRefreshes,lateOpened:late.opened,
              lateSnapshot:late.flow.snapshot(),
            }}));
            """
        )
        self.assertEqual(result["failedResultCalls"], 0)
        self.assertEqual(result["failedRefreshes"], 1)
        self.assertEqual(result["failedOpened"][0][0]["title"], "The recovery details changed.")
        self.assertNotIn("private", json.dumps(result["failedOpened"]))
        self.assertEqual(result["lateResultCalls"], 0)
        self.assertEqual(result["lateRefreshes"], 0)
        self.assertEqual(result["lateOpened"], [])
        self.assertFalse(result["lateSnapshot"]["recoveryCheckPending"])
        self.assertIsNone(result["lateSnapshot"]["recoveryCheckContext"])

    def test_startup_does_not_call_recovery_api_and_route_change_clears_dialog(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        startup = app.rsplit("state.route = routeFromHash();", 1)[1]
        self.assertNotIn("loadRecoveryProjection", startup)
        self.assertIn('state.activeDialog === "recovery"', app)
        self.assertIn("closeDialog({ restoreFocus: false })", app)
        self.assertIn('data-action="check-recovery-result"', app)
        self.assertIn("recoveryDialogGeneration += 1", app)


if __name__ == "__main__":
    unittest.main()
