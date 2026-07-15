from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

from tests.test_loci_flow_stage_b2_adapter import _payload


ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"


class LociFlowB4BDuplicateConfirmationTests(unittest.TestCase):
    maxDiff = None

    def run_node(self, body: str) -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            import assert from "node:assert/strict";
            import {{ validateOrganizationResponse }} from {json.dumps((LOCI / 'data_source.js').as_uri())};
            import {{ buildProductionStageB1Contract }} from {json.dumps((LOCI / 'view_models.js').as_uri())};

            const appSource={json.dumps((LOCI / 'app.js').read_text(encoding='utf-8'))};
            const resolver=appSource.slice(
              appSource.indexOf("function currentDuplicateChoice"),
              appSource.indexOf("let dialogPayload"),
            );
            const duplicateFunctions=appSource.slice(
              appSource.indexOf("function duplicateDraft"),
              appSource.indexOf("function actionFor"),
            );
            const makeConfirm=new Function(
              "viewSource", "state", "announce", "closeDialog", "loadCurrent",
              "handleMutation", "dataSource", "setDialog", "refreshDialog",
              `${{resolver}}\n${{duplicateFunctions}}\nreturn confirmDuplicate;`,
            );
            const payload=validateOrganizationResponse({json.dumps(_payload())});
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

    def test_production_duplicate_confirmation_uses_the_rendered_choice_once(self) -> None:
        result = self.run_node(
            """
            const selectedCopyId=payload.choices_needed[1].copies[0].id;
            const duplicateDrafts={
              D1:{optionId:"choose-for-bin",selectedCopyIds:[selectedCopyId]},
            };
            let stageB1=buildProductionStageB1Contract({
              snapshot:{status:"completed",backendState:payload},duplicateDrafts,
            });
            const state={
              backendSnapshot:{status:"completed",backendState:payload},duplicateDrafts,
            };
            const viewSource={getState:()=>({stageB1})};
            const decisions=[];
            const events=[];
            let appliedSnapshot=null;
            const dataSource={
              decideChoice:async(...args)=>{
                decisions.push(args);
                return {status:"completed",backendState:payload};
              },
            };
            const closeDialog=(options={})=>events.push(["close",options]);
            const confirmDuplicate=makeConfirm(
              viewSource,state,
              (message)=>events.push(["announce",message]),
              closeDialog,
              async()=>events.push(["reload-current"]),
              async(operation,message,options)=>{
                appliedSnapshot=await operation();
                events.push(["mutation",message,options]);
                if(options.close) closeDialog();
                return appliedSnapshot;
              },
              dataSource,
              ()=>events.push(["set-dialog"]),
              ()=>events.push(["refresh-dialog"]),
            );
            await confirmDuplicate({dataset:{itemId:"D1"}});
            console.log(JSON.stringify({
              selectedCopyId,decisions,events,appliedSnapshot,
            }));
            """
        )
        self.assertEqual(
            result["decisions"],
            [["D1", "choose-for-bin", [result["selectedCopyId"]]]],
        )
        self.assertEqual(result["appliedSnapshot"]["status"], "completed")
        self.assertEqual(
            [event[0] for event in result["events"]],
            ["mutation", "close"],
        )
        self.assertTrue(result["events"][0][2]["close"])

    def test_missing_rendered_choice_closes_announces_and_reloads_without_retry(self) -> None:
        result = self.run_node(
            """
            const selectedCopyId=payload.choices_needed[1].copies[0].id;
            const duplicateDrafts={
              D1:{optionId:"choose-for-bin",selectedCopyIds:[selectedCopyId]},
            };
            const contract=buildProductionStageB1Contract({
              snapshot:{status:"completed",backendState:payload},duplicateDrafts,
            });
            const stageB1={
              ...contract,
              organization:{...contract.organization,choicesNeeded:[]},
            };
            const state={
              backendSnapshot:{status:"completed",backendState:payload},duplicateDrafts,
            };
            const events=[];
            let decisions=0;
            const confirmDuplicate=makeConfirm(
              {getState:()=>({stageB1})},state,
              (message)=>events.push(["announce",message]),
              (options={})=>events.push(["close",options]),
              async()=>events.push(["reload-current"]),
              async()=>events.push(["mutation"]),
              {decideChoice:async()=>{decisions+=1;}},
              ()=>events.push(["set-dialog"]),
              ()=>events.push(["refresh-dialog"]),
            );
            await confirmDuplicate({dataset:{itemId:"D1"}});
            console.log(JSON.stringify({events,decisions,draft:state.duplicateDrafts.D1||null}));
            """
        )
        self.assertEqual(result["decisions"], 0)
        self.assertIsNone(result["draft"])
        self.assertEqual(
            [event[0] for event in result["events"]],
            ["close", "announce", "reload-current"],
        )
        self.assertFalse(result["events"][0][1]["restoreFocus"])
        self.assertIn("no longer available", result["events"][1][1])

    def test_duplicate_confirmation_does_not_add_execution_or_change_other_confirmations(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        duplicate = app[
            app.index("function confirmDuplicate"):app.index("function actionFor")
        ]
        destination = app[
            app.index("function confirmDestination"):app.index("function duplicateDraft")
        ]
        movement = app[
            app.index("async function confirmMove"):app.index("async function checkFiles")
        ]
        self.assertIn("currentDuplicateChoice(choiceId)", duplicate)
        self.assertIn("dataSource.decideChoice(", duplicate)
        self.assertNotIn("confirmMovement", duplicate)
        self.assertNotIn("Duplicate Bin", duplicate)
        self.assertIn("dataSource.decideChoice(choiceId, optionId)", destination)
        self.assertIn("await dataSource.confirmMovement(", movement)
        self.assertIn("state.movement.confirmationText !== preview.confirmation", movement)


if __name__ == "__main__":
    unittest.main()
