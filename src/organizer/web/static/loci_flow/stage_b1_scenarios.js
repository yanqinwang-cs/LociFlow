export const STAGE_B1_SCENARIO_IDS = Object.freeze([
  "accepted_default",
  "not_checked",
  "checking",
  "no_changes",
  "recommendations",
  "destination_choice",
  "duplicate_choice",
  "ready_for_approval",
  "ready_to_move",
  "stale_generation",
  "stale_revision",
  "disconnected_pages",
]);

export const DEFAULT_STAGE_B1_SCENARIO_ID = "accepted_default";

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function scope(lastCheckedLabel = "Today, 9:42 AM") {
  return {
    displayName: "Downloads",
    breadcrumb: ["Downloads"],
    lastCheckedLabel,
  };
}

function freshness(lastCheckedLabel = "Today, 9:42 AM") {
  return {
    lastCheckedLabel,
    lastOrganizedLabel: "12 July, 4:18 PM",
    nextCheckLabel: "Not scheduled",
  };
}

function recommendation(overrides = {}) {
  return {
    id: "recommend-report",
    type: "move",
    name: "Quarterly report.pdf",
    kind: "PDF",
    sizeLabel: "284 KB",
    currentFolderLabel: "Downloads",
    destinationFolderLabel: "Documents › Reports",
    changeText: "Move to Reports",
    reasonText: "The file name and document type match the Reports folder.",
    decision: "undecided",
    ...overrides,
  };
}

function secondRecommendation(overrides = {}) {
  return {
    id: "recommend-budget",
    type: "move",
    name: "Budget 2026.xlsx",
    kind: "Spreadsheet",
    sizeLabel: "96 KB",
    currentFolderLabel: "Downloads",
    destinationFolderLabel: "Documents › Finance",
    changeText: "Move to Finance",
    reasonText: "The year and workbook name match the Finance folder.",
    decision: "undecided",
    ...overrides,
  };
}

function destinationChoice(overrides = {}) {
  return {
    id: "choose-notes-folder",
    choiceType: "destination",
    name: "Project notes.md",
    kind: "Markdown",
    sizeLabel: "31 KB",
    currentFolderLabel: "Downloads",
    reasonText: "The file could belong with either project notes or the current folder.",
    options: [
      {
        optionId: "notes-project",
        label: "Projects › Elarian › Notes",
        supportingText: "Keep these notes with the Elarian project.",
        action: "move",
      },
      {
        optionId: "notes-keep",
        label: "Keep in Downloads",
        supportingText: "Leave the file in its current folder.",
        action: "keep",
      },
    ],
    selectedOptionId: null,
    confirmed: false,
    ...overrides,
  };
}

function duplicateChoice(overrides = {}) {
  return {
    id: "choose-brand-copies",
    choiceType: "duplicate",
    name: "brand-pack.zip",
    kind: "ZIP archive",
    sizeLabel: "1.4 MB",
    identicalCopyCount: 3,
    copies: [
      { copyId: "brand-assets", name: "brand-pack.zip", folderLabel: "Projects › Elarian › Assets", selectedForBin: true },
      { copyId: "brand-downloads", name: "brand-pack.zip", folderLabel: "Downloads", selectedForBin: false },
      { copyId: "brand-shared", name: "brand-pack.zip", folderLabel: "Documents › Shared", selectedForBin: false },
    ],
    decision: "choose_for_bin",
    confirmed: false,
    ...overrides,
  };
}

function keptByStandards() {
  return [{
    itemId: "kept-submission",
    name: "submission.pdf",
    kind: "PDF",
    currentFolderLabel: "Teaching › CS2030S › Submissions",
    standardName: "Keep Submissions unchanged",
  }];
}

function movementTree(files = []) {
  if (!files.length) return { nodes: [] };
  return {
    nodes: [{
      id: "tree-documents",
      type: "folder",
      name: "Documents",
      kind: "folder",
      children: files.map((file, index) => ({
        id: `tree-file-${index + 1}`,
        type: "file",
        name: file.name,
        kind: file.kind,
        note: file.note || "Move",
        children: [],
      })),
    }],
  };
}

function disconnectedPages() {
  return {
    duplicateBin: { connected: false, items: [] },
    activity: { connected: false, entries: [] },
    standards: { connected: false, items: [] },
  };
}

function organization(overrides = {}) {
  return {
    style: {
      selected: "cautious",
      eyebrow: "Current folder",
      options: {
        cautious: {
          label: "Cautious",
          summary: "Safe changes only",
          explanation: "Only makes changes with a clear destination. Creates very few new folders.",
        },
        standard: {
          label: "Standard",
          summary: "Normal organization",
          explanation: "Organizes clear files and asks about changes that need a choice.",
        },
        proactive: {
          label: "Proactive",
          summary: "In-depth organization",
          explanation: "Builds a more complete folder structure and asks about more changes.",
        },
      },
    },
    recommendations: [],
    choicesNeeded: [],
    keptByStandards: keptByStandards(),
    movementTree: movementTree(),
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
    ...overrides,
  };
}

function workflow(stage, status, label, action, enabled = true, extras = {}) {
  return {
    stage,
    status,
    primaryAction: { label, action, enabled },
    ...extras,
  };
}

function baseScenario(id, workflowState, organizationState = organization(), extras = {}) {
  const pageState = disconnectedPages();
  return {
    id,
    scope: scope(extras.lastCheckedLabel),
    freshness: freshness(extras.lastCheckedLabel),
    workflow: workflowState,
    organization: organizationState,
    duplicateBin: pageState.duplicateBin,
    activity: pageState.activity,
    standards: pageState.standards,
    settings: { connected: true },
  };
}

function buildScenario(id) {
  if (id === "not_checked") {
    return baseScenario(
      id,
      workflow("idle", "Files have not been checked yet", "Check files", "check-files"),
      organization({ keptByStandards: [] }),
      { lastCheckedLabel: "Not checked yet" },
    );
  }
  if (id === "checking") {
    return baseScenario(
      id,
      workflow("checking", "Checking files…", "Checking files…", "none", false),
      organization({ keptByStandards: [] }),
      { lastCheckedLabel: "Checking now" },
    );
  }
  if (id === "no_changes") {
    return baseScenario(
      id,
      workflow("idle", "Everything is organized", "Check again", "check-again"),
      organization({ keptByStandards: [] }),
    );
  }
  if (id === "recommendations") {
    const accepted = recommendation({ decision: "accept" });
    const pending = secondRecommendation();
    return baseScenario(
      id,
      workflow("organizing", "1 change is waiting", "Continue organizing", "continue-organizing"),
      organization({
        recommendations: [accepted, pending],
        movementTree: movementTree([{ name: accepted.name, kind: accepted.kind }]),
        movementSummary: { filesToMove: 1, namesToChange: 0, foldersToCreate: 0, duplicateCopies: 0 },
        approval: {
          readyForApproval: false,
          filesToMove: 1,
          duplicateCopiesDeferred: 0,
          choicesRemaining: 1,
          confirmLabel: "Approve",
          supportingText: "No files have moved yet.",
        },
      }),
    );
  }
  if (id === "destination_choice") {
    const accepted = recommendation({ decision: "accept" });
    return baseScenario(
      id,
      workflow("organizing", "1 change is waiting", "Continue organizing", "continue-organizing"),
      organization({
        recommendations: [accepted],
        choicesNeeded: [destinationChoice()],
        movementTree: movementTree([{ name: accepted.name, kind: accepted.kind }]),
        movementSummary: { filesToMove: 1, namesToChange: 0, foldersToCreate: 0, duplicateCopies: 0 },
        approval: {
          readyForApproval: false,
          filesToMove: 1,
          duplicateCopiesDeferred: 0,
          choicesRemaining: 1,
          confirmLabel: "Approve",
          supportingText: "No files have moved yet.",
        },
      }),
    );
  }
  if (id === "duplicate_choice") {
    const accepted = recommendation({ decision: "accept" });
    return baseScenario(
      id,
      workflow("organizing", "1 change is waiting", "Continue organizing", "continue-organizing"),
      organization({
        recommendations: [accepted],
        choicesNeeded: [duplicateChoice()],
        movementTree: movementTree([{ name: accepted.name, kind: accepted.kind }]),
        movementSummary: { filesToMove: 1, namesToChange: 0, foldersToCreate: 0, duplicateCopies: 1 },
        approval: {
          readyForApproval: false,
          filesToMove: 1,
          duplicateCopiesDeferred: 1,
          choicesRemaining: 1,
          confirmLabel: "Approve",
          supportingText: "No files have moved yet.",
        },
      }),
    );
  }
  if (id === "ready_for_approval") {
    const report = recommendation({ decision: "accept" });
    const budget = secondRecommendation({ decision: "accept" });
    const copies = duplicateChoice({ confirmed: true });
    return baseScenario(
      id,
      workflow("approving", "Ready to approve", "Review and approve", "review-and-approve"),
      organization({
        recommendations: [report, budget],
        choicesNeeded: [copies],
        movementTree: movementTree([
          { name: report.name, kind: report.kind },
          { name: budget.name, kind: budget.kind },
        ]),
        movementSummary: { filesToMove: 2, namesToChange: 0, foldersToCreate: 0, duplicateCopies: 1 },
        approval: {
          readyForApproval: true,
          filesToMove: 2,
          duplicateCopiesDeferred: 1,
          choicesRemaining: 0,
          confirmLabel: "Approve",
          supportingText: "No files have moved yet.",
        },
      }),
    );
  }
  if (id === "ready_to_move") {
    const report = recommendation({ decision: "accept" });
    const budget = secondRecommendation({ decision: "accept" });
    return baseScenario(
      id,
      workflow("ready_to_move", "Ready to move", "Move files — not connected yet", "none", false),
      organization({
        recommendations: [report, budget],
        movementTree: movementTree([
          { name: report.name, kind: report.kind },
          { name: budget.name, kind: budget.kind },
        ]),
        movementSummary: { filesToMove: 2, namesToChange: 0, foldersToCreate: 0, duplicateCopies: 0 },
        approval: {
          readyForApproval: false,
          filesToMove: 2,
          duplicateCopiesDeferred: 0,
          choicesRemaining: 0,
          confirmLabel: "Approve",
          supportingText: "No files have moved yet.",
        },
      }),
    );
  }
  if (id === "stale_generation") {
    return baseScenario(
      id,
      workflow("stale", "Files have changed", "Check again", "check-again", true, {
        message: "Check the folder again before continuing.",
        staleKind: "generation",
      }),
      organization({ keptByStandards: [] }),
    );
  }
  if (id === "stale_revision") {
    return baseScenario(
      id,
      workflow("stale", "Organization choices were updated", "Reload choices", "reload-choices", true, {
        message: "Reload the latest choices.",
        staleKind: "revision",
      }),
      organization({ keptByStandards: [] }),
    );
  }
  if (id === "disconnected_pages") {
    return baseScenario(
      id,
      workflow("idle", "Files have not been checked yet", "Check files", "check-files"),
      organization({ keptByStandards: [] }),
      { lastCheckedLabel: "Not checked yet" },
    );
  }
  throw new Error(`Unknown Stage B1 scenario: ${id}`);
}

export function createStageB1Fixture(acceptedFixture, scenarioId = DEFAULT_STAGE_B1_SCENARIO_ID) {
  if (!STAGE_B1_SCENARIO_IDS.includes(scenarioId)) {
    throw new Error(`Unknown Stage B1 scenario: ${scenarioId}`);
  }
  const fixture = clone(acceptedFixture);
  if (scenarioId !== DEFAULT_STAGE_B1_SCENARIO_ID) fixture.stageB1 = buildScenario(scenarioId);
  return fixture;
}
