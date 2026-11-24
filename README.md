# bootAI

`bootAI` is a cautious local file organizer for scanning folders, reviewing metadata, and applying explicitly approved move plans.

The project is safety-first: dry-run is default, real movement requires exact confirmation, the tool never permanently deletes files, the tool never overwrites files, and all real movement goes through approved move plans and operation logs.

## Current Status

The accepted Loci Flow integration is complete through 1.0F. The primary local
web application has six areas: **Home**, **Organize**, **Duplicate Bin**,
**Activity**, **Standards**, and **Settings**. Home summarizes the selected root
and recent verified work; Organize owns scan results, recommendations, choices,
approval, and ordinary movement. Duplicate Bin is reversible protected storage,
not Trash. Activity shows only real filesystem movement, restore, failure, and
Undo records. Standards are loaded fail-closed once per scan and filter forbidden
changes before suggestions are displayed. The authoritative Standards page lists
the durable v2 catalog, supports activate/deactivate, and provides guided creation
for keeping a folder unchanged or keeping matching course-code or project-code
files together. Settings exposes only currently implemented configuration.

The current safe workflow is:

```text
authenticated root-locked launch
  -> deterministic scan
  -> recommendations and choices
  -> revision-bound decisions and approval
  -> exact-confirmed movement or Duplicate Bin movement
  -> durable intent and executor-owned movement
  -> verification
  -> Activity and exact-confirmed Undo or Restore
```

The tool can currently:

- Scan folders read-only.
- Detect exact duplicates with SHA-256.
- Build duplicate review plans.
- Apply approved duplicate review moves with undo logs.
- Detect review candidates.
- Group document-like files deterministically.
- Load saved organization rules fail-closed and bind them immutably to one scan.
- Report alias-normalized anchor decisions for suggested groups, anchors needing a user decision, and ignored terms.
- Infer existing organization patterns for reports without writing rules.
- Export inferred organization rule candidates for manual review.
- Apply accepted organization rule decisions only after exact confirmation.
- Audit how accepted organization rules affect report output.
- Export rule-aware organization suggestions as a JSON batch-review file.
- Apply approved organization-review rows through the existing executor after exact confirmation.
- Verify an organization-review apply result against its operation log and current filesystem state.
- Suggest organization plans.
- Flag isolated code files as candidates for review.
- Optionally refine organization suggestions with local Ollama.
- Apply approved organization plans with undo logs.
- Undo logged move operations.
- Generate read-only JSON reports for manual review or external scheduler runs.
- Generate static HTML report viewers from the same report data.
- Review duplicate, organization, and review-candidate move candidates in a batch CLI session.
- Detect reviewed-plan conflicts before approved batch apply.
- Remember prior batch-review decisions as review state.
- Apply saved reviewed-plan JSON files after validation and exact confirmation.
- Resume saved reviewed-plan sessions, edit decisions, and save a new collision-safe revision.
- Filter, sort, and paginate review-session rows without changing decisions or saved-plan contents.
- Change decisions for the current displayed page only after an exact decision confirmation.
- Track unsaved review decisions locally, require exact confirmation before discarding them, and present grouped help and clearer conflict summaries.
- Coordinate scan-report construction and review-session creation or resume through UI-independent application services.
- List and load validated scan reports and reviewed plans through a root-contained artifact boundary.
- Launch a single-worker local web shell for one immutable validated root.
- Establish one browser session through a single-use launch URL, then redirect to a clean authenticated page.
- Serve locally bundled HTMX and Bootstrap assets with strict Host and response-header controls.
- Scan and reload real organization state without rescanning during navigation.
- Review recommended destinations and exact-duplicate choices using server-owned IDs.
- Approve a complete revision-bound organization plan and inspect its movement tree.
- Preview and exactly confirm ordinary movement, then verify the result.
- Recover durable movement state after restart or a lost browser response.
- Inspect durable Activity and exactly confirm ordinary Undo.
- Approve, separately confirm, and verify movement into Duplicate Bin.
- Restore selected Duplicate Bin copies to their exact previous paths.
- Use the same authoritative restore service from Activity Undo and Duplicate Bin Restore.
- Start a new organization cycle after completed movement or restore.
- Load Standards fail-closed and bind one immutable Standards state to each scan.
- Read and mutate a revision-protected durable v2 Standards catalog, migrating a
  valid v1 file on the first mutation.
- List authoritative Standards and activate or deactivate them without moving files.
- Preview and create deterministic Standards for keeping a folder unchanged or
  keeping matching course-code or project-code files together.
- Require a fresh Check before changed Standards affect organization planning and
  block Standards mutation while an approved plan is active.

## Interface Direction

The local web application is the intended primary interface for ordinary users.
It uses a production backend/API → data source → page-specific view model →
accepted renderer pipeline. Browser mutations are authenticated POSTs protected
by CSRF, exact same-origin checks, immutable root configuration, scan generation,
and decision revision. Routes call application services and never import
`executor.py`; `executor.py` remains the sole user-file movement implementation.

The next binding stage is **1.0G — Release Launcher and Packaging Contract
Inspection**. It is inspection only: it must resolve native folder-selection
ownership, root validation before server creation, launch-token and browser
bootstrap handling, clean shutdown, packaged process lifecycle, the macOS
packaging approach, safe terminal hiding, failure and cancellation behavior, and
packaging dependency constraints before any launcher implementation is approved.
Accessibility auditing, large-folder performance testing, and nontechnical-user
testing remain separate later release-hardening stages.

See the [local web architecture contract](docs/WEB_ARCHITECTURE.md) and [web threat model](docs/WEB_THREAT_MODEL.md) for the frozen Stage 11 requirements.

## Setup

Use Python 3.13 or newer. Core CLI installation has no mandatory third-party runtime dependencies. Install the exact optional web and web-test dependencies for the local shell and full suite:

```bash
python3 -m pip install -e ".[web,web-test]"
```

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Quickstart

Run commands with `PYTHONPATH=src` from the repository root:

Launch Loci Flow for one locked folder:

```bash
PYTHONPATH=src python3 -m organizer.web --root "$HOME/Downloads"
PYTHONPATH=src python3 -m organizer.web --root "$HOME/Downloads" --no-browser
```

The launcher binds only to `127.0.0.1`, selects a dynamic port unless `--port` is supplied, and uses a private single-use launch URL. The authenticated application keeps that validated root immutable for the process lifetime.

Existing CLI commands remain unchanged:

```bash
PYTHONPATH=src python3 -m organizer.cli /path/to/folder
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --duplicates
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --plan-organization
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --report
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --html-report
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --review-plans
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --review-plans --ignore-review-state
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --export-rule-candidates
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --export-organization-review
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --apply-organization-review AI_Review/reviews/organization_review.approved.json --confirm "APPLY ORGANIZATION REVIEW"
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --verify-organization-apply AI_Review/reviews/organization_review_apply_result.json
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --resume-reviewed-plan AI_Review/review_sessions/reviewed_plan.json
```

Apply commands require exact confirmation:

```bash
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --apply-organization-plan
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --apply-organization-plan --confirm APPLY_ORGANIZATION_PLAN
```

Undo uses an operation log path printed by an apply command:

```bash
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --undo-log /path/to/folder/AI_Review/operation_logs/operation_log_YYYYMMDD.json
```

## CLI Examples

Read-only and dry-run commands:

```bash
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --max-depth 2
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --review-candidates
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --project-groups
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --plan-review-candidates
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --plan-duplicates
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --report
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --report --report-output /path/to/folder/AI_Review/reports/manual_report.json
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --html-report
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --html-report --html-report-output /path/to/folder/AI_Review/reports/manual_report.html
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --review-plans
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --export-rule-candidates
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --export-organization-review
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --export-organization-review --organization-review-output AI_Review/reviews/manual_review.json
```

Approved move commands:

```bash
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --apply-duplicate-plan --confirm APPLY_DUPLICATE_PLAN
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --apply-organization-plan --confirm APPLY_ORGANIZATION_PLAN
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --apply-reviewed-plan AI_Review/review_sessions/reviewed_plan.json --confirm APPLY_REVIEWED_PLAN
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --apply-rule-decisions AI_Review/rules/organization_rule_candidates.reviewed.json --confirm "APPLY ORGANIZATION RULES"
```

Local Ollama refinement:

```bash
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --refine-groups --llm-provider ollama --llm-model qwen2.5:7b
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --plan-refined-organization --llm-provider ollama --llm-model qwen2.5:7b
PYTHONPATH=src python3 -m organizer.cli /path/to/folder --apply-refined-organization-plan --llm-provider ollama --llm-model qwen2.5:7b --confirm APPLY_REFINED_ORGANIZATION_PLAN
```

## Safety Notes

- Use a small disposable folder for manual testing.
- Inspect dry-run output before using exact confirmation.
- Keep operation logs when testing undo.
- Do not run apply commands on important folders while testing.
- Report mode writes a JSON report file but does not move scanned files.
- HTML report mode writes JSON and HTML report files but does not move scanned files.
- HTML reports do not approve moves, apply moves, perform review actions, write operation logs, or start a server.
- Normal organization suggestions are document-only by default.
- Code/project files are excluded from normal organization suggestions.
- Isolated code files may appear as `orphan_code` candidates for review.
- Protected-context files can appear in factual reports but are excluded from actionable move plans by default.
- Generated web/archive assets and contextual project-output files are excluded from actionable move plans by default.
- Organization suggestions require narrow repeated document-set evidence or an explicit locked anchor.
- Broad anchors such as course codes, project names, personal names, and organization-like terms are non-actionable by default.
- Organization rules are optional and read-only; create `AI_Review/config/organization_rules.json` manually if you need locked anchors, ignored terms, or aliases.
- Locked anchors still require at least two eligible safe files and do not bypass protected/generated/project-output exclusions.
- Reports show anchor decisions after alias normalization: suggested narrow groups, broad anchors needing a decision, and ignored terms.
- Reports show inferred organization patterns as weak preference evidence only.
- Compound folders such as `cs1010x finals/` or `EvoSim images/` can provide report-only evidence.
- Inferred rule candidates are suggestions for manual review; they are not written automatically.
- Rule candidate export writes review JSON only and does not create `organization_rules.json`.
- Rule decisions update `organization_rules.json` only through `--apply-rule-decisions` with exact `APPLY ORGANIZATION RULES` confirmation.
- Accepted organization rules do not move files and do not write operation logs.
- Reports include a rule-aware audit when organization rules exist and explain when rules are missing or invalid.
- Organization-review export writes editable review JSON only. It cannot apply approved rows and does not write operation logs.
- Organization-review apply reads only an explicitly supplied reviewed file, requires exact confirmation, and moves only `approve` rows through `executor.py`.
- Review mode approve/reject/save commands do not move files.
- Review mode stores decision memory under `AI_Review/review_state/review_decisions.json`.
- Review state is decision memory, not an operation log, and does not record filesystem success.
- Review mode applies approved moves only after exact `APPLY_REVIEWED_PLAN` confirmation.
- Review-candidate rows are candidates for review and use `R` IDs in batch review.
- Review mode blocks apply when one source or destination has multiple approved moves.
- Saved reviewed plans are validated as untrusted input before approved moves are applied.
- Resumed reviewed plans keep their explicit decisions authoritative and do not load review-state memory.
- Operation logs remain authoritative for actual successful moves and undo.
- Post-apply verification compares the apply summary, operation log, and filesystem without moving files.
- `executor.py` is the only module that performs real movement.
- Review, grouping, and LLM modules produce facts or suggestions; they do not execute moves.
- Loci Flow exposes no arbitrary file-serving route and never accepts source, destination, root, or operation-log paths from the browser.
- Its signed session cookie is integrity-protected, not confidential; it contains only authentication state and opaque session/CSRF values.

Current limitations are explicit: there is no Search, general Files browser,
permanent deletion, Empty Bin, system Trash handoff, multi-root execution,
root switching within a running process, scheduler daemon, notifications,
natural-language Standards interpretation, automatic LLM execution, or
speculative Settings workflow. Duplicate Bin remains reversible protected
storage, and occupied exact restore destinations block restoration rather than
being overwritten. Standards expose guided deterministic controls rather than
raw JSON, filesystem paths, regex, or LLM-dependent interpretation.

## Documentation

- [Documentation index](docs/README.md)
- [CLI reference](docs/CLI_REFERENCE.md)
- [Report format](docs/REPORT_FORMAT.md)
- [Sample report](docs/examples/sample_report.json)
- [Manual testing guide](docs/MANUAL_TESTING.md)
- [Release notes](docs/RELEASE_NOTES.md)
- [Safety constitution](docs/SAFETY.md)
- [Local web architecture contract](docs/WEB_ARCHITECTURE.md)
- [Local web threat model](docs/WEB_THREAT_MODEL.md)
- [Architecture decisions](docs/adr/0001-local-web-stack.md)
- [Application-service decision](docs/adr/0003-application-service-layer.md)
