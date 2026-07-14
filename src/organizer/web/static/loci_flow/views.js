const ICON_SPRITE = "/static/loci_flow/assets/file-icons.svg";
const SVG_NAMESPACE = ["http:", "", "www.w3.org", "2000", "svg"].join("/");

const KIND_LABELS = Object.freeze({
  folder: "Folder",
  pdf: "PDF",
  text: "Text",
  markdown: "Markdown",
  word: "Word document",
  spreadsheet: "Spreadsheet",
  image: "Image",
  presentation: "Presentation",
  source_code: "Source code",
  archive: "ZIP/archive",
  document: "Document",
});

const ICON_KEYS = Object.freeze({ text: "txt", word: "document", source_code: "code" });

const REASON_LABELS = Object.freeze({
  matched_existing_group: "A clear destination matches this file.",
  multiple_safe_destinations: "More than one safe folder fits.",
  review_risk_requires_choice: "A closer look is recommended.",
  exact_duplicate_choice: "Identical copies need your decision.",
  project_folder_protected: "Project folder kept unchanged.",
  submission_folder_protected: "Submission folder kept unchanged.",
  shared_folder_protected: "Shared folder kept unchanged.",
});

const OPTION_LABELS = Object.freeze({
  keep: "Keep here",
  keep_all_copies: "Keep all copies",
  choose_copies_for_duplicate_bin: "Choose copies for Duplicate Bin",
});

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  for (const [name, value] of Object.entries(options.attrs || {})) {
    if (value !== null && value !== false) node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const [name, value] of Object.entries(options.dataset || {})) node.dataset[name] = String(value);
  for (const child of Array.isArray(children) ? children : [children]) if (child) node.append(child);
  return node;
}

function icon(kind, className = "file-icon") {
  const knownKind = Object.hasOwn(KIND_LABELS, kind) ? kind : "document";
  const iconKey = ICON_KEYS[knownKind] || knownKind;
  const svg = document.createElementNS(SVG_NAMESPACE, "svg");
  const use = document.createElementNS(SVG_NAMESPACE, "use");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", `${ICON_SPRITE}#${iconKey}`);
  svg.append(use);
  return svg;
}

function pageHeader(eyebrow, title, copy, action = null) {
  const intro = el("div", {}, [
    el("div", { className: "eyebrow", text: eyebrow }),
    el("h1", { text: title }),
    el("p", { text: copy }),
  ]);
  return el("header", { className: "page-header" }, action ? [intro, action] : [intro]);
}

function stageStrip(active) {
  const stages = ["Check", "Organize", "Approve", "Move", "Verify"];
  return el("div", { className: "stage-strip", attrs: { "aria-label": "Organization stages" } }, stages.map((label, index) =>
    el("div", { className: `stage${index < active ? " complete" : index === active ? " active" : ""}`, text: `${index + 1}. ${label}` }),
  ));
}

export function formatDate(value) {
  if (!value) return "No organization history yet";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function reasonLabel(code) {
  return REASON_LABELS[code] || "Organization information is available for review.";
}

function optionLabel(option) {
  if (option.action === "move") return `Move to ${option.destination_folder}`;
  return OPTION_LABELS[option.action] || "Review this option";
}

function metric(label, count, tone = "") {
  return el("div", { className: `card metric ${tone}`.trim() }, [el("strong", { text: count }), el("span", { text: label })]);
}

function scopeCard(slice) {
  return el("section", { className: "card scope-card", attrs: { "aria-labelledby": "scope-heading" } }, [
    el("div", { className: "scope-main" }, [
      icon("folder", "scope-icon"),
      el("div", { className: "scope-copy" }, [
        el("div", { className: "eyebrow", text: "Selected scope" }),
        el("h2", { text: slice.scope.display_name, attrs: { id: "scope-heading" } }),
        el("p", { text: "This root is fixed by the running bootAI session." }),
      ]),
    ]),
    el("div", { className: "scope-facts" }, [
      el("div", { className: "fact" }, [el("strong", { text: formatDate(slice.freshness.last_checked_at) }), el("span", { text: "Last checked" })]),
      el("div", { className: "fact" }, [el("strong", { text: slice.freshness.last_organized_at ? formatDate(slice.freshness.last_organized_at) : "No moves recorded yet" }), el("span", { text: "Last organized" })]),
    ]),
  ]);
}

function summaryList(summary) {
  const rows = [
    ["Files to move", summary.files_to_move],
    ["Names to change", summary.names_to_change],
    ["Folders to create", summary.folders_to_create],
    ["Duplicate copies", summary.duplicate_copies],
  ];
  return el("ul", { className: "summary-list" }, rows.map(([label, value]) => el("li", {}, [el("span", { text: label }), el("strong", { text: value })])));
}

export function renderHome(slice) {
  const totalChanges = slice.review?.summary.choices_remaining ?? slice.summary.recommended_changes + slice.summary.choices_needed;
  const continueButton = totalChanges
    ? el("button", { className: "button primary", text: "Continue organizing", attrs: { type: "button" }, dataset: { action: "continue-organizing" } })
    : el("a", { className: "button primary", text: "Check again", attrs: { href: "/" } });
  return el("div", {}, [
    pageHeader("Organization review", totalChanges ? `${totalChanges} choices are waiting` : "Review is complete", "Live results and temporary decisions from bootAI’s deterministic boundary."),
    stageStrip(totalChanges ? 1 : -1),
    scopeCard(slice),
    el("section", { className: "metric-grid", attrs: { "aria-label": "Organization totals" } }, [
      metric("Recommended changes", slice.summary.recommended_changes, "recommended"),
      metric("Choices needed", slice.summary.choices_needed, "choice"),
      metric("Kept by Standards", slice.summary.kept_by_standards, "protected"),
      metric("Files to move", slice.review?.summary.files_to_move ?? slice.summary.files_to_move),
    ]),
    el("div", { className: "home-grid" }, [
      el("section", { className: "card card-body next-step" }, [
        el("div", {}, [
          el("div", { className: "eyebrow", text: totalChanges ? "Ready to inspect" : "Up to date" }),
          el("h2", { text: totalChanges ? "See the plan before anything changes" : "Everything is organized" }),
          el("p", { text: totalChanges ? "Recommendations and choices can be reviewed without moving files." : "You can inspect the complete approved or reviewed result." }),
        ]),
        continueButton,
      ]),
      el("section", { className: "card", attrs: { "aria-labelledby": "movement-summary-heading" } }, [
        el("div", { className: "card-header" }, [el("div", {}, [el("h2", { text: "Movement summary", attrs: { id: "movement-summary-heading" } }), el("p", { text: "Counts supplied by the backend" })])]),
        el("div", { className: "card-body" }, summaryList(slice.summary)),
      ]),
    ]),
    el("div", { className: "history-grid" }, [
      el("section", { className: "card card-body" }, [el("div", { className: "eyebrow", text: "Duplicate Bin" }), el("h2", { text: "Not connected yet" }), el("p", { text: "Duplicate choices are available under Organize; no copies can be moved in this stage." })]),
      el("section", { className: "card card-body" }, [el("div", { className: "eyebrow", text: "Last file move" }), el("h2", { text: "No moves recorded yet" }), el("p", { text: "The read-only contract contains no operation history." })]),
    ]),
  ]);
}

function snapshotForChoice(choice) {
  return choice.item || choice.copies[0];
}

function reviewRecommendation(slice, changeId) {
  return slice.review?.recommendations.find((item) => item.change_id === changeId) || { decision: "undecided" };
}

function reviewChoice(slice, choiceId) {
  return slice.review?.choices.find((item) => item.choice_id === choiceId) || { option_id: null, selected_copy_ids: [] };
}

function changeRow(entry, type, selectedId, slice, ui) {
  const item = type === "choice" ? snapshotForChoice(entry) : entry.item;
  const review = type === "recommended" ? reviewRecommendation(slice, entry.id) : reviewChoice(slice, entry.id);
  const status = type === "recommended"
    ? review.decision === "accept" ? "Accepted" : review.decision === "keep" ? "Kept here" : "Recommended"
    : review.option_id ? "Choice saved" : "Choice needed";
  const subtitle = type === "recommended"
    ? `Move · ${formatBytes(item.size_bytes)} · ${item.current_folder} → ${entry.destination_folder}`
    : `${KIND_LABELS[item.kind] || KIND_LABELS.document} · ${formatBytes(item.size_bytes)} · ${reasonLabel(entry.reason_code)}`;
  const button = el("button", {
    className: `change-row${entry.id === selectedId ? " selected" : ""}`,
    attrs: { type: "button", "aria-pressed": entry.id === selectedId ? "true" : "false", "aria-label": `${item.name}, ${KIND_LABELS[item.kind] || KIND_LABELS.document}, ${type === "recommended" ? "recommended change" : "choice needed"}` },
    dataset: { selectId: entry.id },
  }, [
    icon(item.kind),
    el("span", { className: "row-copy" }, [el("strong", { text: item.name }), el("span", { text: subtitle })]),
    el("span", { className: `row-status ${type}`, text: status }),
  ]);
  if (type === "recommended") {
    const actions = el("div", { className: "row-actions", attrs: { "aria-label": `Decision for ${item.name}` } }, [
      el("button", { className: "button compact", text: "Accept", attrs: { type: "button", disabled: ui.busy || slice.review.status === "ready_to_move" }, dataset: { action: "recommendation-decision", changeId: entry.id, decision: "accept" } }),
      el("button", { className: "button compact", text: "Keep here", attrs: { type: "button", disabled: ui.busy || slice.review.status === "ready_to_move" }, dataset: { action: "recommendation-decision", changeId: entry.id, decision: "keep" } }),
    ]);
    return el("div", { className: "decision-row" }, [button, actions]);
  }
  const options = el("div", { className: "option-actions", attrs: { "aria-label": `Options for ${item.name}` } }, entry.options.map((option) => {
    const action = option.id === "choose-for-bin" ? "start-duplicate-choice" : "choice-option";
    return el("button", {
      className: `button compact${review.option_id === option.id ? " selected-option" : ""}`,
      text: optionLabel(option),
      attrs: { type: "button", "aria-pressed": review.option_id === option.id ? "true" : "false", disabled: ui.busy || slice.review.status === "ready_to_move" },
      dataset: { action, choiceId: entry.id, optionId: option.id },
    });
  }));
  const children = [button, options];
  if (entry.choice_type === "exact_duplicate" && ui.duplicateDrafts?.has(entry.id)) {
    const selected = ui.duplicateDrafts.get(entry.id);
    const copies = el("fieldset", { className: "copy-picker" }, [
      el("legend", { text: "Choose copies for future Duplicate Bin" }),
      ...entry.copies.map((copy) => el("label", {}, [
        el("input", { attrs: { type: "checkbox", value: copy.id, checked: selected.has(copy.id) ? "checked" : null }, dataset: { action: "toggle-duplicate-copy", choiceId: entry.id, copyId: copy.id } }),
        el("span", { text: `${copy.name} · ${copy.current_folder}` }),
      ])),
      el("p", { className: "inline-note", text: "Select at least one copy and leave at least one unselected." }),
      el("button", {
        className: "button primary compact",
        text: "Save duplicate choice",
        attrs: { type: "button", disabled: selected.size < 1 || selected.size >= entry.copies.length || ui.busy },
        dataset: { action: "save-duplicate-choice", choiceId: entry.id },
      }),
    ]);
    children.push(copies);
  }
  return el("div", { className: "decision-row" }, children);
}

function keptRow(kept, selectedId) {
  const selectionId = `standard:${kept.item.id}`;
  return el("button", {
    className: `change-row${selectionId === selectedId ? " selected" : ""}`,
    attrs: { type: "button", "aria-pressed": selectionId === selectedId ? "true" : "false", "aria-label": `${kept.item.name}, ${KIND_LABELS[kept.item.kind] || KIND_LABELS.document}, kept by ${kept.standard.name}` },
    dataset: { selectId: selectionId },
  }, [
    icon(kept.item.kind),
    el("span", { className: "row-copy" }, [el("strong", { text: kept.item.name }), el("span", { text: `${kept.standard.name} · ${reasonLabel(kept.reason_code)}` })]),
    el("span", { className: "row-status protected", text: "Kept" }),
  ]);
}

function sectionCard(title, description, count, rows, emptyCopy) {
  return el("section", { className: "card" }, [
    el("div", { className: "card-header" }, [
      el("div", {}, [el("h2", { text: title }), el("p", { text: description })]),
      el("span", { className: "section-count", text: count, attrs: { "aria-label": `${count} ${title.toLowerCase()}` } }),
    ]),
    rows.length ? el("div", { className: "row-list" }, rows) : el("div", { className: "card-body", text: emptyCopy }),
  ]);
}

function keptCard(count, rows) {
  return el("details", { className: "card kept-card" }, [
    el("summary", {}, [
      el("span", {}, [el("strong", { text: "Kept by Standards" }), el("small", { text: `${count} files kept in place` })]),
      el("span", { className: "section-count", text: count }),
    ]),
    rows.length ? el("div", { className: "row-list" }, rows) : el("div", { className: "card-body", text: "No files were kept by active Standards." }),
  ]);
}

function findSelected(slice, selectedId) {
  const recommended = slice.recommended_changes.find((entry) => entry.id === selectedId);
  if (recommended) return { entry: recommended, type: "recommended", item: recommended.item };
  const choice = slice.choices_needed.find((entry) => entry.id === selectedId);
  if (choice) return { entry: choice, type: "choice", item: snapshotForChoice(choice) };
  const kept = slice.kept_by_standards.find((entry) => `standard:${entry.item.id}` === selectedId);
  if (kept) return { entry: kept, type: "kept", item: kept.item };
  return null;
}

function detailsCard(slice, selectedId) {
  const selected = findSelected(slice, selectedId);
  if (!selected) return el("section", { className: "card card-body" }, [
    el("h2", { text: "Change details" }),
    el("p", { text: "Select a recommended change or choice to inspect its backend-provided metadata." }),
  ]);
  const { entry, item, type } = selected;
  const details = [
    ["Item", item.name], ["Kind", KIND_LABELS[item.kind] || KIND_LABELS.document],
    ["Current folder", item.current_folder], ["Relative path", item.relative_path],
    ["Size", formatBytes(item.size_bytes)], ["Modified", formatDate(item.modified_at)],
    ["Why", reasonLabel(entry.reason_code)],
  ];
  if (type === "recommended") details.splice(4, 0, ["New folder", entry.destination_folder]);
  if (type === "choice") {
    if (entry.identical_copy_count !== undefined) {
      details.splice(4, 0, ["Matching copies", entry.identical_copy_count]);
      details.push(["Copy locations", entry.copies.map((copy) => copy.current_folder).join(", ")]);
    }
    details.push(["Available actions", entry.options.map(optionLabel).join("; ")]);
  }
  if (type === "kept") {
    details.splice(4, 0, ["Standard", entry.standard.name]);
    details.push(["Status", "Kept in place"]);
  }
  return el("section", { className: "card" }, [
    el("div", { className: "card-header" }, [el("div", {}, [el("h2", { text: "Change details" }), el("p", { text: "Read-only backend metadata" })])]),
    el("div", { className: "card-body" }, [
      el("dl", { className: "detail-grid" }, details.map(([term, value]) => el("div", { className: "detail-item" }, [el("dt", { text: term }), el("dd", { text: value })]))),
      el("p", { className: "inline-note", text: "Decisions are temporary. No file is moved from this screen." }),
    ]),
  ]);
}

function fileKindByPath(slice, relativePath) {
  for (const change of slice.recommended_changes) if (change.item.relative_path === relativePath) return change.item.kind;
  return "document";
}

function movementTree(slice) {
  const entries = slice.summary.movement_tree.map((entry) => el("li", {}, [
    el("div", { className: "movement-folder" }, [icon("folder"), el("span", { text: entry.folder })]),
    el("ul", {}, entry.files.map((relativePath) => el("li", { className: "movement-file" }, [icon(fileKindByPath(slice, relativePath)), el("span", { text: relativePath })]))),
  ]));
  return el("section", { className: "card" }, [
    el("div", { className: "card-header" }, [el("div", {}, [el("h2", { text: "Movement tree" }), el("p", { text: "Backend-provided destinations" })])]),
    el("div", { className: "card-body" }, entries.length ? el("ul", { className: "movement-tree" }, entries) : el("p", { text: "No file movement is proposed." })),
  ]);
}

export function renderOrganize(slice, selectedId, ui = {}) {
  const state = { busy: false, duplicateDrafts: new Map(), ...ui };
  const recommendedRows = slice.recommended_changes.map((entry) => changeRow(entry, "recommended", selectedId, slice, state));
  const choiceRows = slice.choices_needed.map((entry) => changeRow(entry, "choice", selectedId, slice, state));
  const keptRows = slice.kept_by_standards.map((entry) => keptRow(entry, selectedId));
  const pendingIds = slice.review.recommendations.filter((item) => item.decision === "undecided").map((item) => item.change_id);
  const bulk = el("button", {
    className: "button primary",
    text: "Accept all recommended changes",
    attrs: { type: "button", disabled: !pendingIds.length || state.busy || slice.review.status === "ready_to_move" },
    dataset: { action: "accept-all-recommended" },
  });
  const approve = slice.review.summary.ready_for_approval
    ? el("button", { className: "button primary", text: "Review and approve", attrs: { type: "button", disabled: state.busy }, dataset: { action: "open-approval" } })
    : null;
  return el("div", {}, [
    pageHeader("Temporary decisions", "Organize", "Choose what should happen. No files move while you review.", bulk),
    stageStrip(1),
    el("p", { className: "inline-note", text: "Organization styles are not connected yet." }),
    state.errorMessage ? el("div", { className: "inline-error", text: state.errorMessage, attrs: { role: "alert" } }) : null,
    el("div", { className: "organize-layout" }, [
      el("div", { className: "change-stack" }, [
        sectionCard("Recommended changes", "Clear moves identified by the planner", slice.summary.recommended_changes, recommendedRows, "No recommended changes in this scan."),
        sectionCard("Choices needed", "Options are shown without saving a decision", slice.summary.choices_needed, choiceRows, "No choices are needed in this scan."),
        keptCard(slice.summary.kept_by_standards, keptRows),
      ]),
      el("aside", { className: "details-column", attrs: { "aria-label": "Organization details" } }, [detailsCard(slice, selectedId), movementTree(slice)]),
    ]),
    el("section", { className: "card card-body approval-callout" }, [
      el("div", {}, [
        el("div", { className: "eyebrow", text: slice.review.summary.ready_for_approval ? "Ready for final review" : "Choices remaining" }),
        el("h2", { text: slice.review.summary.ready_for_approval ? "Review the complete plan" : `${slice.review.summary.choices_remaining} decisions still needed` }),
        el("p", { text: "Approval performs a fresh backend safety check. No files have moved yet." }),
      ]),
      approve,
    ]),
  ]);
}

export function renderApproval(slice, busy = false, errorMessage = "") {
  const summary = slice.review.summary;
  return el("div", {}, [
    pageHeader("Final review", "Ready to organize", "Confirm the backend-derived summary before approval."),
    stageStrip(2),
    errorMessage ? el("div", { className: "inline-error", text: errorMessage, attrs: { role: "alert" } }) : null,
    el("section", { className: "card approval-panel" }, [
      el("h2", { text: `${summary.files_to_move} files will move` }),
      el("p", { text: `${summary.duplicate_copies_for_future_bin} duplicate copies are marked for Duplicate Bin.` }),
      el("p", { className: "inline-note", text: "No files have moved yet." }),
      el("div", { className: "approval-actions" }, [
        el("button", { className: "button", text: "Back to changes", attrs: { type: "button", disabled: busy }, dataset: { action: "close-approval" } }),
        el("button", { className: "button primary", text: busy ? "Checking…" : "Approve", attrs: { type: "button", disabled: busy }, dataset: { action: "approve-organization" } }),
      ]),
    ]),
  ]);
}

export function renderReady(slice, showSummary = false) {
  const summary = slice.review.approval.summary;
  return el("div", {}, [
    pageHeader("Approved plan", "Ready to move", "The organization has been checked and approved."),
    stageStrip(3),
    el("section", { className: "card approval-panel" }, [
      el("h2", { text: "No files have moved yet." }),
      el("p", { text: `${summary.files_to_move} files are approved to move. ${summary.duplicate_copies_for_future_bin} duplicate copies are marked for future Duplicate Bin handling.` }),
      showSummary ? el("dl", { className: "detail-grid" }, [
        ["Files to move", summary.files_to_move],
        ["Duplicate copies marked", summary.duplicate_copies_for_future_bin],
        ["Names to change", summary.names_to_change],
        ["Folders to create", summary.folders_to_create],
      ].map(([term, value]) => el("div", { className: "detail-item" }, [el("dt", { text: term }), el("dd", { text: value })]))) : null,
      el("div", { className: "approval-actions" }, [
        el("a", { className: "button", text: "Back to Home", attrs: { href: "#/home" } }),
        el("button", { className: "button", text: showSummary ? "Hide approved summary" : "View approved summary", attrs: { type: "button" }, dataset: { action: "toggle-approved-summary" } }),
        el("button", { className: "button primary", text: "Move files — not connected yet", attrs: { type: "button", disabled: "disabled", "aria-disabled": "true" } }),
      ]),
    ]),
  ]);
}

const ACTIVITY_STATUS_LABELS = Object.freeze({
  verified: "Completed",
  partial_movement: "Partly completed",
  movement_failed: "Failed",
  verification_failed: "Could not confirm",
  restored: "Restored",
  partial_undo: "Partly completed",
  undo_failed: "Failed",
  undo_verification_failed: "Could not confirm",
  prepared: "Needs attention",
  moving: "Needs attention",
  moved_unverified: "Needs attention",
  undoing: "Needs attention",
  restored_unverified: "Needs attention",
  interrupted: "Needs attention",
  recovery_required: "Needs attention",
});

function activitySummary(entry) {
  if (entry.type === "restore") {
    const parts = [`${entry.files_restored} ${entry.files_restored === 1 ? "file" : "files"} restored`];
    if (entry.files_failed) parts.push(`${entry.files_failed} failed`);
    if (entry.files_unattempted) parts.push(`${entry.files_unattempted} not attempted`);
    return parts.join(" · ");
  }
  const parts = [`${entry.files_moved} ${entry.files_moved === 1 ? "file" : "files"} moved`];
  if (entry.files_failed) parts.push(`${entry.files_failed} failed`);
  if (entry.files_unattempted) parts.push(`${entry.files_unattempted} not attempted`);
  return parts.join(" · ");
}

function activityDetailList(entry) {
  if (!entry.details.length) return el("p", { className: "activity-none", text: "No file details recorded." });
  return el("ul", { className: "activity-detail-list" }, entry.details.map((detail) => {
    const from = entry.type === "restore" ? detail.moved_from : detail.source;
    const to = entry.type === "restore" ? detail.restored_to : detail.destination;
    return el("li", {}, [
      el("span", { text: from }),
      el("span", { className: "activity-arrow", text: "→", attrs: { "aria-hidden": "true" } }),
      el("span", { text: to }),
      el("strong", { text: detail.status }),
    ]);
  }));
}

function activityRow(entry, expanded, busy) {
  const actions = [
    el("button", {
      className: "button compact",
      text: expanded ? "Hide details" : "Details",
      attrs: { type: "button", "aria-expanded": expanded },
      dataset: { action: "toggle-activity", activityId: entry.id },
    }),
  ];
  if (entry.undoable) actions.push(el("button", {
    className: "button primary compact",
    text: busy ? "Checking…" : "Undo move",
    attrs: { type: "button", disabled: busy },
    dataset: { action: "preview-undo", operationId: entry.id },
  }));
  return el("article", { className: "card activity-row" }, [
    el("header", {}, [
      el("div", {}, [
        el("h2", { text: entry.title }),
        el("p", { text: activitySummary(entry) }),
        el("span", { className: "activity-time", text: `${ACTIVITY_STATUS_LABELS[entry.status] || "Needs attention"} · ${formatDate(entry.completed_at || entry.started_at)}` }),
      ]),
      el("div", { className: "activity-actions" }, actions),
    ]),
    expanded ? el("div", { className: "activity-details" }, [activityDetailList(entry)]) : null,
  ]);
}

function undoDialog(preview, busy, errorMessage) {
  if (!preview) return null;
  return el("div", { className: "dialog-backdrop" }, [
    el("section", { className: "card undo-dialog", attrs: { role: "dialog", "aria-modal": "true", "aria-labelledby": "undo-dialog-title" } }, [
      el("h2", { text: "Undo this move?", attrs: { id: "undo-dialog-title" } }),
      el("p", { text: `${preview.files_to_restore} ${preview.files_to_restore === 1 ? "file" : "files"} will return to ${preview.files_to_restore === 1 ? "its" : "their"} previous folders.` }),
      errorMessage ? el("p", { className: "inline-error", text: errorMessage, attrs: { role: "alert" } }) : null,
      el("div", { className: "approval-actions" }, [
        el("button", { className: "button", text: "Cancel", attrs: { type: "button", disabled: busy }, dataset: { action: "cancel-undo" } }),
        el("button", { className: "button primary", text: busy ? "Restoring…" : "Undo move", attrs: { type: "button", disabled: busy }, dataset: { action: "confirm-undo" } }),
      ]),
    ]),
  ]);
}

export function renderActivity(activity, ui = {}) {
  const state = { status: "ready", expandedIds: new Set(), busy: false, preview: null, errorMessage: "", ...ui };
  const controls = el("div", { className: "activity-actions" }, [
    el("button", { className: "button compact", text: "Expand all", attrs: { type: "button", disabled: !activity.length }, dataset: { action: "expand-all-activity" } }),
    el("button", { className: "button compact", text: "Collapse all", attrs: { type: "button", disabled: !activity.length }, dataset: { action: "collapse-all-activity" } }),
  ]);
  let content;
  if (state.status === "loading" || state.status === "idle") content = el("p", { className: "loading", text: "Loading Activity…" });
  else if (state.status === "error") content = el("section", { className: "error-state" }, [
    el("h2", { text: "Activity could not be loaded." }),
    el("p", { text: state.errorMessage || "Try loading Activity again." }),
    el("button", { className: "button", text: "Try again", attrs: { type: "button" }, dataset: { action: "retry-activity" } }),
  ]);
  else if (!activity.length) content = el("section", { className: "empty-state" }, [
    el("h2", { text: "No file moves recorded." }),
    el("p", { text: "Completed organization moves and restores will appear here." }),
  ]);
  else content = el("section", { className: "activity-list", attrs: { "aria-label": "File movement Activity" } }, activity.map((entry) => activityRow(entry, state.expandedIds.has(entry.id), state.busy)));
  return el("div", {}, [
    pageHeader("Durable history", "Activity", "File moves and verified restores for this bootAI root.", controls),
    content,
    undoDialog(state.preview, state.busy, state.errorMessage),
  ]);
}

const NON_INTEGRATED = Object.freeze({
  "duplicate-bin": ["Duplicate Bin", "Duplicate Bin movement is not part of this read-only stage. Exact-copy choices appear under Organize."],
  standards: ["Standards", "Active protections are applied by the backend before this UI receives the plan. Standards editing is not connected here."],
});

export function renderUnintegrated(route) {
  const [title, copy] = NON_INTEGRATED[route];
  return el("div", {}, [
    pageHeader("Integration boundary", title, "This destination is preserved from the accepted prototype."),
    el("section", { className: "not-integrated" }, [
      el("h2", { text: route === "duplicate-bin" ? "Duplicate Bin is not connected yet." : "Standards editing is not connected yet." }),
      el("p", { text: copy }),
      el("a", { className: "button", text: "Return to Organize", attrs: { href: "#/organize" } }),
    ]),
  ]);
}

export function renderSettings(theme) {
  return el("div", {}, [
    pageHeader("Local display", "Settings", "Only appearance is available. It changes this page in memory and does not alter backend state."),
    el("div", { className: "settings-grid" }, [
      el("section", { className: "card settings-card" }, [el("h2", { text: "Appearance" }), el("p", { text: `Current theme: ${theme}. Use the Light and Dark controls in the toolbar.` })]),
      el("section", { className: "card settings-card" }, [el("h2", { text: "Organization controls" }), el("p", { text: "Scope changes, Standards editing, and movement are intentionally unavailable. Approval stops at Ready to move." })]),
    ]),
  ]);
}

export function renderLoadState(status, errorCode = "") {
  if (status === "loading") return el("p", { className: "loading", text: "Loading organization information…" });
  const states = {
    scan_not_ready: ["Files have not been checked yet", "Use the existing bootAI workflow to check the fixed root, then return here."],
    scan_in_progress: ["Checking files…", "Loci Flow will check again while this page stays open."],
    scan_failed: ["File check failed", "Use the existing scan workflow to try again safely."],
    authentication_required: ["Session unavailable", "Reload bootAI and authenticate before opening Loci Flow."],
    contract_error: ["Organization response could not be displayed", "The response did not match the committed read-only contract."],
    network_error: ["bootAI could not be reached", "Check that the local web application is still running."],
    organization_unavailable: ["Organization information is unavailable", "This scan could not produce the read-only organization view."],
    scan_generation_mismatch: ["Files have changed", "Check the folder again before continuing."],
    decision_revision_mismatch: ["Organization choices were updated elsewhere.", "Reload choices before continuing."],
    source_changed: ["A file changed after it was checked.", "Reload organization before approving."],
    overwrite_detected: ["A destination file now exists.", "Reload organization before approving."],
    unsafe_destination: ["A destination is no longer available.", "Reload organization before approving."],
    duplicate_group_changed: ["Duplicate copies changed after they were checked.", "Reload organization before approving."],
  };
  const code = errorCode || status;
  const [title, copy] = states[code] || ["Organization information could not be loaded.", "Try loading the current read-only result again."];
  const action = code === "scan_not_ready"
    ? el("a", { className: "button primary", text: "Check files", attrs: { href: "/" } })
    : code === "scan_failed"
      ? el("a", { className: "button", text: "Try again", attrs: { href: "/" } })
      : code === "scan_in_progress" || code === "authentication_required"
        ? null
        : el("button", { className: "button", text: "Try again", attrs: { type: "button" }, dataset: { action: "retry-load" } });
  return el("section", { className: "error-state" }, [el("h2", { text: title }), el("p", { text: copy }), action, el("span", { className: "error-code", text: code })]);
}
