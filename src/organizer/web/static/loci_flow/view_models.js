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
  return {
    mode: "production_fixture",
    scenarioId: contract.id,
    page,
    store,
    scope: { ...contract.scope },
    freshness: { ...contract.freshness },
    workflow: { ...contract.workflow, primaryAction: { ...contract.workflow.primaryAction } },
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
    activity: { ...contract.activity },
    standards: { ...contract.standards },
    settings: { ...contract.settings },
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

function workflowFor(snapshot, override) {
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
    return {
      stage: "ready_to_move",
      status: "Ready to move",
      primaryAction: { label: "Move files — not connected yet", action: "none", enabled: false },
    };
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
} = {}) {
  const backend = snapshot?.backendState || null;
  const scopeName = backend?.scope.display_name || "Selected folder";
  const lastCheckedLabel = backend
    ? formatLocalTimestamp(backend.freshness.last_checked_at)
    : snapshot?.status === "checking" ? "Checking now" : "Not checked yet";
  const organization = backend
    ? organizationFromBackend(backend, {
      destination: destinationDrafts,
      duplicate: duplicateDrafts,
    })
    : defaultOrganization();
  return {
    id: "backend",
    scope: {
      displayName: scopeName,
      breadcrumb: [scopeName],
      lastCheckedLabel,
    },
    freshness: {
      lastCheckedLabel,
      lastOrganizedLabel: "Not organized yet",
      nextCheckLabel: "Not scheduled",
    },
    workflow: workflowFor(snapshot, workflowOverride),
    organization,
    duplicateBin: { connected: false, items: [] },
    activity: { connected: false, entries: [] },
    standards: { connected: false, items: [] },
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
      organizeCount: state.changes.filter((change) => change.decision === "pending").length,
      duplicateBinCount: state.duplicateBinItems.length,
    };
  }
  const unresolvedRecommendations = model.organization.recommendations.filter((item) => item.decision === "undecided").length;
  const unresolvedChoices = model.organization.choicesNeeded.filter((item) => !item.confirmed).length;
  return {
    ...model,
    toolbarFreshnessLabel: toolbarFreshnessLabel(model.scope.lastCheckedLabel),
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

export function buildDialogViewModel(sourceState) {
  return sourceState.getState().stageB1 ? contractModel(sourceState, "dialog") : sourceState;
}
