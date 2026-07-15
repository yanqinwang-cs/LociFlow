import { breadcrumb, escapeHtml, fileIcon, renderMovementTree } from "./tree.js";

const TYPE_LABELS = {
  move: ["file moves", "Move"],
  rename: ["name changes", "Rename"],
  create_folder: ["new folders", "Create folder"],
  duplicate: ["duplicate copies", "Duplicate copy"],
};

function button(label, action, options = {}) {
  const classes = ["button", options.primary ? "button-primary" : "button-secondary", options.quiet ? "button-quiet" : "", options.danger ? "button-danger" : ""].filter(Boolean).join(" ");
  return `<button type="button" class="${classes}" data-action="${action}" ${options.attrs || ""} ${options.disabled ? "disabled" : ""}>${escapeHtml(label)}</button>`;
}

function pageHeader(title, subtitle, actions = "") {
  return `<header class="page-header"><div><p class="eyebrow">Sample Downloads</p><h1>${escapeHtml(title)}</h1><p>${escapeHtml(subtitle)}</p></div>${actions ? `<div class="page-actions">${actions}</div>` : ""}</header>`;
}

function stages(state) {
  const labels = [
    ["checking", "Check"],
    ["organizing", "Organize"],
    ["approving", "Approve"],
    ["moving", "Move"],
    ["verifying", "Verify"],
  ];
  return `<ol class="stages" aria-label="Organization steps">${labels.map(([stage, label], index) => `<li class="${state.workflowStage === stage ? "current" : ""}"><span>${index + 1}</span>${label}</li>`).join("")}</ol>`;
}

export function renderHome(store) {
  const state = store.getState();
  const waiting = state.changes.filter((change) => change.decision === "pending").length;
  const completed = state.workflowCompleted && state.workflowStage === "idle";
  const primary = completed
    ? button("Check again", "check-again", { primary: true })
    : waiting ? button("Continue organizing", "continue-organizing", { primary: true })
      : state.changes.length ? button("Continue organizing", "continue-organizing", { primary: true })
        : button("Check files", "check-files", { primary: true });
  const status = completed ? "Everything is organized" : waiting ? `${waiting} ${waiting === 1 ? "change is" : "changes are"} waiting` : "Changes are ready to approve";
  return `${pageHeader("Home", "A clear starting point for the active folder.", primary)}${stages(state)}
    ${completed ? '<section class="completion-banner" role="status"><strong>Last move completed successfully</strong><span>Today, 6:42 PM</span></section>' : ""}
    <section class="home-status card" aria-labelledby="file-status-heading"><div><p class="eyebrow">Current state</p><h2 id="file-status-heading">File status</h2><strong class="home-main-status">${escapeHtml(status)}</strong></div><dl class="status-grid"><div><dt>Last checked</dt><dd>${escapeHtml(state.lastChecked)}</dd></div><div><dt>Last organized</dt><dd>${escapeHtml(state.lastOrganized)}</dd></div><div><dt>Next check</dt><dd>${escapeHtml(state.nextCheck)}</dd></div><div><dt>Current folder</dt><dd>${escapeHtml(state.currentFolder)}</dd></div></dl></section>
    <section class="home-summaries" aria-label="Folder summary">
      <article class="summary-card"><span class="summary-icon" aria-hidden="true">01</span><div><h2>Organization</h2><strong>${completed ? "Everything is organized" : waiting ? `${waiting} changes waiting` : "Ready to approve"}</strong></div>${completed ? button("Check again", "check-again") : button("Continue organizing", "continue-organizing")}</article>
      <article class="summary-card"><span class="summary-icon" aria-hidden="true">02</span><div><h2>Duplicate Bin</h2><strong>${state.duplicateBinItems.length} ${state.duplicateBinItems.length === 1 ? "file" : "files"}</strong></div>${button("Open Duplicate Bin", "open-duplicate-bin")}</article>
      <article class="summary-card"><span class="summary-icon" aria-hidden="true">03</span><div><h2>Last file move</h2><strong>${escapeHtml(state.lastMoveSummary || "No files have moved yet.")}</strong></div>${button("View Activity", "open-activity")}</article>
    </section>`;
}

function styleControl(store) {
  const state = store.getState();
  return `<section class="style-control card" aria-labelledby="style-heading"><div><p class="eyebrow">Current sample</p><h2 id="style-heading">Organization style</h2></div><fieldset class="segmented style-options" aria-label="Organization style">${Object.entries(store.fixtures.organizationStyles).map(([key, style]) => `<button type="button" data-action="set-style" data-style="${key}" aria-pressed="${state.organizationStyle === key}"><strong>${escapeHtml(style.label)}</strong><span>${escapeHtml(style.summary)}</span></button>`).join("")}</fieldset><p class="style-help">${escapeHtml(store.fixtures.organizationStyles[state.organizationStyle].explanation)}</p></section>`;
}

function changePrimary(change) {
  const mapping = {
    move: ["Accept", "accept"],
    rename: ["Use new name", "use-name"],
    create_folder: ["Create folder", "create"],
    duplicate: ["Move to Duplicate Bin", "move-bin"],
  };
  return mapping[change.type];
}

function changeRow(change, state) {
  const selected = state.selectedChangeIds.includes(change.id);
  const primary = changePrimary(change);
  return `<div class="change-row ${selected ? "selected" : ""}" data-change-row="${change.id}" role="listitem">
    <label class="row-check"><input type="checkbox" data-action="toggle-change" data-change-id="${change.id}" ${selected ? "checked" : ""}><span class="visually-hidden">Select ${escapeHtml(change.name)}</span></label>
    ${fileIcon(change.icon, change.kind)}
    <button type="button" class="change-name" data-action="activate-change" data-change-id="${change.id}">${escapeHtml(change.name)}</button>
    <span class="change-description">${escapeHtml(change.changeText)}</span>
    <span class="change-size">${escapeHtml(change.size)}</span>
    <button type="button" class="row-action" data-action="decide-change" data-change-id="${change.id}" data-choice="${primary[1]}">${escapeHtml(primary[0])}</button>
  </div>`;
}

function actionFilters(store) {
  const state = store.getState();
  const counts = store.changeCounts();
  return `<div class="action-counts" aria-label="Change types"><button type="button" data-action="filter-changes" data-type="all" aria-pressed="${state.actionFilter === "all"}"><strong>${counts.total}</strong>All changes</button>${Object.entries(TYPE_LABELS).map(([type, [label]]) => `<button type="button" data-action="filter-changes" data-type="${type}" aria-pressed="${state.actionFilter === type}"><strong>${counts[type]}</strong>${escapeHtml(label)}</button>`).join("")}</div>`;
}

function bulkBar(store) {
  const state = store.getState();
  const visible = store.waitingChanges();
  const selected = state.selectedChangeIds.length;
  return `<div class="bulk-bar"><label><input type="checkbox" data-action="select-all-visible" ${visible.length && visible.every((change) => state.selectedChangeIds.includes(change.id)) ? "checked" : ""}> Select all visible</label><strong>${selected} selected</strong><span class="bulk-spacer"></span>${button("Accept selected", "bulk-accept", { disabled: !selected })}${button("Keep selected", "bulk-keep", { disabled: !selected })}${button("Change folder", "bulk-folder", { disabled: !selected })}${button("Remove changes", "bulk-remove", { disabled: !selected })}</div>`;
}

function changesPanel(store) {
  const state = store.getState();
  const recommended = store.recommendedChanges();
  const choices = store.choiceChanges();
  const visibleRecommended = state.actionFilter === "all" ? recommended : recommended.filter((change) => change.type === state.actionFilter);
  const visibleChoices = state.actionFilter === "all" ? choices : choices.filter((change) => change.type === state.actionFilter);
  return `<div class="decision-sections" id="changes-to-confirm" tabindex="-1">
    <section class="changes-panel card" aria-labelledby="recommended-heading"><header class="section-header"><div><p class="eyebrow">Safe to accept together</p><h2 id="recommended-heading">Recommended changes</h2></div>${button(`Accept all recommended changes${recommended.length ? ` (${recommended.length})` : ""}`, "accept-recommended", { primary: true, disabled: !recommended.length })}</header><div class="change-list recommended-list" role="list" aria-label="Recommended changes">${visibleRecommended.length ? visibleRecommended.map((change) => changeRow(change, state)).join("") : '<div class="empty-list"><strong>All recommended changes accepted</strong><span>Accepted changes now appear below.</span></div>'}</div></section>
    <section class="changes-panel card" aria-labelledby="choices-heading"><header class="section-header"><div><p class="eyebrow">Needs your judgment</p><h2 id="choices-heading">Choices needed</h2></div><span class="choice-count">${choices.length} ${choices.length === 1 ? "choice" : "choices"}</span></header>${actionFilters(store)}${bulkBar(store)}<div class="change-list" role="list" aria-label="Choices needed">${visibleChoices.length ? visibleChoices.map((change) => changeRow(change, state)).join("") : '<div class="empty-list"><strong>No choices remain</strong><span>Review the complete batch when you are ready.</span></div>'}</div></section>
  </div>`;
}

function detailActions(change) {
  if (change.type === "move") return `${button("Accept", "decide-change", { primary: true, attrs: `data-change-id="${change.id}" data-choice="accept"` })}${button("Keep here", "decide-change", { attrs: `data-change-id="${change.id}" data-choice="keep"` })}${button("Change folder", "open-folder", { attrs: `data-change-id="${change.id}"` })}`;
  if (change.type === "rename") return `${button("Use new name", "decide-change", { primary: true, attrs: `data-change-id="${change.id}" data-choice="use-name"` })}${button("Keep current name", "decide-change", { attrs: `data-change-id="${change.id}" data-choice="keep-name"` })}`;
  if (change.type === "create_folder") return `${button("Create folder", "decide-change", { primary: true, attrs: `data-change-id="${change.id}" data-choice="create"` })}${button("Do not create", "decide-change", { attrs: `data-change-id="${change.id}" data-choice="skip-folder"` })}`;
  if (change.type === "duplicate") return `${button("Move to Duplicate Bin", "decide-change", { primary: true, attrs: `data-change-id="${change.id}" data-choice="move-bin"` })}${button("Keep all copies", "decide-change", { attrs: `data-change-id="${change.id}" data-choice="keep-copies"` })}`;
  return "";
}

function reuseSuggestion(state) {
  if (!state.showReuseSuggestion) return "";
  return `<aside class="reuse-suggestion" aria-label="Organization suggestion"><button type="button" class="dismiss" data-action="dismiss-reuse" aria-label="Dismiss">×</button><strong>Use this organization again?</strong><div>${button("Just this time", "dismiss-reuse")}${button("Suggest next time", "dismiss-reuse")}${button("Save as standard", "open-standard-builder", { primary: true })}</div></aside>`;
}

function itemDetails(store) {
  const state = store.getState();
  const selected = state.selectedChangeIds;
  if (selected.length > 1) return `<aside class="item-details card" aria-labelledby="details-heading"><p class="eyebrow">Bulk choice</p><h2 id="details-heading">Item details</h2><strong class="detail-title">${selected.length} files selected</strong><p>Use the bulk actions above to update every compatible selected row at once.</p></aside>`;
  const change = store.activeChange();
  if (!change) return `<aside class="item-details card" aria-labelledby="details-heading"><p class="eyebrow">Current choice</p><h2 id="details-heading">Item details</h2><div class="empty-details"><strong>No item selected</strong><span>Select a change to see its folders and actions.</span></div>${reuseSuggestion(state)}</aside>`;
  const standard = store.standardForChange(change);
  return `<aside class="item-details card" aria-labelledby="details-heading"><p class="eyebrow">Current choice</p><h2 id="details-heading">Item details</h2><div class="detail-title-row">${fileIcon(change.icon, change.kind)}<div><strong class="detail-title">${escapeHtml(change.name)}</strong><span>${escapeHtml(TYPE_LABELS[change.type][1])}</span></div></div><dl class="details-list"><div><dt>Kind</dt><dd>${escapeHtml(change.kind)}</dd></div><div><dt>Size</dt><dd>${escapeHtml(change.size)}</dd></div><div><dt>Current folder</dt><dd>${breadcrumb(change.currentFolder)}</dd></div><div><dt>${change.type === "rename" ? "New name" : "New folder"}</dt><dd>${change.type === "rename" ? escapeHtml(change.newName) : breadcrumb(change.newFolder)}</dd></div><div><dt>Why this change</dt><dd>${escapeHtml(change.reason)}</dd></div>${standard ? `<div><dt>Standard</dt><dd>${escapeHtml(standard.name)}</dd></div>` : ""}</dl><div class="detail-actions">${detailActions(change)}${change.type === "move" ? button("Save as standard", "open-standard-builder", { quiet: true }) : ""}</div>${reuseSuggestion(state)}</aside>`;
}

function keptByStandards(state) {
  if (!state.keptByStandards.length) return "";
  return `<details class="kept-summary card"><summary><span><strong>Kept by Standards</strong><small>${state.keptByStandards.length} ${state.keptByStandards.length === 1 ? "file" : "files"} kept in place</small></span></summary><ul>${state.keptByStandards.map((item) => `<li>${escapeHtml(item.name)} · ${escapeHtml(item.standardName)}</li>`).join("")}</ul></details>`;
}

function workflowResult(store) {
  const state = store.getState();
  if (state.workflowStage === "moving") {
    return `<section class="workflow-result card" role="status"><p class="eyebrow">Move</p><h2>Test move complete</h2><p>No real files were changed.</p>${button("Verify test move", "verify-test-move", { primary: true })}</section>`;
  }
  const counts = state.verificationResult?.counts || { movedFiles: 0, renamedFiles: 0, createdFolders: 0, duplicateBinFiles: 0 };
  return `<section class="workflow-result card" role="status"><p class="eyebrow">Verify</p><h2>Test move verified</h2><div class="verification-counts"><strong>${counts.movedFiles} ${counts.movedFiles === 1 ? "file" : "files"} moved</strong><span>${counts.renamedFiles} ${counts.renamedFiles === 1 ? "name" : "names"} changed</span><span>${counts.createdFolders} ${counts.createdFolders === 1 ? "folder" : "folders"} created</span><span>${counts.duplicateBinFiles} ${counts.duplicateBinFiles === 1 ? "copy" : "copies"} moved to Duplicate Bin</span></div><p>No real files were changed.</p><div>${button("Done", "complete-workflow", { primary: true })}${button("View Activity", "open-activity")}</div></section>`;
}

export function renderOrganize(store) {
  const state = store.getState();
  if (["moving", "verifying"].includes(state.workflowStage)) {
    return `${pageHeader("Organize", "The test move and its checks are separate steps.")}${stages(state)}${workflowResult(store)}`;
  }
  const approval = store.canReviewAndApprove() ? `${button("Review and approve", "review-and-approve", { primary: true })}<small>No real files will be changed.</small>` : "";
  return `${pageHeader("Organize", "Choose what happens, then check the compact file map.", approval)}${styleControl(store)}${keptByStandards(state)}<div class="organize-layout"><div class="organize-main">${changesPanel(store)}<section class="movement-panel card" aria-labelledby="movement-heading"><header class="section-header"><div><p class="eyebrow">Accepted changes</p><h2 id="movement-heading">Where files will go</h2></div><span class="panel-note">Shown automatically</span></header>${renderMovementTree(state.changes)}</section></div>${itemDetails(store)}</div>`;
}

function duplicateRow(item, state) {
  const selected = state.selectedBinIds.includes(item.id);
  return `<div class="bin-row" role="row"><span role="cell"><input type="checkbox" data-action="toggle-bin" data-bin-id="${item.id}" aria-label="Select ${escapeHtml(item.name)}" ${selected ? "checked" : ""}></span><span class="bin-name" role="cell">${fileIcon(item.icon, item.kind)}<strong>${escapeHtml(item.name)}</strong></span><span role="cell">${breadcrumb(item.originalFolder)}</span><span role="cell">${escapeHtml(item.dateMoved)}</span><span role="cell">${escapeHtml(item.size)}</span><span role="cell">${escapeHtml(item.kind)}</span><span role="cell">${button("Restore", "restore-one", { attrs: `data-bin-id="${item.id}"` })}</span></div>`;
}

export function renderDuplicateBin(store) {
  const state = store.getState();
  const items = state.duplicateBinItems;
  return `${pageHeader("Duplicate Bin", "Duplicate copies moved out of the way.", button("Restore selected", "restore-selected", { primary: true, disabled: !state.selectedBinIds.length }))}<section class="bin-card card">${items.length ? `<div class="bin-toolbar"><label><input type="checkbox" data-action="select-all-bin" ${state.selectedBinIds.length === items.length ? "checked" : ""}> Select all</label><strong>${items.length} ${items.length === 1 ? "file" : "files"}</strong></div><div class="bin-table" role="table" aria-label="Duplicate Bin"><div class="bin-row bin-header" role="row"><span role="columnheader"></span><span role="columnheader">Name</span><span role="columnheader">Original folder</span><span role="columnheader">Date moved</span><span role="columnheader">Size</span><span role="columnheader">Kind</span><span role="columnheader"></span></div>${items.map((item) => duplicateRow(item, state)).join("")}</div>` : '<div class="empty-state"><span class="empty-state-icon">0</span><h2>Duplicate Bin is empty</h2><p>Copies moved here during organization will appear here.</p></div>'}</section>`;
}

function activityCategory(title, items) {
  return `<section class="activity-category"><div><h3>${escapeHtml(title)}</h3><strong>${items.length}</strong></div>${items.length ? `<details><summary>Show ${items.length} ${items.length === 1 ? "file" : "files"}</summary><ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></details>` : '<span class="none-label">None</span>'}</section>`;
}

function activityRow(entry, expanded) {
  const categories = [
    ["Files moved", entry.details.filesMoved],
    ["Names changed", entry.details.namesChanged],
    ["Folders created", entry.details.foldersCreated],
    ["Copies moved to Duplicate Bin", entry.details.copiesMoved],
    ["Files restored", entry.details.filesRestored],
    ["Problems", entry.details.problems],
  ];
  return `<article class="activity-row"><header><div><h2>${escapeHtml(entry.title)}</h2><p>${escapeHtml(entry.summary)}</p><span>${escapeHtml(entry.status)} · ${escapeHtml(entry.time)}</span></div><div>${button(expanded ? "Hide details" : "Details", "toggle-activity", { attrs: `data-activity-id="${entry.id}" aria-expanded="${expanded}"` })}${entry.undoable ? button("Undo move", "undo-move", { primary: true }) : ""}</div></header>${expanded ? `<div class="activity-details">${categories.map(([title, items]) => activityCategory(title, items)).join("")}</div>` : ""}</article>`;
}

export function renderActivity(store) {
  const state = store.getState();
  return `${pageHeader("Activity", "File moves and undo.", `${button("Expand all", "expand-all-activity")}${button("Collapse all", "collapse-all-activity")}`)}<section class="activity-list" aria-label="Completed file moves">${state.activityMoves.length ? state.activityMoves.map((entry) => activityRow(entry, state.expandedActivityIds.includes(entry.id))).join("") : '<div class="empty-state"><h2>No file moves yet</h2><p>Completed moves will appear here.</p></div>'}</section>`;
}

function standardRow(standard) {
  return `<article class="standard-row"><div><h2>${escapeHtml(standard.name)}</h2><p>${escapeHtml(standard.description)}</p><span>Applies to: ${breadcrumb(standard.appliesTo)}</span></div><strong class="standard-state ${standard.active ? "on" : ""}">${standard.active ? "On" : "Paused"}</strong><div>${button("Edit", "open-standard-builder", { quiet: true, attrs: `data-standard-id="${standard.id}"` })}${button(standard.active ? "Pause" : "Turn on", "toggle-standard", { attrs: `data-standard-id="${standard.id}"` })}</div></article>`;
}

function suggestedStandards(state) {
  const interpretation = state.interpretation;
  if (interpretation.status === "clarification") {
    return `<div class="interpretation-result clarification-result" role="status"><strong>${escapeHtml(interpretation.clarification.question)}</strong><div>${interpretation.clarification.choices.map((choice) => button(choice.label, "clarify-preference", { attrs: `data-interpret-choice="${escapeHtml(choice.label)}"` })).join("")}</div></div>`;
  }
  if (interpretation.status === "unsafe") {
    return `<div class="interpretation-result unsafe-result" role="status"><strong>This cannot be turned into a safe Standard.</strong><p>Try one of these supported alternatives:</p><ul>${interpretation.alternatives.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>${button("Cancel", "cancel-interpretation")}</div>`;
  }
  if (interpretation.status === "unsupported") {
    return `<div class="interpretation-result" role="status"><strong>Try a more specific organization preference.</strong><p>Name what should stay together, what may be renamed, or which folder should stay unchanged.</p>${button("Cancel", "cancel-interpretation")}</div>`;
  }
  if (interpretation.status !== "suggested") return "";
  return `<div class="interpretation-result" role="status"><p>Loci Flow interpreted your request as:</p><h3>Suggested Standards</h3><div class="suggested-list">${interpretation.suggestions.map((suggestion, index) => `<article class="suggested-standard"><span>${index + 1}</span><div><strong>${escapeHtml(suggestion.name)}</strong><p>${escapeHtml(suggestion.description)}</p><small>Applies to: ${breadcrumb(suggestion.appliesTo)}</small></div><div>${button("Edit", "edit-suggested-standard", { quiet: true, attrs: `data-suggestion-id="${suggestion.id}"` })}${button("Remove", "remove-suggested-standard", { attrs: `data-suggestion-id="${suggestion.id}"` })}</div></article>`).join("")}</div><div class="interpretation-actions">${button("Cancel", "cancel-interpretation")}${button("Use these Standards", "confirm-suggested-standards", { primary: true, disabled: !interpretation.suggestions.length })}</div></div>`;
}

function interpretationPanel(state) {
  return `<section class="interpretation-panel card" aria-labelledby="interpretation-heading"><div><p class="eyebrow">Fixture-driven interpretation</p><h2 id="interpretation-heading">Describe an organization preference</h2><p>Loci Flow turns your description into Standards for you to review.</p></div><label><span class="visually-hidden">Organization preference</span><textarea id="preference-input" rows="2" placeholder="Keep my coding projects together and rename only standalone files without breaking imports.">${escapeHtml(state.interpretation.text)}</textarea></label>${button("Interpret preference", "interpret-preference", { primary: true })}${suggestedStandards(state)}</section>`;
}

export function renderStandards(store) {
  const state = store.getState();
  return `${pageHeader("Standards", "Reusable instructions for future organization.", button("Add standard", "open-standard-builder", { primary: true }))}${interpretationPanel(state)}<section class="standards-intro card"><div><strong>Simple and reusable</strong><span>Standards use familiar choices and sample folders.</span></div>${button("See a sample disagreement", "open-standard-conflict", { quiet: true })}</section><section class="standards-list" aria-label="Saved standards">${state.standards.map(standardRow).join("")}</section>`;
}

export const SETTINGS_SECTIONS = [
  ["appearance", "Appearance"],
  ["notifications", "Notifications"],
  ["local-ai", "Local AI"],
  ["privacy", "Privacy"],
  ["advanced", "Advanced"],
];

function settingSwitch(label, help, key, checked) {
  return `<label class="setting-row"><span><strong>${escapeHtml(label)}</strong><small>${escapeHtml(help)}</small></span><input type="checkbox" data-action="toggle-notification" data-setting-key="${key}" ${checked ? "checked" : ""}></label>`;
}

function settingsContent(store) {
  const state = store.getState();
  const section = state.settingsSection;
  if (section === "appearance") return `<header><h2>Appearance</h2><p>Use one synchronized choice throughout the app.</p></header><div class="settings-card"><fieldset class="segmented settings-theme" aria-label="Appearance"><button type="button" data-action="set-theme" data-theme-value="light" aria-pressed="${state.theme === "light"}">Light</button><button type="button" data-action="set-theme" data-theme-value="dark" aria-pressed="${state.theme === "dark"}">Dark</button></fieldset></div>`;
  if (section === "notifications") return `<header><h2>Notifications</h2><p>Sample choices only. Browser permission is not requested.</p></header><div class="settings-card">${settingSwitch("Organization ready", "Let me know when choices are waiting.", "organizationReady", state.settings.notifications.organizationReady)}${settingSwitch("Move completed", "Let me know when a file move finishes.", "moveCompleted", state.settings.notifications.moveCompleted)}${settingSwitch("Move failed", "Let me know when a file could not be moved.", "moveFailed", state.settings.notifications.moveFailed)}</div>`;
  if (section === "local-ai") return `<header><h2>Local AI</h2><p>Optional advice remains narrow and local.</p></header><div class="settings-card statements"><p><strong>Off by default</strong></p><p><strong>Local Ollama only</strong></p><p>No connection is made in this prototype.</p></div>`;
  if (section === "privacy") return `<header><h2>Privacy</h2><p>Clear boundaries for this prototype.</p></header><div class="settings-card statements"><p><strong>Sample files only</strong></p><p>No real files are accessed.</p><p>No telemetry.</p><p>No data is sent online.</p></div>`;
  return `<header><h2>Advanced</h2><p>Return the authored sample state when needed.</p></header><div class="settings-card advanced-actions"><div><strong>Reset prototype</strong><span>Return navigation and choices to their starting state.</span>${button("Reset prototype", "reset-prototype", { danger: true })}</div><div><strong>Restore sample data</strong><span>Restore the same starting sample.</span>${button("Restore sample data", "reset-prototype")}</div></div>`;
}

export function renderSettings(store) {
  const section = store.getState().settingsSection;
  return `${pageHeader("Settings", "A small set of clear app preferences.")}<div class="settings-layout"><nav class="settings-nav" aria-label="Settings sections">${SETTINGS_SECTIONS.map(([id, label]) => `<button type="button" data-action="set-settings-section" data-section="${id}" aria-current="${section === id ? "page" : "false"}">${escapeHtml(label)}</button>`).join("")}</nav><section class="settings-content">${settingsContent(store)}</section></div>`;
}

export function renderRoute(store, route) {
  if (route === "organize") return renderOrganize(store);
  if (route === "duplicate-bin") return renderDuplicateBin(store);
  if (route === "activity") return renderActivity(store);
  if (route === "standards") return renderStandards(store);
  if (route === "settings") return renderSettings(store);
  return renderHome(store);
}

function folderPicker(store, name = "standard-folder") {
  return `<div class="folder-picker" role="radiogroup" aria-label="Sample folders">${store.fixtures.folderChoices.map((parts, index) => `<label><input type="radio" name="${name}" value="${escapeHtml(parts.join("|"))}" ${index === 1 ? "checked" : ""}>${fileIcon("folder", "Folder")}${breadcrumb(parts)}</label>`).join("")}</div>`;
}

export function renderDialog(store, type, payload = {}) {
  if (type === "folder") return {
    eyebrow: "Choose a sample folder",
    title: "Change folder",
    description: "Select a clear folder from the sample.",
    content: folderPicker(store, "change-folder-choice"),
    actions: `${button("Cancel", "close-dialog")}${button("Confirm folder", "confirm-folder", { primary: true, attrs: `data-change-id="${payload.changeId}"` })}`,
  };
  if (type === "standard-builder" && payload.suggestedStandardId) {
    const suggestion = store.getState().interpretation.suggestions.find((item) => item.id === payload.suggestedStandardId);
    if (suggestion) return {
      eyebrow: "Suggested Standard",
      title: "Edit suggested standard",
      description: "Review the interpreted instruction before it becomes active.",
      content: `<div class="suggestion-editor"><label class="builder-field"><span>Name</span><input id="suggestion-name" type="text" value="${escapeHtml(suggestion.name)}"></label><label class="builder-field"><span>Description</span><textarea id="suggestion-description" rows="3">${escapeHtml(suggestion.description)}</textarea></label><label class="builder-field"><span>Applies to</span><input id="suggestion-folder" type="text" value="${escapeHtml(suggestion.appliesTo.join(" › "))}"></label></div>`,
      actions: `${button("Cancel", "close-dialog")}${button("Update suggestion", "save-suggested-standard", { primary: true, attrs: `data-suggestion-id="${payload.suggestedStandardId}"` })}`,
    };
  }
  if (type === "standard-builder") return {
    eyebrow: "Guided builder",
    title: payload.standardId ? "Edit standard" : "Add standard",
    description: "Choose familiar instructions and sample folders.",
    content: `<fieldset class="builder-section choice-cards"><legend>What should Loci Flow do?</legend>${["Keep a folder unchanged", "Keep files together", "Keep current file names", "Use existing folders only", "Sort similar files"].map((label) => `<label><input type="radio" name="standard-action" value="${label}" ${label === "Sort similar files" ? "checked" : ""}><span>${label}</span></label>`).join("")}</fieldset><div class="builder-grid"><label class="builder-field"><span>What should Loci Flow recognize?</span><select id="standard-recognize"><option>Course code</option><option selected>Person name</option><option>Project code</option><option>Year</option></select></label><div class="preset-examples"><strong>Examples</strong><span>Course code: CS2030S, MA2001</span><span>Person name: Aisha, Benjamin Tan</span><span>Project code: Elarian, PRJ-204</span><span>Year: 2025, 2026</span></div></div><fieldset class="builder-section choice-cards compact"><legend>How should these files be grouped?</legend>${["Create a folder for each value", "Move into an existing matching folder", "Keep matching files together"].map((label, index) => `<label><input type="radio" name="standard-grouping" value="${label}" ${index === 0 ? "checked" : ""}><span>${label}</span></label>`).join("")}</fieldset><label class="second-level"><input type="checkbox" id="standard-file-type"> <span><strong>Add another level</strong><small>File type · Aisha_Lab03.py → Aisha › Code</small></span></label><fieldset class="builder-section"><legend>Where should this work?</legend>${folderPicker(store)}</fieldset>`,
    actions: `${button("Cancel", "close-dialog")}${button("Save standard", "save-standard", { primary: true })}`,
  };
  if (type === "standard-conflict") return {
    eyebrow: "Choose which instruction to use",
    title: "These standards do not agree",
    description: "Pick the plain-language result you want.",
    content: `<div class="standard-comparison"><section><span>Existing</span><strong>Keep all student files together</strong></section><section><span>New</span><strong>Group student files by file type</strong></section></div><div class="conflict-actions">${button("Use the new standard here", "close-dialog")}${button("Change the existing standard", "close-dialog")}${button("Use both in different folders", "close-dialog")}${button("Keep the existing standard", "close-dialog")}</div>`,
    actions: button("Cancel", "close-dialog"),
  };
  if (type === "approval") {
    const counts = store.approvalSummary();
    return {
    eyebrow: "Final approval",
    title: "Ready to organize",
    description: "No real files will be changed.",
    content: `<div class="approval-summary"><p><strong>${counts.movedFiles}</strong><span>${counts.movedFiles === 1 ? "file will move" : "files will move"}</span></p><p><strong>${counts.renamedFiles}</strong><span>${counts.renamedFiles === 1 ? "name will change" : "names will change"}</span></p><p><strong>${counts.createdFolders}</strong><span>${counts.createdFolders === 1 ? "folder will be created" : "folders will be created"}</span></p><p><strong>${counts.duplicateBinFiles}</strong><span>${counts.duplicateBinFiles === 1 ? "copy will move" : "copies will move"} to Duplicate Bin</span></p></div>`,
    actions: `${button("Back to changes", "close-dialog")}${button("Approve test move", "approve-test-move", { primary: true })}`,
    };
  };
  return { eyebrow: "Loci Flow", title: "Confirm", description: "Choose what happens next.", content: "", actions: button("Close", "close-dialog") };
}
