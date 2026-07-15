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
