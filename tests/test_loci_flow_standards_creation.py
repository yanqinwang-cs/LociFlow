from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

from tests.test_loci_flow_stage_b2_adapter import _payload
from tests.test_loci_flow_standards_adapter import _standards


ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"


def _preview() -> dict[str, object]:
    return {
        "preview_token": "opaque-preview-token",
        "expires_at": "2026-07-18T10:15:00+00:00",
        "preview": {
            "name": "Keep matching CS1010X files together",
            "description": "Files matching CS1010X will be treated as one group.",
            "scope_label": "Selected folder",
            "effect_summary": "Matching files shape grouping before suggestions are created.",
            "matching_files": 2,
            "fresh_scan_required": True,
            "attention_message": None,
        },
    }


def _context() -> dict[str, object]:
    return {
        "scope_choices": [
            {"id": "scope-root", "label": "Selected folder", "kind": "root"},
            {"id": "scope-documents", "label": "Documents", "kind": "folder"},
        ],
        "expires_at": "2026-07-18T10:15:00+00:00",
        "revision": 7,
    }


class LociFlowStandardsCreationTests(unittest.TestCase):
    maxDiff = None

    def run_node(self, body: str, imports: str = "") -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            {imports}
            const organizationPayload={json.dumps(_payload())};
            const standardsPayload={json.dumps(_standards())};
            const creationContext={json.dumps(_context())};
            const previewPayload={json.dumps(_preview())};
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

    def test_api_sends_preset_dependent_exact_forms_and_token_only_save(self) -> None:
        result = self.run_node(
            """
            const calls=[];
            const fetchImpl=async(url,options)=>{
              calls.push({url,fields:Object.fromEntries(options.body.entries())});
              if(url.endsWith("/creation-context")) return {ok:true,status:200,json:async()=>clone(creationContext)};
              if(url.endsWith("/preview")) return {ok:true,status:200,json:async()=>clone(previewPayload)};
              return {ok:true,status:200,json:async()=>({revision:8,items:clone(standardsPayload.items),fresh_scan_required:true,save_message:"Standard saved."})};
            };
            await getStandardsCreationContext(7,fetchImpl,"csrf");
            await previewStandardCreation({presetType:"keep_folder_unchanged",scopeId:"scope-documents",expectedRevision:7},fetchImpl,"csrf");
            await previewStandardCreation({presetType:"keep_matching_files_together",scopeId:"scope-root",matcherKind:"course_code",matcherValue:"CS1010X",expectedRevision:7},fetchImpl,"csrf");
            await saveStandardCreation({previewToken:"opaque-preview-token",expectedRevision:7},fetchImpl,"csrf");
            console.log(JSON.stringify(calls));
            """,
            f'import {{ getStandardsCreationContext,previewStandardCreation,saveStandardCreation }} from {json.dumps((LOCI / "api.js").as_uri())};',
        )
        self.assertEqual(
            result,
            [
                {
                    "url": "/api/standards/creation-context",
                    "fields": {
                        "csrf_token": "csrf",
                        "expected_revision": "7",
                    },
                },
                {
                    "url": "/api/standards/preview",
                    "fields": {
                        "csrf_token": "csrf",
                        "expected_revision": "7",
                        "preset_type": "keep_folder_unchanged",
                        "scope_id": "scope-documents",
                    },
                },
                {
                    "url": "/api/standards/preview",
                    "fields": {
                        "csrf_token": "csrf",
                        "expected_revision": "7",
                        "preset_type": "keep_matching_files_together",
                        "scope_id": "scope-root",
                        "matcher_kind": "course_code",
                        "matcher_value": "CS1010X",
                    },
                },
                {
                    "url": "/api/standards",
                    "fields": {
                        "csrf_token": "csrf",
                        "expected_revision": "7",
                        "preview_token": "opaque-preview-token",
                    },
                },
            ],
        )
        serialized = json.dumps(result)
        for forbidden in ("path", "parameters", "destination", "regex", "json"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_list_refresh_preserves_context_and_revision_change_invalidates_it(self) -> None:
        result = self.run_node(
            """
            let revision=7;let contextCalls=0;let previewCalls=0;
            const apiClient={
              getCurrentOrganization:async()=>clone(organizationPayload),
              getStandards:async()=>({...clone(standardsPayload),revision}),
              getStandardsCreationContext:async()=>{contextCalls+=1;return clone(creationContext);},
              previewStandardCreation:async()=>{previewCalls+=1;return clone(previewPayload);},
            };
            const source=createBackendDataSource({apiClient,csrfToken:"csrf"});
            await source.loadStandards();
            await source.loadStandardsCreationContext(7);
            await source.loadStandards();
            await source.previewStandardCreation({presetId:"keep_folder_unchanged",scopeId:"scope-root"},7);
            revision=8;
            await source.loadStandards();
            let stale=null;
            try{await source.previewStandardCreation({presetId:"keep_folder_unchanged",scopeId:"scope-root"},8);}catch(error){stale=error.code;}
            console.log(JSON.stringify({contextCalls,previewCalls,stale}));
            """,
            f'import {{ createBackendDataSource }} from {json.dumps((LOCI / "data_source.js").as_uri())};',
        )
        self.assertEqual(result, {
            "contextCalls": 1,
            "previewCalls": 1,
            "stale": "invalid_standards_contract",
        })

    def test_add_standard_requests_one_context_and_failure_does_not_open_dialog(self) -> None:
        app_source = (LOCI / "app.js").read_text(encoding="utf-8")
        result = self.run_node(
            """
            const resetStart=appSource.indexOf("function resetStandardBuilder");
            const resetEnd=appSource.indexOf("function discardStandardBuilder",resetStart);
            const openStart=appSource.indexOf("async function openStandardBuilder");
            const openEnd=appSource.indexOf("function standardBuilderInputComplete",openStart);
            if(Math.min(resetStart,resetEnd,openStart,openEnd)<0)throw new Error("builder functions unavailable");
            const make=(fail=false)=>{
              const state={standards:{loadStatus:"ready",revision:7,scopeChoices:[],creationAvailable:true,creationMessage:null,mutationPending:false,errorMessage:null,conflictMessage:null,noticeMessage:null,builder:{open:false}}};
              let calls=0;let opens=0;let clears=0;
              const dataSource={
                clearStandardsCreationContext:()=>{clears+=1;},
                loadStandardsCreationContext:async()=>{calls+=1;if(fail)throw {code:"application_stopping"};return clone(creationContext);},
              };
              const factory=new Function("state","dataSource","announce","render","openDialog","standardsErrorMessage",`${appSource.slice(resetStart,resetEnd)}\n${appSource.slice(openStart,openEnd)};return {openStandardBuilder,resetStandardBuilder};`);
              const api=factory(state,dataSource,()=>{},()=>{},()=>{opens+=1;},(error)=>error.code);
              return {state,api,result:()=>({calls,opens,clears,open:state.standards.builder.open,choices:state.standards.scopeChoices.length,error:state.standards.errorMessage,pending:state.standards.mutationPending})};
            };
            const success=make();await success.api.openStandardBuilder({});const successResult=success.result();success.api.resetStandardBuilder();const closed=success.result();
            const failure=make(true);await failure.api.openStandardBuilder({});const failureResult=failure.result();
            console.log(JSON.stringify({success:successResult,closed,failure:failureResult}));
            """,
            f"const appSource={json.dumps(app_source)};",
        )
        self.assertEqual(result["success"]["calls"], 1)
        self.assertEqual(result["success"]["opens"], 1)
        self.assertTrue(result["success"]["open"])
        self.assertEqual(result["success"]["choices"], 2)
        self.assertFalse(result["success"]["pending"])
        self.assertFalse(result["closed"]["open"])
        self.assertEqual(result["closed"]["choices"], 0)
        self.assertEqual(result["failure"]["calls"], 1)
        self.assertEqual(result["failure"]["opens"], 0)
        self.assertFalse(result["failure"]["open"])
        self.assertEqual(result["failure"]["error"], "application_stopping")

    def test_data_source_validates_preview_save_and_blocks_double_submission(self) -> None:
        result = self.run_node(
            """
            let releasePreview; let releaseSave;
            const heldPreview=new Promise((resolve)=>{releasePreview=resolve;});
            const heldSave=new Promise((resolve)=>{releaseSave=resolve;});
            const calls=[];
            const apiClient={
              getCurrentOrganization:async()=>clone(organizationPayload),
              getStandards:async()=>clone(standardsPayload),
              getStandardsCreationContext:async(value,csrf)=>{calls.push(["context",value,csrf]);return clone(creationContext);},
              previewStandardCreation:async(value,csrf)=>{calls.push(["preview",value,csrf]);return heldPreview;},
              saveStandardCreation:async(value,csrf)=>{calls.push(["save",value,csrf]);return heldSave;},
            };
            const source=createBackendDataSource({apiClient,csrfToken:"csrf"});
            await source.loadStandards();
            await source.loadStandardsCreationContext(7);
            const previewRequest=source.previewStandardCreation({presetId:"keep_matching_files_together",scopeId:"scope-root",matcherKind:"course_code",matcherValue:"CS1010X"},7);
            let secondPreview=null;
            try{await source.previewStandardCreation({presetId:"keep_matching_files_together",scopeId:"scope-root",matcherKind:"course_code",matcherValue:"CS1010X"},7);}catch(error){secondPreview=error.code;}
            releasePreview(clone(previewPayload));
            const preview=await previewRequest;
            const saveRequest=source.saveStandardCreation(preview.preview_token,7);
            let secondSave=null;
            try{await source.saveStandardCreation(preview.preview_token,7);}catch(error){secondSave=error.code;}
            releaseSave({revision:8,items:clone(standardsPayload.items),fresh_scan_required:true,save_message:"Standard saved."});
            const saved=await saveRequest;
            const malformed=clone(previewPayload); malformed.preview.relative_path="Documents";
            let malformedError=null;
            try{validateStandardPreviewResponse(malformed);}catch(error){malformedError=error.code;}
            console.log(JSON.stringify({calls,preview,saved,secondPreview,secondSave,malformedError,pending:source.isStandardsMutationPending()}));
            """,
            f'import {{ createBackendDataSource,validateStandardPreviewResponse }} from {json.dumps((LOCI / "data_source.js").as_uri())};',
        )
        self.assertEqual(
            [call[0] for call in result["calls"]],
            ["context", "preview", "save"],
        )
        self.assertEqual(result["secondPreview"], "standards_mutation_in_progress")
        self.assertEqual(result["secondSave"], "standards_mutation_in_progress")
        self.assertEqual(result["malformedError"], "invalid_standards_contract")
        self.assertFalse(result["pending"])
        self.assertEqual(result["saved"]["save_message"], "Standard saved.")

    def test_view_model_and_dialog_expose_only_guided_creation_controls(self) -> None:
        result = self.run_node(
            """
            const backend=validateOrganizationResponse(clone(organizationPayload));
            const make=(builder,creationAvailable=true,creationMessage=null)=>{
              const standards={loadStatus:"ready",revision:7,items:clone(standardsPayload.items),presets:clone(standardsPayload.presets),scopeChoices:clone(creationContext.scope_choices),creationAvailable,creationMessage,mutationPending:false,freshScanRequired:false,builder};
              const contract=buildProductionStageB1Contract({snapshot:{status:"completed",backendState:backend},standards});
              const source={getState:()=>({route:"standards",theme:"light",selectedItemId:null,stageB1:contract})};
              const page=renderRoute(buildStandardsViewModel(source),"standards");
              const dialog=renderDialog(buildDialogViewModel(source),"standard-builder");
              return {page,dialog};
            };
            const empty={open:true,presetId:"",scopeId:"",matcherKind:"course_code",matcherValue:"",status:"idle",preview:null,errorMessage:null};
            const matching={...empty,presetId:"keep_matching_files_together",scopeId:"scope-root",matcherValue:"CS1010X"};
            const ready={...matching,status:"preview_ready",preview:clone(previewPayload.preview)};
            const pending={...matching,status:"save_pending",preview:clone(previewPayload.preview)};
            const available=make(empty);
            const matchingDialog=make(matching).dialog;
            const readyDialog=make(ready).dialog;
            const pendingDialog=make(pending).dialog;
            const unavailable=make(empty,false,"Check files before adding a Standard.");
            console.log(JSON.stringify({available,matchingDialog,readyDialog,pendingDialog,unavailable}));
            """,
            "\n".join(
                [
                    f'import {{ validateOrganizationResponse }} from {json.dumps((LOCI / "data_source.js").as_uri())};',
                    f'import {{ buildProductionStageB1Contract,buildStandardsViewModel,buildDialogViewModel }} from {json.dumps((LOCI / "view_models.js").as_uri())};',
                    f'import {{ renderRoute,renderDialog }} from {json.dumps((LOCI / "views.js").as_uri())};',
                ]
            ),
        )
        self.assertIn('data-action="open-standard-builder"', result["available"]["page"])
        self.assertIn("Keep a folder unchanged", result["available"]["dialog"]["content"])
        self.assertIn("Keep matching files together", result["available"]["dialog"]["content"])
        self.assertNotIn("Exact code", result["available"]["dialog"]["content"])
        self.assertIn("Course code", result["matchingDialog"]["content"])
        self.assertIn("Project code", result["matchingDialog"]["content"])
        self.assertIn("Exact code", result["matchingDialog"]["content"])
        self.assertIn("Save standard", result["readyDialog"]["actions"])
        self.assertIn("CS1010X", result["readyDialog"]["content"])
        self.assertIn("Saving…", result["pendingDialog"]["actions"])
        self.assertIn("disabled", result["pendingDialog"]["content"])
        self.assertIn("Check files before adding", result["unavailable"]["page"])
        self.assertIn('data-action="none"', result["unavailable"]["page"])
        combined = json.dumps(result)
        for forbidden in (
            "relative_path",
            "configured_root_identity",
            "preview_token",
            "person name",
            "year",
            "regex",
            "json",
        ):
            self.assertNotIn(forbidden, combined.lower())

    def test_conflict_is_safe_and_not_retried_and_error_messages_are_concrete(self) -> None:
        result = self.run_node(
            """
            let saveCalls=0;
            const apiClient={
              getCurrentOrganization:async()=>clone(organizationPayload),
              getStandards:async()=>clone(standardsPayload),
              saveStandardCreation:async()=>{saveCalls+=1;throw new LociFlowApiError("standard_conflict","This Standard conflicts with another active Standard.",409);},
            };
            const source=createBackendDataSource({apiClient,csrfToken:"csrf"});
            await source.loadStandards();
            let code=null;let message=null;
            try{await source.saveStandardCreation("opaque-preview-token",7);}catch(error){code=error.code;message=error.message;}
            const messages=Object.fromEntries([
              "preview_expired","preview_superseded","folder_scope_stale",
              "active_plan_conflict","invalid_matcher",
            ].map((item)=>[item,standardsErrorMessage({code:item})]));
            console.log(JSON.stringify({saveCalls,code,message,messages}));
            """,
            "\n".join(
                [
                    f'import {{ LociFlowApiError }} from {json.dumps((LOCI / "api.js").as_uri())};',
                    f'import {{ createBackendDataSource,standardsErrorMessage }} from {json.dumps((LOCI / "data_source.js").as_uri())};',
                ]
            ),
        )
        self.assertEqual(result["saveCalls"], 1)
        self.assertEqual(result["code"], "standard_conflict")
        self.assertIn("conflicts", result["message"])
        self.assertIn("expired", result["messages"]["preview_expired"])
        self.assertIn("newer preview", result["messages"]["preview_superseded"])
        self.assertIn("Check files again", result["messages"]["folder_scope_stale"])
        self.assertIn("Finish or restart", result["messages"]["active_plan_conflict"])
        self.assertIn("valid course or project code", result["messages"]["invalid_matcher"])

    def test_fixture_mode_cannot_preview_or_save_live_standards(self) -> None:
        result = self.run_node(
            """
            const fixture=createFixtureDataSource();
            const codes=[];
            for(const operation of [
              ()=>fixture.previewStandardCreation({},0),
              ()=>fixture.saveStandardCreation("token",0),
            ]){
              try{await operation();}catch(error){codes.push(error.code);}
            }
            console.log(JSON.stringify(codes));
            """,
            f'import {{ createFixtureDataSource }} from {json.dumps((LOCI / "data_source.js").as_uri())};',
        )
        self.assertEqual(result, ["standards_unavailable", "standards_unavailable"])

    def test_matcher_input_preserves_dom_node_caret_and_native_edit_order(self) -> None:
        app_source = (LOCI / "app.js").read_text(encoding="utf-8")
        result = self.run_node(
            """
            const start=appSource.indexOf("function standardBuilderInputComplete");
            const end=appSource.indexOf("async function previewStandard",start);
            if(start<0||end<0) throw new Error("Standards input orchestration source not found");
            const functions=appSource.slice(start,end);
            const builder={open:true,presetId:"keep_matching_files_together",scopeId:"scope-root",matcherKind:"course_code",matcherValue:"",status:"preview_ready",preview:clone(previewPayload.preview),previewToken:"old-token",expiresAt:previewPayload.expires_at,errorMessage:"old error"};
            const state={activeDialog:"standard-builder",standards:{builder}};
            const previewButton={disabled:true};
            const saveButton={disabled:false};
            const messages=[{removed:false,remove(){this.removed=true;}},{removed:false,remove(){this.removed=true;}}];
            const input={dataset:{standardBuilderField:"matcher-value"},id:"standard-matcher-value",value:"",selectionStart:0,selectionEnd:0,focused:true};
            let refreshCalls=0;let previewCalls=0;let saveCalls=0;
            const elements={
              dialogContent:{querySelectorAll:()=>messages},
              dialogActions:{querySelector:(selector)=>selector.includes("preview-standard")?previewButton:selector.includes("save-standard")?saveButton:null},
            };
            const make=new Function("state","elements","refreshDialog",`${functions};return {updateStandardBuilderField};`);
            const api=make(state,elements,()=>{refreshCalls+=1;});
            const inputIdentity=input;
            const insert=(text)=>{
              const start=input.selectionStart;const end=input.selectionEnd;
              input.value=input.value.slice(0,start)+text+input.value.slice(end);
              input.selectionStart=input.selectionEnd=start+text.length;
              api.updateStandardBuilderField(input);
            };
            const backspace=()=>{
              let start=input.selectionStart;const end=input.selectionEnd;
              if(start===end&&start>0) start-=1;
              input.value=input.value.slice(0,start)+input.value.slice(end);
              input.selectionStart=input.selectionEnd=start;
              api.updateStandardBuilderField(input);
            };
            const deleteForward=()=>{
              const start=input.selectionStart;let end=input.selectionEnd;
              if(start===end&&end<input.value.length) end+=1;
              input.value=input.value.slice(0,start)+input.value.slice(end);
              input.selectionStart=input.selectionEnd=start;
              api.updateStandardBuilderField(input);
            };

            for(const character of ["C","S","1","0","1","0","X"]) insert(character);
            const sequential={value:input.value,caret:input.selectionStart,sameNode:input===inputIdentity};

            input.value="CS10X";input.selectionStart=input.selectionEnd=4;insert("10");
            const middle={value:input.value,caret:input.selectionStart};
            input.value="CS100X";input.selectionStart=input.selectionEnd=4;insert("1");
            const statedStartVariant={value:input.value,caret:input.selectionStart};

            input.value="CS1010X";input.selectionStart=input.selectionEnd=6;backspace();
            const backed={value:input.value,caret:input.selectionStart};
            input.value="CS1010XX";input.selectionStart=input.selectionEnd=7;deleteForward();
            const deleted={value:input.value,caret:input.selectionStart};
            input.value="CS1010X";input.selectionStart=2;input.selectionEnd=6;insert("2020");
            const replaced={value:input.value,caret:input.selectionStart};

            input.value="";input.selectionStart=input.selectionEnd=0;
            api.updateStandardBuilderField(input);
            const emptyButtons={preview:previewButton.disabled,save:saveButton.disabled};
            insert("PRJ-204");
            const pasted={value:input.value,caret:input.selectionStart};
            input.selectionStart=0;input.selectionEnd=input.value.length;insert("BootAI");
            const selectAllReplacement={value:input.value,caret:input.selectionStart};
            input.value="課程";input.selectionStart=input.selectionEnd=input.value.length;
            api.updateStandardBuilderField(input);
            const composition={value:builder.matcherValue,caret:input.selectionStart};

            const beforeSelectRefresh=refreshCalls;
            api.updateStandardBuilderField({dataset:{standardBuilderField:"matcher-kind"},id:"standard-matcher-kind",value:"project_code"});
            api.updateStandardBuilderField({dataset:{standardBuilderField:"preset"},id:"standard-preset",value:"keep_folder_unchanged"});
            const selectRefreshes=refreshCalls-beforeSelectRefresh;
            console.log(JSON.stringify({sequential,middle,statedStartVariant,backed,deleted,replaced,emptyButtons,pasted,selectAllReplacement,composition,state:{value:builder.matcherValue,status:builder.status,token:builder.previewToken,preview:builder.preview,error:builder.errorMessage},buttons:{preview:previewButton.disabled,save:saveButton.disabled},messages:messages.map((item)=>item.removed),refreshCalls,selectRefreshes,previewCalls,saveCalls}));
            """,
            f"const appSource={json.dumps(app_source)};",
        )
        self.assertEqual(result["sequential"], {"value": "CS1010X", "caret": 7, "sameNode": True})
        self.assertEqual(result["middle"], {"value": "CS1010X", "caret": 6})
        self.assertEqual(result["statedStartVariant"], {"value": "CS1010X", "caret": 5})
        self.assertEqual(result["backed"], {"value": "CS101X", "caret": 5})
        self.assertEqual(result["deleted"], {"value": "CS1010X", "caret": 7})
        self.assertEqual(result["replaced"], {"value": "CS2020X", "caret": 6})
        self.assertEqual(result["emptyButtons"], {"preview": True, "save": True})
        self.assertEqual(result["pasted"], {"value": "PRJ-204", "caret": 7})
        self.assertEqual(result["selectAllReplacement"], {"value": "BootAI", "caret": 6})
        self.assertEqual(result["composition"], {"value": "課程", "caret": 2})
        self.assertEqual(result["previewCalls"], 0)
        self.assertEqual(result["saveCalls"], 0)
        self.assertTrue(result["buttons"]["save"])
        self.assertTrue(all(result["messages"]))
        self.assertEqual(result["selectRefreshes"], 2)
        self.assertEqual(result["refreshCalls"], 2)

    def test_builder_close_and_route_change_still_clear_preview_token(self) -> None:
        app_source = (LOCI / "app.js").read_text(encoding="utf-8")
        result = self.run_node(
            """
            const closeStart=appSource.indexOf("function closeDialog");
            const closeEnd=appSource.indexOf("function resetStandardBuilder",closeStart);
            const navigateStart=appSource.indexOf("function navigate");
            const navigateEnd=appSource.indexOf("function setDialog",navigateStart);
            if(Math.min(closeStart,closeEnd,navigateStart,navigateEnd)<0) throw new Error("Dialog lifecycle source not found");
            const builder={open:true,previewToken:"token",preview:clone(previewPayload.preview)};
            const state={route:"standards",activeDialog:"standard-builder",activity:{},movement:{},duplicateMovement:{},duplicateBin:{},standards:{builder}};
            let closeResets=0;let routeResets=0;
            const reset=()=>{closeResets+=1;builder.open=false;builder.preview=null;builder.previewToken=null;};
            const elements={dialog:{open:true,close(){this.open=false;}}};
            const closeMake=new Function("state","elements","resetStandardBuilder","resetMovementConfirmation","resetDuplicateMovementConfirmation","resetDuplicateRestoreConfirmation",`let dialogPayload={};let dialogTrigger=null;${appSource.slice(closeStart,closeEnd)};return closeDialog;`);
            closeMake(state,elements,reset,()=>{},()=>{},()=>{})();
            const afterClose={token:builder.previewToken,open:builder.open,resets:closeResets};

            builder.open=true;builder.preview=clone(previewPayload.preview);builder.previewToken="route-token";state.activeDialog="standard-builder";state.route="standards";
            const discard=({close})=>{routeResets+=1;builder.open=false;builder.preview=null;builder.previewToken=null;if(close)state.activeDialog=null;};
            const windowObject={location:{hash:"#/standards"}};
            const navigateMake=new Function("state","routes","window","discardActivityPreview","discardMovementConfirmation","discardDuplicateMovementConfirmation","discardDuplicateRestoreConfirmation","discardStandardBuilder","render","loadActivity","loadDuplicateBin","loadStandards",`${appSource.slice(navigateStart,navigateEnd)};return navigate;`);
            navigateMake(state,new Set(["home","standards"]),windowObject,()=>{},()=>{},()=>{},()=>{},discard,()=>{},()=>{},()=>{},()=>{})("home");
            console.log(JSON.stringify({afterClose,afterRoute:{token:builder.previewToken,open:builder.open,resets:routeResets,route:state.route,dialog:state.activeDialog}}));
            """,
            f"const appSource={json.dumps(app_source)};",
        )
        self.assertEqual(result["afterClose"], {"token": None, "open": False, "resets": 1})
        self.assertEqual(
            result["afterRoute"],
            {"token": None, "open": False, "resets": 1, "route": "home", "dialog": None},
        )

    def test_app_invalidates_fields_and_coalesces_preview_and_save_without_scan(self) -> None:
        app_source = (LOCI / "app.js").read_text(encoding="utf-8")
        result = self.run_node(
            """
            const start=appSource.indexOf("function resetStandardBuilder");
            const end=appSource.indexOf("async function loadActivity",start);
            if(start<0||end<0) throw new Error("Standards builder orchestration source not found");
            const functions=appSource.slice(start,end);
            const state={
              route:"standards",activeDialog:"standard-builder",selectedItemId:"old",selectedRowIds:["old"],
              destinationDrafts:{old:"x"},duplicateDrafts:{old:"x"},workflowOverride:null,
              movement:{previewVersion:0,previewStatus:"idle",preview:null,confirmationText:""},
              duplicateMovement:{previewVersion:0,previewStatus:"idle",preview:null,confirmationText:""},
              standards:{loadStatus:"ready",revision:7,items:clone(standardsPayload.items),presets:clone(standardsPayload.presets),scopeChoices:clone(creationContext.scope_choices),creationAvailable:true,creationMessage:null,mutationPending:false,pendingStandardId:null,errorMessage:null,conflictMessage:null,freshScanRequired:false,noticeMessage:null,loadVersion:0,builder:{open:true,presetId:"keep_matching_files_together",scopeId:"scope-root",matcherKind:"course_code",matcherValue:"OLD",status:"preview_ready",preview:clone(previewPayload.preview),previewToken:"old-token",expiresAt:previewPayload.expires_at,errorMessage:null}},
            };
            let previewCalls=0;let saveCalls=0;let loadCalls=0;let scanCalls=0;
            let releasePreview;let releaseSave;
            const heldPreview=new Promise((resolve)=>{releasePreview=resolve;});
            const heldSave=new Promise((resolve)=>{releaseSave=resolve;});
            const dataSource={
              previewStandardCreation:async()=>{previewCalls+=1;return heldPreview;},
              saveStandardCreation:async()=>{saveCalls+=1;return heldSave;},
              loadStandards:async()=>{loadCalls+=1;return {...clone(standardsPayload),revision:8};},
              startScan:async()=>{scanCalls+=1;},
            };
            const events=[];
            const elements={dialog:{open:false,close:()=>events.push("close")}};
            const make=new Function("state","dataSource","render","syncShell","standardsErrorMessage","announce","discardMovementConfirmation","discardDuplicateMovementConfirmation","workflowOverride","openDialog","refreshDialog","setDialog","elements",`let dialogPayload={};let dialogTrigger=null;let standardsLoadPromise=null;${functions};return {resetStandardBuilder,discardStandardBuilder,updateStandardBuilderField,previewStandard,saveStandard};`);
            const api=make(state,dataSource,()=>events.push("render"),()=>events.push("sync"),(error)=>error.message||error.code,(message)=>events.push(message),()=>events.push("discard-move"),()=>events.push("discard-bin"),(status,message,action,label,staleKind)=>({status,message,primaryAction:{action,label},staleKind}),()=>{},()=>events.push("refresh"),()=>events.push("dialog"),elements);
            api.updateStandardBuilderField({dataset:{standardBuilderField:"matcher-value"},id:"standard-matcher-value",value:"CS1010X"});
            const invalidated={status:state.standards.builder.status,token:state.standards.builder.previewToken,preview:state.standards.builder.preview,value:state.standards.builder.matcherValue};
            const firstPreview=api.previewStandard();
            const duplicatePreview=api.previewStandard();
            releasePreview(clone(previewPayload));
            await Promise.all([firstPreview,duplicatePreview]);
            const previewed={status:state.standards.builder.status,token:state.standards.builder.previewToken};
            const firstSave=api.saveStandard();
            const duplicateSave=api.saveStandard();
            releaseSave({revision:8,items:clone(standardsPayload.items),fresh_scan_required:true,save_message:"Standard saved."});
            await Promise.all([firstSave,duplicateSave]);
            console.log(JSON.stringify({invalidated,previewed,previewCalls,saveCalls,loadCalls,scanCalls,builder:state.standards.builder,fresh:state.standards.freshScanRequired,drafts:[state.selectedRowIds,state.destinationDrafts,state.duplicateDrafts],workflow:state.workflowOverride,notice:state.standards.noticeMessage}));
            """,
            f"const appSource={json.dumps(app_source)};",
        )
        self.assertEqual(
            result["invalidated"],
            {"status": "idle", "token": None, "preview": None, "value": "CS1010X"},
        )
        self.assertEqual(result["previewed"]["status"], "preview_ready")
        self.assertEqual(result["previewed"]["token"], "opaque-preview-token")
        self.assertEqual(result["previewCalls"], 1)
        self.assertEqual(result["saveCalls"], 1)
        self.assertEqual(result["loadCalls"], 1, result)
        self.assertEqual(result["scanCalls"], 0)
        self.assertFalse(result["builder"]["open"])
        self.assertIsNone(result["builder"]["previewToken"])
        self.assertTrue(result["fresh"])
        self.assertEqual(result["drafts"], [[], {}, {}])
        self.assertEqual(result["workflow"]["primaryAction"]["action"], "check-again")
        self.assertEqual(result["notice"], "Standard saved.")


if __name__ == "__main__":
    unittest.main()
