# Safety Constitution

## Invariants

- Never permanently delete files.
- Never overwrite files.
- Never move outside the selected scan root.
- Always validate paths against the selected scan root.
- Dry-run is the default behavior.
- Exact explicit confirmation is required for apply behavior.
- Every successful move requires an operation log.
- Undo support is required for real movement.
- Direct symlink sources are rejected for moving.
- Unsafe symlink destination parents are rejected.
- Deterministic Python is the source of truth for facts.
- LLM output is advisory only.
- Full file contents are not sent to the LLM by default.
- Planner, review, grouping, and LLM modules must not move files.
- Normal organization suggestions must remain conservative and document-like by default.
- Code/project files must not be broadly organized by default.
- Orphan code is a candidate for review only.
- Report generation must not move files and may only create a new report file.
- HTML report generation must not move files and may only create JSON/HTML report files.
- HTML reports must not approve moves, apply moves, perform review actions, write operation logs, or start a server.
- Review approve/reject/save commands must not move files.
- Review-candidate rows are candidates for review and must not be described as disposal targets.
- Review state records human decisions only.
- Review state is not an operation log and must not be used for undo.
- Remembered decisions must not bypass exact confirmation.
- Failed moves must not become success records in review state.
- Reviewed-plan JSON files are not operation logs.
- A source or destination path must not have multiple approved reviewed-plan moves.
- Reviewed-plan conflicts must block apply until the user rejects conflicting approved rows.
- Saved reviewed-plan JSON files are untrusted input and must be validated before use.
- Resumed reviewed-plan decisions are authoritative and must not be replaced by review-state memory.
- Resume, decision editing, and saving must not scan, regenerate plans, or move files.
- Resumed reviewed-plan saves must not overwrite their input file.
- Review filters, sort order, and pagination are temporary display state only.
- View state must not change decisions, remove hidden rows from saved output, or target rows by page position.
- Bulk page decisions must preview exact stable IDs and require their decision-specific typed confirmation.
- Bulk page decisions affect the current displayed page only and must not save, apply, move files, or target hidden/off-page rows.
- Unsaved review-decision tracking is session-local and must not be serialized or treated as filesystem success.
- Quitting with unsaved review decisions requires exact `QUIT WITHOUT SAVING` confirmation; cancelling preserves decisions and moves no files.
- Rejected saved reviewed-plan items must be ignored for movement.
- Protected-context files are not actionable move candidates by default.
- Generated web/archive assets are not actionable move candidates by default.
- Organization suggestions require strong grouping evidence, not confidence alone.
- Broad course/name/project/organization anchors are non-actionable by default.
- Concrete organization suggestions require narrow repeated document-set evidence or an explicit locked anchor.
- Organization rules are read-only in Stage 10.4.4.
- Existing organization pattern inference is report-only and must not write organization rules.
- Existing folder patterns must not create `MovePlanItem` values directly or approve broad organization.
- Inferred rule candidates are advisory until manually reviewed.
- Rule decisions must not write `organization_rules.json` without exact `APPLY ORGANIZATION RULES` confirmation.
- Rule-review config updates must not create `MovePlanItem` values, call `executor.py`, move files, or write movement operation logs.
- `preferred_granularities` are advisory only in Stage 10.6.
- Rule-aware audit is read-only and must not write or modify organization rules.
- Rule-aware audit must not create movement-plan items, call `executor.py`, move files, or write operation logs.
- Preferred granularities remain advisory and non-behavior-changing in Stage 10.7.
- Organization-review export may write a new JSON review file but must not create execution-ready movement plans or apply reviewed rows.
- Organization-review destinations must be safe relative paths under the controlled `Organized/` output namespace; they do not need to exist during review.
- Organization-review decisions remain review metadata and must not bypass a later separately designed confirmation and validation stage.
- Organization-review apply requires exact `APPLY ORGANIZATION REVIEW` confirmation before the review path is read or validated.
- Only approved organization-review rows may become `MovePlanItem` values; rejected and undecided rows remain summary metadata.
- Duplicate approved sources or destinations must block organization-review apply before executor use.
- Organization-review apply-result files do not replace executor operation logs; undo continues to use operation logs.
- Organization apply verification reports are audit records, not operation logs, and cannot trigger movement or undo.
- Verification inputs must be regular non-symlink files under the selected root.
- Locked anchors must not bypass protected/generated/project-output exclusions.
- Locked anchors should require at least two eligible safe files before producing organization suggestions.
- Ignored terms win over locked anchors after alias normalization.
- Exact duplicate facts must remain distinct from duplicate move candidates.
- Batch review apply requires exact `APPLY_REVIEWED_PLAN` confirmation.
- `executor.py` is the only movement module.
- Approved duplicate, organization, and review-candidate moves all use `executor.py`.
- Organization apply requires exact confirmation and keeps dry-run planning as the default.

## Current Loci Flow 1.0F Protections

- One process is locked to one resolved, validated root; browser requests cannot change it.
- Root escape, direct symlink sources, unsafe symlink parents, and destinations outside the root are rejected.
- Existing destinations are never overwritten. Restore to an occupied previous path is blocked.
- Standards loading fails closed. Missing storage means no saved user Standards;
  malformed, unsupported, or unavailable storage cannot silently fall back to a
  different saved configuration.
- One scan owns one immutable Standards load result. Grouping, reporting,
  organization projection, review, decisions, and approval use that scan-bound
  state without reloading Standards from disk.
- Active Standards filter forbidden organization changes before remaining recommendations are displayed.
- The durable v2 Standards catalog is authoritative. Valid v1 storage is migrated
  only on the first mutation; built-in protections remain distinct from saved
  Standards.
- Standards list, activate, deactivate, preview, and creation mutations require
  authenticated POST, CSRF, exact same-origin validation, a current catalog
  revision, and conflict rejection.
- Guided creation is limited to keeping a folder unchanged and keeping matching
  course-code or project-code files together. The browser receives no raw JSON,
  filesystem paths, regex, or LLM-dependent interpretation controls.
- Standards mutation is blocked while an approved organization plan is active.
  A successful change requires a fresh Check before the new catalog can affect
  organization planning.
- Standards mutations create no Activity records, call no movement service, and
  never import or invoke `executor.py`.
- Organization mutations validate the immutable scan generation and expected decision revision.
- Approval is required before an ordinary movement authorization can be issued.
- Ordinary movement and Duplicate Bin movement each require a separate exact confirmation.
- Durable operation intent is written before any executor call.
- `executor.py` remains the sole implementation that moves or restores user files.
- Movement, ordinary Undo, and Duplicate Bin restore outcomes are verified against current filesystem state.
- Durable terminal results are replayed without automatically repeating movement after restart or a lost response.
- Activity is projected only from durable real movement, restore, failure, and Undo records.
- Ordinary Undo uses a one-use server-owned authorization, the shared execution coordinator, and post-Undo verification.
- Duplicate Bin approval claims every selected and retained group member, and the complete group is rehashed before movement.
- The scanner prunes the protected Duplicate Bin subtree so stored copies cannot re-enter normal organization planning.
- Duplicate Bin Restore and Duplicate Bin Activity Undo share one restore service and restore only selected copies to exact previous paths.
- Fully restored historical duplicate batches are reconciled once on restart and release their active claims without deleting history.
- Duplicate Bin is reversible protected storage. There is no permanent deletion, Empty Bin, or system Trash handoff.
- Current behavior requires no LLM. LLM output remains advisory and cannot approve or execute an action.

## Release Launcher Inspection Boundary

The next binding stage, 1.0G, is inspection only. Before any packaged launcher
implementation is approved, it must establish ownership and fail-closed behavior
for native folder selection, root validation before server creation, launch-token
handling, browser bootstrap, clean shutdown, packaged process lifecycle, macOS
packaging, terminal visibility, cancellation and failures, and packaging
dependencies.

The inspection must preserve one immutable validated root per running process,
loopback-only binding, single-use launch authentication, and current route and
movement boundaries. It does not authorize root switching, multi-root operation,
Windows packaging, a scheduler daemon, notifications, natural-language Standards
interpretation, speculative Settings, or product workflow redesign. Accessibility
auditing, large-folder performance, and nontechnical-user testing remain separate
later release-hardening stages.

## Local Web Invariants

- The browser, all browser-provided values, and all loaded JSON artifacts are untrusted.
- Each web-server process must use one resolved, validated, immutable root.
- The browser must not change the root or submit arbitrary source or destination paths.
- Browser actions use stable session-scoped IDs that the backend maps to server-held data and revalidates.
- The server binds to `127.0.0.1` with one worker and must not default to `0.0.0.0`.
- A one-time launch token establishes a signed, host-only, browser-lifetime session cookie with `HttpOnly` and `SameSite=Strict`.
- Launch-token comparison and consumption are atomic; wrong, blank, and replayed tokens receive the same generic failure. A success clears any prior session and redirects away from the secret URL.
- Signed session data is integrity-protected but not confidential. Store no root or launch token in it, and persist no session secret.
- Every state-changing request uses POST and requires a session-bound CSRF token and exact same-origin validation. Organization mutations also require the current scan generation and expected decision revision; movement and restore use one-use server-owned authorizations.
- Origin validation fails closed and requires one exact loopback origin matching scheme, host, and port. Missing or malformed Origin is not accepted.
- GET requests are read-only. Refreshing a result must not repeat a mutation.
- Trusted Host validation rejects unexpected hosts, CORS is not enabled, and frontend assets are bundled locally.
- Session replacement is serialized, exact-set bulk previews are bound to the browser session and scan generation, and stale organization revisions are rejected rather than merged.
- One root-bound process permits one active scan job and one active execution or restore operation at a time.
- Web routes must not import `executor.py`; only the execution application service may delegate to existing executor functions.
- Web movement and restore are available only through their authenticated application-service boundaries, fresh validation, exact confirmation, shared coordinator, durable intent, and verification.
- An arbitrary path-based file-serving endpoint is forbidden.
- Accessibility targets WCAG 2.2 AA from the first web screen.
- Stage 11.5 review mutations are POST-only and require authentication, CSRF, Origin, current generation, stable IDs or a one-use opaque preview token, and POST/redirect/GET. GET remains read-only.
- Web decisions map Organize to `approved`, Keep here to `rejected`, and Review later to `undecided`. Current-page actions affect only the exact frozen page IDs after typed confirmation.
- Reviewed-plan saving is explicit, collision-safe, includes hidden and off-page rows, and never moves files. There is no autosave or browser-selected output path.
- Unsaved decisions block a replacement scan with `409 Conflict`; they are never silently discarded or assigned to a later generation.
- Movement, Undo, and Restore controls are rendered only when authoritative production state says the operation is currently eligible.
- Consumer cards consolidate repeated source findings for presentation only. Duplicate findings take precedence, then organization, then needs attention; secondary rows, decisions, conflicts, and saved output remain intact.
- A consumer card action changes only its primary row. It must not silently change secondary module choices.
- Consumer surfaces are strict enums mapped to fixed routes, never browser-provided return URLs or paths.
- Internal navigation preserves server-held dirty choices and must not show a global leave-site warning. Dirty replacement scans remain blocked by `409`.
- Fresh scan-generated web rows with new or stale memory state begin `undecided`; untouched findings must never become approved merely by saving a module.
- CLI batch review (`--review-plans`) starts every new or stale row `undecided` as well; a remembered rejection whose file changed returns to `undecided`, never `approved`, and saving persists only decisions the user made.
- The executor refuses a batch whose destinations collide, including names differing only in letter case or Unicode form, and creates each destination exclusively so a move never replaces an existing file. The operation log is rewritten after every move so a batch that stops partway still has a log for undo.
- `--apply-organization-review` refuses approved rows whose source is a protected path such as `.git/` or `AI_Review/`.
- Module saves include every row in the selected fixed category, advance only that module's saved baseline, and pass rows through existing review-state eligibility rules.
- Guided handled/skipped state is generation-bound memory only. Skipped rows retain the existing `undecided` serialized value.
- A replacement scan remains blocked while any module is dirty and must identify each affected module.

The complete controls and threat mappings are in [WEB_THREAT_MODEL](WEB_THREAT_MODEL.md).

## Storage-Recovery Claims

Moving a file to another folder on the same filesystem does not save storage space. The primary consumer interface uses `Space used by extra copies` or `Potential duplicate space`. Technical interfaces may report `Potential duplicate bytes` or `Duplicate candidates for review`.

Do not report `Space saved` or `Storage recovered` unless an operation actually removes data from that filesystem or transfers it elsewhere. Loci Flow through 1.0F adds no permanent removal, Trash integration, or automatic disposal. The existing reversible-move, operation-history, and restore requirements remain in force.

## Risk Categories

- Exact duplicates: files with matching SHA-256 hashes.
- Empty files: candidate for review unless they are known placeholders.
- Temporary files: candidate for review when filename patterns match system or temporary artifacts.
- Backup or copy files: candidate for review when filename tokens indicate version markers.
- Project organization candidates: suggested groups based on deterministic course-code or token signals.
- Orphan code: isolated code files outside detected project contexts, surfaced only as candidates for review.
- Protected contexts: dependency folders, Git internals, virtual environments, app/framework bundles, protected workspaces, and project/package contexts excluded from actionable plans.
- Generated asset contexts: browser-saved asset folders, generated web assets, and contextual project-output folders excluded from actionable plans.

## Language Rules

Use cautious language: `candidate for review`, `suggested`, `dry-run`, `approved move`, and `operation log`. Avoid claims that imply disposal, automatic action, or certainty beyond the deterministic facts.

Do not use these phrases except when listing forbidden wording:

- `safe to delete`
- `useless`
- `automatic cleanup`
- `permanent cleanup`
