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
TOKEN = "4d7b4e92-14c8-4bb9-8328-3d271f355574"
OPERATION_ID = "77249be2-06f0-41a4-a7c4-6c25b97a3ab1"


def _ready_payload() -> dict[str, object]:
    payload = _payload()
    review = payload["review"]
    review["status"] = "ready_to_move"
    review["decision_revision"] = 3
    review["recommendations"][0]["decision"] = "accept"
    review["choices"][0]["option_id"] = "destination-1"
    review["choices"][1]["option_id"] = "keep-all"
    review["summary"] = {
        "pending_recommendations": 0,
        "pending_choices": 0,
        "accepted_moves": 2,
        "kept_recommendations": 0,
        "duplicate_copies_for_future_bin": 0,
        "choices_remaining": 0,
        "ready_for_approval": True,
        "files_to_move": 2,
        "names_to_change": 0,
        "folders_to_create": 0,
    }
    review["approval"] = {
        "approved_at": "2026-07-15T12:05:00+00:00",
        "summary": {
            "files_to_move": 2,
            "duplicate_copies_for_future_bin": 0,
            "names_to_change": 0,
            "folders_to_create": 0,
            "choices_remaining": 0,
        },
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
    return payload


class LociFlowStageB4BMovementAdapterTests(unittest.TestCase):
    maxDiff = None

    def run_node(self, body: str) -> object:
        if not shutil.which("node"):
            self.skipTest("Node is unavailable")
        script = textwrap.dedent(
            f"""
            import assert from "node:assert/strict";
            import * as api from {json.dumps((LOCI / 'api.js').as_uri())};
            import {{
              createBackendDataSource, createFixtureDataSource,
              isUncertainMovementError, movementErrorMessage,
              validateMovementPreview, validateMovementResponse,
              validateOrganizationResponse,
            }} from {json.dumps((LOCI / 'data_source.js').as_uri())};
            import {{
              buildMovementConfirmationViewModel, buildProductionStageB1Contract,
              buildRouteViewModel,
            }} from {json.dumps((LOCI / 'view_models.js').as_uri())};
            import {{ renderRoute }} from {json.dumps((LOCI / 'views.js').as_uri())};

            const ready={json.dumps(_ready_payload())};
            const token={json.dumps(TOKEN)};
            const operationId={json.dumps(OPERATION_ID)};
            const clone=(value)=>JSON.parse(JSON.stringify(value));
            const preview={{
              files_to_move:2,
              duplicate_copies_deferred:1,
              confirmation:"MOVE APPROVED FILES",
              confirmation_token:token,
            }};
            const operation={{
              id:operationId,
              status:"verified",
              started_at:"2026-07-15T12:06:00+00:00",
              completed_at:"2026-07-15T12:06:01+00:00",
              files_expected:2,
              files_moved:2,
              files_failed:0,
              files_unattempted:0,
              verification_status:"passed",
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

    def test_api_uses_fixed_csrf_only_preview_and_token_only_execution_forms(self) -> None:
        result = self.run_node(
            """
            const requests=[];
            const fetcher=async(url,options)=>{
              requests.push({url,options});
              if(url.endsWith("/preview")) return {ok:true,status:200,json:async()=>clone(preview)};
              return {ok:true,status:200,json:async()=>({operation:clone(operation)})};
            };
            const prepared=await api.previewOrganizationMove(fetcher,"csrf-value");
            const moved=await api.moveApprovedOrganization({
              confirmationToken:prepared.confirmation_token,
              confirmation:"MOVE APPROVED FILES",
            },fetcher,"csrf-value");
            let altered=false;
            try {
              await api.moveApprovedOrganization({
                confirmationToken:token,
                confirmation:"MOVE APPROVED FILES ",
              },fetcher,"csrf-value");
            } catch(error) { altered=error.code==="invalid_confirmation"; }
            const serialized=requests.map(({url,options})=>({
              url,method:options.method,credentials:options.credentials,
              fields:[...options.body.entries()],
            }));
            console.log(JSON.stringify({prepared,moved,altered,serialized}));
            """
        )
        self.assertTrue(result["altered"])
        self.assertEqual(
            [request["url"] for request in result["serialized"]],
            ["/api/organization/move/preview", "/api/organization/move"],
        )
        self.assertEqual(
            result["serialized"][0]["fields"],
            [["csrf_token", "csrf-value"]],
        )
        self.assertEqual(
            dict(result["serialized"][1]["fields"]),
            {
                "csrf_token": "csrf-value",
                "confirmation_token": TOKEN,
                "confirmation": "MOVE APPROVED FILES",
            },
        )
        for request in result["serialized"]:
            self.assertEqual(request["method"], "POST")
            self.assertEqual(request["credentials"], "same-origin")
            fields = dict(request["fields"])
            for forbidden in (
                "path", "root", "source", "destination", "movement_tree",
                "plan_digest", "scan_generation", "decision_revision",
                "operation", "approval",
            ):
                self.assertNotIn(forbidden, fields)

    def test_api_and_data_source_normalize_errors_validate_contracts_and_serialize(self) -> None:
        result = self.run_node(
            """
            const calls=[];
            let release;
            const pending=new Promise((resolve)=>{release=resolve;});
            const client={
              getCurrentOrganization:async()=>clone(ready),
              previewOrganizationMove:async(csrf)=>{calls.push(["preview",csrf]);return clone(preview);},
              moveApprovedOrganization:async(payload,csrf)=>{
                calls.push(["move",payload,csrf]);
                await pending;
                return {operation:clone(operation)};
              },
              getActivity:async()=>({activity:[]}),
            };
            const source=createBackendDataSource({apiClient:client,csrfToken:"csrf"});
            await source.loadCurrentState();
            const prepared=await source.previewMovement();
            const first=source.confirmMovement(prepared.confirmation_token,prepared.confirmation);
            let duplicateBlocked=false;
            try { await source.confirmMovement(prepared.confirmation_token,prepared.confirmation); }
            catch(error) { duplicateBlocked=error.code==="movement_or_undo_in_progress"; }
            release();
            const moved=await first;
            const fixture=createFixtureDataSource();
            let fixtureBlocked=false;
            try { await fixture.previewMovement(); }
            catch(error) { fixtureBlocked=error.code==="movement_unavailable"; }
            let fixtureExecutionBlocked=false;
            try { await fixture.confirmMovement(token,"MOVE APPROVED FILES"); }
            catch(error) { fixtureExecutionBlocked=error.code==="movement_unavailable"; }
            const malformed=[];
            for(const candidate of [
              {...preview,confirmation:"move approved files"},
              {...preview,confirmation_token:"not-a-token"},
              {...preview,source:"Inbox/a.txt"},
            ]) {
              try { validateMovementPreview(candidate); malformed.push(false); }
              catch(error) { malformed.push(error.code==="invalid_movement_contract"); }
            }
            let normalized=false;
            const failure=async()=>({ok:false,status:409,json:async()=>({error:{code:"invalid_confirmation_token",message:"internal"}})});
            try { await api.previewOrganizationMove(failure,"csrf"); }
            catch(error) { normalized=error.code==="invalid_confirmation_token" && movementErrorMessage(error).includes("no longer"); }
            console.log(JSON.stringify({
              calls,prepared,moved,duplicateBlocked,fixtureBlocked,fixtureExecutionBlocked,
              malformed,normalized,
              uncertain:[
                isUncertainMovementError({code:"request_failed"}),
                isUncertainMovementError({code:"http_503"}),
                isUncertainMovementError({code:"invalid_confirmation_token"}),
              ],
            }));
            """
        )
        self.assertEqual([call[0] for call in result["calls"]], ["preview", "move"])
        self.assertEqual(result["calls"][1][1], {
            "confirmationToken": TOKEN,
            "confirmation": "MOVE APPROVED FILES",
        })
        self.assertTrue(result["duplicateBlocked"])
        self.assertTrue(result["fixtureBlocked"])
        self.assertTrue(result["fixtureExecutionBlocked"])
        self.assertTrue(all(result["malformed"]))
        self.assertTrue(result["normalized"])
        self.assertEqual(result["uncertain"], [True, True, False])
        self.assertEqual(result["moved"]["operation"]["status"], "verified")

    def test_view_models_cover_ready_confirmation_progress_results_and_stale_state(self) -> None:
        result = self.run_node(
            """
            const valid=validateOrganizationResponse(ready);
            const workflow=(execution,movement={})=>{
              const payload=clone(valid);
              payload.execution={...payload.execution,...execution};
              const contract=buildProductionStageB1Contract({
                snapshot:{status:"completed",backendState:payload},movement,
              });
              const source={getState:()=>({route:"organize",theme:"light",selectedItemId:null,stageB1:contract})};
              const organize=buildRouteViewModel(source,"organize");
              const home=buildRouteViewModel(source,"home");
              return {
                organize:organize.workflow,home:home.workflow,
                html:renderRoute(organize,"organize"),homeHtml:renderRoute(home,"home"),
              };
            };
            const readyState=workflow({});
            const loading=workflow({}, {previewStatus:"loading"});
            const pending=workflow({}, {executionPending:true});
            const verified=workflow({
              status:"verified",operation_id:operationId,files_moved:2,
              files_unattempted:0,verification_status:"passed",
            });
            const failed=workflow({
              status:"movement_failed",operation_id:operationId,files_moved:0,
              files_failed:1,files_unattempted:1,verification_status:"not_run",
            });
            const recovered=workflow({
              status:"moving",operation_id:operationId,files_unattempted:2,
              verification_status:"not_run",
            });
            const stale=workflow({}, {
              errorCode:"invalid_confirmation_token",
              errorMessage:"This move confirmation is no longer available.",
            });
            const previewFailure=workflow({}, {
              errorCode:"movement_or_undo_in_progress",
              errorMessage:"Another file action is running.",
            });
            const dialogEmpty=buildMovementConfirmationViewModel(preview,"");
            const dialogExact=buildMovementConfirmationViewModel(preview,"MOVE APPROVED FILES");
            console.log(JSON.stringify({
              readyState,loading,pending,verified,failed,recovered,stale,previewFailure,
              dialogEmpty,dialogExact,
            }));
            """
        )
        self.assertEqual(result["readyState"]["organize"]["stage"], "ready_to_move")
        self.assertEqual(result["readyState"]["organize"]["primaryAction"], {
            "label": "Move files", "action": "preview-move", "enabled": True,
        })
        self.assertEqual(result["readyState"]["home"]["primaryAction"]["label"], "Open Organize")
        self.assertIn('data-action="preview-move"', result["readyState"]["html"])
        self.assertNotIn("not connected", result["readyState"]["html"].lower())
        self.assertEqual(result["loading"]["organize"]["primaryAction"]["label"], "Preparing move…")
        self.assertEqual(result["pending"]["organize"]["stage"], "move_in_progress")
        self.assertEqual(result["verified"]["organize"]["stage"], "move_completed")
        self.assertIn("2 files moved.", result["verified"]["homeHtml"])
        self.assertNotIn("No files have moved yet.", result["verified"]["homeHtml"])
        self.assertEqual(result["failed"]["organize"]["status"], "Files did not move")
        self.assertEqual(result["recovered"]["organize"]["status"], "Move in progress")
        self.assertEqual(result["stale"]["organize"]["status"], "Organization changed")
        self.assertEqual(result["previewFailure"]["organize"]["status"], "Move could not be prepared")
        self.assertEqual(result["previewFailure"]["organize"]["primaryAction"]["action"], "preview-move")
        self.assertFalse(result["dialogEmpty"]["canConfirm"])
        self.assertTrue(result["dialogExact"]["canConfirm"])
        self.assertEqual(result["dialogExact"]["stage"], "confirmation_ready")
        self.assertIn("separate Duplicate Bin step", result["dialogExact"]["duplicateMessage"])
        serialized = json.dumps(result)
        for forbidden in (
            "authorization token", "plan digest", "decision revision",
            "scan generation", "durable operation", "executor", "replay",
        ):
            self.assertNotIn(forbidden, serialized.lower())

    def test_app_orchestration_orders_preview_confirmation_reload_and_activity(self) -> None:
        app = (LOCI / "app.js").read_text(encoding="utf-8")
        views = (LOCI / "views.js").read_text(encoding="utf-8")
        combined = app + views
        preview_move = app[app.index("async function previewMove"):app.index("async function confirmMove")]
        confirm_move = app[app.index("async function confirmMove"):app.index("async function checkFiles")]
        close_dialog = app[app.index("function closeDialog"):app.index("function resetMovementConfirmation")]
        reset_confirmation = app[
            app.index("function resetMovementConfirmation"):app.index("function discardMovementConfirmation")
        ]
        snapshot_refresh = app[app.index("function applySnapshot"):app.index("function syncShell")]
        self.assertIn('state.movement.previewStatus === "loading"', preview_move)
        self.assertIn("state.movement.executionPending", preview_move)
        self.assertIn("await dataSource.previewMovement()", preview_move)
        self.assertIn("!preview", confirm_move)
        self.assertIn("state.movement.confirmationText !== preview.confirmation", confirm_move)
        self.assertIn("state.movement.executionPending", confirm_move)
        self.assertIn("await dataSource.confirmMovement(", confirm_move)
        self.assertIn("applySnapshot(await dataSource.loadCurrentState())", confirm_move)
        self.assertIn("await loadActivity({ force: true, refresh: true })", confirm_move)
        self.assertIn("isUncertainMovementError", app)
        self.assertIn('state.activeDialog === "movement-confirmation"', app)
        self.assertIn("resetMovementConfirmation()", close_dialog)
        self.assertIn("state.movement.preview = null", reset_confirmation)
        self.assertIn('state.movement.confirmationText = ""', reset_confirmation)
        self.assertIn("previousPlanKey !== nextPlanKey", snapshot_refresh)
        self.assertIn("discardMovementConfirmation()", snapshot_refresh)
        self.assertEqual(app.count("dataSource.confirmMovement("), 1)
        self.assertNotIn("undo-move", views[views.index("function contractReadyToMove"):views.index("export function renderOrganize")])
        self.assertNotIn("localStorage", combined)
        self.assertNotIn("sessionStorage", combined)
        self.assertNotIn("Trash", combined)
        self.assertNotIn("safe to delete", combined.lower())


if __name__ == "__main__":
    unittest.main()
