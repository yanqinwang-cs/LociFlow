# Loci Flow — UX 1.1 Focused Prototype

Loci Flow is a standalone, fixture-driven prototype for evaluating a focused organization-planning companion. It is not a general file manager and is not intended to replace Finder or File Explorer.

> Loci Flow works alongside Finder or File Explorer to prepare personalized, visible, and reversible file-organization changes.

The prototype concentrates on preparing and editing organization plans, comparing current and planned folder structures, resolving uncertain recommendations and conflicts, reviewing exact duplicate copies separately, simulating confirmed application and verification, reviewing activity, providing a short-term undo failsafe, and representing learned habits and explicit locked rules.

Finder or File Explorer remains responsible for ordinary search, browsing, previews, opening files, general renaming, arbitrary movement, Trash, and other normal filesystem work. The prototype may acknowledge a simulated **Reveal in Finder** action for a plan-relevant file, but it does not reproduce those native file-management functions.

## Status and boundary

This directory is isolated product research. It does not replace bootAI's accepted Stage 11 production architecture, existing web interface, schemas, safety owners, executor, persistence formats, CLI, or documentation.

The focused prototype is a zero-build ES-module SPA whose state is cloned from `fixtures.json` and stored in browser `localStorage`, with an in-memory fallback. That is a convenient research mechanism only. It is **not** the accepted production architecture, security model, or persistence design.

The accepted production web direction remains a root-bound FastAPI, Jinja2, HTMX, Bootstrap, and minimal-JavaScript application that treats browser input as untrusted, keeps authoritative state and paths server-side, uses existing JSON artifacts, and delegates across the established application-service boundary. This prototype implements none of those server-side controls and makes no production-security claim.

In particular, web Apply and restore remain production-deferred. Stage 11.8 is the first production stage permitted to add confirmed web Apply and verification; Stage 11.9 owns production web history and confirmed restore. Their appearance in this prototype is simulated workflow research, not an integration or a claim that those web capabilities currently exist.

## Run locally

No build step, package manager, external asset, remote font, CDN, or runtime dependency is required. From the repository root, serve only the focused prototype directory on loopback:

```sh
python3 -m http.server 8000 \
  --bind 127.0.0.1 \
  --directory prototype/loci-flow-ux-1-1-focused
```

Then open `http://127.0.0.1:8000/`. The explicit loopback binding prevents the static server from listening on other network interfaces.

Opening `index.html` directly is not supported because the browser loads the JSON fixture and ES modules through HTTP.

## Reset

Open **Settings → Advanced → Reset prototype**, confirm the reset, and the application will:

- remove its focused-prototype localStorage state;
- clone the complete initial fixture again;
- restore Downloads as up to date;
- restore the ready 38-change organization plan;
- restore two exact-copy groups, Activity, settings, rules, selections, and demo outcomes;
- end any active mutable workflow; and
- return to Home.

If localStorage is unavailable or full, state remains usable in memory for the current page session. Reloading then restores the fixture default.

## Validation

Run the focused contract test from the repository's activated `.venv`:

```sh
PYTHONPATH=src python3 -m unittest tests.test_loci_flow_focused_prototype -v
```

Run the complete repository suite with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

If Node is already installed, optional syntax-only checks require no package installation:

```sh
node --check prototype/loci-flow-ux-1-1-focused/store.js
node --check prototype/loci-flow-ux-1-1-focused/views.js
node --check prototype/loci-flow-ux-1-1-focused/app.js
```

Validate the fixture independently with:

```sh
python3 -m json.tool prototype/loci-flow-ux-1-1-focused/fixtures.json >/dev/null
```

The contract test protects semantic routes, fixture counts and references, workflow and stage definitions, local-only runtime behavior, accessibility hooks, forbidden integrations, critical safety language, and preservation of the Pass 1 prototype. It deliberately does not freeze page geometry, exact DOM nesting, component classes, navigation placement, or noncritical prose.

## Fixture contract

The fixture represents one fictional mixed-purpose Downloads location. Every name, date, path, file, decision, and operation is sample data.

Initial state:

- Downloads is the active location and is **Up to date**.
- No mutable workflow is open.
- One organization plan is ready with exactly 38 proposed changes across exactly five destinations.
- `Elarian logo.svg` and `final.pdf` are the two **Review** items.
- `Notes.docx` is the single **Conflict** item.
- Exactly two active exact-copy groups are ready: one has a recommended working copy and one is ambiguous.
- Preferred-copy-edited and changed-extra examples are recheck scenarios outside that active duplicate count.
- One recent organization operation is **Applied and verified**.
- One duplicate operation has short-term undo available.
- One older operation has **Undo unavailable**.
- Fixtures also cover **Partially completed**, **Verification needed**, **Failed with no changes**, and **Undone and verified**.
- Plan preparation is Manual and every filesystem action requires confirmation.
- Organization-plan-ready notifications are enabled.
- One deterministic locked rule maps filenames beginning with CS1010X or CS1010S to Study › CS1010S.
- Local AI is off and telemetry is not implemented.

Stable prototype IDs connect canonical files and folders to plan changes, duplicate groups, Activity operations, and locked rules. Names, breadcrumbs, counts, and summaries are derived from those records where practical instead of being maintained as independent display state.

## State and workflow architecture

One versioned, namespaced localStorage object holds the complete prototype state. The store clones fixtures on first use, validates the stored version, persists mutations, derives route projections through selectors, keeps a bounded plan-edit undo history, and resets atomically to the initial fixture. All routes read the same state.

The organization-plan lifecycle is separate from the currently open mutable workflow. A ready plan may exist while the active workflow is `none`.

Only one mutable workflow may be active:

- `none`;
- `organization`;
- `duplicates`;
- `applying`; or
- `verifying`.

Organize and Duplicates remain visible in primary navigation. Opening the other mutable area while a plan or duplicate review is in progress never switches silently. A compact dialog offers a return to the current workflow or an explicit discard-and-switch action. Applying and Verifying prevent competing mutable work.

Selection uses stable IDs. Current and Planned views synchronize the same selected file; keyboard-operable checkboxes provide multi-selection. Plan edits update the Planned projection and its counts without changing canonical file locations. Apply mutates canonical fixture records only after the simulated recheck/apply/verify sequence reaches the appropriate stage.

Quick plan edits use immediate local feedback and one bounded undo snapshot. Staged operations use a shared state model with completed, active, waiting, and failed stages and announce each active stage through an ARIA live region.

## Application structure

The only primary destinations are:

- Home;
- Organize;
- Duplicates;
- Activity; and
- Settings.

Advanced exists only inside Settings. There are no Search, Files, general file-detail, or general browsing routes.

The application uses one persistent shell and one shared grammar for navigation, toolbars, local inline SVG icons, file and folder rows, selection, inspectors, dialogs, status labels, staged progress, and responsive transformation.

### Home

Home is a compact command surface that reports the active Downloads location and freshness, organization-plan readiness, duplicate-group readiness, the most recent result, and any workflow already in progress. It does not include search, a folder map, general browsing, promotional copy, or dashboard analytics.

### Organize

On wide screens the main workspace is:

```text
Current organization | Planned organization | Inspector
```

Current is a read-only projection of the latest checked fixture state. Planned uses the same folder/file rows and shows the expected structure after the plan. Both panes include only affected branches, proposed destinations, nearby context, and collapsed aggregate rows. Planned always states **Preview — no files have moved.**

The workspace supports synchronized single selection, checkbox multi-selection, Select all visible, **Move to…**, **Leave unchanged**, removing files from the plan, removing a proposed destination group from the plan, issue resolution, and plan-edit undo. Removing a plan destination means its files remain in their current locations; it never deletes a folder or file.

Rows use exactly three primary plan states: **Ready**, **Review**, and **Conflict**. Review and Conflict rows remain in the planned hierarchy rather than becoming a separate exception queue. Selecting an issue shows its concise reason and resolution controls in the Inspector.

Apply remains visible. If issues remain, activating it explains what blocks application and offers a direct route to the relevant Review or Conflict rows. Review items may be accepted, changed, or left unchanged in any order, and the Conflict may be resolved at any time. Once all issues are resolved, one concise confirmation precedes execution.

### Duplicates

Duplicates uses the same shell, rows, selection, inspector, dialogs, and stage component as Organize. It presents duplicate groups, the selected group's members, and concise recommendation evidence.

The user may choose a working copy, keep all copies, select extras, and preview consolidation. Consolidation means selected extra copies move to a reversible fixture holding area after recheck and confirmation. Files are not deleted, and storage is not recovered. A changed extra is excluded because it is no longer an exact copy; an edited preferred copy requires reconfirmation.

Organization Apply and duplicate consolidation remain separate workflows.

### Activity and undo

Activity is a stacked, collapsible operation list with no decorative timeline or analytics. Its filters are limited to All, Completed, and Needs attention. Expanded operations show concise outcomes, affected canonical file rows, verification truth, undo availability, and no more than the relevant actions.

Undo is a short-term accident failsafe, not filesystem time travel. A recent clean organization operation may be undone and verified. A narrow duplicate recovery is labelled **Return this copy**, not full-operation undo. When later changes make reversal unsafe, Activity says **Undo unavailable**, explains that affected folders changed, and recommends preparing a new organization plan.

### Settings

Settings uses eight compact categories:

- Location;
- Plan preparation;
- Rules;
- Duplicates;
- Notifications;
- Local AI;
- Privacy; and
- Advanced.

The fixture defaults to manual plan preparation and **Always ask** before application. The locked rule can be inspected, paused, edited, or removed. Notification choices persist but send no real notification. Local AI represents an off-by-default, advisory-only local Ollama concept and makes no connection. Privacy states that the fixture is local, uploads nothing, and implements no telemetry. Advanced contains demo outcomes, reset, fixture diagnostics, export placeholders, and the temporary Loci Flow working-name notice.

## Stage-based progress

Substantial operations use factual stages without percentages, fake time estimates, or equal-duration claims.

Plan preparation:

1. Checking files
2. Finding groups
3. Checking conflicts
4. Preparing plan

Organization application:

1. Rechecking plan
2. Applying changes
3. Verifying
4. Complete

Duplicate consolidation:

1. Rechecking copies
2. Consolidating
3. Verifying
4. Complete

Success is never shown before Verifying completes. Alternative demo outcomes—Partially completed, Failed before changes, and Verification incomplete—are selectable only in **Settings → Advanced** and state what changed, what did not, and what still needs attention.

## Capability classification

The rows below distinguish production capabilities from prototype simulations. “Supported in production” does not mean connected to this prototype.

| Classification | Capability | Focused-prototype treatment |
| --- | --- | --- |
| Supported through Stage 11.5.2 | Read-only metadata scanning; SHA-256 exact-duplicate detection; conservative deterministic organization suggestions; optional advisory local Ollama refinement; review candidates; root-contained reports and artifacts | Represented only by authored fixture evidence. No scanner, hash function, Ollama client, report owner, or artifact service is imported or called. |
| Supported through Stage 11.5.2 | Secure root-bound local web launch; explicit scan; guided Duplicates, Organize, and Needs attention queues; stable-ID decisions; independently saved module plans; complete Advanced review/save | Not reused by this SPA. The prototype has no server session, CSRF, Origin, Host, revision, root lock, artifact write, or production route. |
| Supported through existing CLI owners | Exact-confirmed approved duplicate, organization, reviewed-plan, and organization-review movement through `executor.py`; operation logs; logged undo; read-only organization-apply verification. Organization-rule decisions are separately exact-confirmed configuration updates that never call the executor or move files. | Simulated as fixture-only Apply, Verify, Activity, and undo flows. The prototype never calls the CLI, executor, verification owner, or filesystem. |
| Bounded prototype research | Current-versus-Planned comparison, bulk plan editing, integrated Review/Conflict resolution, one-active-workflow behavior, working-copy selection, reversible-holding explanation, compact Activity, and settings concepts | Implemented only in browser state for structural and interaction evaluation. Reset restores all fixture state. |
| Research-dependent | Personalized grouping quality, recommendation explanations, relationship inference, habit learning, proactive plan timing, rename suggestions, conflict usefulness, and behavior at realistic file counts | Presented as hypotheses and authored examples, not validated intelligence, a scoring formula, or production behavior. |
| Production-deferred | Stage 11.5.3 planned-change tree; Stage 11.6 resume/revisions/multi-tab protection; Stage 11.7 execution preflight; Stage 11.8 web Apply/verification; Stage 11.9 history/restore; Stage 11.10 packaging and formal accessibility/performance hardening | The prototype may explore their user-facing grammar but does not implement their production ownership, persistence, security, or filesystem effects. |
| Deliberately deferred beyond this pass | Drag-and-drop, filesystem watchers, SQLite, multiple locations, semantic search, embeddings, OCR, content indexing, native Trash, permanent removal, graph layouts, zoom/pan, real notifications, telemetry, uploads, cloud APIs, and automatic execution | Not implemented or claimed. |

## Safety and local-only guarantees

The persistent notice is literal:

> Prototype using sample files. No files on your computer are accessed or changed.

The focused prototype:

- fetches only its bundled `fixtures.json`;
- imports only its sibling JavaScript modules;
- makes no backend, production route, API, executor, CLI, Ollama, notification, analytics, telemetry, upload, or cloud call;
- never requests a folder or file permission;
- exposes no file input or arbitrary file-serving endpoint;
- uses no filesystem API;
- never opens, renames, moves, deletes, overwrites, restores, or otherwise mutates a real file;
- never uses operating-system Trash;
- never permanently deletes fixture files;
- never claims that moving a duplicate copy recovers storage; and
- never writes an operation log, reviewed plan, report, export, or other artifact.

All Apply, Verify, Consolidate, Reveal in Finder, Activity, undo, notification, location, Local AI, diagnostics, and export behaviors are fixture simulations. Browser state is not evidence that a filesystem operation succeeded.

## Responsive behavior

Desktop is primary.

- Around 1440 px, Organize shows Current, Planned, and Inspector together.
- Around 1024 px, Current and Planned remain side by side while Inspector moves into a bounded region below them; it remains explicitly hideable.
- Around 820 px, Current and Planned may use a segmented switch while preserving synchronized selection; Inspector remains available without horizontal overflow.
- Settings converts its category list to a compact narrow-width selector.
- Activity remains stacked.
- Controls wrap as controls, never as concatenated text, and type remains readable.

## Accessibility

The prototype targets WCAG 2.2 AA principles and provides semantic landmarks, a skip link, complete keyboard operation, visible focus, labelled controls, accessible names and titles for icon-only actions, text-plus-icon statuses, native or equivalently trapped dialogs, Escape handling, focus restoration, keyboard multi-selection, live stage announcements, `aria-current` on the active stage, sufficient contrast, and no drag-only interaction.

Motion is restrained to selection, local plan changes, progress, inspector transitions, and outcomes. `prefers-reduced-motion` removes sliding and pulsing while preserving every state and announcement.

## Acceptance walkthrough

Reset first so counts and decisions are deterministic. Use the normal **Applied and verified** Advanced outcome for the main path.

1. On Home, identify Downloads as up to date and see that an organization plan is ready.
2. Confirm the application says this is a preview and that no files have moved.
3. Open the organization plan.
4. Compare the Current and Planned organization for the same affected branches.
5. Select multiple proposed files using keyboard-operable selection controls.
6. Choose **Move to…**, select another destination, confirm the planning edit, and see the Planned view and counts update locally.
7. Remove one file from the plan and confirm it remains in its current location.
8. Remove one proposed destination group from the plan and confirm the dialog explains how many files remain in their current locations.
9. Select a row labelled **Review** and read its concise reason in the Inspector.
10. Activate Apply while Review items remain and confirm the interface explains the gate and offers **Show review items**.
11. Accept, change, or leave unchanged each Review item. Resolve them in either order.
12. Select the **Conflict** row, read the destination collision, and resolve it; confirm issue resolution is not order-dependent.
13. With no unresolved issues, inspect the concise final confirmation and apply the simulated organization plan.
14. Distinguish **Rechecking plan**, **Applying changes**, **Verifying**, and **Complete** visually and through live announcements.
15. Confirm the final result is **Applied and verified** and that success appeared only after verification.
16. Open Duplicates and select one exact-copy group.
17. Choose or confirm its working copy without describing it as the original.
18. Select extra copies, inspect the consolidation preview, and consolidate them through Rechecking, Consolidating, Verifying, and Complete.
19. Confirm the interface says the copies moved to a reversible holding area, were not deleted, and did not recover storage.
20. Open Activity and expand a recent operation to inspect its outcome and affected file rows.
21. Find the short-term undo failsafe for a recent clean operation and distinguish a narrow **Return this copy** action from full undo.
22. Expand the older operation, understand why undo is unavailable, and find **Prepare a new organization plan**.
23. In **Settings → Rules**, pause the locked CS1010X/CS1010S organization rule.
24. In **Settings → Notifications**, disable **Organization plan ready**, navigate away and back, reload, and confirm it persists.
25. Reset, start Organize, then choose Duplicates. Confirm the switch dialog offers **Continue plan** and **Discard plan and open Duplicates** rather than silently changing workflows.
26. In **Settings → Advanced**, reset the prototype and confirm the complete initial fixture and Home counts return.
27. Repeat the primary flows using only the keyboard, including selection, disclosure, dialogs, Escape, focus restoration, and Apply progress announcements.
28. Test at approximately 820 px and confirm there is no horizontal overflow, concatenated control text, broken card layout, or inaccessible Current/Planned comparison.

Manual completion of these tasks is evidence about prototype behavior only. It does not approve the product direction or establish production readiness.

## Known limitations

- Every recommendation and outcome is authored fixture behavior.
- File counts include aggregate rows rather than a complete Downloads materialization.
- Timing exists only to make staged progress perceptible; it is not a performance estimate.
- Holding-area and undo behavior cannot prove real recovery safety.
- The prototype does not exercise production authentication, artifact validation, path safety, preflight, executor locks, operation logs, or post-apply filesystem verification.
- Reload persistence is browser-local and single-tab; it is not production revision or multi-tab conflict handling.
- The focused structure intentionally excludes Finder-like search, browsing, and arbitrary file actions.

## Usability-review questions

Implementation is not approval. Before another pass or any production integration, observe unfamiliar users and ask:

- Does the companion positioning make it clear when to use Loci Flow versus Finder or File Explorer?
- Can users immediately distinguish current filesystem truth from the planned preview?
- Does synchronized Current/Planned selection reduce comparison effort?
- Is the Inspector discoverable without competing with the folder outlines?
- Are **Ready**, **Review**, and **Conflict** distinct without feeling alarming?
- Does an Apply-gate explanation lead users directly to the unresolved row?
- Do bulk selection and **Move to…** feel familiar without drag-and-drop?
- Does removing a proposed destination clearly mean “leave these files where they are,” not deletion?
- Is the one-active-workflow switch dialog protective without feeling punitive?
- Can users choose a duplicate working copy without assuming provenance or “original” status?
- Is the reversible holding-area model understood, including the absence of deletion and storage recovery?
- Do people understand the difference between full short-term undo, **Return this copy**, and **Undo unavailable**?
- Are staged Apply and Verify states noticeable without unnecessary delay or spectacle?
- Is Home compact enough to answer the next-action questions without becoming a dashboard?
- At 820 px, is switching Current/Planned still an adequate substitute for simultaneous comparison?
- Are labels, icons, focus, feedback, and settings understandable to keyboard and assistive-technology users?

Human usability review is required before a later prototype pass, production planning-tree work, drag-and-drop exploration, database consideration, watcher design, or backend expansion.
