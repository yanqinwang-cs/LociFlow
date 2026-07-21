from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


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
            const projection={{state:"needs_attention",title:"File changes need attention.",message:"Review what happened before continuing.",affected_items:2}};
            const dialog=buildRecoveryDialogViewModel(projection);
            console.log(JSON.stringify({{
              home:home.workflow,organize:organize.workflow,
              homeHtml:renderRoute(home,"home"),organizeHtml:renderRoute(organize,"organize"),dialog,
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
        for forbidden in ("Continue", "Check result", "input", "file tree"):
            self.assertNotIn(forbidden, json.dumps(result["dialog"]))

    def test_api_and_data_source_validate_exact_four_field_contract(self) -> None:
        result = self.run_node(
            f"""
            import {{getCurrentRecovery}} from {json.dumps((LOCI / 'api.js').as_uri())};
            import {{createBackendDataSource,validateRecoveryResponse}} from {json.dumps((LOCI / 'data_source.js').as_uri())};
            const payload={{state:"needs_attention",title:"File changes need attention.",message:"Review what happened before continuing.",affected_items:1}};
            const calls=[];
            const fetcher=async(url,options)=>{{calls.push([url,options.method]);return {{ok:true,status:200,json:async()=>payload}};}};
            const direct=await getCurrentRecovery(fetcher);
            const source=createBackendDataSource({{apiClient:{{getCurrentOrganization:async()=>({{}}),getCurrentRecovery:async()=>payload}},csrfToken:"csrf"}});
            const loaded=await source.loadRecoveryProjection();
            let extraRejected=false;
            try{{validateRecoveryResponse({{...payload,path:"Inbox/secret.txt"}});}}catch(error){{extraRejected=error.code==="invalid_recovery_contract";}}
            console.log(JSON.stringify({{calls,direct,loaded,extraRejected}}));
            """
        )
        self.assertEqual(result["calls"], [["/api/recovery/current", "GET"]])
        self.assertEqual(set(result["loaded"]), {"state", "title", "message", "affected_items"})
        self.assertTrue(result["extraRejected"])

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
            const SAFE_RECOVERY_FAILURE={{state:"cannot_continue_automatically",title:"Safe",message:"Safe",affected_items:0}};
            const make=new Function("state","dataSource","openRecoveryDialog","SAFE_RECOVERY_FAILURE",`let recoveryLoadPending=false;{function_source};return reviewRecovery;`);
            const review=make(state,dataSource,openRecoveryDialog,SAFE_RECOVERY_FAILURE);
            const firstTrigger={{disabled:false}};
            const first=review(firstTrigger);const duplicate=review(firstTrigger);
            const during={{calls,disabled:firstTrigger.disabled}};
            release({{state:"clear",title:"Clear",message:"Clear",affected_items:0}});
            await Promise.all([first,duplicate]);
            held=Promise.resolve({{state:"needs_attention",title:"New",message:"New",affected_items:1}});
            const secondTrigger={{disabled:false}};await review(secondTrigger);
            held=Promise.reject(new Error("/private/root/secret.json"));
            await review({{disabled:false}});
            let routeRelease;
            held=new Promise(resolve=>{{routeRelease=resolve;}});
            const routed=review({{disabled:false}});state.route="activity";
            routeRelease({{state:"needs_attention",title:"Stale",message:"Stale",affected_items:9}});
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

    def test_startup_does_not_call_recovery_api_and_route_change_clears_dialog(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        startup = app.rsplit("state.route = routeFromHash();", 1)[1]
        self.assertNotIn("loadRecoveryProjection", startup)
        self.assertIn('state.activeDialog === "recovery"', app)
        self.assertIn("closeDialog({ restoreFocus: false })", app)
        self.assertNotIn("check-result", app.lower())


if __name__ == "__main__":
    unittest.main()
