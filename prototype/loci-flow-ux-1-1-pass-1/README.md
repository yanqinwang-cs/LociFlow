# Loci Flow — UX 1.1 Pass 1

Loci Flow is a standalone, fixture-driven product prototype for exploring higher-level file-management intentions. It tests whether people can find files, understand locations and relationships, customize a visible organization plan, handle exact copies, and recover from recent mistakes without repeatedly navigating a complete folder tree.

This directory is deliberately isolated from bootAI's production web application and application services. Loci Flow is a temporary prototype-facing name; it does not rename or replace the repository, package, CLI, or current web interface.

The prototype notice is literal:

> Prototype using sample files. No files on your computer are accessed or changed.

## Run locally

No build step, package manager, runtime dependency, remote font, CDN, or external network service is required. A basic local static server is needed because the browser loads the JSON fixture and ES modules.

From the repository root:

```sh
python3 -m http.server 8000 \
  --bind 127.0.0.1 \
  --directory prototype/loci-flow-ux-1-1-pass-1
```

Then open `http://127.0.0.1:8000/` in a browser. The explicit loopback binding keeps the prototype local to the computer.

Run the isolated fixture contract test with:

```sh
PYTHONPATH=src python3 -m unittest tests.test_loci_flow_prototype -v
```

Run the full repository suite with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Reset

Use **Settings → Advanced → Reset prototype**. Reset clears the namespaced prototype state, restores `fixtures.json`, and reopens onboarding.

For prototype debugging, the same reset can be performed from the browser console and followed by a reload:

```js
localStorage.removeItem("loci-flow:ux-1-1-pass-1:v1");
```

When browser storage is unavailable or full, the prototype keeps the same state in memory until the page is reloaded.

## Fixture model and assumptions

The fixture represents a fictional mixed-purpose user, Maya Chen, and one sample location named Downloads. It contains study, personal, work, travel, receipt, image, manual, and multiple project material. None of the filenames, previews, dates, paths, forms, or activities refer to real user data.

- The visible hierarchy includes Projects (Elarian, Northstar, Website Redesign), Study (CS1010X, CS1010S, Mathematics), Work (Client Forms, Reports), Receipts, Images, Travel, and Manuals.
- `84 loose files` and large unchanged collections are aggregate nodes. The fixture materializes 61 representative file records rather than every item in those collections.
- All folders and files have canonical fixture IDs. Search results, relationships, plan changes, duplicate groups, locked-rule previews, and Activity refer to those IDs. The UI derives the current filename and breadcrumb instead of copying them into each feature.
- The fixed fixture date is 12 July 2026. Relative labels are presentation only and should not be read as the host computer's state.
- The initial organization plan contains exactly 38 file changes across Elarian, Northstar, CS1010S study material, Client Forms, and Receipts.
- The plan begins with two review-required items: weak evidence for `Elarian logo.svg` and ambiguous membership for `final.pdf`. A separate blocking conflict records the different `Notes.docx` already in Elarian.
- Exactly two exact-copy groups are active. The preferred-copy-edited and extra-copy-changed examples are recheck scenarios and do not inflate that active count.
- Search has authored evidence for exact, partial, distinctive-identifier, many-close, weak-only, no-result, and refined cases. The acceptance query has a deliberately fixed order: `CS1010X 2016 Finals.pdf`, `CS1010X 2016-2017 Finals.pdf`, `CS1010X 2016 Final Solutions.pdf`, `CS1010S 2016 Finals.pdf`, then `CS1010X 2019 Finals.pdf`.
- The initial Activity history contains every required outcome: Applied and verified, Partially completed, Verification needed, Failed with no changes, Undone and verified, and Undo unavailable.
- Settings default to manual plan preparation, confirmation before every application, only organization-plan-ready notifications enabled, telemetry disabled, and one active deterministic CS1010X/CS1010S rule.

## Shared prototype state

`fixtures.json` is cloned into one store at startup. The store persists under `loci-flow:ux-1-1-pass-1:v1`, with an in-memory fallback, and every route reads the same canonical records.

Direct rename, move, and exact-copy decisions update canonical file state. If a referenced file also appears in the active organization plan, the store reconciles the existing plan when the result is unambiguous. It removes a completed planned rename or move, recalculates the remaining plan summary, and marks only the affected group **Needs review** when the consequence is ambiguous. The plan must never silently retain an old source name, source location, destination, or exact-copy assumption.

Planning edits keep a bounded undo history. Applying a verified plan updates the fixture files, removes the active plan from Home and Organize, and adds one readable Activity operation. Dismiss removes the plan; regenerate starts again from the fixture-derived plan while retaining the rest of the session state. Notification and other setting changes survive navigation and reload.

This client state is a prototype mechanism, not a proposed production persistence architecture.

## What is simulated

- **Onboarding:** one or two concise screens communicate sample Downloads selection, local analysis, confirmation before movement, and immediate entry to Home.
- **Search and details:** authored ranking evidence plus simple filename/path fallback, optional refinements, derived breadcrumbs, nearby files, and explained related files.
- **Open and Show in Finder:** in-app acknowledgement only; there is no operating-system handoff.
- **Rename and move:** local validation, extension preservation, collision checking, Applying, Verifying, and canonical fixture updates only.
- **Organization:** exception-first review, group and individual edits, planned filename changes, planning undo, recheck, confirmation, and four selectable demo outcomes.
- **Exact copies:** working-copy selection, keep all, consolidation preview, and movement into a fixture-only reversible holding area. Nothing is deleted and no storage recovery is claimed.
- **Activity and undo:** readable fixture changes, recent full undo, narrow return of one consolidated copy, and honest older-operation unavailability.
- **Settings:** presentation and fixture-state controls only. Notification toggles do not send notifications; indexing controls do not index host content; telemetry remains off and collects nothing.

There is no backend call, FastAPI connection, production module import, real filesystem API, folder-permission request, upload, external request, notification delivery, telemetry collection, permanent deletion, operating-system Trash integration, or mutation of files outside this fixture.

## Capability classification

The rows below separate prototype claims from repository capabilities. “Supported” does not mean connected to this prototype.

| Classification | Functions | Pass 1 treatment |
| --- | --- | --- |
| Supported by the current backend | SHA-256 exact-duplicate grouping; conservative organization suggestions; explicit reviewed moves through the single executor; operation logs; logged undo; separate post-apply verification | Represented with fixture language and flows only. No production owner is imported or called. |
| Bounded new prototype work | Loci Flow navigation and terminology; shared fixture state; authored search cases; direct rename/move simulation; exception-first plan editing; exact-copy working-copy choices; readable Activity; Settings state | Fully local, reversible by reset, and intended for structural usability evaluation. |
| Research-dependent | Search ranking beyond authored cases; metadata/content indexing quality; inferred relationships; rename suggestion quality; learned habits; plan grouping and freshness behavior at realistic scale | Shown as hypotheses with plain-language evidence, not as validated intelligence or a final scoring formula. |
| Deliberately deferred | Production integration; real folder approval; semantic search; embeddings; OCR; content indexing; watchers; SQLite; multiple real locations; operating-system notifications and Trash; telemetry; zoom, pan, drag-to-move, force layouts, relationship webs, and complete dynamic trees | Not implemented or claimed in Pass 1. |

The current Stage 11.5.2 web interface still has its existing review-oriented boundary. This prototype does not add a production apply, restore, arbitrary file-serving, or executor route.

## Acceptance walkthrough

Use the normal `verified` Apply outcome for the primary walkthrough. The interface must allow the two review items and the conflict to be handled in any order; the numbered sequence below is a test path, not product logic.

1. Complete the concise onboarding and confirm that Home explains Loci Flow, names Downloads, says **Up to date**, and keeps the sample-file notice visible.
2. Search for `CS1010X 2016 Finals` from Home and confirm that the query is preserved on Search.
3. Confirm that `CS1010X 2016 Finals.pdf` is first and labelled **Strong match** with a readable explanation.
4. Read its Downloads breadcrumb from the result or details without opening a separate folder browser.
5. Open details and follow the related solutions file; confirm that the relationship has a plain-language reason and remains distinct from nearby files.
6. Open `53538976.jpg`, rename it to `Elarian Architecture Diagram.jpg`, and confirm the `.jpg` extension and name update across Search, Files, details, relationships, and Organize.
7. Move `Architecture.pdf` to Projects › Elarian, confirm the preview once, and verify that its breadcrumb and referenced plan change reconcile.
8. Open Files, enter one major branch at a time, use breadcrumbs to return, and open a bounded aggregate preview.
9. Open Organize and confirm that the direct move reduced the reset fixture’s 38 changes to 37 while preserving five destinations, two review items, and one blocking conflict.
10. Resolve or explicitly accept either `Elarian logo.svg` or `final.pdf`; confirm that only the relevant review count and group change.
11. Resolve the `Notes.docx` destination collision and confirm Apply remains unavailable until both Tier 2 items are also resolved or accepted.
12. Resolve the remaining review gate, run the short recheck, inspect the concise confirmation, and choose Apply.
13. Confirm that **Applying** and **Verifying** appear as separate visible and announced stages.
14. Confirm **Applied and verified**, then verify that Home no longer shows the active plan and Activity contains the new operation.
15. In Duplicates, choose a working copy for the ambiguous Japan itinerary group and inspect the recommended Lumina manual working copy.
16. Inspect the changed Website brief recheck and understand: “This file changed and is no longer an exact copy.”
17. Open a recent Activity operation and read its verification result and bounded change list.
18. Find the short-term undo failsafe on the recent organization operation and the limited return action on the consolidated-copy operation.
19. Open the older archive operation, read **Undo is no longer available**, and see the recommendation to prepare a new organization plan.
20. Change any notification toggle, navigate away and back, reload, and confirm the fixture setting persists while Home remains authoritative.
21. Open Settings → Locked rules, inspect the CS1010X/CS1010S match preview and the destination Study › CS1010S.
22. Open Settings → Advanced, reset the prototype, and confirm onboarding plus the exact initial counts, names, decisions, activities, and settings are restored.

Also exercise keyboard-only navigation, visible focus, Escape and focus restoration for dialogs, the Applying/Verifying live announcement, and narrow-laptop reflow. A manual usability review is still required before UX 1.1 Pass 2; implementation does not constitute approval.

## Intentionally lightweight and deferred visuals

The Home folder map, focused Files hierarchy, and annotated planned structure use bounded connected blocks, indentation, counts, and collapsed aggregate nodes. They are sufficient to test orientation and hierarchy without implying a complete filesystem or conceptual distance model.

Pass 1 intentionally excludes dense relationship networks, a full Finder replacement, force-directed layout, zoom, pan, drag-and-drop, animated file routing, and complete dynamic filesystem trees. Safe previews are convincing fixture placeholders rather than rendered user documents.
