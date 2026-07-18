# Architecture

## Pipeline

```text
Filesystem
  -> facts
     -> scanner.py
     -> duplicates.py
  -> supported CLI and report paths
     -> planner.py, review.py, scope.py, grouping.py
     -> advisory llm_refinement.py
     -> reports.py and html_report.py
     -> review_session.py and review_state.py
  -> scan application state
  -> Standards-first organization projection
     -> deterministic owner modules
     -> recommendations, choices, kept items, movement tree
  -> revision-bound decisions and approval
     -> immutable approved organization plan
  -> ordinary movement authorization
     -> durable organization operation repository
     -> shared execution coordinator
     -> executor.py
     -> verification
  -> Activity
     -> durable organization movement and Undo records
  -> ordinary Undo authorization
     -> shared execution coordinator
     -> executor.py
     -> post-Undo verification
  -> Duplicate Bin approval catalog
     -> all-member active claims
     -> movement authorization and durable operation records
     -> shared execution coordinator
     -> executor.py
     -> verification and protected listing
  -> unified Duplicate Bin restore
     -> Duplicate Bin page Restore or Activity Undo
     -> shared execution coordinator
     -> executor.py
     -> exact-path verification

Browser
  -> authenticated route
  -> application service
  -> deterministic owner modules
  -> executor.py only for explicitly authorized movement or restoration

Production frontend
  -> backend/API
  -> data source
  -> page-specific view model
  -> accepted renderer
```

## Module Ownership

| Module | Ownership |
| --- | --- |
| `models.py` | dataclasses shared across stages |
| `safety.py` | shared root-containment validation |
| `scanner.py` | filesystem metadata scanning |
| `duplicates.py` | SHA-256 hashing and exact duplicate grouping |
| `planner.py` | duplicate review planning |
| `review.py` | heuristic review candidate detection and review planning |
| `scope.py` | deterministic organization-scope and orphan-code classification helpers |
| `organization_rules.py` | organization rules loading, validation, and serialization support |
| `grouping.py` | deterministic project grouping and organization suggestions |
| `pattern_inference.py` | report-only inference of existing folder organization patterns |
| `rule_review.py` | organization rule candidate export, reviewed-decision validation, and confirmed config updates |
| `rule_audit.py` | read-only audit of organization-rule effects in reports |
| `organization_review.py` | export and validation of rule-aware organization review JSON; never applies rows |
| `organization_apply_review.py` | approved organization-review conversion, executor orchestration, and apply-result summaries |
| `organization_verify.py` | read-only comparison of apply summaries, operation logs, and filesystem state |
| `llm_refinement.py` | advisory LLM prompt, payload, validation, refined suggestions |
| `ollama_client.py` | local Ollama client only |
| `reports.py` | read-only report assembly, JSON report writing, and authoritative scan-report loading/validation |
| `html_report.py` | read-only static HTML rendering from report dictionaries |
| `review_session.py` | batch review-session construction, decisions, reviewed-plan JSON writing, and saved-plan validation |
| `review_state.py` | persistent human review decision memory |
| `application/scan_service.py` | UI-independent scan/report coordination and summary projection |
| `application/review_service.py` | immutable review-session coordination through existing review owners |
| `application/artifact_service.py` | root-contained listing/loading of scan reports and reviewed plans only |
| `application/view_models.py` | frozen, interface-independent application result types |
| Loci Flow organization projection and decision services | Standards-first consumer projection, temporary revisions, exact-set decisions, validation, and approval |
| Loci Flow organization operation repositories and services | durable ordinary movement authorization, intent, replay, verification, and recovery projection |
| `application/activity_service.py` | Activity projection from durable movement, Undo, Duplicate Bin movement, and restore records |
| Loci Flow ordinary Undo repositories and services | authorization, durable restore intent, executor delegation, and verification |
| `application/execution_coordinator.py` | one shared process-wide movement or restore operation at a time |
| `application/duplicate_bin_repository.py` | durable approved batches, all-member claims, lifecycle, restart reconciliation, and protected catalog |
| Duplicate Bin movement authorization and operation repositories | one-use authorization, durable intent, replay, and verified movement state |
| `application/duplicate_bin_restore_service.py` | sole application owner for Duplicate Bin page Restore and Duplicate Bin Activity Undo |
| `web/config.py` | frozen per-launch root and ephemeral security configuration |
| `web/server.py` | IPv4-loopback socket binding, browser bootstrap, and single-worker Uvicorn lifecycle |
| `web/app.py` | isolated FastAPI construction, middleware, templates, static mounting, and generic errors |
| `web/security.py` | one-time launch gate, session/CSRF helpers, exact Origin validation, and security headers |
| `web/routes/home.py` | health, single-use launch bootstrap, and authenticated welcome routes only |
| Loci Flow authenticated routes | strict no-store GET projections and authenticated CSRF/same-origin POST mutations using server-owned IDs |
| Loci Flow data source and page view models | validate production contracts and adapt backend state to the accepted renderer without owning backend policy |
| `executor.py` | movement-specific source/destination/symlink validation, approved move execution, operation logs, and undo |
| `cli.py` | substantial current command-line and use-case orchestration |

## Current Loci Flow Web Direction

Loci Flow through Integration 1.0E is bootAI's primary consumer interface. Its
six areas are Home, Organize, Duplicate Bin, Activity, Standards, and Settings.
Home summarizes real scope and verified work; Organize owns recommendations,
choices, approval, and ordinary movement. Activity contains only real
filesystem operations and their outcomes. Standards filter forbidden changes
before the remaining plan is displayed, but writable Standards are not yet an
implemented product capability.

The required and implemented dependency direction is:

```text
Local browser
    ↓
Web interface layer
    ↓
Application-service layer
    ↓
Existing deterministic bootAI modules
    ↓
executor.py for explicitly approved movement and undo only
```

The web layer does not reproduce scanner, duplicate, grouping, review, safety,
movement, restore, or Undo logic. Route modules never import `executor.py`.
Authenticated routes accept stable server-owned IDs, exact confirmations, scan
generations, and revisions rather than source, destination, root, catalog, or
operation-log paths. Application services revalidate approved intent and may
delegate explicit movement or restoration to `executor.py`; `executor.py`
remains the sole user-file movement implementation.

Organization decisions are temporary, generation-bound, and revision-bound.
Approvals, movement authorizations, and restore authorizations are server-owned.
Durable operation intent is written before movement, then verification and
replay-safe reconciliation project the authoritative outcome to the browser.

Consumer presenters group rows by a safety-validated normalized source key and choose one primary card using duplicate → organization → attention precedence. Secondary rows remain visible inside that card and retain independent stable IDs, decisions, conflicts, and serialization. Consumer actions mutate only the primary row through the application service. Advanced review continues to expose all rows. No consumer presenter owns persistence, grouping, duplicate detection, or movement.

Module saves select every authoritative row for one fixed category and reuse `review_session.py` serialization. `saved_decisions` remains the full per-row baseline; a module save advances only its rows, while full save advances all rows. Temporary handled/deferred queue IDs live only in the locked web holder and are invalidated by a new generation.

Each web-server process is bound to one validated immutable root. Browser requests submit stable session-scoped IDs and allowed actions rather than source or destination paths. Existing JSON artifacts remain authoritative; temporary UI, job, security, and revision state may remain in memory, with no initial database.

The current process binds an already-listening IPv4 socket to `127.0.0.1`, normally on an operating-system-selected port, and hands it to one Uvicorn worker with proxy headers and access logging disabled. A one-time launch token establishes a signed browser-lifetime session and immediately redirects away from the token URL. All frontend resources are installed package data; no runtime asset leaves the process origin.

The complete contract is in [WEB_ARCHITECTURE](WEB_ARCHITECTURE.md), and mandatory localhost security controls are in [WEB_THREAT_MODEL](WEB_THREAT_MODEL.md).

The current deterministic behavior requires no LLM. `llm_refinement.py` remains
advisory only: LLM output cannot authorize, approve, or execute movement.

## Facts, Suggestions, Approved Moves, Execution, Undo

Facts come from deterministic Python: paths, sizes, hashes, extensions, and inferred deterministic groups. `scanner.py`, `duplicates.py`, `scope.py`, `review.py`, and `grouping.py` produce facts or suggestions.

Suggestions are represented as `MovePlanItem` objects and printed as dry-run plans. Normal organization suggestions are conservative and document-like by default. `scope.py` excludes protected/project/package/application internals, generated web/archive assets, and contextual project-output files from actionable plans. `organization_rules.py` loads `AI_Review/config/organization_rules.json` for grouping and report decisions. `grouping.py` resolves aliases before final anchor decisions, reports broad course/name/project/organization anchors as preference-dependent by default, and creates concrete organization suggestions only for narrow repeated document sets or locked anchors. Exact duplicate groups remain factual; duplicate review plans are stricter actionable candidates. `llm_refinement.py` produces advisory suggestions only and stores them separately from deterministic `ProjectGroup` data.

Reports serialize facts and suggestions into JSON for manual review or external scheduler runs. `pattern_inference.py` enriches reports with weak evidence from existing user folders, such as course-code foldering or person/student foldering. This evidence can rank `Needs decision` anchors and suggest manual rule candidates, but it does not write `organization_rules.json`, create `MovePlanItem` values directly, or approve broad organization. `reports.py` may write a new report file under the scan root, but it does not execute moves or approve actions. `html_report.py` renders the same report dictionary into a static HTML viewer and may write an HTML report file under the scan root. HTML reports do not approve moves, apply moves, perform review actions, write operation logs, or start a server.

Rule review is a configuration workflow, not a movement workflow. `rule_review.py` exports inferred rule candidates to manually editable JSON, validates reviewed decisions as untrusted input, and writes `organization_rules.json` only after exact `APPLY ORGANIZATION RULES` confirmation through the CLI. It does not create `MovePlanItem` values, import `executor.py`, write operation logs, or move files. Rule apply result files are configuration-update audit records only.

Rule-aware audit is report-only. `rule_audit.py` compares conservative defaults with loaded explicit organization rules in memory, reports per-rule effects and broad-impact warnings, and does not create movement-plan items, write rules, import `executor.py`, or move files.

Organization-review export is also report-derived and read-only. `organization_review.py` consumes serialized organization suggestions, anchor decisions, organization rules, and rule-audit context already assembled by `reports.py`. It writes editable review records, not `MovePlanItem` values or execution-ready plans. Stage 10.8 has no path from an approved review row to filesystem movement.

Organization-review apply is a separate confirmed workflow. `organization_apply_review.py` treats the reviewed JSON as user-approved intent, converts only approved rows to `MovePlanItem` values, checks normalized source/destination conflicts, and delegates all preflight and movement to `executor.py`. Its apply-result JSON is a user-readable summary; the executor operation log remains authoritative for successful moves and undo.

Post-apply verification is separate from execution and undo. `organization_verify.py` treats both JSON artifacts as untrusted input, normalizes their paths under the scan root, compares successful source/destination pairs, and checks current source and destination state. It writes an audit report only and does not import or call executor movement functions.

Batch review sessions collect duplicate, deterministic organization, and review-candidate `MovePlanItem` values for command-line review. Review-candidate rows keep `category = "review_candidate"` separate from `review_category` metadata such as `empty`, `temporary`, or `backup_or_copy`. Approve/reject decisions and reviewed-plan JSON records do not execute moves. Approved rows are checked for source and destination conflicts before apply. Saved reviewed-plan JSON is treated as untrusted input when loaded later; `review_session.py` validates it, rejects approved move conflicts, and converts only approved records back into `MovePlanItem` values. Final apply still uses `executor.py`.

Saved batch review sessions can also be resumed through `review_session.py`. Resume reconstructs existing rows and decisions from validated reviewed-plan JSON without scanning or regenerating candidates. Review state is not applied to resumed rows. Saving writes a collision-safe sibling in the same reviewed-plan format; any later apply still uses the existing validator, exact confirmation, conflict checks, and executor path.

Review view state is an in-memory projection over all session rows. `review_session.py` owns deterministic filters, one stable sort key, and pagination. The CLI displays the resulting page, while decisions, conflict checks, saving, and apply continue to use stable IDs and the complete session row list. View state is never serialized.

Bulk page decisions reuse that projection and the existing single-ID decision helpers. A preview freezes the exact stable IDs displayed on the current page, separates changed and idempotent rows, and requires a decision-specific confirmation before mutating in-memory decisions. It does not save or enter the executor path.

Review-session dirty state is a session-local boolean derived only from actual decision changes. It starts clean for generated and resumed sessions, clears after a successful save, and requires exact `QUIT WITHOUT SAVING` confirmation before unsaved decisions are discarded. It is presentation/session state only: it is not serialized and does not alter review-state memory, conflict detection, apply confirmation, or executor behavior.

Review state is separate from reviewed-plan JSON and operation logs. `review_state.py` stores human review decision memory under `AI_Review/review_state/review_decisions.json`, matches remembered decisions back to current rows by source, destination, category, review category, size, and modified time, and flags stale prior decisions when metadata changes. Review state records intent only. It is not an operation log, does not record filesystem success, and is not used for undo.

Approved moves are explicit `MovePlanItem` values accepted by a user-facing flow. Execution is isolated in `executor.py`, which validates and applies approved duplicate, organization, and review-candidate moves only. Undo is driven by operation logs written by `executor.py`.

Planner, review, grouping, and LLM modules do not execute actions. `executor.py` does not decide what should move; it only validates and applies explicit `MovePlanItem` objects. `cli.py` orchestrates user-facing flow.

Stage 8 organization apply reuses `executor.py` for approved organization moves. It does not create another movement subsystem.
