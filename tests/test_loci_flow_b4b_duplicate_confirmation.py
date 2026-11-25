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
            app.index("function confirmDuplicate(target)"):app.index("function actionFor")
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

    def test_shared_dialog_clears_inert_before_reuse_for_approval(self) -> None:
        result = self.run_node(
            """
            const selectedCopyId=payload.choices_needed[1].copies[0].id;
            const duplicateDrafts={
              D1:{optionId:"choose-for-bin",selectedCopyIds:[selectedCopyId]},
            };
            const stageB1=buildProductionStageB1Contract({
              snapshot:{status:"completed",backendState:payload},duplicateDrafts,
            });
            const state={
              route:"organize",theme:"light",loadStatus:"ready",pendingMutation:false,
              backendSnapshot:{status:"completed",backendState:payload},duplicateDrafts,
              destinationDrafts:{},selectedItemId:null,selectedRowIds:[],workflowOverride:null,
              activeDialog:"duplicate",
              activity:{loadStatus:"ready",pendingUndo:false},
              movement:{previewStatus:"idle",executionPending:false,execution:null},
              duplicateMovement:{previewStatus:"idle",executionPending:false},
              duplicateBin:{previewStatus:"idle",restorePending:false},
            };
            const viewSource={getState:()=>({stageB1})};
            const events=[];
            const elements={
              app:{inert:false,setAttribute:()=>{}},
              nav:{querySelectorAll:()=>[],querySelector:()=>null},
              dialog:{
                open:true,inert:false,
                close(){this.open=false;events.push(["dialog-closed"]);},
                showModal(){this.open=true;events.push(["dialog-opened"]);},
                querySelector:()=>({focus:()=>{}}),
              },
              dialogEyebrow:{textContent:""},dialogTitle:{textContent:""},
              dialogDescription:{textContent:""},dialogContent:{innerHTML:""},
              dialogActions:{innerHTML:""},
            };
            const document={
              documentElement:{dataset:{}},activeElement:null,
              querySelectorAll:()=>[],querySelector:()=>null,
            };
            const window={requestAnimationFrame:(callback)=>callback()};
            let releaseDuplicate;
            const duplicatePending=new Promise((resolve)=>{releaseDuplicate=resolve;});
            let approvals=0;
            let failApproval=false;
            const dataSource={
              decideChoice:async()=>{events.push(["duplicate-request"]);await duplicatePending;return {status:"completed"};},
              approve:async()=>{approvals+=1;if(failApproval)throw {code:"request_failed"};return {status:"completed"};},
            };
            const syncSource=appSource.slice(
              appSource.indexOf("function syncShell"),
              appSource.indexOf("function replaceActivityEmptyState"),
            );
            const dialogSource=appSource.slice(
              appSource.indexOf("function setDialog"),
              appSource.indexOf("function resetMovementConfirmation"),
            );
            const pendingSource=appSource.slice(
              appSource.indexOf("function setPending"),
              appSource.indexOf("async function loadCurrent"),
            );
            const mutationSource=appSource.slice(
              appSource.indexOf("async function handleMutation"),
              appSource.indexOf("async function reloadAfterConflict"),
            );
            const actionSource=appSource.slice(
              appSource.indexOf("function actionFor"),
              appSource.indexOf('document.addEventListener("click"'),
            );
            const makeLifecycle=new Function(
              "viewSource","state","elements","document","window","buildShellViewModel",
              "renderDialog","buildDialogViewModel","announce","applySnapshot","render",
              "workflowOverride","reloadAfterConflict","dataSource","loadCurrent","setDialog",
              "refreshDialog",
              `let dialogPayload={},dialogTrigger=null;\n${syncSource}\n${dialogSource}\n${pendingSource}\n${mutationSource}\n${resolver}\n${duplicateFunctions}\n${actionSource}\nreturn {confirmDuplicate,actionFor};`,
            );
            const lifecycle=makeLifecycle(
              viewSource,state,elements,document,window,
              ()=>({scope:{displayName:"Downloads"},toolbarFreshnessLabel:"Now",organizeCount:1,duplicateBinCount:0}),
              ()=>({eyebrow:"Final approval",title:"Ready to organize",description:"Confirm",content:"",actions:""}),
              ()=>({}),
              (message)=>events.push(["announce",message]),
              (snapshot)=>events.push(["snapshot",snapshot.status]),
              ()=>events.push(["render"]),
              (status)=>({status,primaryAction:{}}),
              async()=>events.push(["reload-conflict"]),
              dataSource,
              async()=>events.push(["reload-current"]),
              ()=>{},
              ()=>{},
            );

            const duplicateResult=lifecycle.confirmDuplicate({dataset:{itemId:"D1"}});
            await Promise.resolve();
            const duringDuplicate={
              pending:state.pendingMutation,dialogInert:elements.dialog.inert,dialogOpen:elements.dialog.open,
            };
            releaseDuplicate();
            await duplicateResult;
            const afterDuplicate={
              pending:state.pendingMutation,dialogInert:elements.dialog.inert,dialogOpen:elements.dialog.open,
            };

            lifecycle.actionFor({dataset:{action:"review-and-approve"}});
            const approvalOpened={dialogInert:elements.dialog.inert,dialogOpen:elements.dialog.open};
            lifecycle.actionFor({dataset:{action:"close-dialog"}});
            const backWorked=!elements.dialog.open;

            lifecycle.actionFor({dataset:{action:"review-and-approve"}});
            await lifecycle.actionFor({dataset:{action:"approve-organization"}});
            const afterApproval={approvals,pending:state.pendingMutation,dialogInert:elements.dialog.inert};

            lifecycle.actionFor({dataset:{action:"review-and-approve"}});
            failApproval=true;
            await lifecycle.actionFor({dataset:{action:"approve-organization"}});
            const afterError={pending:state.pendingMutation,dialogInert:elements.dialog.inert,dialogOpen:elements.dialog.open};
            console.log(JSON.stringify({
              duringDuplicate,afterDuplicate,approvalOpened,backWorked,afterApproval,afterError,events,
            }));
            """
        )
        self.assertEqual(
            result["duringDuplicate"],
            {"pending": True, "dialogInert": True, "dialogOpen": True},
        )
        self.assertEqual(
            result["afterDuplicate"],
            {"pending": False, "dialogInert": False, "dialogOpen": False},
        )
        self.assertEqual(
            result["approvalOpened"],
            {"dialogInert": False, "dialogOpen": True},
        )
        self.assertTrue(result["backWorked"])
        self.assertEqual(
            result["afterApproval"],
            {"approvals": 1, "pending": False, "dialogInert": False},
        )
        self.assertEqual(
            result["afterError"],
            {"pending": False, "dialogInert": False, "dialogOpen": True},
        )


if __name__ == "__main__":
    unittest.main()
