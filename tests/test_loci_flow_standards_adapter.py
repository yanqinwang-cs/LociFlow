from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from tests.test_loci_flow_stage_b2_adapter import _payload

ROOT = Path(__file__).resolve().parents[1]
LOCI = ROOT / "src" / "organizer" / "web" / "static" / "loci_flow"


def _standards() -> dict[str, object]:
    return {
        "revision": 7,
        "presets": [
            {
                "id": "keep_folder_unchanged",
                "name": "Keep a folder unchanged",
                "description": "Files in this folder stay where they are.",
            },
            {
                "id": "keep_matching_files_together",
                "name": "Keep matching files together",
                "description": "Files with one course or project code stay grouped.",
            },
        ],
        "creation_available": True,
        "creation_message": None,
        "items": [
            {
                "id": "builtin-protect-project-folders",
                "name": "Protect project folders",
                "description": "Project files stay together.",
                "active": True,
                "mutable": False,
                "scope_label": "Protected project folders",
                "effect_summary": "Project files are excluded from suggestions.",
                "preset_type": "built_in_protection",
                "attention_message": None,
            },
            {
                "id": "e9b608f3-0787-4fa7-a067-6448b2c47193",
                "name": "Keep matching EvoSim files together",
                "description": "Matching EvoSim files may be grouped together.",
                "active": True,
                "mutable": True,
                "scope_label": "Selected folder",
                "effect_summary": "The matcher shapes grouping before suggestions are created.",
                "preset_type": "keep_matching_files_together",
                "attention_message": "A fresh check is recommended.",
            },
            {
                "id": "f053678e-90e2-473b-82b7-34dcb859f9ce",
                "name": "Keep Archive unchanged",
                "description": "Files in this folder stay where they are.",
                "active": False,
                "mutable": True,
                "scope_label": "Archive",
                "effect_summary": "Matching files are filtered before choices are created.",
                "preset_type": "keep_folder_unchanged",
                "attention_message": None,
            },
        ],
    }


class LociFlowStandardsAdapterTests(unittest.TestCase):
    maxDiff = None

    def run_node(self, body: str, imports: str = "") -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            import assert from "node:assert/strict";
            {imports}
            const payload={json.dumps(_payload())};
            const standardsPayload={json.dumps(_standards())};
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

    def test_api_uses_exact_safe_request_contract_and_normalized_errors(self) -> None:
        result = self.run_node(
            """
            const calls=[];
            const fetchImpl=async(url,options)=>{
              calls.push({url,method:options.method,credentials:options.credentials,
                accept:options.headers.Accept,contentType:options.headers["Content-Type"]||null,
                fields:options.body ? Object.fromEntries(options.body.entries()) : null});
              if(url.endsWith("/state")) return {ok:false,status:409,json:async()=>({error:{code:"standard_revision_mismatch",message:"Standards changed. Reload the latest list."}})};
              return {ok:true,status:200,json:async()=>clone(standardsPayload)};
            };
            const listed=await getStandards(fetchImpl);
            let error=null;
            try{
              await setStandardActive({standardId:standardsPayload.items[1].id,active:false,expectedRevision:7},fetchImpl,"csrf");
            }catch(value){error={name:value.name,code:value.code,message:value.message,status:value.status};}
            console.log(JSON.stringify({listed,calls,error}));
            """,
            f'import {{ getStandards,setStandardActive }} from {json.dumps((LOCI / "api.js").as_uri())};',
        )
        self.assertEqual(result["listed"], _standards())
        self.assertEqual(result["calls"][0]["url"], "/api/standards")
        self.assertEqual(result["calls"][0]["method"], "GET")
        self.assertEqual(result["calls"][1]["url"], "/api/standards/state")
        self.assertEqual(
            result["calls"][1]["fields"],
            {
                "csrf_token": "csrf",
                "expected_revision": "7",
                "standard_id": _standards()["items"][1]["id"],
                "active": "false",
            },
        )
        self.assertTrue(all(call["credentials"] == "same-origin" for call in result["calls"]))
        self.assertTrue(all(call["accept"] == "application/json" for call in result["calls"]))
        self.assertEqual(result["error"]["code"], "standard_revision_mismatch")
        self.assertIn("Reload the latest list", result["error"]["message"])
        serialized = json.dumps(result)
        for forbidden in ("relative_path", "matcher_value", "parameters", "configured_root"):
            self.assertNotIn(forbidden, serialized)

    def test_data_source_validates_projection_and_blocks_double_mutation(self) -> None:
        result = self.run_node(
            """
            let resolveMutation;
            const held=new Promise((resolve)=>{resolveMutation=resolve;});
            const calls=[];
            const apiClient={
              getCurrentOrganization:async()=>clone(payload),
              getStandards:async()=>clone(standardsPayload),
              setStandardActive:async(value,csrf)=>{calls.push([value,csrf]);return held;},
            };
            const source=createBackendDataSource({apiClient,csrfToken:"csrf"});
            const listed=await source.loadStandards();
            const first=source.setStandardActive(standardsPayload.items[1].id,false,7);
            let secondError=null;
            try{await source.setStandardActive(standardsPayload.items[1].id,false,7);}catch(error){secondError=error.code;}
            const updated=clone(standardsPayload); updated.revision=8; updated.items[1].active=false;
            resolveMutation({revision:updated.revision,items:updated.items,fresh_scan_required:true});
            const result=await first;
            const malformed=clone(standardsPayload); malformed.items[0].relative_path="private/file.txt";
            let unsafeRejected=false;
            try{validateStandardsResponse(malformed);}catch{unsafeRejected=true;}
            console.log(JSON.stringify({listed,calls,secondError,result,pending:source.isStandardsMutationPending(),unsafeRejected}));
            """,
            f'import {{ createBackendDataSource,validateStandardsResponse }} from {json.dumps((LOCI / "data_source.js").as_uri())};',
        )
        self.assertEqual(result["listed"], _standards())
        self.assertEqual(len(result["calls"]), 1)
        self.assertEqual(
            result["calls"][0][0],
            {
                "standardId": _standards()["items"][1]["id"],
                "active": False,
                "expectedRevision": 7,
            },
        )
        self.assertEqual(result["secondError"], "standards_mutation_in_progress")
        self.assertTrue(result["result"]["fresh_scan_required"])
        self.assertFalse(result["pending"])
        self.assertTrue(result["unsafeRejected"])

    def test_view_model_and_renderer_cover_loading_ready_error_and_safe_controls(self) -> None:
        result = self.run_node(
            """
            const backend=validateOrganizationResponse(clone(payload));
            const project=(standards)=>{
              const contract=buildProductionStageB1Contract({snapshot:{status:"completed",backendState:backend},standards});
              const source={getState:()=>({route:"standards",theme:"light",selectedItemId:null,stageB1:contract})};
              const model=buildStandardsViewModel(source);
              return {model,html:renderRoute(model,"standards")};
            };
            const loading=project({loadStatus:"loading",revision:null,items:[],presets:[],scopeChoices:[],creationAvailable:false});
            const ready=project({loadStatus:"ready",revision:7,items:clone(standardsPayload.items),presets:clone(standardsPayload.presets),scopeChoices:[],creationAvailable:true,mutationPending:false,freshScanRequired:false});
            const pending=project({loadStatus:"ready",revision:7,items:clone(standardsPayload.items),presets:clone(standardsPayload.presets),scopeChoices:[],creationAvailable:true,mutationPending:true,pendingStandardId:standardsPayload.items[1].id,freshScanRequired:true});
            const error=project({loadStatus:"error",revision:null,items:[],presets:[],scopeChoices:[],creationAvailable:false,errorMessage:"Standards could not be loaded."});
            console.log(JSON.stringify({loading:loading.html,ready:ready.html,pending:pending.html,error:error.html,readyModel:ready.model.standards,pendingOrganization:pending.model.organization}));
            """,
            "\n".join(
                [
                    f'import {{ validateOrganizationResponse }} from {json.dumps((LOCI / "data_source.js").as_uri())};',
                    f'import {{ buildProductionStageB1Contract,buildStandardsViewModel }} from {json.dumps((LOCI / "view_models.js").as_uri())};',
                    f'import {{ renderRoute }} from {json.dumps((LOCI / "views.js").as_uri())};',
                ]
            ),
        )
        self.assertIn("Loading Standards", result["loading"])
        self.assertIn("Standards could not be loaded", result["error"])
        self.assertIn("Protect project folders", result["ready"])
        self.assertIn("Keep matching EvoSim files together", result["ready"])
        self.assertIn(">On<", result["ready"])
        self.assertIn(">Paused<", result["ready"])
        self.assertEqual(result["ready"].count('data-action="set-standard-active"'), 2)
        built_in = result["ready"].split("Protect project folders", 1)[1].split("</article>", 1)[0]
        self.assertNotIn("Pause", built_in)
        self.assertIn("Check files again", result["pending"])
        self.assertIn('aria-busy="true"', result["pending"])
        self.assertIn(">Pause<", result["pending"])
        self.assertEqual(result["pendingOrganization"]["recommendations"], [])
        self.assertEqual(result["pendingOrganization"]["choicesNeeded"], [])
        self.assertEqual(result["readyModel"]["revision"], 7)
        for forbidden in (
            "Edit",
            "Delete",
            "Interpret preference",
            "matcher_value",
            "relative_path",
            "keep_matching_files_together",
            "builtin-protect-project-folders",
        ):
            self.assertNotIn(forbidden, result["ready"])
        self.assertNotIn('data-action="archive', result["ready"])
        self.assertIn('data-action="open-standard-builder"', result["ready"])
        self.assertEqual(result["ready"].count("data-standard-scope"), 3)
        self.assertEqual(result["ready"].count("data-standard-effect"), 3)
        self.assertNotIn("foldersProject files", result["ready"])
        self.assertNotIn("folderMatching files", result["ready"])
        self.assertIn("Always on", built_in)
        visible_text = re.sub(r"<[^>]+>", " ", result["ready"])
        self.assertNotIn(str(_standards()["revision"]), visible_text)
        for item in _standards()["items"]:
            self.assertNotIn(item["id"], visible_text)

    def test_app_coalesces_loads_and_mutation_reloads_without_retry_or_scan(self) -> None:
        app_source = (LOCI / "app.js").read_text(encoding="utf-8")
        result = self.run_node(
            """
            const start=appSource.indexOf("async function loadStandards");
            const end=appSource.indexOf("async function loadActivity",start);
            if(start<0||end<0) throw new Error("Standards orchestration source not found");
            const functions=appSource.slice(start,end);
            const baseState=()=>({
              route:"standards",selectedItemId:"old",selectedRowIds:["old"],
              destinationDrafts:{old:"x"},duplicateDrafts:{old:"x"},workflowOverride:null,
              standards:{loadStatus:"idle",revision:null,items:[],mutationPending:false,
                pendingStandardId:null,errorMessage:null,conflictMessage:null,
                freshScanRequired:false,loadVersion:0,builder:{open:false}},
            });
            const events=[];
            let loads=0;
            let mutations=0;
            let scans=0;
            let authoritativeActive=true;
            let release;
            const firstLoad=new Promise((resolve)=>{release=resolve;});
            const state=baseState();
            const dataSource={
              loadStandards:()=>{loads+=1;return loads===1?firstLoad:Promise.resolve({...clone(standardsPayload),items:clone(standardsPayload.items).map((item,index)=>index===1?{...item,active:authoritativeActive}:item)});},
              setStandardActive:async(id,active,revision)=>{mutations+=1;authoritativeActive=active;events.push(["mutation",id,active,revision]);return {fresh_scan_required:true};},
              startScan:async()=>{scans+=1;},
            };
            const make=new Function("state","dataSource","render","syncShell","standardsErrorMessage","announce","discardMovementConfirmation","discardDuplicateMovementConfirmation","workflowOverride","discardStandardBuilder","openDialog","refreshDialog","setDialog","elements",`let standardsLoadPromise=null;${functions};return {loadStandards,setStandardActive};`);
            const api=make(state,dataSource,()=>events.push(["render"]),()=>events.push(["sync"]),(error)=>error.message||error.code,(message)=>events.push(["announce",message]),()=>events.push(["discard-move"]),()=>events.push(["discard-bin"]),(status,message,action,label,staleKind)=>({status,message,primaryAction:{action,label},staleKind}),()=>{},()=>{},()=>{},()=>{},{dialog:{open:false}});
            const first=api.loadStandards();
            const second=api.loadStandards();
            const during={loads,status:state.standards.loadStatus};
            release(clone(standardsPayload));
            await Promise.all([first,second]);
            const target={dataset:{standardId:standardsPayload.items[1].id,active:"false"}};
            const mutation=api.setStandardActive(target);
            const duplicate=api.setStandardActive(target);
            await Promise.all([mutation,duplicate]);
            const afterPause={mutations,loads,active:state.standards.items[1].active};
            await api.setStandardActive({dataset:{standardId:standardsPayload.items[1].id,active:"true"}});
            const success={loads,mutations,scans,state:clone(state),events:clone(events)};

            const conflictState=baseState(); conflictState.standards={...conflictState.standards,loadStatus:"ready",revision:7,items:clone(standardsPayload.items)};
            let conflictMutations=0; let conflictLoads=0;
            const conflictSource={
              loadStandards:async()=>{conflictLoads+=1;return clone(standardsPayload);},
              setStandardActive:async()=>{conflictMutations+=1;const error=new Error("Standards changed. Reload the latest list.");error.code="standard_revision_mismatch";throw error;},
            };
            const conflictApi=make(conflictState,conflictSource,()=>{},()=>{},(error)=>error.message,()=>{},()=>{},()=>{},()=>({}),()=>{},()=>{},()=>{},()=>{},{dialog:{open:false}});
            await conflictApi.setStandardActive(target);
            console.log(JSON.stringify({during,afterPause,success,conflict:{mutations:conflictMutations,loads:conflictLoads,state:conflictState}}));
            """,
            f"const appSource={json.dumps(app_source)};",
        )
        self.assertEqual(result["during"], {"loads": 1, "status": "loading"})
        self.assertEqual(result["afterPause"], {"mutations": 1, "loads": 2, "active": False})
        self.assertEqual(result["success"]["mutations"], 2)
        self.assertEqual(result["success"]["loads"], 3)
        self.assertEqual(result["success"]["scans"], 0)
        self.assertTrue(result["success"]["state"]["standards"]["items"][1]["active"])
        mutation_events = [event for event in result["success"]["events"] if event[0] == "mutation"]
        self.assertEqual([event[2] for event in mutation_events], [False, True])
        self.assertTrue(all(event[3] == 7 for event in mutation_events))
        self.assertFalse(result["success"]["state"]["standards"]["mutationPending"])
        self.assertTrue(result["success"]["state"]["standards"]["freshScanRequired"])
        self.assertEqual(result["success"]["state"]["selectedRowIds"], [])
        self.assertEqual(result["success"]["state"]["destinationDrafts"], {})
        self.assertEqual(result["success"]["state"]["workflowOverride"]["primaryAction"]["action"], "check-again")
        self.assertEqual(result["conflict"]["mutations"], 1)
        self.assertEqual(result["conflict"]["loads"], 1)
        self.assertIn("Reload the latest list", result["conflict"]["state"]["standards"]["conflictMessage"])

    def test_fixture_isolation_and_protected_prototype(self) -> None:
        result = self.run_node(
            """
            const fixture=createFixtureDataSource();
            let loadError=null;let mutationError=null;
            try{await fixture.loadStandards();}catch(error){loadError=error.code;}
            try{await fixture.setStandardActive("x",false,0);}catch(error){mutationError=error.code;}
            console.log(JSON.stringify({kind:fixture.kind,loadError,mutationError}));
            """,
            f'import {{ createFixtureDataSource }} from {json.dumps((LOCI / "data_source.js").as_uri())};',
        )
        self.assertEqual(result, {
            "kind": "fixture",
            "loadError": "standards_unavailable",
            "mutationError": "standards_unavailable",
        })
        production = "\n".join(
            (LOCI / name).read_text(encoding="utf-8")
            for name in ("app.js", "views.js", "view_models.js")
        )
        self.assertNotIn("create Standard", production)
        self.assertNotIn("confirm-suggested-standards", (LOCI / "app.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
