const KIND_ICON_MAP = Object.freeze({
  folder: "folder",
  pdf: "pdf",
  txt: "txt",
  "text document": "txt",
  markdown: "markdown",
  "word document": "document",
  document: "document",
  spreadsheet: "spreadsheet",
  "csv spreadsheet": "spreadsheet",
  image: "image",
  presentation: "presentation",
  "source code": "code",
  code: "code",
  "zip archive": "archive",
  archive: "archive",
});

const KIND_LABELS = Object.freeze({
  folder: "Folder",
  pdf: "PDF",
  text: "TXT",
  markdown: "Markdown",
  word: "Word document",
  spreadsheet: "Spreadsheet",
  image: "Image",
  presentation: "Presentation",
  source_code: "Source code",
  archive: "ZIP/archive",
  document: "Document",
});

const REASON_LABELS = Object.freeze({
  matched_existing_group: "Clear folder match",
  multiple_safe_destinations: "More than one folder fits",
  exact_duplicate_choice: "Identical copies found",
  project_folder_protected: "Project folder kept unchanged",
  submission_folder_protected: "Submission folder kept unchanged",
  shared_folder_protected: "Shared folder kept unchanged",
});

const ORGANIZATION_STYLE = Object.freeze({
  selected: "cautious",
  eyebrow: "Current folder",
  options: Object.freeze({
    cautious: Object.freeze({
      label: "Cautious",
      summary: "Safe changes only",
      explanation: "Shows only changes the current deterministic plan supports.",
    }),
    standard: Object.freeze({
      label: "Standard",
      summary: "Unavailable",
      explanation: "Only the current deterministic plan is available.",
    }),
    proactive: Object.freeze({
      label: "Proactive",
      summary: "Unavailable",
      explanation: "Only the current deterministic plan is available.",
    }),
  }),
});

const MOVEMENT_STATUS_LABELS = Object.freeze({
  verified: "Completed",
  partial_movement: "Partly completed",
  movement_failed: "Failed",
  verification_failed: "Could not confirm",
  prepared: "Needs attention",
  moving: "Needs attention",
  moved_unverified: "Needs attention",
  interrupted: "Needs attention",
  recovery_required: "Needs attention",
});

const RESTORE_STATUS_LABELS = Object.freeze({
  restored: "Restored",
  partial_undo: "Partly completed",
  undo_failed: "Failed",
  undo_verification_failed: "Could not confirm",
  prepared: "Needs attention",
  undoing: "Needs attention",
  restored_unverified: "Needs attention",
  interrupted: "Needs attention",
});
const DUPLICATE_MOVE_STATUS_LABELS = Object.freeze({
  moved: "Completed",
  partially_moved: "Partly completed",
  movement_failed: "Failed",
  verification_failed: "Could not confirm",
  prepared: "Needs attention",
  moving: "Needs attention",
  moved_unverified: "Needs attention",
  interrupted: "Needs attention",
});
const DUPLICATE_RESTORE_STATUS_LABELS = Object.freeze({
  restored: "Restored",
  partially_restored: "Partly completed",
  restore_failed: "Failed",
  verification_failed: "Could not confirm",
  prepared: "Needs attention",
  restoring: "Needs attention",
  restored_unverified: "Needs attention",
  interrupted: "Needs attention",
});

function iconForKind(kind = "") {
  return KIND_ICON_MAP[String(kind).toLowerCase()] || "unknown";
}

function toolbarFreshnessLabel(label) {
  if (label === "Not checked yet" || label === "Checking now") return label;
  if (label.startsWith("Today, ")) return `Checked today at ${label.slice("Today, ".length)}`;
  return `Checked ${label}`;
}

function acceptedModel(store, page) {
  const state = store.getState();
  return {
    mode: "accepted_default",
    page,
    store,
    scope: {
      displayName: state.currentFolder,
      breadcrumb: [state.currentFolder],
      lastCheckedLabel: state.lastChecked,
    },
    freshness: {
      lastCheckedLabel: state.lastChecked,
      lastOrganizedLabel: state.lastOrganized,
      nextCheckLabel: state.nextCheck,
    },
  };
}

function normalizeRecommendation(item) {
  return {
    ...item,
    icon: iconForKind(item.kind),
    size: item.sizeLabel,
    currentFolder: item.currentFolderLabel,
    proposedFolder: item.destinationFolderLabel,
    reason: item.reasonText,
  };
}

function normalizeChoice(item) {
  return {
    ...item,
    type: item.choiceType === "duplicate" ? "duplicate" : "move",
    icon: iconForKind(item.kind),
    size: item.sizeLabel,
    currentFolder: item.currentFolderLabel,
    proposedFolder: item.choiceType === "destination"
      ? item.options.find((option) => option.optionId === item.selectedOptionId)?.label || "Choice needed"
      : "Duplicate Bin choice",
    reason: item.reasonText || "Choose what should happen to these identical copies.",
  };
}

function normalizeKept(item) {
  return {
    ...item,
    id: item.itemId,
    icon: iconForKind(item.kind),
    currentFolder: item.currentFolderLabel,
  };
}

function contractModel(store, page) {
  const state = store.getState();
  const contract = state.stageB1;
  if (!contract) return acceptedModel(store, page);
  const recommendations = contract.organization.recommendations.map(normalizeRecommendation);
  const choicesNeeded = contract.organization.choicesNeeded.map(normalizeChoice);
  const kept = contract.organization.keptByStandards.map(normalizeKept);
  const allItems = [...recommendations, ...choicesNeeded, ...kept];
  const selectedItemId = state.selectedItemId || allItems[0]?.id || null;
  const activity = {
    ...contract.activity,
    entries: [...contract.activity.entries],
    expandedIds: [...(contract.activity.expandedIds || [])],
  };
  const workflow = {
    ...contract.workflow,
    primaryAction: { ...contract.workflow.primaryAction },
  };
  if (
    page === "home"
    && workflow.stage === "ready_to_move"
    && workflow.primaryAction.action === "preview-move"
  ) {
    workflow.primaryAction = {
      label: "Open Organize",
      action: "continue-organizing",
      enabled: true,
    };
  }
  return {
    mode: "production_fixture",
    scenarioId: contract.id,
    page,
    store,
    scope: { ...contract.scope },
    freshness: { ...contract.freshness },
    workflow,
    organization: {
      ...contract.organization,
      style: {
        ...contract.organization.style,
        options: { ...contract.organization.style.options },
      },
      recommendations,
      choicesNeeded,
      keptByStandards: kept,
      selectedItem: allItems.find((item) => item.id === selectedItemId) || null,
    },
    duplicateBin: { ...contract.duplicateBin },
    activity,
    standards: { ...contract.standards },
    settings: { ...contract.settings },
    getState() {
      return {
        activityMoves: activity.entries,
        expandedActivityIds: activity.expandedIds,
        duplicateBinItems: [...contract.duplicateBin.items],
        selectedBinIds: [...contract.duplicateBin.selectedIds],
      };
    },
  };
}

function labelForKind(kind) {
  return KIND_LABELS[kind] || KIND_LABELS.document;
}

function reasonLabel(code) {
  return REASON_LABELS[code] || "Organization information available";
}

function folderLabel(path, scopeName) {
  if (path === ".") return scopeName;
  return path.split("/").join(" › ");
}

function fileName(path) {
  return path.split("/").at(-1) || path;
}

export function formatFileSize(bytes) {
  if (!Number.isSafeInteger(bytes) || bytes < 0) return "Size unavailable";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const digits = value >= 10 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unitIndex]}`;
}

export function formatLocalTimestamp(value) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(timestamp);
}

function countLabel(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

function activitySummary(entry) {
  const movement = new Set(["organization", "duplicate_bin_move"]).has(entry.type);
  const copies = new Set(["duplicate_bin_move", "duplicate_bin_restore"]).has(entry.type);
  const completed = movement ? entry.files_moved : entry.files_restored;
  const incomplete = entry.files_failed + entry.files_unattempted;
  const noun = copies ? "copy" : "file";
  const completeLabel = movement
    ? countLabel(completed, `${noun} moved`, `${noun === "copy" ? "copies" : "files"} moved`)
    : countLabel(completed, `${noun} restored`, `${noun === "copy" ? "copies" : "files"} restored`);
  if (!incomplete) return completeLabel;
  const incompleteLabel = movement
    ? countLabel(incomplete, `${noun} did not move`, `${noun === "copy" ? "copies" : "files"} did not move`)
    : countLabel(incomplete, `${noun} did not restore`, `${noun === "copy" ? "copies" : "files"} did not restore`);
  return `${completeLabel} · ${incompleteLabel}`;
}

function activityPath(detail, type) {
  return new Set(["organization", "duplicate_bin_move"]).has(type)
    ? `${detail.source} → ${detail.destination}`
    : `${detail.moved_from} → ${detail.restored_to}`;
}

function activityDetails(entry) {
  const movement = new Set(["organization", "duplicate_bin_move"]).has(entry.type);
  const successfulStatuses = movement
    ? new Set(["moved", "verified"])
    : new Set(["restored", "verified"]);
  const successes = [];
  const problems = [];
  for (const detail of entry.details) {
    const path = activityPath(detail, entry.type);
    if (successfulStatuses.has(detail.status)) successes.push(path);
    else {
      let label = "Needs attention";
      if (detail.status === "failed") {
        label = movement ? "Did not move" : "Did not restore";
      } else if (new Set(["pending", "unattempted"]).has(detail.status)) {
        label = "Not attempted";
      }
      problems.push(`${path} — ${label}`);
    }
  }
  return {
    filesMoved: entry.type === "organization" ? successes : [],
    namesChanged: [],
    foldersCreated: [],
    copiesMoved: entry.type === "duplicate_bin_move" ? successes : [],
    filesRestored: movement ? [] : successes,
    problems,
  };
}

function verificationLabel(value) {
  if (value === "passed") return "Verified";
  if (value === "failed") return "Could not confirm";
  return null;
}

export function buildProductionActivityEntries(entries = []) {
  return entries.map((entry) => ({
    id: entry.id,
    type: entry.type,
    title: entry.title,
    status: ({
      organization: MOVEMENT_STATUS_LABELS,
      restore: RESTORE_STATUS_LABELS,
      duplicate_bin_move: DUPLICATE_MOVE_STATUS_LABELS,
      duplicate_bin_restore: DUPLICATE_RESTORE_STATUS_LABELS,
    }[entry.type] || {})[entry.status] || "Needs attention",
    time: entry.completed_at || entry.started_at
      ? formatLocalTimestamp(entry.completed_at || entry.started_at)
      : "Time unavailable",
    summary: activitySummary(entry),
    verificationLabel: verificationLabel(entry.verification_status),
    undoable: entry.undoable,
    relatedMovementId: entry.related_operation_id,
    details: activityDetails(entry),
  }));
}

function defaultOrganization() {
  return {
    style: ORGANIZATION_STYLE,
    recommendations: [],
    choicesNeeded: [],
    keptByStandards: [],
    movementTree: { nodes: [] },
    movementSummary: {
      filesToMove: 0,
      namesToChange: 0,
      foldersToCreate: 0,
      duplicateCopies: 0,
    },
    approval: {
      readyForApproval: false,
      filesToMove: 0,
      duplicateCopiesDeferred: 0,
      choicesRemaining: 0,
      confirmLabel: "Approve",
      supportingText: "No files have moved yet.",
    },
  };
}

function moveCountMessage(execution) {
  const moved = countLabel(execution.files_moved, "file moved", "files moved");
  const remaining = execution.files_failed + execution.files_unattempted;
  if (!remaining) return `${moved} and checked.`;
  return `${moved}. ${countLabel(remaining, "file needs attention", "files need attention")}.`;
}

function moveSummary(execution) {
  if (!execution) return null;
  if (execution.status === "verified") {
    return `${countLabel(execution.files_moved, "file moved", "files moved")}.`;
  }
  if (new Set(["prepared", "moving", "moved_unverified"]).has(execution.status)) {
    return "File move in progress.";
  }
  if (new Set([
    "partial_movement", "movement_failed", "verification_failed", "interrupted",
    "recovery_required",
  ]).has(execution.status)) {
    if (execution.files_moved > 0) return moveCountMessage(execution);
    return "Latest move needs attention.";
  }
  return null;
}

function lastMoveSummary(execution, activityEntries = []) {
  const current = moveSummary(execution);
  if (current) return current;
  const latestMovement = activityEntries.find((entry) => entry.type === "organization");
  return moveSummary(latestMovement);
}

function lastOrganizedLabel(execution, activityEntries = []) {
  if (execution?.status === "verified") {
    const matchingActivity = activityEntries.find((entry) => (
      entry.type === "organization"
      && entry.status === "verified"
      && entry.id === execution.operation_id
    ));
    const completedAt = execution.completed_at || matchingActivity?.completed_at;
    return completedAt ? formatLocalTimestamp(completedAt) : "Completed";
  }
  const latestVerified = activityEntries.find((entry) => (
    entry.type === "organization" && entry.status === "verified"
  ));
  if (!latestVerified) return "Not organized yet";
  return latestVerified.completed_at
    ? formatLocalTimestamp(latestVerified.completed_at)
    : "Completed";
}

function movementStatusLabel(workflow) {
  if (workflow.stage === "ready_to_move") return "Move ready";
  if (workflow.stage === "move_in_progress") return "Moving files";
  if (new Set(["move_completed", "ready_for_check"]).has(workflow.stage)) {
    return "Move verified";
  }
  if (workflow.stage === "move_failed") return "Move needs attention";
  return "No move ready";
}

function bannerStatusLabel(workflow) {
  if (workflow.stage === "checking") return "Checking files";
  if (workflow.stage === "ready_to_move") return "Ready to move";
  if (workflow.stage === "move_in_progress") return "Moving files";
  if (new Set(["move_completed", "ready_for_check"]).has(workflow.stage)) {
    return "Move verified";
  }
  if (workflow.stage === "move_failed" || workflow.stage === "stale") {
    return "Move needs attention";
  }
  return "Review changes";
}

function movementWorkflow(backend, movement) {
  const execution = movement?.execution || backend.execution;
  if (movement?.executionPending) {
    return {
      stage: "move_in_progress",
      status: "Moving files…",
      message: "Loci Flow is checking each approved move.",
      primaryAction: { label: "Moving files…", action: "none", enabled: false },
    };
  }
  if (execution.status === "verified") {
    return {
      stage: "ready_for_check",
      status: "Files moved",
      message: moveCountMessage(execution),
      primaryAction: { label: "Check again", action: "check-again", enabled: true },
    };
  }
  if (execution.status === "partial_movement") {
    return {
      stage: "move_failed",
      status: "Move partly completed",
      message: moveCountMessage(execution),
      primaryAction: { label: "View Activity", action: "open-activity", enabled: true },
    };
  }
  if (execution.status === "movement_failed") {
    return {
      stage: "move_failed",
      status: "Files did not move",
      message: moveCountMessage(execution),
      primaryAction: { label: "View Activity", action: "open-activity", enabled: true },
    };
  }
  if (execution.status === "verification_failed") {
    return {
      stage: "move_failed",
      status: "Move could not be confirmed",
      message: "Open Activity to see which files need attention.",
      primaryAction: { label: "View Activity", action: "open-activity", enabled: true },
    };
  }
  if (new Set(["prepared", "moving", "moved_unverified"]).has(execution.status)) {
    return {
      stage: "move_in_progress",
      status: "Move in progress",
      message: "The latest file move is still being checked.",
      primaryAction: { label: "Reload result", action: "reload-choices", enabled: true },
    };
  }
  if (new Set(["interrupted", "recovery_required"]).has(execution.status)) {
    return {
      stage: "move_failed",
      status: "Move needs attention",
      message: "Open Activity to review the latest file move.",
      primaryAction: { label: "View Activity", action: "open-activity", enabled: true },
    };
  }
  if (movement?.previewStatus === "loading") {
    return {
      stage: "ready_to_move",
      status: "Ready to move",
      primaryAction: { label: "Preparing move…", action: "none", enabled: false },
    };
  }
  if (movement?.errorMessage) {
    const changed = new Set([
      "approved_plan_not_found", "approved_plan_stale", "scan_generation_mismatch",
      "decision_revision_mismatch", "invalid_confirmation_token", "invalid_confirmation",
    ]).has(movement.errorCode);
    return {
      stage: "move_failed",
      status: changed ? "Organization changed" : "Move could not be prepared",
      message: movement.errorMessage,
      primaryAction: changed
        ? { label: "Load latest plan", action: "reload-choices", enabled: true }
        : { label: "Try again", action: "preview-move", enabled: true },
    };
  }
  if (execution.status === "ready") {
    return {
      stage: "ready_to_move",
      status: "Ready to move",
      primaryAction: { label: "Move files", action: "preview-move", enabled: true },
    };
  }
  return {
    stage: "move_failed",
    status: "No files are ready to move",
    message: "Load the latest organization before continuing.",
    primaryAction: { label: "Load latest plan", action: "reload-choices", enabled: true },
  };
}

function workflowFor(snapshot, override, movement) {
  if (override) return { ...override, primaryAction: { ...override.primaryAction } };
  if (!snapshot || snapshot.status === "loading") {
    return {
      stage: "checking",
      status: "Opening Loci Flow…",
      primaryAction: { label: "Opening…", action: "none", enabled: false },
    };
  }
  if (snapshot.status === "not_checked") {
    return {
      stage: "idle",
      status: "Files have not been checked yet",
      primaryAction: { label: "Check files", action: "check-files", enabled: true },
    };
  }
  if (snapshot.status === "checking") {
    return {
      stage: "checking",
      status: "Checking files…",
      primaryAction: { label: "Checking files…", action: "none", enabled: false },
    };
  }
  if (snapshot.status === "failed") {
    return {
      stage: "stale",
      status: "File check failed",
      message: "Try again.",
      staleKind: "generation",
      primaryAction: { label: "Try again", action: "check-again", enabled: true },
    };
  }
  const backend = snapshot.backendState;
  const totalChanges = backend.recommended_changes.length + backend.choices_needed.length;
  if (backend.review.status === "ready_to_move") {
    return movementWorkflow(backend, movement);
  }
  if (totalChanges === 0) {
    return {
      stage: "idle",
      status: "Everything is organized",
      primaryAction: { label: "Check again", action: "check-again", enabled: true },
    };
  }
  if (backend.review.summary.ready_for_approval) {
    return {
      stage: "approving",
      status: "Ready to approve",
      primaryAction: { label: "Review and approve", action: "review-and-approve", enabled: true },
    };
  }
  const waiting = backend.review.summary.pending_recommendations + backend.review.summary.pending_choices;
  return {
    stage: "organizing",
    status: `${waiting} ${waiting === 1 ? "change is" : "changes are"} waiting`,
    primaryAction: { label: "Continue organizing", action: "continue-organizing", enabled: true },
  };
}

function duplicateMovementState(backend, state = null) {
  const batch = backend?.duplicate_batch || null;
  const execution = state?.execution || null;
  if (state?.executionPending) {
    return {
      stage: "moving",
      status: "Moving copies…",
      message: "Selected copies are moving to Duplicate Bin.",
      action: { label: "Moving copies…", action: "none", enabled: false },
    };
  }
  if (execution?.status === "moved") {
    return {
      stage: "completed",
      status: "Copies moved",
      message: countLabel(execution.copies_moved, "copy moved", "copies moved"),
      action: { label: "Open Duplicate Bin", action: "open-duplicate-bin", enabled: true },
    };
  }
  if (execution?.status === "partially_moved") {
    return {
      stage: "attention",
      status: "Some copies were moved",
      message: "Review the result in Duplicate Bin and Activity.",
      action: { label: "View Activity", action: "open-activity", enabled: true },
    };
  }
  if (execution && new Set(["prepared", "moving", "moved_unverified"]).has(execution.status)) {
    return {
      stage: "moving",
      status: "Duplicate Bin move in progress",
      message: "Load the latest result before trying again.",
      action: { label: "Reload result", action: "reload-choices", enabled: true },
    };
  }
  if (execution || state?.errorMessage) {
    return {
      stage: "attention",
      status: "Duplicate Bin move needs attention",
      message: state?.errorMessage || "Review the latest result in Activity.",
      action: { label: "View Activity", action: "open-activity", enabled: true },
    };
  }
  if (state?.previewStatus === "loading") {
    return {
      stage: "preview_loading",
      status: "Preparing Duplicate Bin move…",
      message: null,
      action: { label: "Preparing…", action: "none", enabled: false },
    };
  }
  if (batch?.status === "approved") {
    return {
      stage: "ready",
      status: "Approved duplicate copies are ready",
      message: countLabel(batch.copies, "copy is ready", "copies are ready"),
      action: {
        label: "Move copies to Duplicate Bin",
        action: "preview-duplicate-bin-move",
        enabled: true,
      },
    };
  }
  if (batch && new Set(["moving", "executing"]).has(batch.status)) {
    return {
      stage: "moving",
      status: "Duplicate Bin move in progress",
      message: "Load the latest result before trying again.",
      action: { label: "Reload result", action: "reload-choices", enabled: true },
    };
  }
  if (batch && new Set(["moved", "completed"]).has(batch.status)) {
    return {
      stage: "completed",
      status: "Copies moved",
      message: "Moved copies are available in Duplicate Bin.",
      action: { label: "Open Duplicate Bin", action: "open-duplicate-bin", enabled: true },
    };
  }
  if (batch && !new Set(["superseded", "cancelled"]).has(batch.status)) {
    return {
      stage: "attention",
      status: "Duplicate Bin move needs attention",
      message: "Review the latest result in Activity.",
      action: { label: "View Activity", action: "open-activity", enabled: true },
    };
  }
  return {
    stage: "unavailable",
    status: "No approved duplicate copies",
    message: null,
    action: { label: "Move copies to Duplicate Bin", action: "none", enabled: false },
  };
}

function duplicateBinContract(state = null) {
  const items = (state?.items || []).map((item) => ({
    id: item.id,
    name: item.name,
    previousFolder: item.previous_folder.split("/"),
    dateMoved: formatLocalTimestamp(item.moved_at),
    size: formatFileSize(item.size),
    kind: labelForKind(item.kind),
    icon: iconForKind(item.kind),
    restoreAvailable: item.restore_available,
    attentionMessage: item.attention_message,
  }));
  return {
    connected: Boolean(state),
    loadStatus: state?.loadStatus || "idle",
    items,
    selectedIds: [...(state?.selectedIds || [])],
    errorMessage: state?.errorMessage || null,
    noticeMessage: state?.noticeMessage || null,
    restorePending: Boolean(state?.restorePending),
  };
}

function standardsContract(state = null) {
  return {
    connected: Boolean(state),
    loadStatus: state?.loadStatus || "idle",
    revision: state?.revision ?? null,
    items: (state?.items || []).map((item) => ({
      id: item.id,
      name: item.name,
      description: item.description,
      active: item.active,
      mutable: item.mutable,
      scopeLabel: item.scope_label,
      effectSummary: item.effect_summary,
      presetType: item.preset_type,
      attentionMessage: item.attention_message,
    })),
    mutationPending: Boolean(state?.mutationPending),
    pendingStandardId: state?.pendingStandardId || null,
    errorMessage: state?.errorMessage || null,
    conflictMessage: state?.conflictMessage || null,
    freshScanRequired: Boolean(state?.freshScanRequired),
  };
}

function recommendationModels(backend, scopeName) {
  const decisions = new Map(
    backend.review.recommendations.map((item) => [item.change_id, item.decision]),
  );
  return backend.recommended_changes.map((change) => ({
    id: change.id,
    type: "move",
    name: change.item.name,
    kind: labelForKind(change.item.kind),
    sizeLabel: formatFileSize(change.item.size_bytes),
    currentFolderLabel: folderLabel(change.item.current_folder, scopeName),
    destinationFolderLabel: folderLabel(change.destination_folder, scopeName),
    changeText: `Move to ${folderLabel(change.destination_folder, scopeName).split(" › ").at(-1)}`,
    reasonText: reasonLabel(change.reason_code),
    decision: decisions.get(change.id) || "undecided",
  }));
}

function choiceModels(backend, scopeName, drafts) {
  const decisions = new Map(backend.review.choices.map((item) => [item.choice_id, item]));
  return backend.choices_needed.map((choice) => {
    const state = decisions.get(choice.id);
    if (choice.choice_type === "choose_destination") {
      const selectedOptionId = drafts.destination?.[choice.id] ?? state.option_id;
      return {
        id: choice.id,
        choiceType: "destination",
        name: choice.item.name,
        kind: labelForKind(choice.item.kind),
        sizeLabel: formatFileSize(choice.item.size_bytes),
        currentFolderLabel: folderLabel(choice.item.current_folder, scopeName),
        reasonText: reasonLabel(choice.reason_code),
        options: choice.options.map((option) => ({
          optionId: option.id,
          label: option.action === "keep"
            ? "Keep here"
            : folderLabel(option.destination_folder, scopeName),
          supportingText: option.action === "keep"
            ? "Leave the file in its current folder."
            : "Move the file to this existing folder.",
          action: option.action,
        })),
        selectedOptionId,
        confirmed: state.option_id !== null,
      };
    }
    const draft = drafts.duplicate?.[choice.id];
    const optionId = draft?.optionId ?? state.option_id;
    const selectedIds = new Set(draft?.selectedCopyIds ?? state.selected_copy_ids);
    const first = choice.copies[0];
    return {
      id: choice.id,
      choiceType: "duplicate",
      name: first.name,
      kind: labelForKind(first.kind),
      sizeLabel: formatFileSize(first.size_bytes),
      currentFolderLabel: `${choice.copies.length} folders`,
      reasonText: reasonLabel(choice.reason_code),
      identicalCopyCount: choice.identical_copy_count,
      copies: choice.copies.map((copy) => ({
        copyId: copy.id,
        name: copy.name,
        folderLabel: folderLabel(copy.current_folder, scopeName),
        selectedForBin: selectedIds.has(copy.id),
      })),
      decision: optionId === "keep-all" ? "keep_all" : "choose_for_bin",
      confirmed: state.option_id !== null,
    };
  });
}

function keptModels(backend, scopeName) {
  return backend.kept_by_standards.map((kept) => ({
    itemId: kept.item.id,
    name: kept.item.name,
    kind: labelForKind(kept.item.kind),
    currentFolderLabel: folderLabel(kept.item.current_folder, scopeName),
    standardName: kept.standard.name,
    reasonText: reasonLabel(kept.reason_code),
  }));
}

function suppliedMovementTree(backend, scopeName) {
  return {
    nodes: backend.summary.movement_tree.map((entry, folderIndex) => ({
      id: `backend-folder-${folderIndex + 1}`,
      type: "folder",
      name: folderLabel(entry.folder, scopeName),
      kind: "folder",
      children: entry.files.map((path, fileIndex) => ({
        id: `backend-file-${folderIndex + 1}-${fileIndex + 1}`,
        type: "file",
        name: fileName(path),
        kind: labelForKind(
          backend.recommended_changes.find((item) => item.item.relative_path === path)?.item.kind,
        ),
        note: "Move",
        children: [],
      })),
    })),
  };
}

function organizationFromBackend(backend, drafts) {
  const scopeName = backend.scope.display_name;
  const reviewSummary = backend.review.summary;
  return {
    style: ORGANIZATION_STYLE,
    recommendations: recommendationModels(backend, scopeName),
    choicesNeeded: choiceModels(backend, scopeName, drafts),
    keptByStandards: keptModels(backend, scopeName),
    movementTree: suppliedMovementTree(backend, scopeName),
    movementSummary: {
      filesToMove: reviewSummary.files_to_move,
      namesToChange: reviewSummary.names_to_change,
      foldersToCreate: reviewSummary.folders_to_create,
      duplicateCopies: reviewSummary.duplicate_copies_for_future_bin,
    },
    approval: {
      readyForApproval: reviewSummary.ready_for_approval && backend.review.status !== "ready_to_move",
      filesToMove: reviewSummary.files_to_move,
      duplicateCopiesDeferred: reviewSummary.duplicate_copies_for_future_bin,
      choicesRemaining: reviewSummary.choices_remaining,
      confirmLabel: "Approve",
      supportingText: "No files have moved yet.",
    },
  };
}

export function buildProductionStageB1Contract({
  snapshot,
  workflowOverride = null,
  destinationDrafts = {},
  duplicateDrafts = {},
  activity = null,
  movement = null,
  duplicateMovement = null,
  duplicateBin = null,
  standards = null,
} = {}) {
  const backend = snapshot?.backendState || null;
  const scopeName = backend?.scope.display_name || "Selected folder";
  const lastCheckedLabel = backend
    ? formatLocalTimestamp(backend.freshness.last_checked_at)
    : snapshot?.status === "checking" ? "Checking now" : "Not checked yet";
  const organization = backend && !standards?.freshScanRequired
    ? organizationFromBackend(backend, {
      destination: destinationDrafts,
      duplicate: duplicateDrafts,
    })
    : defaultOrganization();
  const execution = movement?.execution || backend?.execution;
  const workflow = workflowFor(snapshot, workflowOverride, movement);
  const activityEntries = activity?.entries || [];
  return {
    id: "backend",
    scope: {
      displayName: scopeName,
      breadcrumb: [scopeName],
      lastCheckedLabel,
    },
    freshness: {
      lastCheckedLabel,
      lastOrganizedLabel: lastOrganizedLabel(execution, activityEntries),
      nextCheckLabel: "Not scheduled",
    },
    workflow,
    organization: {
      ...organization,
      duplicateMovement: duplicateMovementState(backend, duplicateMovement),
    },
    duplicateBin: duplicateBinContract(duplicateBin),
    activity: activity
      ? {
        connected: true,
        loadStatus: activity.loadStatus,
        entries: buildProductionActivityEntries(activity.entries || []),
        expandedIds: [...(activity.expandedIds || [])],
        errorMessage: activity.errorMessage || null,
        noticeMessage: activity.noticeMessage || null,
        lastMoveSummary: lastMoveSummary(execution, activityEntries),
      }
      : {
        connected: false,
        entries: [],
        expandedIds: [],
        lastMoveSummary: lastMoveSummary(execution, activityEntries),
      },
    standards: standardsContract(standards),
    settings: { connected: true },
  };
}

export function buildHomeViewModel(sourceState) {
  return sourceState.getState().stageB1 ? contractModel(sourceState, "home") : sourceState;
}

export function buildOrganizeViewModel(sourceState) {
  return sourceState.getState().stageB1 ? contractModel(sourceState, "organize") : sourceState;
}

export function buildDuplicateBinViewModel(sourceState) {
  return sourceState.getState().stageB1 ? contractModel(sourceState, "duplicate-bin") : sourceState;
}

export function buildActivityViewModel(sourceState) {
  return sourceState.getState().stageB1 ? contractModel(sourceState, "activity") : sourceState;
}

export function buildStandardsViewModel(sourceState) {
  return sourceState.getState().stageB1 ? contractModel(sourceState, "standards") : sourceState;
}

export function buildSettingsViewModel(sourceState) {
  return sourceState.getState().stageB1 ? contractModel(sourceState, "settings") : sourceState;
}

export function buildShellViewModel(sourceState) {
  const model = contractModel(sourceState, "shell");
  if (model.mode === "accepted_default") {
    const state = sourceState.getState();
    return {
      ...model,
      toolbarFreshnessLabel: toolbarFreshnessLabel(model.scope.lastCheckedLabel),
      movementStatusLabel: "Movement off",
      organizeCount: state.changes.filter((change) => change.decision === "pending").length,
      duplicateBinCount: state.duplicateBinItems.length,
    };
  }
  const unresolvedRecommendations = model.organization.recommendations.filter((item) => item.decision === "undecided").length;
  const unresolvedChoices = model.organization.choicesNeeded.filter((item) => !item.confirmed).length;
  return {
    ...model,
    toolbarFreshnessLabel: toolbarFreshnessLabel(model.scope.lastCheckedLabel),
    bannerStatusLabel: bannerStatusLabel(model.workflow),
    movementStatusLabel: movementStatusLabel(model.workflow),
    organizeCount: unresolvedRecommendations + unresolvedChoices,
    duplicateBinCount: model.duplicateBin.connected ? model.duplicateBin.items.length : 0,
  };
}

export function buildRouteViewModel(sourceState, route) {
  if (route === "organize") return buildOrganizeViewModel(sourceState);
  if (route === "duplicate-bin") return buildDuplicateBinViewModel(sourceState);
  if (route === "activity") return buildActivityViewModel(sourceState);
  if (route === "standards") return buildStandardsViewModel(sourceState);
  if (route === "settings") return buildSettingsViewModel(sourceState);
  return buildHomeViewModel(sourceState);
}

export function buildMovementConfirmationViewModel(preview, confirmationText = "") {
  const count = preview.files_to_move;
  const duplicates = preview.duplicate_copies_deferred;
  return {
    stage: "confirmation_ready",
    eyebrow: "Final confirmation",
    title: "Move these files?",
    description: `${countLabel(count, "file will move", "files will move")}.`,
    duplicateMessage: duplicates > 0
      ? `${countLabel(duplicates, "duplicate copy is", "duplicate copies are")} not part of this move. ${duplicates === 1 ? "It will remain" : "They will remain"} for the separate Duplicate Bin step.`
      : null,
    instruction: `Type ${preview.confirmation} to continue.`,
    confirmationText,
    canConfirm: confirmationText === preview.confirmation,
    cancelLabel: "Cancel",
    confirmLabel: "Move files",
  };
}

export function buildDuplicateBinConfirmationViewModel(
  preview,
  confirmationText = "",
  kind = "movement",
) {
  const restore = kind === "restore";
  const count = restore ? preview.copies_to_restore : preview.copies_to_move;
  return {
    eyebrow: restore ? "Duplicate Bin" : "Final confirmation",
    title: restore ? "Restore these copies?" : "Move these copies to Duplicate Bin?",
    description: restore
      ? `${countLabel(count, "copy will return", "copies will return")} to the previous location.`
      : `${countLabel(count, "copy will move", "copies will move")} to Duplicate Bin.`,
    instruction: `Type ${preview.confirmation} to continue.`,
    confirmationText,
    canConfirm: confirmationText === preview.confirmation,
    cancelLabel: "Cancel",
    confirmLabel: restore ? "Restore copies" : "Move copies",
  };
}

export function buildDialogViewModel(sourceState) {
  return sourceState.getState().stageB1 ? contractModel(sourceState, "dialog") : sourceState;
}
