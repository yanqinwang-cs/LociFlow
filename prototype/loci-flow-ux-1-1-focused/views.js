const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const slug = (value) =>
  String(value || "")
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");

const icon = (name, label = "") =>
  `<svg class="icon" ${label ? `role="img" aria-label="${escapeHtml(label)}"` : 'aria-hidden="true"'}><use href="#icon-${escapeHtml(name)}"></use></svg>`;

const statusLabel = (status) => {
  const text = String(status || "Ready");
  return `<span class="row-status status-label" data-status="${escapeHtml(slug(text))}">${escapeHtml(text)}</span>`;
};

const button = (
  label,
  action,
  { variant = "secondary", attrs = "", iconName = "", disabled = false, focusKey = "" } = {},
) =>
  `<button class="button button-${escapeHtml(variant)}" type="button" data-action="${escapeHtml(action)}" data-focus-key="${escapeHtml(focusKey || action)}" ${attrs} ${disabled ? "disabled" : ""}>${iconName ? icon(iconName) : ""}<span>${escapeHtml(label)}</span></button>`;

function pageHeader(title, description = "", actions = "") {
  return `<header class="page-header"><div><h1>${escapeHtml(title)}</h1>${description ? `<p>${escapeHtml(description)}</p>` : ""}</div>${actions ? `<div class="page-actions">${actions}</div>` : ""}</header>`;
}

function commandRow({ iconName, title, summary, stats = [], action, actionLabel, status = "" }) {
  return `<section class="command-row" data-component="command-row">
    <span class="command-icon" aria-hidden="true">${icon(iconName)}</span>
    <div class="command-copy"><div class="command-title"><h2>${escapeHtml(title)}</h2>${status ? statusLabel(status) : ""}</div><p>${escapeHtml(summary)}</p>${stats.length ? `<div class="command-stats">${stats.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}</div>
    <div class="command-action">${button(actionLabel, action, { variant: action === "open-workflow" ? "primary" : "secondary", attrs: title === "Organization" ? 'data-workflow="organization"' : title === "Duplicates" ? 'data-workflow="duplicates"' : "", focusKey: `home-${slug(title)}-${action}` })}</div>
  </section>`;
}

function renderHome(store) {
  const state = store.getState();
  const summary = store.planSummary();
  const recent = state.activity[0];
  const active = state.workflow.active;
  const planExists = state.plan.status === "ready";
  const organizationLabel = active === "organization" ? "Resume" : planExists ? "Review" : "Prepare";
  const organizationAction = planExists ? "open-workflow" : "prepare-plan";
  const duplicateCount = state.duplicates.activeGroupIds.length;
  const duplicateLabel = active === "duplicates" ? "Resume" : "Review";
  const workflowCopy = active === "none" ? "No workflow is open." : active === "organization" ? "Organization plan in progress." : active === "duplicates" ? "Duplicate review in progress." : "A sample operation is in progress.";

  return {
    title: "Home",
    html: `${pageHeader("Home", "Downloads is ready for organization planning.")}
      <div class="home-layout">
        <div class="home-main">
          <section class="panel command-list" aria-label="Planning commands">
            ${commandRow({ iconName: "folder", title: "Downloads", summary: state.location.lastCheckedLabel, stats: [state.location.statusLabel], action: "check-files", actionLabel: "Check", status: state.location.statusLabel })}
            ${commandRow({ iconName: "organize", title: "Organization", summary: planExists ? `${summary.changes} proposed changes across ${summary.destinations} destinations.` : "No active plan.", stats: planExists ? [`${summary.review} review`, `${summary.conflicts} conflict`] : [], action: organizationAction, actionLabel: organizationLabel, status: active === "organization" ? "In progress" : planExists ? "Ready" : "None" })}
            ${commandRow({ iconName: "duplicates", title: "Duplicates", summary: `${duplicateCount} exact-copy ${duplicateCount === 1 ? "group" : "groups"} ready.`, stats: ["Reviewed separately from organization"], action: "open-workflow", actionLabel: duplicateLabel, status: active === "duplicates" ? "In progress" : duplicateCount ? "Ready" : "Complete" })}
            ${commandRow({ iconName: "activity", title: "Recent", summary: `${recent.title} · ${recent.summary}`, stats: [recent.status], action: "view-activity", actionLabel: "View", status: recent.status })}
          </section>
        </div>
        <aside class="home-aside">
          <section class="panel recent-operation" aria-labelledby="workflow-heading"><p class="eyebrow">Current session</p><h2 id="workflow-heading">${escapeHtml(workflowCopy)}</h2><p>Loci Flow works alongside Finder or File Explorer. It prepares sample plans; ordinary file management stays in the native file manager.</p>${active === "organization" || active === "duplicates" ? button("Resume workflow", "open-workflow", { variant: "primary", attrs: `data-workflow="${escapeHtml(active)}"` }) : ""}</section>
          <section class="notice-panel" data-status="ready"><strong>Fixture only</strong><p>Every action changes browser-local sample state. Confirmation and verification are simulated.</p></section>
        </aside>
      </div>`,
  };
}

function filteredPlanChanges(store) {
  const state = store.getState();
  const changes = store.includedChanges();
  if (state.plan.issueFilter === "review") return changes.filter((change) => change.status === "Review");
  if (state.plan.issueFilter === "conflict") return changes.filter((change) => change.status === "Conflict");
  if (state.plan.issueFilter === "issues") return changes.filter((change) => ["Review", "Conflict"].includes(change.status));
  return changes;
}

function fileRow(store, change, context) {
  const state = store.getState();
  const file = store.fileById(change.fileId);
  const selected = state.selection.planChangeIds.includes(change.id);
  const focused = state.selection.inspectorItemId === change.id;
  const updated = state.plan.recentlyChangedIds.includes(change.id);
  const planned = context === "planned";
  const path = planned ? store.plannedPath(change.id) : store.filePath(file.id);
  return `<li class="file-row resource-row ${focused ? "is-selected" : ""}" data-selected="${focused}" data-component="file-row" data-status="${escapeHtml(slug(change.status))}" data-file-id="${escapeHtml(file.id)}" data-change-id="${escapeHtml(change.id)}" data-updated="${updated}">
    ${planned ? `<span class="row-check"><input type="checkbox" data-action="toggle-plan-selection" data-change-id="${escapeHtml(change.id)}" data-focus-key="plan-select-${escapeHtml(change.id)}" aria-label="Select ${escapeHtml(file.name)}" ${selected ? "checked" : ""}></span>` : `<span class="row-check" aria-hidden="true"></span>`}
    <span class="row-icon" aria-hidden="true">${icon("file")}</span>
    <button class="row-copy" type="button" data-action="focus-plan-change" data-change-id="${escapeHtml(change.id)}" data-focus-key="plan-${escapeHtml(context)}-${escapeHtml(change.id)}"><span class="row-name">${escapeHtml(planned ? store.plannedFileName(change) : file.name)}</span><span class="row-meta"><span class="row-path">${escapeHtml(path)}</span><span>${escapeHtml(file.kind)}</span><span>${escapeHtml(file.sizeLabel)}</span></span></button>
    <span class="row-trailing">${statusLabel(change.status)}</span>
  </li>`;
}

function plannedGroup(store, group, visibleIds) {
  const state = store.getState();
  const expanded = state.plan.expandedGroupIds.includes(group.id);
  const changes = group.changeIds.map(store.changeById).filter((change) => change?.included !== false && visibleIds.has(change.id));
  if (!changes.length) return "";
  const focused = state.selection.inspectorItemId === group.id;
  return `<li class="tree-branch"><div class="folder-row tree-row ${focused ? "is-selected" : ""}" data-kind="folder" data-selected="${focused}" data-expanded="${expanded}">
      <button class="row-disclosure" type="button" data-action="toggle-plan-group" data-group-id="${escapeHtml(group.id)}" data-focus-key="plan-disclosure-${escapeHtml(group.id)}" aria-expanded="${expanded}" aria-label="${expanded ? "Collapse" : "Expand"} ${escapeHtml(group.label)}" title="${expanded ? "Collapse" : "Expand"} ${escapeHtml(group.label)}">${icon("chevron")}</button>
      <span class="row-icon" aria-hidden="true">${icon("folder")}</span>
      <button class="row-copy" type="button" data-action="focus-plan-group" data-group-id="${escapeHtml(group.id)}" data-focus-key="plan-group-${escapeHtml(group.id)}"><span class="row-name">${escapeHtml(group.label)}</span><span class="row-meta">${changes.length} planned ${changes.length === 1 ? "file" : "files"} · ${escapeHtml(store.folderPath(group.destinationFolderId))}</span></button>
      <span class="row-trailing">${statusLabel(changes.some((change) => change.status === "Conflict") ? "Conflict" : changes.some((change) => change.status === "Review") ? "Review" : "Ready")}</span>
    </div>${expanded ? `<ul class="tree-children">${changes.map((change) => fileRow(store, change, "planned")).join("")}</ul>` : ""}</li>`;
}

function planInspector(store) {
  const state = store.getState();
  const selectedId = state.selection.inspectorItemId;
  const group = store.groupById(selectedId);
  if (group) {
    const activeChanges = group.changeIds.map(store.changeById).filter((change) => change?.included !== false);
    return `<div class="inspector-body"><div class="inspector-heading"><span class="row-icon" aria-hidden="true">${icon("folder")}</span><div><p class="eyebrow">Planned folder</p><h3>${escapeHtml(group.label)}</h3></div></div><dl class="inspector-facts"><div><dt>Destination</dt><dd>${escapeHtml(store.folderPath(group.destinationFolderId))}</dd></div><div><dt>Files</dt><dd>${activeChanges.length}</dd></div><div><dt>Reason</dt><dd>${escapeHtml(group.reason)}</dd></div></dl><div class="inspector-actions">${button("Remove folder from plan", "confirm-remove-group", { variant: "danger", attrs: `data-group-id="${escapeHtml(group.id)}"` })}</div></div>`;
  }
  const change = store.changeById(selectedId);
  if (!change) return `<div class="inspector-empty"><div><span class="row-icon" aria-hidden="true">${icon("info")}</span><p>Select a planned file or folder to inspect it.</p></div></div>`;
  const file = store.fileById(change.fileId);
  const review = state.plan.reviewItems.find((item) => item.changeId === change.id && item.status === "unresolved");
  const conflict = state.plan.conflicts.find((item) => item.changeId === change.id && item.status === "unresolved");
  return `<div class="inspector-body"><div class="inspector-heading"><span class="row-icon" aria-hidden="true">${icon("file")}</span><div><p class="eyebrow">Selected file</p><h3>${escapeHtml(file.name)}</h3></div></div><p>${statusLabel(change.status)}</p><dl class="inspector-facts"><div><dt>Current</dt><dd>${escapeHtml(store.filePath(file.id))}</dd></div><div><dt>Planned</dt><dd>${escapeHtml(store.plannedPath(change.id))}</dd></div><div><dt>Reason</dt><dd>${escapeHtml((review || conflict)?.reason || change.reason)}</dd></div></dl><div class="inspector-actions">${review ? button("Accept suggestion", "accept-review", { variant: "primary", attrs: `data-review-id="${escapeHtml(review.id)}"` }) : ""}${conflict ? button("Keep both with rename", "resolve-conflict", { variant: "primary", attrs: `data-conflict-id="${escapeHtml(conflict.id)}"` }) : ""}${button("Move to…", "select-and-move", { attrs: `data-change-id="${escapeHtml(change.id)}"`, iconName: "move" })}${button("Leave unchanged", "select-and-leave", { variant: "quiet", attrs: `data-change-id="${escapeHtml(change.id)}"`, iconName: "leave" })}${button("Reveal in Finder", "reveal-plan-file", { variant: "quiet", attrs: `data-file-id="${escapeHtml(file.id)}"` })}</div></div>`;
}

function renderOrganize(store) {
  const state = store.getState();
  const unaffected = store.getFixture().aggregateRows.find((item) => item.id === "aggregate-other-files");
  if (state.plan.status !== "ready") {
    const message = state.plan.status === "applied"
      ? "The last sample plan was applied and verified."
      : state.plan.status === "undone"
        ? "The last applied sample plan was undone and verified. Prepare a fresh plan before applying again."
        : state.plan.status === "up-to-date"
          ? "The latest approved organization intent is already reflected in the sample files."
          : state.plan.status === "dismissed"
            ? "The sample plan was discarded."
            : "The last sample outcome needs another plan.";
    return { title: "Organize", html: `${pageHeader("Organize", "Compare current and planned organization.")}<section class="empty-state"><span aria-hidden="true">${icon("organize")}</span><h2>No active organization plan</h2><p>${escapeHtml(message)}</p>${button("Prepare plan", "prepare-plan", { variant: "primary", iconName: "refresh" })}</section>` };
  }
  const summary = store.planSummary();
  const visible = filteredPlanChanges(store);
  const visibleIds = new Set(visible.map((change) => change.id));
  const selectedCount = state.selection.planChangeIds.length;
  const feedback = state.workflow.feedback;
  const pane = state.selection.narrowPane;
  return {
    title: "Organize",
    html: `<section class="organize-shell" data-workflow="organization">
      ${pageHeader("Organize", "Compare the checked structure with the editable plan.", `<span class="status-chip" data-status="ready">${escapeHtml(state.location.statusLabel)}</span>`)}
      <div class="toolbar plan-toolbar" data-component="toolbar"><div class="toolbar-group"><strong class="toolbar-title">${escapeHtml(state.plan.title)}</strong><span class="status-label" data-status="${summary.review || summary.conflicts ? "review" : "ready"}">${summary.review + summary.conflicts} unresolved</span></div><div class="toolbar-actions">${state.plan.issueFilter !== "all" ? button("Show all", "clear-issue-filter", { variant: "quiet" }) : ""}${button("Check files", "check-files", { variant: "quiet", iconName: "refresh" })}${button(state.selection.inspectorOpen ? "Hide inspector" : "Inspector", "toggle-inspector", { variant: "quiet", iconName: "info", attrs: `data-has-selection="${Boolean(state.selection.inspectorItemId)}"` })}</div></div>
      <div class="pane-switcher" role="group" aria-label="Comparison pane"><button type="button" data-action="set-narrow-pane" data-pane="current" data-focus-key="pane-current" aria-pressed="${pane === "current"}">Current</button><button type="button" data-action="set-narrow-pane" data-pane="planned" data-focus-key="pane-planned" aria-pressed="${pane === "planned"}">Planned</button></div>
      <div class="toolbar selection-toolbar" ${selectedCount ? "" : "hidden"} data-component="selection-toolbar"><div class="toolbar-group"><strong>${selectedCount} selected</strong>${button("Clear", "clear-plan-selection", { variant: "quiet" })}</div><div class="toolbar-actions">${button("Move to…", "move-selected", { variant: "primary", iconName: "move" })}${button("Leave unchanged", "leave-selected", { iconName: "leave" })}${button("Remove from plan", "remove-selected", { variant: "quiet" })}</div></div>
      <div class="comparison-workspace" data-component="comparison-workspace" data-mobile-pane="${escapeHtml(pane)}" data-inspector-open="${state.selection.inspectorOpen}">
        <section class="comparison-pane current-pane plan-pane" aria-labelledby="current-heading"><header class="pane-header"><div><h2 id="current-heading">Current organization</h2><p>Latest checked state · read-only</p></div><span>${visible.length}</span></header><div class="pane-scroll"><ul class="outline-tree" aria-label="Current affected files"><li><div class="folder-row tree-row" data-kind="folder" data-expanded="true"><span class="row-disclosure" aria-hidden="true">${icon("chevron")}</span><span class="row-icon" aria-hidden="true">${icon("folder")}</span><span class="row-copy"><span class="row-name">Downloads</span><span class="row-meta">Affected files and nearest context</span></span><span class="row-trailing"></span></div><ul class="tree-children">${visible.map((change) => fileRow(store, change, "current")).join("")}<li class="aggregate-row resource-row" data-aggregate-id="${escapeHtml(unaffected.id)}"><span class="row-check"></span><span class="row-icon" aria-hidden="true">${icon("folder")}</span><span class="row-copy"><span class="row-name">${escapeHtml(unaffected.label)}</span><span class="row-meta">Not affected by this plan</span></span><span></span></li></ul></li></ul></div></section>
        <section class="comparison-pane planned-pane plan-pane" aria-labelledby="planned-heading"><header class="pane-header"><div><h2 id="planned-heading">Planned organization</h2><p>${escapeHtml(state.plan.previewNotice)}</p></div>${button("Select all visible", "select-all-visible", { variant: "quiet", attrs: `data-change-ids="${escapeHtml(visible.map((change) => change.id).join(","))}"` })}</header><div class="pane-scroll"><ul class="outline-tree" aria-label="Planned files; use the checkboxes to select more than one file">${store.activeGroups().map((group) => plannedGroup(store, group, visibleIds)).join("") || `<li class="empty-state"><p>No items match this focus.</p></li>`}</ul></div></section>
        <aside class="inspector-pane" data-open="${state.selection.inspectorOpen}" data-has-selection="${Boolean(state.selection.inspectorItemId)}" ${state.selection.inspectorOpen ? "" : "hidden"} aria-labelledby="inspector-heading"><header class="inspector-header"><div><h2 id="inspector-heading">Inspector</h2><p>Selection details and plan actions</p></div><button class="icon-button" type="button" data-action="toggle-inspector" data-focus-key="toggle-inspector" data-inspector-close aria-label="Close inspector" title="Close inspector">${icon("close")}</button></header>${planInspector(store)}</aside>
      </div>
      ${feedback ? `<div class="local-feedback" role="status" data-status="complete">${escapeHtml(feedback)}</div>` : ""}
      <footer class="workflow-footer" data-component="plan-bar"><div class="apply-summary"><strong>${summary.changes} changes</strong><span>${summary.destinations} destinations</span><span>${summary.review} review</span><span>${summary.conflicts} conflict</span></div><div class="toolbar-actions">${state.planUndo.snapshots.length ? button("Undo edit", "undo-plan-edit", { variant: "quiet", iconName: "undo" }) : ""}${button("Discard", "confirm-discard-plan", { variant: "quiet" })}${summary.review + summary.conflicts ? button("Show issues", "show-issues", { iconName: "review" }) : ""}${button("Apply", "attempt-apply", { variant: "primary", iconName: "verified" })}</div></footer>
    </section>`,
  };
}

function duplicateMemberRow(store, group, fileId) {
  const state = store.getState();
  const file = store.fileById(fileId);
  const working = group.workingCopyFileId === fileId;
  const selected = state.selection.duplicateExtraFileIds.includes(fileId);
  const focused = state.selection.inspectorItemId === fileId;
  const copyLabel = working
    ? group.recommendation === "chosen" ? "Chosen working copy" : "Recommended working copy"
    : "Extra copy";
  return `<li class="duplicate-member file-row ${focused ? "is-selected" : ""}" data-selected="${focused}" data-component="file-row" data-status="${working ? "working-copy" : "extra-copy"}">${working ? `<span class="row-check" aria-hidden="true">${icon("verified")}</span>` : group.workingCopyFileId ? `<span class="row-check"><input type="checkbox" data-action="toggle-duplicate-extra" data-file-id="${escapeHtml(fileId)}" data-focus-key="duplicate-extra-${escapeHtml(group.id)}-${escapeHtml(fileId)}" aria-label="Select extra copy ${escapeHtml(file.name)}" ${selected ? "checked" : ""}></span>` : `<span class="row-check"></span>`}<span class="row-icon" aria-hidden="true">${icon("file")}</span><button class="row-copy" type="button" data-action="focus-duplicate-file" data-file-id="${escapeHtml(fileId)}" data-focus-key="duplicate-file-${escapeHtml(group.id)}-${escapeHtml(fileId)}"><span class="row-name">${escapeHtml(file.name)}</span><span class="row-meta"><span class="row-path">${escapeHtml(store.filePath(fileId))}</span><span>${escapeHtml(file.sizeLabel)}</span></span></button><span class="row-trailing">${statusLabel(copyLabel)}${!working ? button("Choose", "choose-working-copy", { variant: "quiet", attrs: `data-group-id="${escapeHtml(group.id)}" data-file-id="${escapeHtml(fileId)}"`, focusKey: `choose-working-copy-${group.id}-${fileId}` }) : ""}</span></li>`;
}

function duplicateInspector(store, group) {
  if (!group) return `<div class="inspector-empty"><p>Select an exact-copy group.</p></div>`;
  const working = group.workingCopyFileId ? store.fileById(group.workingCopyFileId) : null;
  const workingHeading = !working
    ? "Working copy needed"
    : group.recommendation === "chosen" ? "Chosen working copy" : "Recommended working copy";
  return `<div class="inspector-body"><p>${statusLabel(group.status)}</p><h3>${workingHeading}</h3><p>${escapeHtml(group.recommendationReason)}</p><dl class="inspector-facts"><div><dt>Exact copies</dt><dd>${group.exactCopyCount}</dd></div><div><dt>File size</dt><dd>${escapeHtml(group.sizeLabel)}</dd></div><div><dt>Extra copies use</dt><dd>${escapeHtml(group.spaceUsedByExtraCopies)}</dd></div><div><dt>Working copy</dt><dd>${escapeHtml(working ? store.filePath(working.id) : "Not chosen")}</dd></div></dl><div class="inspector-actions">${button("Choose working copy", "focus-working-copy-choice", { attrs: `data-group-id="${escapeHtml(group.id)}"`, focusKey: `focus-working-copy-${group.id}` })}${button("Keep all", "keep-all-duplicates", { variant: "quiet", attrs: `data-group-id="${escapeHtml(group.id)}"`, focusKey: `keep-all-inspector-${group.id}` })}${button("Select extras", "select-duplicate-extras", { variant: "quiet", attrs: `data-group-id="${escapeHtml(group.id)}"`, focusKey: `select-extras-${group.id}` })}</div></div>`;
}

function renderDuplicates(store) {
  const state = store.getState();
  const activeGroups = state.duplicates.groups.filter((group) => state.duplicates.activeGroupIds.includes(group.id) && group.status !== "Complete");
  const activeIds = new Set(activeGroups.map((group) => group.id));
  const selectedGroupId = activeIds.has(state.selection.duplicateGroupId) ? state.selection.duplicateGroupId : activeGroups[0]?.id || null;
  const selectedGroup = store.duplicateGroupById(selectedGroupId);
  const selectedExtras = state.selection.duplicateExtraFileIds.length;
  return {
    title: "Duplicates",
    html: `<section class="duplicates-shell" data-workflow="duplicates">${pageHeader("Duplicates", "Review exact copies separately from organization plans.")}
      <div class="duplicates-workspace" data-component="duplicates-workspace">
        <section class="duplicate-groups" aria-labelledby="duplicate-groups-heading"><header class="pane-header"><div><h2 id="duplicate-groups-heading">Exact-copy groups</h2><p>${activeGroups.length} ready</p></div></header><div class="duplicate-groups-list">${activeGroups.map((group) => `<button class="duplicate-group" type="button" data-action="select-duplicate-group" data-group-id="${escapeHtml(group.id)}" data-focus-key="duplicate-group-${escapeHtml(group.id)}" aria-pressed="${group.id === selectedGroupId}"><strong>${escapeHtml(store.fileById(group.memberFileIds[0])?.name || "Exact copies")}</strong><span>${group.exactCopyCount} copies · ${escapeHtml(group.sizeLabel)}</span><span class="duplicate-group-meta">${escapeHtml(group.spaceUsedByExtraCopies)} used by extras · ${escapeHtml(group.status)}</span></button>`).join("") || `<div class="empty-state"><p>No exact-copy groups are waiting for review.</p></div>`}</div></section>
        <section class="duplicate-members" aria-labelledby="duplicate-members-heading"><header class="pane-header"><div><h2 id="duplicate-members-heading">Group files</h2><p>${selectedGroup ? `${selectedGroup.exactCopyCount} files with matching content` : "No active group selected"}</p></div>${selectedGroup ? `<span class="narrow-inspector-trigger">${button("Inspector", "toggle-duplicate-inspector", { variant: "quiet", iconName: "info", focusKey: `duplicate-inspector-${selectedGroup.id}` })}</span>` : ""}</header><div class="pane-scroll"><ul class="member-list" aria-label="Files in the selected exact-copy group">${selectedGroup ? selectedGroup.memberFileIds.map((fileId) => duplicateMemberRow(store, selectedGroup, fileId)).join("") : `<li class="empty-state"><p>Completed groups are no longer actionable.</p></li>`}</ul></div>${selectedGroup ? `<div class="toolbar bulk-toolbar"><div class="toolbar-group"><strong>${selectedExtras} extras selected</strong></div><div class="toolbar-actions">${button("Keep all", "keep-all-duplicates", { variant: "quiet", attrs: `data-group-id="${escapeHtml(selectedGroup.id)}"`, focusKey: `keep-all-toolbar-${selectedGroup.id}` })}${button("Consolidate", "confirm-consolidate", { variant: "primary", attrs: `data-group-id="${escapeHtml(selectedGroup.id)}"`, focusKey: `consolidate-${selectedGroup.id}` })}</div></div>` : ""}</section>
        <aside class="duplicate-inspector inspector-pane" data-open="${state.selection.duplicateInspectorOpen}" data-has-selection="${Boolean(selectedGroup)}" aria-labelledby="duplicate-inspector-heading"><header class="inspector-header"><div><h2 id="duplicate-inspector-heading">Inspector</h2><p>Recommendation and actions</p></div><button class="icon-button narrow-inspector-close" type="button" data-action="toggle-duplicate-inspector" data-focus-key="duplicate-inspector-close" data-inspector-close aria-label="Close duplicate inspector" title="Close inspector">${icon("close")}</button></header>${duplicateInspector(store, selectedGroup)}</aside>
      </div>
      ${state.duplicates.feedback ? `<div class="local-feedback" role="status" data-status="complete">${escapeHtml(state.duplicates.feedback)}</div>` : ""}
      <section class="panel recheck-panel" aria-labelledby="recheck-heading"><header class="issues-header"><div><h2 id="recheck-heading">Changed-copy rechecks</h2><p>These scenarios stay outside the two active groups.</p></div></header>${state.duplicates.rechecks.map((item) => { const fileId = item.preferredFileId || item.excludedFileId; const file = store.fileById(fileId); return `<div class="issue-row" data-status="review"><span class="issue-icon" aria-hidden="true">${icon("refresh")}</span><span class="issue-copy"><strong>${escapeHtml(file?.name)}</strong><small>${escapeHtml(item.message)}</small></span><span class="row-actions">${statusLabel(item.status)}${item.requiresReconfirmation ? button("Reconfirm", "reconfirm-duplicate", { variant: "quiet", attrs: `data-recheck-id="${escapeHtml(item.id)}"`, focusKey: `reconfirm-${item.id}` }) : ""}</span></div>`; }).join("")}</section>
      <section class="notice-panel"><strong>Before consolidation</strong><p>Extra copies move to a reversible holding area. Files are not deleted. Storage is not recovered. Copies are rechecked before applying.</p></section>
    </section>`,
  };
}

function activityVisible(item, filter) {
  if (filter === "completed") return ["Applied and verified", "Undone and verified"].includes(item.status);
  if (filter === "attention") return ["Partially completed", "Verification needed", "Failed with no changes", "Undo unavailable"].includes(item.status);
  return true;
}

function activityFileRow(store, fileId) {
  const file = store.fileById(fileId);
  if (!file) return "";
  return `<li class="file-row resource-row" data-component="file-row"><span class="row-check"></span><span class="row-icon" aria-hidden="true">${icon("file")}</span><span class="row-copy"><span class="row-name">${escapeHtml(file.name)}</span><span class="row-meta"><span class="row-path">${escapeHtml(store.filePath(fileId))}</span><span>${escapeHtml(file.kind)}</span></span></span><span></span></li>`;
}

function renderActivity(store, route) {
  const state = store.getState();
  const filter = route.params.get("filter") || "all";
  const items = state.activity.filter((item) => activityVisible(item, filter));
  return {
    title: "Activity",
    html: `${pageHeader("Activity", "Verified outcomes and short-term recovery.")}
      <nav class="activity-controls segmented-control" aria-label="Activity filter">${[["all", "All"], ["completed", "Completed"], ["attention", "Needs attention"]].map(([id, label]) => `<a class="button button-quiet" href="#/activity?filter=${id}" aria-current="${filter === id ? "page" : "false"}">${label}</a>`).join("")}</nav>
      <section class="activity-list" aria-label="Activity entries">${items.map((item) => { const open = state.expandedActivityIds.includes(item.id); return `<details class="activity-entry" ${open ? "open" : ""}><summary data-action="toggle-activity" data-activity-id="${escapeHtml(item.id)}" data-focus-key="activity-${escapeHtml(item.id)}"><span class="activity-summary-copy"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.summary)}</small></span><span>${statusLabel(item.status)}</span></summary><div class="activity-details"><p>${escapeHtml(item.verification)}</p><ul class="activity-files">${item.fileIds.map((fileId) => activityFileRow(store, fileId)).join("")}</ul><div class="notice-panel" data-status="${item.undo.status === "available" ? "verified" : item.undo.status === "unavailable" ? "review" : "ready"}"><strong>${escapeHtml(item.undo.label)}</strong><p>${escapeHtml(item.undo.detail)}</p><div class="row-actions">${item.undo.status === "available" && item.undo.scope === "full-operation" ? button("Undo and verify", "confirm-undo", { variant: "primary", attrs: `data-activity-id="${escapeHtml(item.id)}"`, iconName: "undo", focusKey: `undo-${item.id}` }) : ""}${item.undo.status === "available" && item.undo.scope === "return-copy" ? button("Return this copy", "confirm-return-copy", { attrs: `data-activity-id="${escapeHtml(item.id)}"`, iconName: "undo", focusKey: `return-copy-${item.id}` }) : ""}${item.status === "Undo unavailable" ? button("Prepare a new organization plan", "prepare-plan", { attrs: 'data-workflow="organization"', focusKey: `prepare-from-${item.id}` }) : ""}</div></div></div></details>`; }).join("") || `<div class="empty-state"><p>No activity matches this filter.</p></div>`}</section>`,
  };
}

const settingsSections = [
  ["location", "Location"],
  ["plan-preparation", "Plan preparation"],
  ["rules", "Rules"],
  ["duplicates", "Duplicates"],
  ["notifications", "Notifications"],
  ["local-ai", "Local AI"],
  ["privacy", "Privacy"],
  ["advanced", "Advanced"],
];

function switchRow(label, help, path, checked) {
  return `<div class="setting-row"><span class="setting-copy"><label for="setting-${escapeHtml(slug(path))}">${escapeHtml(label)}</label><small>${escapeHtml(help)}</small></span><label class="switch-control"><input id="setting-${escapeHtml(slug(path))}" type="checkbox" data-setting-path="${escapeHtml(path)}" data-focus-key="setting-${escapeHtml(slug(path))}" ${checked ? "checked" : ""}><span class="switch-track" aria-hidden="true"></span><span class="visually-hidden">${escapeHtml(label)}</span></label></div>`;
}

function settingsPane(store, section) {
  const state = store.getState();
  const settings = state.settings;
  if (section === "location") return `<header><h2>Location</h2><p>One sample location is active.</p></header><section class="settings-section"><div class="setting-row"><span class="setting-copy"><strong>Downloads</strong><small>${escapeHtml(state.location.statusLabel)} · ${escapeHtml(state.location.lastCheckedLabel)}</small></span>${button("Change location", "change-location", { variant: "secondary" })}</div></section>`;
  if (section === "plan-preparation") return `<header><h2>Plan preparation</h2><p>Choose when a new plan may be proposed.</p></header><section class="settings-section"><fieldset class="radio-stack"><legend>Prepare plans</legend>${[["manual", "Manual"], ["automatic-when-useful", "Automatic when useful"], ["scheduled", "Scheduled"]].map(([value, label]) => `<label class="radio-option"><input type="radio" name="plan-mode" value="${value}" data-setting-path="planPreparation.mode" data-focus-key="plan-mode-${value}" ${settings.planPreparation.mode === value ? "checked" : ""}><span><strong>${label}</strong></span></label>`).join("")}</fieldset><div class="setting-row"><span class="setting-copy"><strong>Application</strong><small>Every sample filesystem action requires confirmation.</small></span><strong>Always ask</strong></div></section>`;
  if (section === "rules") {
    const rules = settings.rules.items;
    return `<header><h2>Rules</h2><p>Explicit deterministic choices for future plans.</p></header><section class="settings-section">${rules.length ? rules.map((rule) => `<div class="setting-row"><span class="setting-copy"><strong>${escapeHtml(rule.label)}</strong><small>${escapeHtml(rule.match)} → ${escapeHtml(store.folderPath(rule.destinationFolderId))}</small></span><div class="row-actions">${statusLabel(rule.active ? "Active" : "Paused")}${button(rule.active ? "Pause" : "Resume", "toggle-rule", { variant: "quiet", attrs: `data-rule-id="${escapeHtml(rule.id)}"`, focusKey: `toggle-rule-${rule.id}` })}${button("Inspect", "inspect-rule", { variant: "quiet", attrs: `data-rule-id="${escapeHtml(rule.id)}"`, focusKey: `inspect-rule-${rule.id}` })}${button("More", "edit-rule", { variant: "quiet", attrs: `data-rule-id="${escapeHtml(rule.id)}"`, iconName: "more", focusKey: `edit-rule-${rule.id}` })}</div></div>`).join("") : `<div class="empty-state"><p>No locked rules.</p></div>`}</section>`;
  }
  if (section === "duplicates") return `<header><h2>Duplicates</h2><p>Exact-copy review remains separate from organization.</p></header><section class="settings-section"><div class="setting-row"><span class="setting-copy"><strong>Confirmation</strong><small>One final confirmation appears before any selected extras move.</small></span><strong>Always ask</strong></div><div class="setting-row"><span class="setting-copy"><strong>Reversible holding area</strong><small>This prototype never deletes copies or claims recovered storage.</small></span><strong>Required</strong></div></section>`;
  if (section === "notifications") return `<header><h2>Notifications</h2><p>Preferences persist; this prototype sends nothing.</p></header><section class="settings-section">${switchRow("Organization plan ready", "Enabled by default.", "notifications.organizationPlanReady", settings.notifications.organizationPlanReady)}${switchRow("Duplicate review ready", "Notify when exact-copy groups are ready.", "notifications.duplicateReviewReady", settings.notifications.duplicateReviewReady)}${switchRow("Apply or verification problem", "Surface attention-worthy outcomes.", "notifications.applyOrVerificationProblem", settings.notifications.applyOrVerificationProblem)}${switchRow("Index problem", "Placeholder preference only.", "notifications.indexProblem", settings.notifications.indexProblem)}</section>`;
  if (section === "local-ai") return `<header><h2>Local AI</h2><p>Advisory concept only; no runtime connection.</p></header><section class="settings-section">${switchRow("Use local Ollama advice", "Off by default. Suggestions never bypass review.", "localAI.enabled", settings.localAI.enabled)}<div class="setting-row"><span class="setting-copy"><strong>Role</strong><small>Advisory grouping suggestions only.</small></span><strong>Local and optional</strong></div></section>`;
  if (section === "privacy") return `<header><h2>Privacy</h2><p>Fixture-only prototype boundaries.</p></header><section class="settings-section"><div class="setting-row"><span class="setting-copy"><strong>Sample data only</strong><small>No real folders are opened and no file contents are transmitted.</small></span>${statusLabel("Verified")}</div><div class="setting-row"><span class="setting-copy"><strong>Uploads</strong><small>No uploads or external services.</small></span><strong>Off</strong></div><div class="setting-row"><span class="setting-copy"><strong>Telemetry</strong><small>No telemetry implementation.</small></span><strong>Off</strong></div></section>`;
  return `<header><h2>Advanced</h2><p>Fixture diagnostics and demo controls.</p></header><section class="settings-section"><label class="setting-row"><span class="setting-copy"><strong>Demo execution outcome</strong><small>Controls the truth shown after a sample operation.</small></span><select data-setting-path="advanced.demoOutcome" data-focus-key="setting-demo-outcome">${settings.advanced.demoOutcomes.map((value) => `<option value="${escapeHtml(value)}" ${settings.advanced.demoOutcome === value ? "selected" : ""}>${escapeHtml(value.replaceAll("-", " "))}</option>`).join("")}</select></label><div class="setting-row"><span class="setting-copy"><strong>Fixture diagnostics</strong><small>${escapeHtml(settings.advanced.fixtureDiagnostics)}</small></span>${statusLabel("Ready")}</div><div class="setting-row"><span class="setting-copy"><strong>Exports</strong><small>Placeholders only; no file is created.</small></span><span class="row-actions">${button("Export summary", "export-placeholder", { variant: "quiet", focusKey: "export-summary" })}${button("Export notes", "export-placeholder", { variant: "quiet", focusKey: "export-notes" })}</span></div><div class="setting-row"><span class="setting-copy"><strong>Reset prototype</strong><small>Restore every fixture choice and workflow.</small></span>${button("Reset", "confirm-reset", { variant: "danger" })}</div><p class="caption">${escapeHtml(settings.advanced.workingNameNotice)}</p></section>`;
}

function renderSettings(store, route) {
  const requested = route.params.get("section") || "location";
  const section = settingsSections.some(([id]) => id === requested) ? requested : "location";
  return {
    title: "Settings",
    html: `${pageHeader("Settings", "Compact prototype preferences.")}<label class="settings-select">Category<select data-settings-section data-focus-key="settings-section-select">${settingsSections.map(([id, label]) => `<option value="${id}" ${section === id ? "selected" : ""}>${label}</option>`).join("")}</select></label><div class="settings-layout"><nav class="settings-nav" aria-label="Settings categories">${settingsSections.map(([id, label]) => `<a href="#/settings?section=${id}" data-focus-key="settings-${id}" aria-current="${section === id ? "page" : "false"}">${label}</a>`).join("")}</nav><section class="settings-pane" data-component="settings-pane">${settingsPane(store, section)}</section></div>`,
  };
}

function renderRoute(store, route) {
  if (route.name === "organize") return renderOrganize(store);
  if (route.name === "duplicates") return renderDuplicates(store);
  if (route.name === "activity") return renderActivity(store, route);
  if (route.name === "settings") return renderSettings(store, route);
  return renderHome(store);
}

function destinationOptions(store, excluded = "") {
  return store.activeGroups().filter((group) => group.destinationFolderId !== excluded).map((group) => `<label class="radio-option"><input type="radio" name="destinationFolderId" value="${escapeHtml(group.destinationFolderId)}" data-focus-key="destination-${escapeHtml(group.destinationFolderId)}" required><span><strong>${escapeHtml(group.label)}</strong><small>${escapeHtml(store.folderPath(group.destinationFolderId))}</small></span></label>`).join("");
}

function renderDialog(store, type, payload = {}) {
  const state = store.getState();
  if (type === "switch-workflow") {
    const fromOrganization = payload.active === "organization";
    return { eyebrow: "One workflow at a time", title: fromOrganization ? "An organization plan is active." : "A duplicate review is active.", description: "Your current choices stay intact unless you explicitly discard them.", content: "", actions: `${button(fromOrganization ? "Continue plan" : "Continue duplicate review", "continue-workflow", { variant: "quiet", attrs: `data-workflow="${escapeHtml(payload.active)}"` })}${button(fromOrganization ? "Discard plan and open Duplicates" : "Discard duplicate review and open Organize", "discard-and-switch", { variant: "danger", attrs: `data-workflow="${escapeHtml(payload.target)}"` })}` };
  }
  if (type === "destination") return { eyebrow: "Planning edit", title: `Move ${state.selection.planChangeIds.length} selected ${state.selection.planChangeIds.length === 1 ? "file" : "files"} to…`, description: "This updates the Planned pane only.", content: `<form id="destination-form" data-submit="move-selected"><fieldset class="radio-stack"><legend>Destination</legend>${destinationOptions(store)}</fieldset><p class="dialog-note">No files move until the plan is applied and verified.</p></form>`, actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}<button class="button button-primary" type="submit" form="destination-form">Update plan</button>` };
  if (type === "remove-group") {
    const group = store.groupById(payload.groupId);
    const count = group.changeIds.map(store.changeById).filter((change) => change?.included !== false).length;
    return { eyebrow: "Planning edit", title: `Remove ${group.label} from this plan?`, description: `${count} files will remain in their current locations.`, content: `<p>This removes a proposed folder and its included changes from the plan. It does not delete a folder or file.</p>`, actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Remove from plan", "remove-group", { variant: "danger", attrs: `data-group-id="${escapeHtml(group.id)}"` })}` };
  }
  if (type === "discard-plan") return { eyebrow: "Organization plan", title: "Discard this plan?", description: "The current sample files remain unchanged.", content: `<p>You can prepare the fixture plan again from Home.</p>`, actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Discard plan", "discard-plan", { variant: "danger" })}` };
  if (type === "review-gate") return { eyebrow: "Apply unavailable", title: `${payload.count} ${payload.count === 1 ? "item still needs" : "items still need"} review.`, description: "Accept, change, or leave each item before applying.", content: "", actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Show review items", "focus-review-items", { variant: "primary" })}` };
  if (type === "conflict-gate") return { eyebrow: "Apply unavailable", title: `${payload.count} ${payload.count === 1 ? "conflict prevents" : "conflicts prevent"} this plan from being applied.`, description: "Resolve the destination conflict or leave the file unchanged.", content: "", actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Show conflict", "focus-conflicts", { variant: "primary" })}` };
  if (type === "apply-confirm") {
    const summary = store.planSummary();
    return { eyebrow: "Final confirmation", title: "Apply organization plan?", description: "The sample plan will be rechecked before any result is recorded.", content: `<dl class="apply-summary dialog-summary"><div><dt>Changes</dt><dd>${summary.changes}</dd></div><div><dt>Destinations</dt><dd>${summary.destinations}</dd></div><div><dt>Unresolved issues</dt><dd>None</dd></div></dl><p class="dialog-note">This applies only to fixture state.</p>`, actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Apply", "apply-plan", { variant: "primary" })}` };
  }
  if (type === "consolidate") {
    const group = store.duplicateGroupById(payload.groupId);
    const count = state.selection.duplicateExtraFileIds.length;
    return { eyebrow: "Duplicate review", title: "Consolidate extra copies?", description: `${count} selected ${count === 1 ? "copy" : "copies"} will move to the reversible holding area.`, content: `<ul class="dialog-facts"><li>Copies are rechecked before applying.</li><li>Files are not deleted.</li><li>Storage is not recovered.</li></ul>${!group?.workingCopyFileId ? `<p class="form-error" role="alert">Choose a working copy first.</p>` : ""}`, actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Consolidate extra copies", "consolidate-duplicates", { variant: "primary", attrs: `data-group-id="${escapeHtml(payload.groupId)}"`, disabled: !group?.workingCopyFileId || count === 0 })}` };
  }
  if (type === "undo") return { eyebrow: "Short-term failsafe", title: "Undo this recent operation?", description: "Only cleanly reversible sample changes will be returned.", content: `<p>The result will be checked before it is reported.</p>`, actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Undo and verify", "undo-activity", { variant: "primary", attrs: `data-activity-id="${escapeHtml(payload.activityId)}"` })}` };
  if (type === "return-copy") return { eyebrow: "Narrow recovery", title: "Return this copy?", description: "This returns one unaffected sample copy, not the full duplicate operation.", content: "", actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Return this copy", "return-copy", { variant: "primary", attrs: `data-activity-id="${escapeHtml(payload.activityId)}"` })}` };
  if (type === "inspect-rule") {
    const rule = state.settings.rules.items.find((item) => item.id === payload.ruleId);
    return { eyebrow: "Locked rule", title: rule.label, description: "A deterministic choice for future organization plans.", content: `<dl class="fact-list"><div><dt>Match</dt><dd>${escapeHtml(rule.match)}</dd></div><div><dt>Destination</dt><dd>${escapeHtml(store.folderPath(rule.destinationFolderId))}</dd></div><div><dt>Status</dt><dd>${rule.active ? "Active" : "Paused"}</dd></div></dl>`, actions: button("Close", "close-dialog", { variant: "primary" }) };
  }
  if (type === "edit-rule") {
    const rule = state.settings.rules.items.find((item) => item.id === payload.ruleId);
    const destinations = store.getFixture().initialState.plan.groups;
    return { eyebrow: "Locked rule", title: "Edit rule", description: "Changes affect future fixture plans only.", content: `<form id="rule-form" data-submit="edit-rule" data-rule-id="${escapeHtml(rule.id)}"><label>Filename match<input name="match" value="${escapeHtml(rule.match)}" required></label><label>Destination<select name="destinationFolderId">${destinations.map((group) => `<option value="${escapeHtml(group.destinationFolderId)}" ${group.destinationFolderId === rule.destinationFolderId ? "selected" : ""}>${escapeHtml(group.label)}</option>`).join("")}</select></label></form>`, actions: `${button("Remove rule", "confirm-remove-rule", { variant: "danger", attrs: `data-rule-id="${escapeHtml(rule.id)}"` })}<button class="button button-primary" type="submit" form="rule-form">Save</button>` };
  }
  if (type === "remove-rule") return { eyebrow: "Locked rule", title: "Remove this rule?", description: "Future fixture plans will no longer use it.", content: "", actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Remove rule", "remove-rule", { variant: "danger", attrs: `data-rule-id="${escapeHtml(payload.ruleId)}"` })}` };
  if (type === "reset") return { eyebrow: "Advanced", title: "Reset the prototype?", description: "Every fixture choice, workflow, setting, and Activity entry returns to its initial state.", content: `<p>No real file or folder is involved.</p>`, actions: `${button("Cancel", "close-dialog", { variant: "quiet" })}${button("Reset prototype", "reset-prototype", { variant: "danger" })}` };
  return { eyebrow: "Prototype", title: "Sample interaction", description: "This interaction is simulated.", content: "", actions: button("Close", "close-dialog", { variant: "primary" }) };
}

function renderStageDialog(stages, activeIndex, { title = "Working", message = "", failedIndex = -1 } = {}) {
  return { eyebrow: "Simulated operation", title, description: message, content: `<ol class="stage-list" aria-label="Operation progress">${stages.map((stage, index) => { const status = index === failedIndex ? "error" : index < activeIndex ? "complete" : index === activeIndex ? "active" : "waiting"; return `<li class="stage" data-status="${status}" ${index === activeIndex ? 'aria-current="step" tabindex="-1" data-stage-focus' : ""}><span class="stage-marker" aria-hidden="true">${index < activeIndex ? icon("verified") : status === "error" ? icon("conflict") : ""}</span><span><strong>${escapeHtml(stage.label)}</strong>${index === activeIndex && message ? `<small>${escapeHtml(message)}</small>` : ""}</span></li>`; }).join("")}</ol>`, actions: "" };
}

function outcomeStageStatus(outcome, stage, index, lastIndex) {
  if (outcome === "Failed with no changes") return index === 0 ? "complete" : "error";
  if (outcome === "Verification needed") {
    if (stage.id === "verifying") return "review";
    if (stage.id === "complete" || index === lastIndex) return "waiting";
  }
  if (outcome === "Partially completed" && index === lastIndex) return "review";
  return "complete";
}

function renderOutcomeDialog(operation, definition) {
  const status = operation.status;
  const success = ["Applied and verified", "Undone and verified"].includes(status);
  return { eyebrow: success ? "Verified result" : "Operation result", title: status, description: operation.verification, content: `<ol class="stage-list completed-stages" aria-label="Operation stage outcome">${definition.map((stage, index) => { const stageStatus = outcomeStageStatus(status, stage, index, definition.length - 1); const marker = stageStatus === "complete" ? icon("verified") : stageStatus === "review" ? icon("review") : stageStatus === "error" ? icon("conflict") : ""; const stageState = stageStatus === "complete" ? "Completed" : stageStatus === "review" ? "Needs review" : stageStatus === "error" ? "Stopped" : "Not reached"; return `<li class="stage" data-status="${stageStatus}"><span class="stage-marker" aria-hidden="true">${marker}</span><span><strong>${escapeHtml(stage.label)}</strong><small class="stage-state">${escapeHtml(stageState)}</small></span></li>`; }).join("")}</ol><section class="notice-panel" data-status="${success ? "verified" : "review"}"><strong>${escapeHtml(operation.summary)}</strong><p>${escapeHtml(operation.verification)}</p></section>`, actions: `${button("Close", "close-dialog", { variant: "quiet" })}${button("View Activity", "view-activity", { variant: "primary" })}` };
}

export { escapeHtml, filteredPlanChanges, renderDialog, renderOutcomeDialog, renderRoute, renderStageDialog };
