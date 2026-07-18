# Local Web Architecture Contract

Status: accepted in Stage 11.0; current production implementation completed
through Loci Flow Integration 1.0F.

This document freezes the architecture that all Stage 11 web work must follow. Stages 11.2 through 11.5 establish the secure scan/review/save workflow; Stage 11.5.1 presents it through consumer responsibilities while retaining Advanced review.

The companion [web threat model](WEB_THREAT_MODEL.md) defines the mandatory security controls. The architecture decisions are also recorded in [ADR 0001](adr/0001-local-web-stack.md), [ADR 0002](adr/0002-web-security-boundary.md), and [ADR 0003](adr/0003-application-service-layer.md).

## Current Production State Through Loci Flow 1.0F

The root-locked local application binds to `127.0.0.1`, establishes an
authenticated browser session through a single-use launch URL, and renders six
primary areas: Home, Organize, Duplicate Bin, Activity, Standards, and Settings.
Home and Organize remain separate. Home summarizes real scope, freshness, and
verified work. Organize owns the current scan, Standards-filtered
recommendations, destination and duplicate choices, approval, movement tree,
and ordinary movement. Activity shows only real filesystem movement, restore,
failure, and Undo records. Standards load fail-closed, are bound immutably to the
scan that consumed them, and filter forbidden changes before the remaining plan
is displayed. The authoritative Standards page lists the durable v2 catalog,
supports activate/deactivate, and provides guided deterministic creation.

The production frontend follows backend/API → data source → page-specific view
model → accepted renderer. Fixture scenarios validate the accepted renderer but
are never a production fallback. Runtime shell labels, dialogs, navigation, and
operation status are synchronized from the production view model.

Scanning, decisions, approval, movement preview, ordinary movement, Activity,
Undo, Duplicate Bin movement, and restore are connected. Every mutation is an
authenticated POST protected by session-bound CSRF, exact same-origin and Host
validation, immutable root configuration, strict form fields, scan generation,
and the applicable decision revision or one-use authorization. The browser
sends opaque server-owned IDs and exact confirmations, never filesystem,
catalog, digest, or operation-log paths.

The authenticated route surface has narrow responsibilities: scan mutation and
current organization state; decision and approval mutations; ordinary movement
preview and execution; Activity projection and operation-bound Undo preview and
execution; and Duplicate Bin projection, batch movement preview/execution, and
selected restore preview/execution; plus authoritative Standards listing,
preview, activation, deactivation, and creation. GET projections are no-store
and read-only. Each state-changing route accepts only its fixed strict form and
delegates to an application service.

Durable intent precedes every user-file movement. Routes call application
services and never import `executor.py`; application services revalidate intent
and delegate movement or restoration to `executor.py`, the sole user-file
movement implementation. The shared execution coordinator permits one movement
or restore at a time. Durable records, verification, replay, and restart
reconciliation make lost responses recoverable without automatic repeat
movement.

Duplicate Bin is reversible scanner-pruned protected storage, not Trash.
Approved batches claim all selected and retained group members and rehash the
full group before separately confirmed movement. The page lists physically
moved copies. Duplicate Bin Restore and Activity Undo use the same restore
service, restore selected copies to exact previous paths, reject occupied
destinations, verify outcomes, and preserve durable history.

Standards persistence is a configuration boundary, not a movement boundary. A
durable revision-protected v2 catalog is authoritative; valid v1 storage is
migrated only on the first mutation. One scan performs one fail-closed load and
carries that immutable state through grouping, report, organization projection,
review, decisions, and approval. Repairing or changing storage does not alter an
existing scan; a fresh Check is required.

Standards mutations validate the expected catalog revision, reject conflicts,
and block while an approved organization plan is active. Guided creation is
limited to **Keep a folder unchanged** and **Keep matching files together** with
a course code or project code. Preview and mutation use server-owned IDs and
safe labels, effects, and match counts. They create no Activity records, perform
no filesystem movement, and expose no raw JSON, filesystem paths, regex, or LLM
dependency to the browser.

## Loci Flow Integration 1.0C — Historical Checkpoint

Loci Flow temporary organization decisions are held server-side, in process, and
are keyed by the authenticated opaque session ID and completed scan generation.
They are never stored in the signed cookie or persisted to disk. A process restart
therefore discards both decisions and any Ready-to-move state.

The deterministic organization projection produces its unchanged consumer JSON and
an immutable server-only candidate map in the same pass. Recommendation, choice,
option, and duplicate-copy IDs resolve only through that map. Destination paths and
duplicate hashes are never accepted from or exposed to the browser. Bulk acceptance
submits the exact currently pending recommendation IDs rather than an open-ended
accept-all flag.

Each successful decision request advances one decision revision. Every mutation and
approval supplies the scan generation and expected revision, preventing stale tabs or
old ordinal IDs from silently changing a newer plan. GET restores in-process decisions
and an approved Ready state for the authenticated session.

Final approval is an executor-free application operation. It confirms decision
completeness and performs live source metadata, root, symlink, destination overwrite,
protection, move-conflict, and duplicate-identity checks. The resulting immutable
approved plan remains in memory and exposes only a safe summary. Integration 1.0C
stops at **Ready to move — no files have moved yet**. Movement, Activity, Duplicate
Bin execution, operation logs, verification, and Undo remain unimplemented here;
`executor.py` is not imported by the Loci application service, store, or routes.

## Loci Flow Integration 1.0D1 — Historical Checkpoint

Integration 1.0D1 adds a backend-only movement and verification boundary. The
browser remains deliberately disconnected: no movement route exists, the disabled
**Move files — not connected yet** control is unchanged, and Activity and Undo are
deferred to 1.0D2. Duplicate Bin execution remains deferred to 1.0E.

The immutable `ApprovedOrganizationPlan` is the sole movement authorization source.
An application-owned process-wide coordinator issues an opaque, generation- and
revision-bound one-use authorization and permits only one movement or future restore
operation at a time. Unused authorizations and unexecuted approvals remain in memory
and are invalid after process restart.

Before movement, the execution service repeats source fingerprint, protection,
root, symlink, destination, overwrite, and batch-conflict checks. It then writes a
versioned operation intent under `AI_Review/organization_operations/`. The artifact
contains only root-relative frozen moves and is updated atomically with a same-folder
temporary file, file flush and `fsync`, `os.replace`, and a best-effort directory
`fsync`.

`executor.py` remains the only filesystem movement owner. Its compatible optional
recorder persists `attempting` before each move, then `moved` or `failed` immediately
after the attempt, while retaining the legacy raw operation log. The application
aggregates `moved_unverified`, `verified`, `partial_movement`, `movement_failed`,
`verification_failed`, or `interrupted` and automatically delegates read-only
post-move checks to `organization_verify.py`.

Durable nonterminal or interrupted records survive restart and block a new movement.
Recovery inspection is read-only: it classifies an attempting item from source and
destination presence but never resumes or reverses movement. Activity rendering,
undo authorization, undo verification, and consumer projections remain owned by
1.0D2.

## Loci Flow Integration 1.0D2A — Historical Checkpoint

Integration 1.0D2A projects consumer Activity directly from typed durable movement
records and separately linked typed Undo records. It creates no Activity database or
browser history copy. Completed, partial, failed, verification-failed, and interrupted
records therefore survive process restart and are ordered by parsed completion, start,
or creation time rather than UUID filenames.

Only the latest meaningful, fully verified movement can expose normal Undo. A linked
Undo attempt permanently removes that eligibility in this stage, and later successful,
partial, ambiguous, or nonterminal movement blocks historical Undo. Live eligibility
requires every moved destination to remain a regular non-symlink file under the fixed
root, every original location to remain absent and safe, and the current size and
modified time to match the fingerprint D1 verified after movement. A same-size edit
with an intentionally restored timestamp is outside this initial detection boundary.

Undo preview creates a one-use in-memory authorization bound to the authenticated
browser session, exact operation UUID, and configured-root identity. Movement and Undo
share the same process-wide coordinator lock. The application service writes a typed
prepared record under `AI_Review/organization_undos/`, validates the server-resolved
legacy operation log against the D1 pairs, consumes authorization, and delegates to
`executor.undo_operation_log()`.

`executor.py` remains the only restoration owner. Its optional Undo recorder persists
`attempting` before each restore and `restored` or `failed` immediately afterward while
preserving legacy undo-result logs. The application aggregates complete, partial,
failed, or interrupted state and delegates read-only post-Undo checks to
`organization_verify.py`. Successful verification requires the original file and
fingerprint to be restored and the moved destination to be absent.

The authenticated no-store Activity API and strict CSRF/same-origin Undo POST routes
expose only opaque IDs, safe counts, statuses, and root-relative details. The browser
never submits filesystem or log paths. The Loci Flow Activity page displays durable
movement and restore records with collapsed details and explicit Undo confirmation.
The **Move files — not connected yet** control remains disabled; no public movement
route exists. D2B owns browser movement enablement, and Duplicate Bin execution and
restoration remain deferred to 1.0E.

The preceding statements describe the D2A historical boundary. Browser movement
was connected in D2B and prototype-first B4; Duplicate Bin approval, movement,
restoration, Activity integration, and restart reconciliation were implemented
through 1.0E. Standards integration was completed later through 1.0F, as
summarized in Current Production State above.

## Product Direction

The local web application is bootAI's intended primary interface for ordinary users. It is for nontechnical users who need help finding files, understanding duplicate groups, remembering where files belong, reviewing organization suggestions, recovering from mistaken moves, and identifying potential storage-recovery candidates.

The interfaces have distinct roles:

- The local web application is the primary consumer interface.
- The CLI remains supported for development, scripting, diagnostics, manual fallback, and safety-critical testing.
- Static HTML remains a permanent, read-only report and audit-snapshot format. It does not become an interactive web application.
- A native desktop application is optional, demand-driven, and deferred until after core Stage 11 work.

## Implemented Boundaries Through Stage 11.5.1

Stage 11.0 did not add or modify:

- `src/organizer/web/` or `src/organizer/application/`;
- FastAPI, Uvicorn, Jinja2, HTMX, or Bootstrap dependencies or files;
- routes, forms, sessions, cookies, CSRF code, jobs, templates, or static assets;
- scanner, grouping, review, executor, undo, verification, or report behavior;
- JSON schemas, reviewed-plan formats, report formats, or CLI flags;
- databases, native UI code, launchers, browser-opening code, or packaging;
- cloud APIs, telemetry, permanent removal, or Trash integration.

Stage 11.1 adds `application/scan_service.py`, `review_service.py`, `artifact_service.py`, and `view_models.py`. It does not add `application/preflight_service.py`, `application/execution_service.py`, `src/organizer/web/`, or web dependencies. Artifact loading is deliberately limited to scan reports and reviewed plans; later execution and history artifacts remain deferred.

Stage 11.2 adds `src/organizer/web/`, exact optional web dependencies, installed templates/static resources, and focused HTTP-security helpers. The shell reuses only `safety.validate_under_root`; it does not import application workflows, scan, list or load artifacts, create review sessions, expose a production POST route, or alter any movement or undo path. The existing CLI and JSON formats remain unchanged.

Stage 11.3 adds the explicit read-only scan dashboard and generation-safe in-process scan job. Stage 11.4 adds the authenticated GET-only review explorer, which builds rows from the latest completed scan report through the application review service, keeps view state in validated query parameters, and does not rescan or write artifacts during navigation.

Stage 11.5 adds a web-owned, locked holder for one immutable `ReviewApplicationSession` per completed scan generation. Single-row decisions replace that session through the application service. Current-page previews freeze exact stable IDs server-side and expose only a random one-use token bound to the browser session and scan generation. The three established exact phrases remain `APPROVE CURRENT PAGE`, `REJECT CURRENT PAGE`, and `UNDECIDE CURRENT PAGE`.

User labels preserve existing values: Organize is `approved`, Keep here is `rejected`, and Review later is `undecided`. Saving is explicit and delegates to the existing application/owner path, includes every hidden and off-page row, uses collision-safe naming, and reports a root-relative path. No click autosaves. Dirty state blocks a new scan with `409 Conflict`; successful save clears it. Apply, restore, reviewed-plan resume, revision numbers, and multi-tab stale-state handling are not implemented.

Stage 11.5.1 assigns consumer responsibilities: Home summarizes; Scans collects findings; Duplicates presents exact copies; Organize presents placement suggestions; Needs attention presents ambiguous candidates; Settings displays configuration; Advanced review exposes technical rows. Web-only presenters group cards by safety-validated normalized source, prefer duplicate then organization then attention, and show secondary reasons without merging rows or decisions. Primary actions change only the primary row.

Navigation and presentation are GET-only. Mutations use strict enums or fixed routes. Internal navigation retains server-held dirty choices without a `beforeunload` warning. Module-specific persistence is implemented in 11.5.2; the planned-change tree remains deferred to 11.5.3.

Stage 11.5.2 adds a strict application `ReviewModule` mapping and partial saved baselines over the same immutable session. Fresh web new/stale rows are normalized to `undecided`; remembered decisions are retained. Module artifacts use the existing reviewed-plan schema and fixed collision-safe names. Guided handled/deferred IDs are generation-bound memory only and never become another decision store.

## Current Ownership That Must Be Preserved

The current implementation already separates facts, review decisions, movement, verification, and audit artifacts. Later web work must reuse these owners:

| Current module | Preserved ownership |
| --- | --- |
| `safety.py` | shared root-containment validation |
| `review_session.py` | review rows, stable IDs, decisions, filtering, sorting, pagination, saving, saved-plan validation, and conflict detection |
| `executor.py` | movement-specific source/destination/symlink preflight, all real movement, operation logs, and undo |
| `organization_verify.py` | read-only post-apply verification |
| `reports.py` | report assembly and JSON report writing |
| `html_report.py` | static, read-only HTML rendering |
| `cli.py` | substantial current user-flow orchestration that will be extracted incrementally |

The shared validator in `safety.py` is the root-containment authority. Movement-specific validation remains executor-owned because it is part of the movement boundary. `executor.py` is the only module that may implement moves or undo.

Existing JSON artifacts remain the authoritative persistence formats:

- scan reports;
- reviewed plans and review state;
- organization rules;
- operation and undo logs;
- apply results;
- verification reports.

## Required Dependency Direction

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

The browser is a display and interaction surface. It is not a source of trusted filesystem instructions. The web layer translates validated HTTP requests into application-service calls. Application services coordinate existing domain modules. They do not replace those modules.

### Forbidden dependency directions

```text
web route → executor.py
web route → raw filesystem movement
web route → duplicated scanner, duplicate, grouping, or review logic
template → filesystem operation
browser form → arbitrary absolute path
browser form → arbitrary destination path
web layer → CLI handler simulation
web layer → terminal input()
```

Web routes must never import `executor.py`. Only the future `execution_service.py` may delegate already validated and explicitly approved plans to existing executor functions.

## Application-Service Layer

Stage 11.1 introduces this package:

```text
src/organizer/application/
    __init__.py
    scan_service.py
    review_service.py
    artifact_service.py
    view_models.py
```

`preflight_service.py` and `execution_service.py` remain future ownership slots for Stages 11.7 and 11.8 or later. They do not exist in Stage 11.1.

### `scan_service.py`

- Coordinate scan workflows through the scanner, duplicate, review, grouping, and reporting modules.
- Return typed UI-independent results.
- Never perform terminal input, printing, HTML rendering, movement, or undo.

### `review_service.py`

- Create and resume sessions through `review_session.py`.
- Expose existing filters, sorting, pagination, stable-ID decisions, conflict checks, and saving.
- Never create a second review engine or silently merge stale decisions.

### `artifact_service.py`

- List and load only existing scan reports and reviewed plans for the first web MVP.
- Treat every loaded artifact as untrusted input.
- Resolve and validate artifact paths under the immutable process root.
- Delegate format validation to `reports.py` and `review_session.py`; defer all later artifact formats to their roadmap stages.

### Future `preflight_service.py`

- Perform read-only final-plan and restore checks.
- Detect missing or changed sources, existing destinations, conflicts, root escapes, symlink hazards, tampered artifacts, and stale revisions.
- Never move or restore files.

### Future `execution_service.py`

- Be the only application-service gateway to existing executor functions.
- Accept only a validated, current, explicitly approved plan.
- Enforce fresh preflight and exact confirmation before delegation.
- Return operation, apply, verification, and undo results without implementing movement.

### `view_models.py`

- Define typed presentation data shared by CLI and web presenters where appropriate.
- Contain no request objects, HTML, terminal I/O, or filesystem side effects.

## Web Interface Layer

The Stage 11.2 package is:

```text
src/organizer/web/
    __init__.py
    __main__.py
    app.py
    server.py
    config.py
    security.py

    routes/
        __init__.py
        home.py

    templates/
        base.html
        home.html
        error.html

    static/
        css/
            bootai.css
        js/
            bootai.js
        vendor/
            htmx.min.js
            bootstrap.min.css
            bootstrap.bundle.min.js
        THIRD_PARTY_NOTICES.txt
```

Later stages add workflow routes, presenters, jobs, templates, and fragments only when their roadmap stage begins. Stage 11.2 contains none of those structural placeholders.

The web layer may own routes, forms, HTTP request validation, signed sessions, CSRF enforcement, templates, HTML fragments, presenters, security headers, and user-facing error rendering. It must not own scanning algorithms, duplicate detection, grouping logic, review validation, conflict detection, root safety, movement, undo, or operation-log construction.

## Browser Request Contract

### Loci Flow Integration 1.0B — Historical Checkpoint

`GET /loci-flow` is a separate authenticated, no-store HTML shell that preserves the existing consumer interface. Its Home and Organize views fetch only the same-origin authenticated `GET /api/organization/current` application-service contract. The immutable launch-time root remains server-owned; the browser sends no root, path, style, or decision parameters and has no fixture fallback.

This integration displays real scope and freshness, recommendations, choices, items kept by Standards, file metadata, and the backend-provided movement tree. Duplicate Bin, Activity, and Standards editing are explicitly not integrated. The shell submits no organization decisions and cannot move files. Its visual baseline is the unchanged standalone fixture prototype under `prototype/loci-flow-ux-1-1-focused-policy-controls/`, which is not served at runtime.

At that historical checkpoint, the next stage was temporary decisions and
approval without movement. Decisions, approval, executor-owned browser
movement, operation records, Activity, Undo, and Duplicate Bin restore were
implemented later through 1.0E. Persistent writable Standards were outside that
historical boundary and were implemented later through 1.0F.

The browser submits stable, session-scoped identifiers and allowed actions. The backend maps those identifiers to server-held state and revalidates the corresponding paths.

Conceptually accepted:

```text
row_id=O17
decision=approved
revision=12
csrf_token=<session-bound token>
```

Forbidden:

```text
source=/Users/name/Downloads/file.pdf
destination=/Users/name/Documents/file.pdf
```

State-changing requests must carry a current revision or equivalent version marker. A stale request is rejected and shown as a conflict; it is never silently merged over newer decisions.

## Root-Selection Contract

Each server process operates on exactly one root:

1. Select the root before or during launch.
2. Validate it with existing safety logic.
3. Store its resolved identity as immutable process configuration.
4. Map all session IDs and artifact access back to that root.

The browser cannot change the root by submitting path text. Changing roots requires ending the current root-bound server, validating another root, and starting a new process.

The implemented development launcher is:

```bash
PYTHONPATH=src python3 -m organizer.web --root /path/to/folder
PYTHONPATH=src python3 -m organizer.web --root /path/to/folder --no-browser
PYTHONPATH=src python3 -m organizer.web --root /path/to/folder --port 8123
```

The only launcher options are `--root`, `--no-browser`, and a validated `--port` from 1 through 65535; there is no `--host`. The default is the current user's Downloads directory only when that directory passes configuration validation. A packaged launcher may later use a small native folder chooser before opening the browser; that does not make bootAI a native desktop application.

## Accepted Web Stack and Asset Policy

The accepted stack is:

```text
FastAPI
Jinja2
HTMX
Bootstrap
minimal vanilla JavaScript
Uvicorn
```

Stage 11.2 pins FastAPI 0.139.0, Starlette 1.3.1, Uvicorn 0.50.2, Jinja2 3.1.6, and ItsDangerous 2.2.0 in the optional `web` group. HTTPX 0.28.1 is isolated in `web-test`. Core dependencies remain empty; neither `python-multipart` nor FastAPI broad extras are installed.

The bundled runtime assets are the official HTMX 2.0.10 `dist/htmx.min.js` and Bootstrap 5.3.8 `bootstrap.min.css` and `bootstrap.bundle.min.js`, byte-verified against their published SHA-384 integrity values. `THIRD_PARTY_NOTICES.txt` records versions, sources, integrity values, and the 0BSD/MIT licenses. No source maps are packaged.

All assets must be bundled locally, including HTMX, Bootstrap CSS and JavaScript, icons, custom CSS and JavaScript, and any fonts. CDNs, remote fonts, analytics, telemetry, external scripts, and runtime cloud dependencies are forbidden. The application must remain usable without internet access.

Static HTML audit reports remain self-contained read-only documents. They do not gain routes, sessions, approval controls, or apply behavior.

## Server and Concurrency Policy

The implemented server configuration is:

```text
host: 127.0.0.1
port: dynamically selected when practical
workers: 1
root: one validated and immutable root per launch
```

It must never bind to `0.0.0.0` by default. LAN access, remote access, multi-user hosting, and cloud deployment are outside Stage 11.

The launcher binds and listens on an IPv4 socket before Uvicorn starts, obtains the operating-system-selected port when none is requested, and passes that socket to one programmatic server. Uvicorn access logging and proxy-header handling are disabled. A bounded readiness thread polls only `/healthz`, opens the one-time launch URL after success, and is cancelled and joined during cleanup; the socket is always closed. No port, PID, token, secret, or shutdown file or endpoint is created.

Use a single process and one worker initially. Permit at most one active scan job and one active execution or restore operation in a root-bound process. UI button disabling is only feedback; server-side locks, revisions, idempotency controls, and fresh preflight provide the actual protection.

## Persistence Policy

Do not introduce a database initially. Existing JSON artifacts remain authoritative. Temporary interface state may remain in memory:

- filters, sort, page, and page size;
- dirty-state and current revision;
- scan-job status;
- launch and session tokens;
- CSRF token;
- one-operation locks and replay-prevention state.

A database may be considered only after measured evidence shows a need. It must not become a prerequisite merely to host a single-user localhost UI.

## Security Contract

Localhost is a network security boundary, not a reason to omit security. Stage 11.2 implements loopback-only binding, atomic single-use launch authentication, a signed host-only browser-session cookie, session-bound CSRF helpers, exact fail-closed same-origin validation, Trusted Host enforcement, restrictive headers, and no CORS. The signed cookie is integrity-protected but not confidential. Stage 11.5 mutations add authenticated POST-only handling, strict form allowlists, current-generation checks, locked session replacement, one-use bulk tokens, and POST/redirect/GET. Stage 11.6 adds explicit revisions and multi-tab stale-state rejection. Details and threat-to-control mappings are mandatory in [WEB_THREAT_MODEL.md](WEB_THREAT_MODEL.md).

## Accessibility Contract

Target WCAG 2.2 AA from the first web screen. Every Stage 11 interface must provide:

- complete keyboard access and logical focus order;
- clearly visible focus indicators;
- semantic HTML and labelled controls;
- accessible tables, dialogs, errors, and validation guidance;
- status and progress announcements where appropriate;
- sufficient contrast and no information conveyed by color alone;
- no drag-only interaction;
- usable browser zoom and reflow.

Accessibility is an implementation requirement, not a final packaging pass. Stage 11.10 performs the formal audit and hardening.

## Storage-Recovery Language

Moving a file to another folder on the same filesystem does not save storage space. The interface may report:

- `Potential duplicate bytes`;
- `Space used by extra copies`;
- `Potential duplicate space`;
- `Duplicate candidates for review`.

It must not report `Space saved` or `Storage recovered` unless an operation actually removes data from that filesystem or transfers it elsewhere. Stage 11 adds no permanent removal, Trash integration, automatic disposal, or disposal-safety claims.

## CLI Migration Policy

Do not rewrite `cli.py` in one large refactor. Extract one use case at a time in Stage 11.1 and later:

```text
CLI → application service
Web UI → same application service
```

Preserve existing CLI behavior and tests during each extraction. CLI and web flows must not develop separate scan, review, preflight, execution, or restore implementations.

Stage 11.1 removed the former mandatory `openai` and `python-dotenv` dependencies and their transitive lockfile packages. Core dependencies remain empty and package discovery remains restricted to `src/`. The historical OpenRouter assistant is preserved only under `legacy/openrouter_code_assistant/`, outside bootAI packaging and runtime. Stage 11.2 adds only the isolated optional `web` and `web-test` groups.

## Packaging Goal

The later development workflow may become:

```bash
python -m pip install -e ".[web]"
bootai web
```

The end-user target is:

```text
download
double-click
choose or accept Downloads
browser opens automatically
```

The next binding stage is **1.0G — Release Launcher and Packaging Contract
Inspection**. It must resolve native folder-selection ownership, root validation
before server creation, launch-token handling, browser bootstrap, clean shutdown,
packaged process lifecycle, the macOS packaging approach, safe terminal hiding,
failure and cancellation behavior, and packaging dependency constraints. This
inspection does not pre-approve a packaging tool or implementation.

If a launcher is later approved, its first implementation remains separate from
the later WCAG 2.2 AA audit, large-folder performance testing, and nontechnical-
user testing. Windows packaging is deferred until after the macOS contract and
implementation are proven.

## Historical Stage 11 Sequence

This sequence is retained as project history. Loci Flow 1.0A through 1.0F
overtook the planned-change tree, revision, preflight, browser apply, Activity,
Undo, and restore entries below. Current status is defined at the top of this
document and in `ROADMAP.md`.

### Stage 11.0 — Architecture Contract and Threat Model

Documentation only: stack, ownership, threat model, security controls, accessibility, and complete roadmap.

### Stage 11.1 — Application Services and Dependency Hygiene

Completed: UI-independent scan, immutable review-session, and scan-report/reviewed-plan artifact services; narrow CLI entry migration; mandatory cloud dependency removal; and historical OpenRouter archival. Optional web dependencies were deferred to 11.2. No server and no movement changes were part of 11.1.

### Stage 11.2 — Secure Local Web Shell and Launcher

Completed: FastAPI app factory, loopback-only launcher, dynamic or validated fixed port, bounded automatic browser opening, atomic one-time launch token, signed session, CSRF and Origin foundation, Trusted Host enforcement, locked headers, generic errors, accessible base/welcome templates, and verified local assets. No scan or workflow route exists yet.

### Stage 11.3 — Read-Only Scan Dashboard

Completed: explicit user-triggered scan, one generation-safe in-process scan job, progress polling, failure handling, report generation, and summary cards. No decisions or movement.

### Stage 11.4 — Read-Only Review Explorer

Completed: browse the latest completed scan’s review rows, filter, sort, paginate, inspect stable-ID metadata details, and inspect approved conflicts. Explorer navigation does not rescan or write artifacts. No decision mutation.

### Stage 11.5 — Review Decisions and Reviewed-Plan Saving

Completed: Organize, Keep here, and Review later decisions; current-page exact-confirmed bulk decisions; dirty state; explicit save; collision-safe reviewed-plan artifacts; and scan blocking while unsaved decisions exist. No autosave or movement.

### Stage 11.5.1 — Consumer Information Architecture and UX Simplification

Completed: consumer navigation and cards, one-file-one-primary-surface presentation, secondary reasons, module-separated choices, human formatting, Advanced review, and removal of the false internal-navigation warning. No schema, decision, persistence, or movement change.

### Stage 11.5.2 — Guided Modular Review and Independent Module Plans

Completed: conservative fresh-web decisions, guided queues, temporary skip/revisit state, independent module dirty tracking, fixed module artifacts, and complete-session save compatibility. No movement.

### Stage 11.5.3 — Static Planned-Change Tree (Superseded Plan)

Implemented later as the backend-provided movement tree in Loci Flow 1.0A and
the live accepted Organize renderer in 1.0B.

### Stage 11.6 — Resume, Stale-State, and Multi-Tab Protection (Overtaken)

Loci Flow 1.0C later implemented generation- and revision-bound organization
decisions and stale mutation rejection. This does not claim completion of every
legacy reviewed-plan feature.

### Stage 11.7 — Final Plan Preview and Execution Preflight (Overtaken)

Implemented for the Loci Flow approval and movement boundary through 1.0C and
1.0D1.

### Stage 11.8 — Confirmed Web Apply and Verification (Overtaken)

Implemented for Loci Flow through 1.0D and prototype-first B4 using fresh
preflight, exact confirmation, authenticated strict POST routes, durable intent,
the shared coordinator, executor-owned movement, and verification.

### Stage 11.9 — History, Verification, and Confirmed Restore (Overtaken)

Implemented for Loci Flow through 1.0D2 and 1.0E with durable Activity,
ordinary Undo, Duplicate Bin Restore, exact confirmation, unified restore
ownership, and verification.

### Stage 11.10 — Packaging, Onboarding, Accessibility, and Release Hardening

Begin with the inspection-only 1.0G launcher and packaging contract. If accepted
separately, implement a low-setup macOS launcher with validated native folder
selection, browser bootstrap, clean shutdown, and no unnecessary terminal
exposure. Keep the accessibility audit, large-folder performance testing, and
nontechnical-user testing in later release-hardening stages. Windows packaging
remains deferred.

Native desktop development remains an optional final extension after Stage 11.

## Stage 11.5 MVP Checkpoint

Stage 11.5 is the first product-evaluation checkpoint. A user should be able to launch the development web interface, scan a folder, understand findings, inspect suggestions, make review decisions, and save a valid reviewed plan.

Historical checkpoint statement: apply and Undo were CLI-only at Stage 11.5.
They were implemented later in the Loci Flow browser integration. Product
evaluation remains required before expanding beyond the accepted six areas.
