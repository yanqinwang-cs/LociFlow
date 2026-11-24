const STYLE_KEYS = ["cautious", "standard", "proactive"];
const ACTION_TYPES = ["move", "rename", "create_folder", "duplicate"];
export const WORKFLOW_STAGES = ["idle", "checking", "organizing", "approving", "moving", "verifying"];

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function samePath(left = [], right = []) {
  return left.length === right.length && left.every((part, index) => part === right[index]);
}

function pathStartsWith(path = [], prefix = []) {
  return prefix.length <= path.length && prefix.every((part, index) => path[index] === part);
}

export function authoredCandidateChanges(fixtures, style) {
  return clone([
    ...fixtures.baseChanges,
    ...(style === "standard" || style === "proactive" ? fixtures.standardAdds : []),
    ...(style === "proactive" ? fixtures.proactiveAdds : []),
  ]);
}

export function applyStandardsToChanges(candidateChanges, activeStandards) {
  const keepUnchanged = activeStandards.filter((standard) => (
    standard.active && (standard.effect === "keep_unchanged" || standard.id === "keep-submissions")
  ));
  const allowedChanges = [];
  const keptByStandards = [];

  clone(candidateChanges).forEach((change) => {
    const standard = keepUnchanged.find((item) => pathStartsWith(change.currentFolder, item.appliesTo));
    if (standard) {
      keptByStandards.push({
        id: change.id,
        name: change.name,
        currentFolder: clone(change.currentFolder),
        standardId: standard.id,
        standardName: standard.name,
      });
      return;
    }
    allowedChanges.push(change);
  });

  return { allowedChanges, keptByStandards };
}

export function buildChangeCollections(fixtures, style, standards) {
  const candidateChanges = authoredCandidateChanges(fixtures, style);
  const activeStandards = standards.filter((standard) => standard.active);
  const { allowedChanges, keptByStandards } = applyStandardsToChanges(candidateChanges, activeStandards);
  const changes = allowedChanges.map((change) => ({
    ...change,
    classification: change.recommended ? "recommended" : "choice",
    decision: "pending",
  }));
  return { candidateChanges, activeStandards: clone(activeStandards), changes, keptByStandards };
}

export function deriveActionCounts(changes, pendingOnly = true) {
  const relevant = pendingOnly ? changes.filter((change) => change.decision === "pending") : changes;
  const counts = { move: 0, rename: 0, create_folder: 0, duplicate: 0, total: relevant.length };
  relevant.forEach((change) => {
    if (ACTION_TYPES.includes(change.type)) counts[change.type] += 1;
  });
  return counts;
}

export function recommendedChanges(changes) {
  return changes.filter((change) => change.decision === "pending" && change.classification === "recommended");
}

export function choiceChanges(changes) {
  return changes.filter((change) => change.decision === "pending" && change.classification === "choice");
}

export function compatibleChanges(changes, ids, action) {
  const selected = new Set(ids);
  return changes.filter((change) => {
    if (!selected.has(change.id) || change.decision !== "pending") return false;
    if (action === "accept") return ["move", "rename", "create_folder"].includes(change.type);
    if (action === "change-folder") return change.type === "move";
    return ["keep", "remove"].includes(action);
  });
}

function firstWaiting(changes) {
  return changes.find((change) => change.decision === "pending") || null;
}

function standardFromBuilder(builder, index) {
  const preset = builder.recognize || "Person name";
  const folder = builder.folder?.length ? builder.folder : ["Sample Downloads"];
  const second = builder.fileType ? " and file type" : "";
  const names = {
    "Keep a folder unchanged": "Keep this folder unchanged",
    "Keep files together": "Keep these files together",
    "Keep current file names": "Keep current file names",
    "Use existing folders only": "Use existing folders only",
    "Sort similar files": `Sort by ${preset.toLowerCase()}${second}`,
  };
  return {
    id: `saved-standard-${index}`,
    name: names[builder.action] || names["Sort similar files"],
    appliesTo: clone(folder),
    description: builder.action === "Sort similar files"
      ? `Recognize ${preset.toLowerCase()} and ${builder.grouping?.toLowerCase() || "keep matching files together"}.`
      : "Use this instruction during future organization.",
    active: true,
  };
}

function sampleFilesFromFixtures(fixtures) {
  const allChanges = [
    ...fixtures.baseChanges,
    ...fixtures.standardAdds,
    ...fixtures.proactiveAdds,
  ];
  return allChanges
    .filter((change) => change.type !== "create_folder")
    .map((change) => ({
      id: `sample-${change.id}`,
      changeId: change.id,
      name: change.name,
      folder: clone(change.currentFolder),
      kind: change.kind,
    }));
}

function batchCounts(changes) {
  const duplicateBinFiles = changes
    .filter((change) => change.type === "duplicate")
    .reduce((total, change) => total + (change.copiesToBin || 1), 0);
  const movedFiles = changes.filter((change) => change.type === "move").length;
  const renamedFiles = changes.filter((change) => change.type === "rename").length;
  const createdFolders = changes.filter((change) => change.type === "create_folder").length;
  return {
    movedFiles,
    renamedFiles,
    createdFolders,
    duplicateBinFiles,
    affectedFiles: movedFiles + renamedFiles + duplicateBinFiles,
  };
}

function actionSummary(counts) {
  const parts = [];
  if (counts.movedFiles) parts.push(`${counts.movedFiles} moved`);
  if (counts.renamedFiles) parts.push(`${counts.renamedFiles} ${counts.renamedFiles === 1 ? "name" : "names"} changed`);
  if (counts.createdFolders) parts.push(`${counts.createdFolders} ${counts.createdFolders === 1 ? "folder" : "folders"} created`);
  if (counts.duplicateBinFiles) parts.push(`${counts.duplicateBinFiles} ${counts.duplicateBinFiles === 1 ? "copy" : "copies"} moved to Duplicate Bin`);
  return parts.join(" · ") || "No files moved";
}

function activityDetails(changes) {
  return {
    filesMoved: changes.filter((change) => change.type === "move").map((change) => change.name),
    namesChanged: changes.filter((change) => change.type === "rename").map((change) => change.newName || change.name),
    foldersCreated: changes.filter((change) => change.type === "create_folder").map((change) => change.newFolder.join(" › ")),
    copiesMoved: changes.filter((change) => change.type === "duplicate").flatMap((change) => (
      Array.from({ length: change.copiesToBin || 1 }, () => change.name)
    )),
    filesRestored: [],
    problems: [],
  };
}

function applyCollections(draft, collections) {
  draft.candidateChanges = collections.candidateChanges;
  draft.activeStandards = collections.activeStandards;
  draft.changes = collections.changes;
  draft.keptByStandards = collections.keptByStandards;
}

function updatePlanningStage(draft) {
  draft.workflowStage = draft.changes.some((change) => change.decision === "pending") ? "organizing" : "approving";
}

function interpretationFixture(fixtures, key) {
  return clone(fixtures.interpretations[key]);
}

export function createStore(fixtures) {
  const initialState = () => {
    const standards = clone(fixtures.standards);
    const collections = buildChangeCollections(fixtures, "cautious", standards);
    return {
      route: "home",
      theme: fixtures.settings.theme,
      organizationStyle: "cautious",
      ...collections,
      selectedChangeIds: [],
      selectionAnchorId: null,
      activeChangeId: null,
      actionFilter: "all",
      duplicateBinItems: clone(fixtures.duplicateBinItems),
      selectedBinIds: [],
      activityMoves: clone(fixtures.activityMoves),
      expandedActivityIds: [],
      standards,
      settings: clone(fixtures.settings),
      settingsSection: "appearance",
      workflowStage: "organizing",
      workflowCompleted: false,
      verificationResult: null,
      approvedBatch: null,
      sampleFiles: sampleFilesFromFixtures(fixtures),
      createdFolders: [],
      lastChecked: fixtures.meta.lastChecked,
      lastOrganized: fixtures.meta.lastOrganized,
      nextCheck: fixtures.meta.nextCheck,
      currentFolder: fixtures.meta.currentFolder,
      lastMoveSummary: fixtures.meta.lastMoveSummary,
      stageB1: clone(fixtures.stageB1 || null),
      selectedItemId: fixtures.stageB1
        ? fixtures.stageB1.organization.recommendations[0]?.id
          || fixtures.stageB1.organization.choicesNeeded[0]?.id
          || fixtures.stageB1.organization.keptByStandards[0]?.itemId
          || null
        : null,
      showReuseSuggestion: false,
      moveUndoSnapshot: null,
      interpretation: { status: "idle", text: "", suggestions: [], clarification: null, alternatives: [] },
    };
  };

  let state = initialState();
  const listeners = new Set();
  const emit = () => listeners.forEach((listener) => listener(state));
  const update = (mutator) => {
    mutator(state);
    emit();
  };

  const rebuildPlan = (draft, stage = "organizing") => {
    applyCollections(draft, buildChangeCollections(fixtures, draft.organizationStyle, draft.standards));
    draft.selectedChangeIds = [];
    draft.selectionAnchorId = null;
    draft.activeChangeId = firstWaiting(draft.changes)?.id || null;
    draft.actionFilter = "all";
    draft.workflowStage = stage;
    draft.workflowCompleted = false;
    draft.verificationResult = null;
    draft.approvedBatch = null;
    draft.showReuseSuggestion = false;
  };

  const store = {
    fixtures,
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setRoute(route) {
      update((draft) => { draft.route = route; });
    },
    openOrganize() {
      if (state.stageB1) {
        update((draft) => {
          draft.route = "organize";
          draft.selectedItemId = draft.selectedItemId
            || draft.stageB1.organization.recommendations[0]?.id
            || draft.stageB1.organization.choicesNeeded[0]?.id
            || draft.stageB1.organization.keptByStandards[0]?.itemId
            || null;
        });
        return store.activeContractItem();
      }
      update((draft) => {
        draft.route = "organize";
        if (!["moving", "verifying"].includes(draft.workflowStage)) draft.workflowStage = "organizing";
        const first = firstWaiting(draft.changes);
        draft.activeChangeId = first?.id || null;
        draft.selectedChangeIds = first ? [first.id] : [];
        draft.selectionAnchorId = first?.id || null;
      });
      return store.activeChange();
    },
    startCheck() {
      if (state.stageB1) {
        update((draft) => {
          draft.route = "home";
          draft.stageB1.workflow = {
            stage: "checking",
            status: "Checking files…",
            primaryAction: { label: "Checking files…", action: "none", enabled: false },
          };
          draft.stageB1.scope.lastCheckedLabel = "Checking now";
          draft.stageB1.freshness.lastCheckedLabel = "Checking now";
        });
        return;
      }
      update((draft) => {
        rebuildPlan(draft, "checking");
        draft.route = "home";
        draft.lastChecked = "Today, 6:52 PM";
      });
    },
    setTheme(theme) {
      if (!["light", "dark"].includes(theme)) return;
      update((draft) => {
        draft.theme = theme;
        draft.settings.theme = theme;
      });
    },
    setOrganizationStyle(style) {
      if (state.stageB1) {
        if (!(style in state.stageB1.organization.style.options)
            || style === state.stageB1.organization.style.selected) return false;
        update((draft) => { draft.stageB1.organization.style.selected = style; });
        return true;
      }
      if (!STYLE_KEYS.includes(style) || style === state.organizationStyle) return false;
      update((draft) => {
        draft.organizationStyle = style;
        rebuildPlan(draft);
      });
      return true;
    },
    waitingChanges() {
      const waiting = state.changes.filter((change) => change.decision === "pending");
      return state.actionFilter === "all" ? waiting : waiting.filter((change) => change.type === state.actionFilter);
    },
    recommendedChanges() {
      return recommendedChanges(state.changes);
    },
    choiceChanges() {
      return choiceChanges(state.changes);
    },
    acceptedChanges() {
      return state.changes.filter((change) => change.decision === "accepted");
    },
    changeCounts() {
      return deriveActionCounts(state.changes);
    },
    setActionFilter(type) {
      update((draft) => {
        draft.actionFilter = ACTION_TYPES.includes(type) ? type : "all";
        const first = store.waitingChanges()[0] || null;
        draft.activeChangeId = first?.id || null;
        draft.selectedChangeIds = first ? [first.id] : [];
      });
    },
    activeChange() {
      return state.changes.find((change) => change.id === state.activeChangeId) || null;
    },
    activeContractItem() {
      if (!state.stageB1) return null;
      const organization = state.stageB1.organization;
      return [
        ...organization.recommendations,
        ...organization.choicesNeeded,
        ...organization.keptByStandards,
      ].find((item) => (item.id || item.itemId) === state.selectedItemId) || null;
    },
    selectContractItem(id) {
      if (!state.stageB1) return false;
      const organization = state.stageB1.organization;
      const exists = [
        ...organization.recommendations,
        ...organization.choicesNeeded,
        ...organization.keptByStandards,
      ].some((item) => (item.id || item.itemId) === id);
      if (!exists) return false;
      update((draft) => { draft.selectedItemId = id; });
      return true;
    },
    decideContractRecommendation(id, decision) {
      if (!state.stageB1 || !["accept", "keep"].includes(decision)) return false;
      const item = state.stageB1.organization.recommendations.find((entry) => entry.id === id);
      if (!item) return false;
      update((draft) => {
        const target = draft.stageB1.organization.recommendations.find((entry) => entry.id === id);
        target.decision = decision;
        draft.selectedItemId = id;
      });
      return true;
    },
    selectDestinationOption(id, optionId) {
      if (!state.stageB1) return false;
      const choice = state.stageB1.organization.choicesNeeded.find((item) => (
        item.id === id && item.choiceType === "destination"
      ));
      if (!choice?.options.some((option) => option.optionId === optionId)) return false;
      update((draft) => {
        const target = draft.stageB1.organization.choicesNeeded.find((item) => item.id === id);
        target.selectedOptionId = optionId;
        target.confirmed = false;
        draft.selectedItemId = id;
      });
      return true;
    },
    confirmDestinationChoice(id) {
      if (!state.stageB1) return false;
      const choice = state.stageB1.organization.choicesNeeded.find((item) => (
        item.id === id && item.choiceType === "destination"
      ));
      if (!choice?.options.some((option) => option.optionId === choice.selectedOptionId)) return false;
      update((draft) => {
        const target = draft.stageB1.organization.choicesNeeded.find((item) => item.id === id);
        target.confirmed = true;
      });
      return true;
    },
    setDuplicateDecision(id, decision) {
      if (!state.stageB1 || !["keep_all", "choose_for_bin"].includes(decision)) return false;
      const choice = state.stageB1.organization.choicesNeeded.find((item) => (
        item.id === id && item.choiceType === "duplicate"
      ));
      if (!choice) return false;
      update((draft) => {
        const target = draft.stageB1.organization.choicesNeeded.find((item) => item.id === id);
        target.decision = decision;
        target.confirmed = false;
        if (decision === "keep_all") {
          target.copies.forEach((copy) => { copy.selectedForBin = false; });
        }
        draft.selectedItemId = id;
      });
      return true;
    },
    setDuplicateCopies(id, copyIds) {
      if (!state.stageB1 || !Array.isArray(copyIds) || new Set(copyIds).size !== copyIds.length) return false;
      const choice = state.stageB1.organization.choicesNeeded.find((item) => (
        item.id === id && item.choiceType === "duplicate"
      ));
      const known = new Set(choice?.copies.map((copy) => copy.copyId) || []);
      if (!choice || copyIds.some((copyId) => !known.has(copyId))) return false;
      const selected = new Set(copyIds);
      update((draft) => {
        const target = draft.stageB1.organization.choicesNeeded.find((item) => item.id === id);
        target.decision = "choose_for_bin";
        target.confirmed = false;
        target.copies.forEach((copy) => { copy.selectedForBin = selected.has(copy.copyId); });
        draft.selectedItemId = id;
      });
      return true;
    },
    toggleDuplicateCopy(id, copyId) {
      if (!state.stageB1) return false;
      const choice = state.stageB1.organization.choicesNeeded.find((item) => (
        item.id === id && item.choiceType === "duplicate"
      ));
      if (!choice?.copies.some((copy) => copy.copyId === copyId)) return false;
      const selected = choice.copies.filter((copy) => copy.selectedForBin).map((copy) => copy.copyId);
      const next = selected.includes(copyId)
        ? selected.filter((item) => item !== copyId)
        : [...selected, copyId];
      return store.setDuplicateCopies(id, next);
    },
    duplicateChoiceStatus(id) {
      if (!state.stageB1) return { valid: false, selectedCount: 0, copyCount: 0 };
      const choice = state.stageB1.organization.choicesNeeded.find((item) => (
        item.id === id && item.choiceType === "duplicate"
      ));
      if (!choice) return { valid: false, selectedCount: 0, copyCount: 0 };
      const selectedIds = choice.copies.filter((copy) => copy.selectedForBin).map((copy) => copy.copyId);
      const unique = new Set(selectedIds);
      const valid = choice.decision === "keep_all"
        || (choice.decision === "choose_for_bin"
          && unique.size === selectedIds.length
          && selectedIds.length > 0
          && selectedIds.length < choice.copies.length);
      return { valid, selectedCount: selectedIds.length, copyCount: choice.copies.length };
    },
    confirmDuplicateChoice(id) {
      const status = store.duplicateChoiceStatus(id);
      if (!status.valid) return false;
      update((draft) => {
        const target = draft.stageB1.organization.choicesNeeded.find((item) => item.id === id);
        target.confirmed = true;
      });
      return true;
    },
    selectChange(id, { additive = false, range = false } = {}) {
      const visibleIds = store.waitingChanges().map((change) => change.id);
      if (!visibleIds.includes(id)) return;
      update((draft) => {
        if (range && draft.selectionAnchorId && visibleIds.includes(draft.selectionAnchorId)) {
          const start = visibleIds.indexOf(draft.selectionAnchorId);
          const end = visibleIds.indexOf(id);
          const rangeIds = visibleIds.slice(Math.min(start, end), Math.max(start, end) + 1);
          draft.selectedChangeIds = [...new Set(additive ? [...draft.selectedChangeIds, ...rangeIds] : rangeIds)];
        } else if (additive) {
          draft.selectedChangeIds = draft.selectedChangeIds.includes(id)
            ? draft.selectedChangeIds.filter((item) => item !== id)
            : [...draft.selectedChangeIds, id];
          draft.selectionAnchorId = id;
        } else {
          draft.selectedChangeIds = [id];
          draft.selectionAnchorId = id;
        }
        draft.activeChangeId = id;
      });
    },
    selectAllVisible() {
      update((draft) => {
        draft.selectedChangeIds = store.waitingChanges().map((change) => change.id);
        draft.selectionAnchorId = draft.selectedChangeIds.at(-1) || null;
        draft.activeChangeId = draft.selectedChangeIds[0] || null;
      });
    },
    clearSelection() {
      update((draft) => {
        draft.selectedChangeIds = [];
        draft.selectionAnchorId = null;
      });
    },
    acceptRecommended() {
      const eligible = recommendedChanges(state.changes);
      update((draft) => {
        const eligibleIds = new Set(eligible.map((change) => change.id));
        draft.changes.forEach((change) => {
          if (eligibleIds.has(change.id)) change.decision = "accepted";
        });
        const next = choiceChanges(draft.changes)[0] || firstWaiting(draft.changes);
        draft.activeChangeId = next?.id || null;
        draft.selectedChangeIds = next ? [next.id] : [];
        draft.selectionAnchorId = next?.id || null;
        updatePlanningStage(draft);
      });
      return eligible.map((change) => change.id);
    },
    bulkAction(action, folder = ["Teaching", "2026", "Shared"]) {
      const compatible = compatibleChanges(state.changes, state.selectedChangeIds, action);
      const ids = new Set(compatible.map((change) => change.id));
      update((draft) => {
        draft.changes.forEach((change) => {
          if (!ids.has(change.id)) return;
          if (action === "change-folder") {
            change.newFolder = clone(folder);
            change.changeText = `Move to ${folder.at(-1)}`;
            change.decision = "accepted";
            draft.showReuseSuggestion = true;
          } else {
            change.decision = action === "accept" ? "accepted" : "kept";
          }
        });
        const next = firstWaiting(draft.changes);
        draft.activeChangeId = next?.id || null;
        draft.selectedChangeIds = next ? [next.id] : [];
        draft.selectionAnchorId = next?.id || null;
        updatePlanningStage(draft);
      });
      return compatible.map((change) => change.id);
    },
    decideChange(id, choice) {
      const change = state.changes.find((item) => item.id === id && item.decision === "pending");
      if (!change) return false;
      update((draft) => {
        const target = draft.changes.find((item) => item.id === id);
        if (["accept", "use-name", "create", "move-bin"].includes(choice)) target.decision = "accepted";
        if (["keep", "keep-name", "skip-folder", "keep-copies", "remove"].includes(choice)) target.decision = "kept";
        if (choice === "change-folder") {
          target.newFolder = ["Teaching", "2026", "Shared"];
          target.changeText = "Move to Shared";
          target.decision = "accepted";
          draft.showReuseSuggestion = true;
        }
        const next = firstWaiting(draft.changes);
        draft.activeChangeId = next?.id || null;
        draft.selectedChangeIds = next ? [next.id] : [];
        draft.selectionAnchorId = next?.id || null;
        updatePlanningStage(draft);
      });
      return true;
    },
    canReviewAndApprove() {
      if (state.stageB1) return Boolean(state.stageB1.organization.approval.readyForApproval);
      return state.changes.length > 0
        && state.changes.every((change) => change.decision !== "pending")
        && state.changes.some((change) => change.decision === "accepted")
        && !state.safetyProblems?.length;
    },
    beginApproval() {
      if (!store.canReviewAndApprove()) return false;
      update((draft) => {
        if (draft.stageB1) {
          draft.stageB1.workflow.stage = "approving";
          draft.stageB1.workflow.status = "Ready to approve";
        } else {
          draft.workflowStage = "approving";
        }
      });
      return true;
    },
    approvalSummary() {
      if (state.stageB1) return clone(state.stageB1.organization.approval);
      return batchCounts(store.acceptedChanges());
    },
    approveScenario() {
      if (!state.stageB1 || !store.canReviewAndApprove()) return null;
      const approval = clone(state.stageB1.organization.approval);
      if (approval.choicesRemaining !== 0) return null;
      update((draft) => {
        draft.stageB1.workflow = {
          stage: "ready_to_move",
          status: "Ready to move",
          primaryAction: {
            label: "Move files — not connected yet",
            action: "none",
            enabled: false,
          },
        };
        draft.stageB1.organization.approval.readyForApproval = false;
      });
      return approval;
    },
    reloadScenarioChoices() {
      if (!state.stageB1) return false;
      update((draft) => {
        draft.stageB1.workflow = {
          stage: "organizing",
          status: "Changes are ready to review",
          primaryAction: {
            label: "Continue organizing",
            action: "continue-organizing",
            enabled: true,
          },
        };
      });
      return true;
    },
    approveTestMove() {
      if (!store.canReviewAndApprove()) return null;
      const accepted = clone(store.acceptedChanges());
      const counts = batchCounts(accepted);
      const snapshot = {
        sampleFiles: clone(state.sampleFiles),
        createdFolders: clone(state.createdFolders),
        duplicateBinItems: clone(state.duplicateBinItems),
        changes: clone(state.changes),
        candidateChanges: clone(state.candidateChanges),
        activeStandards: clone(state.activeStandards),
        keptByStandards: clone(state.keptByStandards),
        lastOrganized: state.lastOrganized,
        lastMoveSummary: state.lastMoveSummary,
        workflowCompleted: state.workflowCompleted,
      };
      const binAdds = accepted.filter((change) => change.type === "duplicate").flatMap((change) => (
        Array.from({ length: change.copiesToBin || 1 }, (_, index) => ({
          id: `${change.id}-copy-${index + 1}`,
          name: change.name,
          icon: change.icon,
          kind: change.kind,
          size: change.size,
          originalFolder: clone(change.currentFolder),
          dateMoved: "Today, 6:42 PM",
          moveId: "move-today",
        }))
      ));

      update((draft) => {
        accepted.forEach((change) => {
          const file = draft.sampleFiles.find((item) => item.changeId === change.id);
          if (change.type === "move" && file) file.folder = clone(change.newFolder);
          if (change.type === "rename" && file) file.name = change.newName;
          if (change.type === "create_folder" && !draft.createdFolders.some((folder) => samePath(folder, change.newFolder))) {
            draft.createdFolders.push(clone(change.newFolder));
          }
        });
        draft.duplicateBinItems.push(...binAdds);
        draft.moveUndoSnapshot = clone(snapshot);
        draft.approvedBatch = { id: "move-today", acceptedChanges: accepted, counts, binItemIds: binAdds.map((item) => item.id), preMoveSnapshot: snapshot };
        draft.workflowStage = "moving";
        draft.workflowCompleted = false;
        draft.verificationResult = null;
      });
      return clone(state.approvedBatch);
    },
    verifyTestMove() {
      if (state.workflowStage !== "moving" || !state.approvedBatch) return null;
      const batch = clone(state.approvedBatch);
      const checks = [];
      batch.acceptedChanges.forEach((change) => {
        const file = state.sampleFiles.find((item) => item.changeId === change.id);
        if (change.type === "move") checks.push({ name: `${change.name} is in its destination`, passed: Boolean(file && samePath(file.folder, change.newFolder)) });
        if (change.type === "rename") checks.push({ name: `${change.name} has its expected name`, passed: Boolean(file && file.name === change.newName) });
        if (change.type === "create_folder") checks.push({ name: `${change.name} exists`, passed: state.createdFolders.some((folder) => samePath(folder, change.newFolder)) });
        if (change.type === "duplicate") checks.push({ name: `${change.name} copies are in Duplicate Bin`, passed: batch.binItemIds.every((id) => state.duplicateBinItems.some((item) => item.id === id)) });
      });
      const protectedUnchanged = state.keptByStandards.every((kept) => {
        const before = batch.preMoveSnapshot.sampleFiles.find((item) => item.changeId === kept.id);
        const after = state.sampleFiles.find((item) => item.changeId === kept.id);
        return Boolean(before && after && before.name === after.name && samePath(before.folder, after.folder));
      });
      checks.push({ name: "Active Standards were preserved", passed: protectedUnchanged });
      const passed = checks.every((check) => check.passed);
      const details = activityDetails(batch.acceptedChanges);
      const entry = {
        id: batch.id,
        type: "organization_move",
        title: `Organized ${batch.counts.affectedFiles} files`,
        summary: actionSummary(batch.counts),
        status: "Completed",
        completedAt: "Today, 6:42 PM",
        time: "Today, 6:42 PM",
        movedFiles: clone(details.filesMoved),
        renamedFiles: clone(details.namesChanged),
        createdFolders: clone(details.foldersCreated),
        duplicateBinFiles: clone(details.copiesMoved),
        problems: passed ? [] : checks.filter((check) => !check.passed).map((check) => check.name),
        preMoveSnapshot: clone(batch.preMoveSnapshot),
        undoable: passed,
        details: { ...details, problems: passed ? [] : checks.filter((check) => !check.passed).map((check) => check.name) },
      };
      update((draft) => {
        draft.verificationResult = { passed, checks, counts: clone(batch.counts) };
        draft.workflowStage = "verifying";
        if (passed) {
          draft.activityMoves.forEach((item) => { item.undoable = false; });
          draft.activityMoves.unshift(entry);
          draft.lastOrganized = "Today, 6:42 PM";
          draft.lastMoveSummary = `${batch.counts.affectedFiles} files moved today`;
          draft.workflowCompleted = true;
          draft.changes = [];
          draft.selectedChangeIds = [];
          draft.activeChangeId = null;
        }
      });
      return clone(state.verificationResult);
    },
    completeWorkflow() {
      if (state.workflowStage !== "verifying" || !state.verificationResult?.passed) return false;
      update((draft) => {
        draft.workflowStage = "idle";
        draft.workflowCompleted = true;
        draft.route = "home";
        draft.approvedBatch = null;
      });
      return true;
    },
    toggleBinSelection(id, additive = true) {
      if (!state.duplicateBinItems.some((item) => item.id === id)) return;
      update((draft) => {
        draft.selectedBinIds = additive && draft.selectedBinIds.includes(id)
          ? draft.selectedBinIds.filter((item) => item !== id)
          : additive ? [...draft.selectedBinIds, id] : [id];
      });
    },
    selectAllBin() {
      update((draft) => { draft.selectedBinIds = draft.duplicateBinItems.map((item) => item.id); });
    },
    clearBinSelection() {
      update((draft) => { draft.selectedBinIds = []; });
    },
    restoreDuplicates(ids = state.selectedBinIds) {
      const selected = new Set(ids);
      const restored = state.duplicateBinItems.filter((item) => selected.has(item.id));
      if (!restored.length) return [];
      const entry = {
        id: `restore-${Date.now()}`,
        type: "duplicate_restore",
        title: `Restored ${restored.length} ${restored.length === 1 ? "file" : "files"}`,
        summary: `${restored.length} ${restored.length === 1 ? "file returned" : "files returned"} to the stored folder`,
        status: "Completed",
        time: "Today, 6:48 PM",
        completedAt: "Today, 6:48 PM",
        undoable: false,
        details: {
          filesMoved: [], namesChanged: [], foldersCreated: [], copiesMoved: [],
          filesRestored: restored.map((item) => item.name), problems: [],
        },
      };
      update((draft) => {
        draft.duplicateBinItems = draft.duplicateBinItems.filter((item) => !selected.has(item.id));
        draft.selectedBinIds = [];
        draft.activityMoves.unshift(entry);
        draft.lastMoveSummary = `${restored.length} ${restored.length === 1 ? "file" : "files"} restored today`;
      });
      return restored.map((item) => item.id);
    },
    undoLatestMove() {
      const entry = state.activityMoves.find((item) => item.type === "organization_move" && item.undoable);
      if (!entry?.preMoveSnapshot) return false;
      const snapshot = clone(entry.preMoveSnapshot);
      const restoredCount = batchCounts(state.approvedBatch?.acceptedChanges || [
        ...entry.movedFiles.map(() => ({ type: "move" })),
        ...entry.renamedFiles.map(() => ({ type: "rename" })),
        ...entry.duplicateBinFiles.map(() => ({ type: "duplicate", copiesToBin: 1 })),
      ]).affectedFiles;
      update((draft) => {
        draft.activityMoves.forEach((item) => { item.undoable = false; });
        draft.sampleFiles = snapshot.sampleFiles;
        draft.createdFolders = snapshot.createdFolders;
        draft.duplicateBinItems = snapshot.duplicateBinItems;
        draft.changes = snapshot.changes;
        draft.candidateChanges = snapshot.candidateChanges;
        draft.activeStandards = snapshot.activeStandards;
        draft.keptByStandards = snapshot.keptByStandards;
        draft.lastOrganized = snapshot.lastOrganized;
        draft.lastMoveSummary = snapshot.lastMoveSummary;
        draft.workflowCompleted = snapshot.workflowCompleted;
        draft.workflowStage = "idle";
        draft.route = "home";
        draft.moveUndoSnapshot = null;
        draft.approvedBatch = null;
        draft.verificationResult = null;
        draft.showReuseSuggestion = false;
        draft.activeChangeId = null;
        draft.selectedChangeIds = [];
        draft.selectionAnchorId = null;
        draft.activityMoves.unshift({
          id: "restore-organization-today",
          type: "organization_restore",
          title: "Restored previous organization",
          summary: `${restoredCount} files restored`,
          status: "Completed",
          completedAt: "Today, 6:50 PM",
          time: "Today, 6:50 PM",
          undoable: false,
          details: {
            filesMoved: [], namesChanged: [], foldersCreated: [], copiesMoved: [],
            filesRestored: [...entry.movedFiles, ...entry.renamedFiles, ...entry.duplicateBinFiles], problems: [],
          },
        });
      });
      return true;
    },
    toggleActivity(id) {
      update((draft) => {
        draft.expandedActivityIds = draft.expandedActivityIds.includes(id)
          ? draft.expandedActivityIds.filter((item) => item !== id)
          : [...draft.expandedActivityIds, id];
      });
    },
    setAllActivityExpanded(expanded) {
      update((draft) => {
        draft.expandedActivityIds = expanded ? draft.activityMoves.map((item) => item.id) : [];
      });
    },
    interpretPreference(text) {
      const normalized = text.trim().toLowerCase().replace(/\s+/g, " ");
      let next;
      if (normalized.includes("remove files") || normalized.includes("probably do not need")) {
        const fixture = interpretationFixture(fixtures, "unsafeRemoval");
        next = { status: "unsafe", text, suggestions: [], clarification: null, alternatives: fixture.alternatives };
      } else if (normalized.includes("organize files by name")) {
        const fixture = interpretationFixture(fixtures, "nameAmbiguity");
        next = { status: "clarification", text, suggestions: [], clarification: fixture, alternatives: [] };
      } else if (normalized.includes("coding projects") || normalized.includes("breaking imports")) {
        const fixture = interpretationFixture(fixtures, "codingProjects");
        next = { status: "suggested", text, suggestions: fixture.suggestions.map((item) => ({ ...item, active: false })), clarification: null, alternatives: [] };
      } else {
        next = { status: "unsupported", text, suggestions: [], clarification: null, alternatives: [] };
      }
      update((draft) => { draft.interpretation = next; });
      return clone(next);
    },
    clarifyInterpretation(choice) {
      if (state.interpretation.status !== "clarification") return [];
      const authored = state.interpretation.clarification.choices.find((item) => item.label === choice);
      if (!authored) return [];
      const suggestions = clone(authored.suggestions).map((item) => ({ ...item, active: false }));
      update((draft) => {
        draft.interpretation.status = "suggested";
        draft.interpretation.suggestions = suggestions;
        draft.interpretation.clarification = null;
      });
      return suggestions;
    },
    removeSuggestedStandard(id) {
      update((draft) => {
        draft.interpretation.suggestions = draft.interpretation.suggestions.filter((item) => item.id !== id);
      });
    },
    updateSuggestedStandard(id, values = {}) {
      update((draft) => {
        const suggestion = draft.interpretation.suggestions.find((item) => item.id === id);
        if (suggestion) {
          suggestion.name = values.name?.trim() || suggestion.name;
          suggestion.description = values.description?.trim() || suggestion.description;
          suggestion.appliesTo = values.appliesTo?.length ? clone(values.appliesTo) : suggestion.appliesTo;
        }
      });
      return clone(state.interpretation.suggestions.find((item) => item.id === id) || null);
    },
    confirmSuggestedStandards() {
      const suggestions = clone(state.interpretation.suggestions);
      update((draft) => {
        suggestions.forEach((suggestion, index) => {
          draft.standards.push({
            id: `interpreted-standard-${draft.standards.length + index + 1}`,
            name: suggestion.name,
            description: suggestion.description,
            appliesTo: clone(suggestion.appliesTo),
            active: true,
          });
        });
        draft.interpretation = { status: "idle", text: "", suggestions: [], clarification: null, alternatives: [] };
      });
      return suggestions.length;
    },
    cancelInterpretation() {
      update((draft) => {
        draft.interpretation = { status: "idle", text: "", suggestions: [], clarification: null, alternatives: [] };
      });
    },
    saveStandard(builder = {}) {
      const standard = standardFromBuilder(builder, state.standards.length + 1);
      update((draft) => { draft.standards.push(standard); });
      return standard;
    },
    toggleStandard(id) {
      update((draft) => {
        const standard = draft.standards.find((item) => item.id === id);
        if (standard) {
          standard.active = !standard.active;
          rebuildPlan(draft);
        }
      });
    },
    standardForChange(change) {
      return change?.standardId ? state.standards.find((item) => item.id === change.standardId) || null : null;
    },
    dismissReuseSuggestion() {
      update((draft) => { draft.showReuseSuggestion = false; });
    },
    setSettingsSection(section) {
      if (!["appearance", "notifications", "local-ai", "privacy", "advanced"].includes(section)) return;
      update((draft) => { draft.settingsSection = section; });
    },
    toggleNotification(key) {
      if (!(key in state.settings.notifications)) return;
      update((draft) => { draft.settings.notifications[key] = !draft.settings.notifications[key]; });
    },
    reset() {
      state = initialState();
      emit();
    },
  };

  return store;
}
