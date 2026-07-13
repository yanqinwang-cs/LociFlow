const STORAGE_KEY = "loci-flow:ux-1-1-focused:v1";

const clone = (value) => JSON.parse(JSON.stringify(value));

function localStore() {
  try {
    const probe = `${STORAGE_KEY}:probe`;
    window.localStorage.setItem(probe, "1");
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch (_error) {
    return null;
  }
}

function initialState(fixture) {
  return {
    stateVersion: fixture.fixtureVersion,
    folders: clone(fixture.folders),
    files: clone(fixture.files),
    stageDefinitions: clone(fixture.stageDefinitions),
    ...clone(fixture.initialState),
    workflow: {
      ...clone(fixture.initialState.workflow),
      baseline: null,
      feedback: "",
    },
    plan: {
      ...clone(fixture.initialState.plan),
      issueFilter: "all",
      expandedGroupIds: fixture.initialState.plan.groups.map((group) => group.id),
      recentlyChangedIds: [],
    },
    duplicates: {
      ...clone(fixture.initialState.duplicates),
      feedback: "",
    },
    selection: {
      ...clone(fixture.initialState.selection),
      duplicateGroupId: fixture.initialState.selection.duplicateGroupId || fixture.initialState.duplicates.activeGroupIds[0] || null,
      duplicateExtraFileIds: [],
      duplicateInspectorOpen: false,
      inspectorOpen: true,
      narrowPane: "planned",
    },
    expandedActivityIds: [fixture.initialState.activity[0]?.id].filter(Boolean),
    lastMutation: null,
  };
}

function usableState(value, fixture) {
  return Boolean(
    value &&
      value.stateVersion === fixture.fixtureVersion &&
      Array.isArray(value.files) &&
      Array.isArray(value.folders) &&
      value.plan &&
      value.duplicates &&
      value.workflow &&
      value.settings,
  );
}

function inferredReturnWorkflow(state, definition) {
  if (["organization", "duplicates"].includes(state.workflow?.active)) return state.workflow.active;
  if (definition === "organizationApply") return "organization";
  if (definition === "duplicateConsolidation") return "duplicates";
  return "none";
}

function sanitizePersistedOperation(value, fixture) {
  const operation = value.stagedOperation || clone(fixture.initialState.stagedOperation);
  const transientWorkflow = ["applying", "verifying"].includes(value.workflow?.active);
  const interrupted = operation.status === "working" || transientWorkflow;
  const completedButUncleared = operation.status === "complete" && !transientWorkflow;
  if (!interrupted && !completedButUncleared) return { state: value, changed: false };

  const draft = clone(value);
  if (interrupted) {
    const finalTruthRecorded = operation.status === "working" && operation.activeStageId === "complete";
    const capturedWorkflow = ["organization", "duplicates", "none"].includes(operation.returnWorkflow)
      ? operation.returnWorkflow
      : inferredReturnWorkflow(value, operation.definition);
    let returnWorkflow = capturedWorkflow;

    if (finalTruthRecorded) {
      if (["organization", "duplicates", "none"].includes(operation.nextWorkflow)) returnWorkflow = operation.nextWorkflow;
      else if (operation.definition === "planPreparation") returnWorkflow = "organization";
      else if (["organizationApply", "duplicateConsolidation", "undo"].includes(operation.definition)) returnWorkflow = "none";
      draft.workflow.feedback = operation.definition === "planPreparation"
        ? "Plan preparation completed before reload. No files moved."
        : operation.definition === "freshnessCheck"
          ? "The sample file check completed before reload. The prior workflow and choices were preserved."
          : "The completed sample result was recovered after reload. Activity contains the final truth.";
    } else {
      draft.workflow.feedback = "The staged sample operation was interrupted by reload. No final result was recorded; the prior workflow is ready to continue.";
    }

    draft.workflow.active = returnWorkflow;
    if (["organization", "duplicates"].includes(returnWorkflow)) {
      const preserveCapturedBaseline = !finalTruthRecorded || operation.definition === "freshnessCheck";
      const capturedBaseline = preserveCapturedBaseline && operation.returnWorkflow === returnWorkflow && Object.hasOwn(operation, "returnBaseline")
        ? operation.returnBaseline
        : null;
      draft.workflow.baseline = capturedBaseline == null
        ? clone(returnWorkflow === "organization" ? draft.plan : draft.duplicates)
        : clone(capturedBaseline);
    } else {
      draft.workflow.baseline = null;
    }
  }

  draft.stagedOperation = clone(fixture.initialState.stagedOperation);
  draft.lastMutation = {
    label: interrupted ? "Recovered an interrupted staged operation" : "Cleared a completed staged operation after reload",
    at: now(),
  };
  return { state: draft, changed: true };
}

function getNested(object, path) {
  return path.split(".").reduce((value, key) => value?.[key], object);
}

function setNested(object, path, value) {
  const keys = path.split(".");
  const finalKey = keys.pop();
  const target = keys.reduce((current, key) => {
    if (!current[key] || typeof current[key] !== "object") current[key] = {};
    return current[key];
  }, object);
  target[finalKey] = value;
}

function now() {
  return new Date().toISOString();
}

export function createStore(fixture) {
  const storage = localStore();
  const listeners = new Set();
  let state = initialState(fixture);

  if (storage) {
    try {
      const candidate = JSON.parse(storage.getItem(STORAGE_KEY) || "null");
      if (usableState(candidate, fixture)) {
        const recovered = sanitizePersistedOperation(candidate, fixture);
        state = recovered.state;
        if (recovered.changed) storage.setItem(STORAGE_KEY, JSON.stringify(state));
      }
    } catch (_error) {
      storage.removeItem(STORAGE_KEY);
    }
  }

  const persist = () => {
    if (!storage) return;
    try {
      storage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (_error) {
      // Browser storage is optional; the in-memory state remains authoritative.
    }
  };

  const emit = () => listeners.forEach((listener) => listener(state));

  const mutate = (label, operation) => {
    operation(state);
    state.lastMutation = { label, at: now() };
    persist();
    emit();
  };

  const fileById = (id) => state.files.find((file) => file.id === id);
  const folderById = (id) => state.folders.find((folder) => folder.id === id);
  const changeById = (id) => state.plan.changes.find((change) => change.id === id);
  const changeForFile = (fileId) => state.plan.changes.find((change) => change.fileId === fileId);
  const groupById = (id) => state.plan.groups.find((group) => group.id === id);
  const groupForChange = (changeId) => state.plan.groups.find((group) => group.changeIds.includes(changeId));
  const duplicateGroupById = (id) => state.duplicates.groups.find((group) => group.id === id);

  const breadcrumb = (folderId) => {
    const result = [];
    const seen = new Set();
    let current = folderById(folderId);
    while (current && !seen.has(current.id)) {
      result.unshift(current);
      seen.add(current.id);
      current = current.parentId ? folderById(current.parentId) : null;
    }
    return result;
  };

  const folderPath = (folderId) => breadcrumb(folderId).map((folder) => folder.name).join(" › ");
  const filePath = (fileId) => {
    const file = fileById(fileId);
    return file ? `${folderPath(file.folderId)} › ${file.name}` : "Unknown sample file";
  };
  const plannedFileName = (change) => change?.plannedName || fileById(change?.fileId)?.name || "Unknown sample file";
  const plannedPath = (changeId) => {
    const change = changeById(changeId);
    return change ? `${folderPath(change.destinationFolderId)} › ${plannedFileName(change)}` : "Not in this plan";
  };

  const includedChanges = () => state.plan.changes.filter((change) => change.included !== false);
  const unresolvedReviews = () => state.plan.reviewItems.filter((item) => item.status === "unresolved" && changeById(item.changeId)?.included !== false);
  const unresolvedConflicts = () => state.plan.conflicts.filter((item) => item.status === "unresolved" && changeById(item.changeId)?.included !== false);
  const activeGroups = () => state.plan.groups.filter((group) => group.removed !== true && group.changeIds.some((id) => changeById(id)?.included !== false));

  const planSummary = () => ({
    changes: includedChanges().length,
    destinations: new Set(includedChanges().map((change) => change.destinationFolderId)).size,
    review: unresolvedReviews().length,
    conflicts: unresolvedConflicts().length,
    canApply: state.plan.status === "ready" && unresolvedReviews().length === 0 && unresolvedConflicts().length === 0 && includedChanges().length > 0,
  });

  const pushPlanUndo = (label) => {
    const holder = state.planUndo;
    holder.snapshots.push({ label, plan: clone(state.plan) });
    if (holder.snapshots.length > holder.maxSnapshots) holder.snapshots.shift();
  };

  const setPlanFeedback = (draft, message, changeIds = []) => {
    draft.plan.recentlyChangedIds = [...changeIds];
    draft.workflow.feedback = message;
  };

  const planWithoutNestedIntent = (plan) => {
    const result = clone(plan);
    delete result.intent;
    return result;
  };

  const currentPlanIntent = () => {
    const base = planWithoutNestedIntent(state.plan.intent || state.plan);
    const currentChanges = new Map(state.plan.changes.map((change) => [change.id, change]));
    base.changes = base.changes.map((change) => ({ ...change, ...(currentChanges.get(change.id) || {}) }));
    const currentReviews = new Map(state.plan.reviewItems.map((item) => [item.id, item]));
    base.reviewItems = base.reviewItems.map((item) => ({ ...item, ...(currentReviews.get(item.id) || {}) }));
    const currentConflicts = new Map(state.plan.conflicts.map((item) => [item.id, item]));
    base.conflicts = base.conflicts.map((item) => ({ ...item, ...(currentConflicts.get(item.id) || {}) }));
    return base;
  };

  const freshPlanFromCanonical = () => {
    const source = state.plan.status === "dismissed" ? planWithoutNestedIntent(fixture.initialState.plan) : currentPlanIntent();
    const fresh = clone(source);
    const pendingChanges = fresh.changes
      .filter((change) => change.included !== false)
      .filter((change) => {
        const file = fileById(change.fileId);
        const intendedName = change.plannedName || file?.name;
        return file && (file.folderId !== change.destinationFolderId || file.name !== intendedName);
      })
      .map((change) => {
        const file = fileById(change.fileId);
        return { ...change, sourceFolderId: file.folderId, included: true };
      });
    const pendingIds = new Set(pendingChanges.map((change) => change.id));
    fresh.changes = pendingChanges;
    fresh.groups = fresh.groups
      .map((group) => ({
        ...group,
        removed: false,
        changeIds: pendingChanges
          .filter((change) => change.destinationFolderId === group.destinationFolderId)
          .map((change) => change.id),
      }))
      .filter((group) => group.changeIds.length);
    fresh.reviewItems = fresh.reviewItems.filter((item) => pendingIds.has(item.changeId));
    fresh.conflicts = fresh.conflicts.filter((item) => pendingIds.has(item.changeId));
    fresh.status = pendingChanges.length ? "ready" : "up-to-date";
    fresh.summary = {
      fileChanges: pendingChanges.length,
      destinations: new Set(pendingChanges.map((change) => change.destinationFolderId)).size,
      reviewRequired: fresh.reviewItems.filter((item) => item.status === "unresolved").length,
      blockingConflicts: fresh.conflicts.filter((item) => item.status === "unresolved").length,
    };
    fresh.issueFilter = "all";
    fresh.expandedGroupIds = fresh.groups.map((group) => group.id);
    fresh.recentlyChangedIds = [];
    fresh.intent = planWithoutNestedIntent(source);
    return fresh;
  };

  const selectedMoveWouldCollide = (selected, destinationFolderId) => {
    const selectedIds = new Set(selected.map((change) => change.id));
    const projected = state.files.map((file) => {
      const change = changeForFile(file.id);
      if (!change || change.included === false) return { fileId: file.id, folderId: file.folderId, name: file.name };
      return {
        fileId: file.id,
        folderId: selectedIds.has(change.id) ? destinationFolderId : change.destinationFolderId,
        name: plannedFileName(change),
      };
    });
    return selected.some((change) => {
      const candidate = projected.find((item) => item.fileId === change.fileId);
      return projected.some((item) =>
        item.fileId !== candidate.fileId &&
        item.folderId === candidate.folderId &&
        item.name.toLocaleLowerCase() === candidate.name.toLocaleLowerCase(),
      );
    });
  };

  const duplicateGroupIsActive = (groupId) => state.duplicates.activeGroupIds.includes(groupId);

  const selectNextDuplicateGroup = (draft, completedGroupId) => {
    draft.duplicates.activeGroupIds = draft.duplicates.activeGroupIds.filter((id) => id !== completedGroupId);
    draft.duplicates.status = draft.duplicates.activeGroupIds.length ? "ready" : "complete";
    const nextGroupId = draft.duplicates.activeGroupIds[0] || null;
    const nextGroup = draft.duplicates.groups.find((candidate) => candidate.id === nextGroupId);
    draft.selection.duplicateGroupId = nextGroupId;
    draft.selection.duplicateExtraFileIds = [...(nextGroup?.selectedExtraFileIds || [])];
    draft.selection.duplicateInspectorOpen = false;
    draft.selection.inspectorItemId = nextGroupId;
  };

  const resolveIssuesForExcludedChange = (draft, changeId) => {
    draft.plan.reviewItems.forEach((item) => {
      if (item.changeId === changeId && item.status === "unresolved") {
        item.status = "resolved";
        item.decision = "left-unchanged";
      }
    });
    draft.plan.conflicts.forEach((item) => {
      if (item.changeId === changeId && item.status === "unresolved") {
        item.status = "resolved";
        item.decision = "left-unchanged";
      }
    });
  };

  const activityFileChange = (change) => ({
    fileId: change.fileId,
    beforeFolderId: change.sourceFolderId,
    afterFolderId: change.destinationFolderId,
    beforeName: fileById(change.fileId)?.name,
    afterName: plannedFileName(change),
  });

  const api = {
    getState: () => state,
    getFixture: () => fixture,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    fileById,
    folderById,
    changeById,
    changeForFile,
    groupById,
    groupForChange,
    duplicateGroupById,
    breadcrumb,
    folderPath,
    filePath,
    plannedPath,
    plannedFileName,
    includedChanges,
    unresolvedReviews,
    unresolvedConflicts,
    activeGroups,
    planSummary,

    beginWorkflow(target) {
      if (!['organization', 'duplicates'].includes(target)) return { status: "invalid" };
      const active = state.workflow.active;
      if (active === target) return { status: "resumed", target };
      if (active === "applying" || active === "verifying") return { status: "blocked", active, target };
      if (active !== "none") return { status: "switch-required", active, target };
      mutate(`Started ${target} workflow`, (draft) => {
        draft.workflow.active = target;
        draft.workflow.baseline = target === "organization" ? clone(draft.plan) : clone(draft.duplicates);
        draft.workflow.feedback = target === "organization" ? "Organization plan opened." : "Duplicate review opened.";
      });
      return { status: "started", target };
    },

    discardAndSwitch(target) {
      const previous = state.workflow.active;
      mutate(`Discarded ${previous} edits and opened ${target}`, (draft) => {
        if (previous === "organization" && draft.workflow.baseline) {
          draft.plan = clone(draft.workflow.baseline);
          draft.planUndo.snapshots = [];
        }
        if (previous === "duplicates" && draft.workflow.baseline) draft.duplicates = clone(draft.workflow.baseline);
        draft.workflow.active = target;
        draft.workflow.baseline = target === "organization" ? clone(draft.plan) : clone(draft.duplicates);
        draft.workflow.feedback = previous === "organization" ? "Organization edits discarded. Duplicate review opened." : "Duplicate choices discarded. Organization plan opened.";
        draft.selection.planChangeIds = [];
        draft.selection.duplicateExtraFileIds = [];
        draft.selection.duplicateInspectorOpen = false;
        draft.selection.inspectorItemId = null;
      });
    },

    leaveWorkflowActive() {
      mutate("Kept active workflow for later", (draft) => {
        draft.workflow.feedback = draft.workflow.active === "organization" ? "Organization plan remains in progress." : "Duplicate review remains in progress.";
      });
    },

    setNarrowPane(pane) {
      if (!['current', 'planned'].includes(pane)) return;
      mutate("Changed comparison pane", (draft) => {
        draft.selection.narrowPane = pane;
      });
    },

    toggleInspector(open = !state.selection.inspectorOpen) {
      mutate("Changed inspector visibility", (draft) => {
        draft.selection.inspectorOpen = Boolean(open);
      });
    },

    togglePlanGroup(groupId) {
      mutate("Changed planned folder disclosure", (draft) => {
        const expanded = new Set(draft.plan.expandedGroupIds);
        if (expanded.has(groupId)) expanded.delete(groupId);
        else expanded.add(groupId);
        draft.plan.expandedGroupIds = [...expanded];
      });
    },

    selectPlanChange(changeId, { checked, additive = true } = {}) {
      const change = changeById(changeId);
      if (!change || change.included === false) return;
      mutate("Changed plan selection", (draft) => {
        const selection = new Set(additive ? draft.selection.planChangeIds : []);
        const shouldSelect = checked ?? !selection.has(changeId);
        if (shouldSelect) selection.add(changeId);
        else selection.delete(changeId);
        draft.selection.planChangeIds = [...selection];
        draft.selection.inspectorItemId = changeId;
      });
    },

    focusPlanChange(changeId) {
      if (!changeById(changeId)) return;
      mutate("Focused a plan item", (draft) => {
        draft.selection.inspectorItemId = changeId;
        draft.selection.inspectorOpen = true;
      });
    },

    focusPlanGroup(groupId) {
      if (!groupById(groupId)) return;
      mutate("Focused a planned folder", (draft) => {
        draft.selection.inspectorItemId = groupId;
        draft.selection.inspectorOpen = true;
      });
    },

    selectVisiblePlanChanges(changeIds) {
      const valid = changeIds.filter((id) => changeById(id)?.included !== false);
      mutate("Selected visible plan items", (draft) => {
        draft.selection.planChangeIds = valid;
        draft.selection.inspectorItemId = valid[0] || null;
      });
    },

    clearPlanSelection() {
      mutate("Cleared plan selection", (draft) => {
        draft.selection.planChangeIds = [];
      });
    },

    selectedPlanChanges() {
      const selected = new Set(state.selection.planChangeIds);
      return includedChanges().filter((change) => selected.has(change.id));
    },

    moveSelected(destinationFolderId) {
      const selected = api.selectedPlanChanges();
      if (!selected.length || !folderById(destinationFolderId)) return false;
      const unresolvedReviewWithoutDestinationChange = selected.some((change) =>
        change.destinationFolderId === destinationFolderId &&
        state.plan.reviewItems.some((item) => item.changeId === change.id && item.status === "unresolved"),
      );
      if (unresolvedReviewWithoutDestinationChange) return false;
      if (selectedMoveWouldCollide(selected, destinationFolderId)) return false;
      pushPlanUndo(`Move ${selected.length} planned ${selected.length === 1 ? "file" : "files"}`);
      mutate("Moved selected files within the plan", (draft) => {
        selected.forEach((selectedChange) => {
          const change = draft.plan.changes.find((item) => item.id === selectedChange.id);
          const destinationChanged = change.destinationFolderId !== destinationFolderId;
          const linkedIssue = draft.plan.reviewItems.some((item) => item.changeId === change.id)
            || draft.plan.conflicts.some((item) => item.changeId === change.id);
          change.destinationFolderId = destinationFolderId;
          change.status = "Ready";
          if (destinationChanged && linkedIssue && change.plannedName) change.plannedName = null;
          const targetGroup = draft.plan.groups.find((group) => group.destinationFolderId === destinationFolderId);
          draft.plan.groups.forEach((group) => {
            group.changeIds = group.changeIds.filter((id) => id !== change.id);
          });
          if (targetGroup && !targetGroup.changeIds.includes(change.id)) targetGroup.changeIds.push(change.id);
          draft.plan.reviewItems.forEach((item) => {
            if (item.changeId === change.id) {
              item.status = "resolved";
              item.decision = "changed-destination";
            }
          });
          draft.plan.conflicts.forEach((item) => {
            if (item.changeId === change.id) {
              item.status = "resolved";
              item.decision = "changed-destination";
            }
          });
        });
        setPlanFeedback(draft, `Plan updated. ${selected.length} ${selected.length === 1 ? "file has" : "files have"} a new planned destination. No files moved.`, selected.map((change) => change.id));
      });
      return true;
    },

    leaveSelected() {
      const selected = api.selectedPlanChanges();
      if (!selected.length) return false;
      pushPlanUndo(`Leave ${selected.length} planned ${selected.length === 1 ? "file" : "files"} unchanged`);
      mutate("Left selected files unchanged", (draft) => {
        selected.forEach((selectedChange) => {
          const change = draft.plan.changes.find((item) => item.id === selectedChange.id);
          change.included = false;
          resolveIssuesForExcludedChange(draft, change.id);
        });
        if (selected.some((change) => change.id === draft.selection.inspectorItemId)) {
          draft.selection.inspectorItemId = null;
          draft.selection.inspectorOpen = false;
        }
        draft.selection.planChangeIds = [];
        setPlanFeedback(draft, `Plan updated. ${selected.length} ${selected.length === 1 ? "file remains" : "files remain"} in the current location. No files moved.`, selected.map((change) => change.id));
      });
      return true;
    },

    removeSelected() {
      return api.leaveSelected();
    },

    removeGroup(groupId) {
      const group = groupById(groupId);
      if (!group || group.removed) return 0;
      const affected = group.changeIds.map(changeById).filter((change) => change?.included !== false);
      pushPlanUndo(`Remove ${group.label} from plan`);
      mutate("Removed a proposed folder from the plan", (draft) => {
        const draftGroup = draft.plan.groups.find((item) => item.id === groupId);
        draftGroup.removed = true;
        affected.forEach((item) => {
          const change = draft.plan.changes.find((candidate) => candidate.id === item.id);
          change.included = false;
          resolveIssuesForExcludedChange(draft, change.id);
        });
        draft.selection.planChangeIds = [];
        draft.selection.inspectorItemId = null;
        setPlanFeedback(draft, `Plan updated. ${affected.length} files remain in their current locations. No files moved.`, affected.map((item) => item.id));
      });
      return affected.length;
    },

    undoPlanEdit() {
      const snapshot = state.planUndo.snapshots.at(-1);
      if (!snapshot) return false;
      mutate("Undid the latest plan edit", (draft) => {
        draft.plan = clone(snapshot.plan);
        draft.planUndo.snapshots.pop();
        draft.workflow.feedback = `Undid: ${snapshot.label}. No files moved.`;
      });
      return true;
    },

    acceptReview(reviewId) {
      const review = state.plan.reviewItems.find((item) => item.id === reviewId);
      if (!review || review.status !== "unresolved") return false;
      pushPlanUndo("Accepted a review suggestion");
      mutate("Accepted a review suggestion", (draft) => {
        const item = draft.plan.reviewItems.find((candidate) => candidate.id === reviewId);
        item.status = "resolved";
        item.decision = "accepted";
        const change = draft.plan.changes.find((candidate) => candidate.id === item.changeId);
        change.status = "Ready";
        setPlanFeedback(draft, "Review accepted. Plan updated. No files moved.", [change.id]);
      });
      return true;
    },

    resolveConflict(conflictId, decision = "keep-both-with-rename") {
      const conflict = state.plan.conflicts.find((item) => item.id === conflictId);
      if (!conflict || conflict.status !== "unresolved") return false;
      pushPlanUndo("Resolved a destination conflict");
      mutate("Resolved a destination conflict", (draft) => {
        const item = draft.plan.conflicts.find((candidate) => candidate.id === conflictId);
        item.status = "resolved";
        item.decision = decision;
        const change = draft.plan.changes.find((candidate) => candidate.id === item.changeId);
        change.status = "Ready";
        if (decision === "keep-both-with-rename") change.plannedName = "Notes from Downloads.docx";
        setPlanFeedback(draft, "Conflict resolved. Plan updated. No files moved.", [change.id]);
      });
      return true;
    },

    showPlanIssues(kind = "all") {
      mutate("Focused plan issues", (draft) => {
        draft.plan.issueFilter = kind;
        const issue = kind === "review" ? api.unresolvedReviews()[0] : kind === "conflict" ? api.unresolvedConflicts()[0] : [...api.unresolvedReviews(), ...api.unresolvedConflicts()][0];
        draft.selection.inspectorItemId = issue?.changeId || null;
        draft.selection.inspectorOpen = true;
        draft.selection.narrowPane = "planned";
        draft.workflow.feedback = kind === "review" ? "Showing files that need review." : kind === "conflict" ? "Showing the blocking conflict." : "Showing unresolved plan items.";
      });
    },

    clearIssueFilter() {
      mutate("Showed every planned item", (draft) => {
        draft.plan.issueFilter = "all";
      });
    },

    discardPlan() {
      mutate("Discarded the organization plan", (draft) => {
        draft.plan.status = "dismissed";
        draft.workflow.active = "none";
        draft.workflow.baseline = null;
        draft.workflow.feedback = "Organization plan discarded. No files moved.";
        draft.selection.planChangeIds = [];
      });
    },

    preparePlan() {
      const prepared = freshPlanFromCanonical();
      mutate("Prepared a fresh sample plan", (draft) => {
        draft.plan = prepared;
        draft.planUndo.snapshots = [];
        if (draft.stagedOperation?.status === "working") draft.stagedOperation.nextWorkflow = prepared.changes.length ? "organization" : "none";
        draft.workflow.feedback = prepared.changes.length
          ? `Organization plan ready with ${prepared.changes.length} proposed ${prepared.changes.length === 1 ? "change" : "changes"}. No files moved.`
          : "Sample Downloads is already consistent with the latest approved organization intent. No new changes were proposed.";
      });
      return planSummary();
    },

    setOperation(definition, stageId = null, nextWorkflow = "none") {
      if (["applying", "verifying"].includes(state.workflow.active) || state.stagedOperation?.status === "working") {
        throw new Error("Another sample operation is already in progress.");
      }
      mutate("Started a staged operation", (draft) => {
        const returnWorkflow = ["organization", "duplicates"].includes(draft.workflow.active) ? draft.workflow.active : "none";
        draft.stagedOperation = {
          definition,
          activeStageId: stageId,
          status: "working",
          message: "",
          returnWorkflow,
          returnBaseline: draft.workflow.baseline == null ? null : clone(draft.workflow.baseline),
          nextWorkflow,
        };
        draft.workflow.active = "applying";
      });
    },

    advanceOperation(stageId, message = "") {
      mutate("Advanced a staged operation", (draft) => {
        draft.stagedOperation.activeStageId = stageId;
        draft.stagedOperation.message = message;
        if (stageId === "applying" || stageId === "consolidating") draft.workflow.active = "applying";
        if (stageId === "verifying") draft.workflow.active = "verifying";
      });
    },

    finishOperation(message = "", nextWorkflow = "none") {
      mutate("Finished a staged operation", (draft) => {
        const operation = draft.stagedOperation || {};
        const stopped = message === "Operation stopped";
        const returnWorkflow = ["organization", "duplicates", "none"].includes(operation.returnWorkflow)
          ? operation.returnWorkflow
          : "none";
        const targetWorkflow = stopped ? returnWorkflow : nextWorkflow;
        draft.stagedOperation.status = "complete";
        draft.stagedOperation.message = message;
        draft.workflow.active = targetWorkflow;
        if (["organization", "duplicates"].includes(targetWorkflow)) {
          const preserveCapturedBaseline = stopped || operation.definition === "freshnessCheck";
          const canRestoreCapturedBaseline = preserveCapturedBaseline && operation.returnWorkflow === targetWorkflow && Object.hasOwn(operation, "returnBaseline");
          const capturedBaseline = canRestoreCapturedBaseline ? operation.returnBaseline : null;
          draft.workflow.baseline = capturedBaseline == null
            ? clone(targetWorkflow === "organization" ? draft.plan : draft.duplicates)
            : clone(capturedBaseline);
        } else {
          draft.workflow.baseline = null;
        }
      });
    },

    clearOperation() {
      mutate("Cleared staged operation", (draft) => {
        draft.stagedOperation = clone(fixture.initialState.stagedOperation);
      });
    },

    applyPlan(outcome = state.settings.advanced.demoOutcome) {
      const summary = planSummary();
      if (!summary.canApply) throw new Error("This plan still has unresolved items.");
      const changes = clone(includedChanges());
      let applyCount = changes.length;
      if (outcome === "partial") applyCount = Math.max(1, Math.floor(changes.length * 0.7));
      if (outcome === "failed-before-changes") applyCount = 0;
      const appliedChanges = changes.slice(0, applyCount);
      const reversibleChanges = appliedChanges.map(activityFileChange);
      let status = "Applied and verified";
      let verification = "Every completed sample change matched the approved plan.";
      if (outcome === "partial") {
        status = "Partially completed";
        verification = `${applyCount} of ${changes.length} sample changes were verified; the remainder stayed in their current locations.`;
      } else if (outcome === "failed-before-changes") {
        status = "Failed with no changes";
        verification = "The recheck stopped the sample operation before any file changed.";
      } else if (outcome === "verification-incomplete") {
        status = "Verification needed";
        verification = "The sample changes completed, but the final verification did not finish.";
      }

      mutate("Recorded organization outcome", (draft) => {
        appliedChanges.forEach((applied) => {
          const file = draft.files.find((item) => item.id === applied.fileId);
          file.folderId = applied.destinationFolderId;
          if (applied.plannedName) file.name = applied.plannedName;
          file.modifiedAt = now();
        });
        draft.plan.status = outcome === "verified" ? "applied" : outcome === "failed-before-changes" ? "ready" : "attention";
        draft.activity.unshift({
          id: `activity-organization-${Date.now()}`,
          workflow: "organization",
          status,
          title: "Organized Downloads",
          occurredAt: now(),
          summary: outcome === "failed-before-changes" ? "No sample files changed." : `${applyCount} sample changes completed.`,
          fileIds: appliedChanges.map((change) => change.fileId),
          sourcePlanId: draft.plan.id,
          verification,
          reversibleChanges,
          undo: {
            status: outcome === "verified" ? "available" : outcome === "failed-before-changes" ? "not-needed" : "limited",
            scope: outcome === "verified" ? "full-operation" : outcome === "failed-before-changes" ? "none" : "verified-changes",
            label: outcome === "verified" ? "Undo available" : outcome === "failed-before-changes" ? "No undo needed" : "Review recovery options",
            detail: outcome === "verified" ? "This recent clean sample operation can be reversed." : verification,
          },
        });
        draft.expandedActivityIds = [draft.activity[0].id];
        draft.selection.planChangeIds = [];
        if (applyCount > 0) draft.planUndo.snapshots = [];
        draft.workflow.feedback = status;
      });
      return state.activity[0];
    },

    selectDuplicateGroup(groupId) {
      const group = duplicateGroupById(groupId);
      if (!group) return;
      mutate("Selected an exact-copy group", (draft) => {
        draft.selection.duplicateGroupId = groupId;
        draft.selection.inspectorItemId = groupId;
        draft.selection.duplicateExtraFileIds = [...group.selectedExtraFileIds];
        draft.selection.inspectorOpen = true;
        draft.selection.duplicateInspectorOpen = false;
      });
    },

    focusDuplicateFile(fileId) {
      if (!fileById(fileId)) return;
      mutate("Focused an exact-copy file", (draft) => {
        draft.selection.inspectorItemId = fileId;
        draft.selection.inspectorOpen = true;
        draft.selection.duplicateInspectorOpen = true;
      });
    },

    toggleDuplicateInspector(open = !state.selection.duplicateInspectorOpen) {
      mutate("Changed duplicate inspector visibility", (draft) => {
        draft.selection.duplicateInspectorOpen = Boolean(open);
      });
    },

    chooseWorkingCopy(groupId, fileId) {
      const group = duplicateGroupById(groupId);
      if (!group || !duplicateGroupIsActive(groupId) || group.status === "Complete" || !group.memberFileIds.includes(fileId)) return false;
      mutate("Chose a duplicate working copy", (draft) => {
        const item = draft.duplicates.groups.find((candidate) => candidate.id === groupId);
        item.workingCopyFileId = fileId;
        item.status = "Ready";
        item.recommendation = "chosen";
        item.selectedExtraFileIds = item.memberFileIds.filter((id) => id !== fileId);
        draft.selection.duplicateExtraFileIds = [...item.selectedExtraFileIds];
        draft.selection.inspectorItemId = fileId;
        draft.duplicates.feedback = "Working copy chosen. Review the selected extra copies.";
      });
      return true;
    },

    toggleDuplicateExtra(fileId, checked) {
      const group = duplicateGroupById(state.selection.duplicateGroupId);
      if (!group || !duplicateGroupIsActive(group.id) || group.status === "Complete" || !group.memberFileIds.includes(fileId) || fileId === group.workingCopyFileId) return false;
      mutate("Changed selected duplicate extras", (draft) => {
        const selected = new Set(draft.selection.duplicateExtraFileIds);
        if (checked ?? !selected.has(fileId)) selected.add(fileId);
        else selected.delete(fileId);
        draft.selection.duplicateExtraFileIds = [...selected];
        const item = draft.duplicates.groups.find((candidate) => candidate.id === group.id);
        item.selectedExtraFileIds = [...selected];
        draft.selection.inspectorItemId = fileId;
        draft.duplicates.feedback = `${selected.size} extra ${selected.size === 1 ? "copy" : "copies"} selected.`;
      });
      return true;
    },

    selectDuplicateExtras(groupId) {
      const group = duplicateGroupById(groupId);
      if (!group || !duplicateGroupIsActive(groupId) || group.status === "Complete" || !group.workingCopyFileId) return false;
      mutate("Selected every duplicate extra", (draft) => {
        const selectedExtraFileIds = group.memberFileIds.filter((id) => id !== group.workingCopyFileId);
        draft.selection.duplicateGroupId = groupId;
        draft.selection.duplicateExtraFileIds = selectedExtraFileIds;
        const item = draft.duplicates.groups.find((candidate) => candidate.id === groupId);
        item.selectedExtraFileIds = [...selectedExtraFileIds];
        draft.duplicates.feedback = "Extra copies selected.";
      });
      return true;
    },

    keepAllDuplicates(groupId) {
      const group = duplicateGroupById(groupId);
      if (!group || !duplicateGroupIsActive(groupId) || group.status === "Complete") return false;
      mutate("Kept all exact copies", (draft) => {
        const item = draft.duplicates.groups.find((candidate) => candidate.id === groupId);
        item.decision = "keep-all";
        item.status = "Complete";
        item.selectedExtraFileIds = [];
        selectNextDuplicateGroup(draft, groupId);
        draft.duplicates.feedback = "All copies will stay where they are.";
      });
      return true;
    },

    finishDuplicateReview() {
      if (state.duplicates.activeGroupIds.length || state.workflow.active !== "duplicates") return false;
      mutate("Completed duplicate review", (draft) => {
        draft.workflow.active = "none";
        draft.workflow.baseline = null;
        draft.duplicates.feedback = "Duplicate review complete. Every exact-copy group has an explicit decision.";
      });
      return true;
    },

    finishWorkflowIfEmpty() {
      const active = state.workflow.active;
      const hasWork = active === "organization"
        ? state.plan.status === "ready"
        : active === "duplicates"
          ? state.duplicates.activeGroupIds.length > 0
          : true;
      if (!['organization', 'duplicates'].includes(active) || hasWork) return false;
      mutate("Closed an empty workflow", (draft) => {
        draft.workflow.active = "none";
        draft.workflow.baseline = null;
        draft.workflow.feedback = "No mutable work is waiting in this destination.";
      });
      return true;
    },

    reconfirmDuplicateRecheck(recheckId) {
      const recheck = state.duplicates.rechecks.find((item) => item.id === recheckId);
      if (!recheck || !recheck.requiresReconfirmation) return false;
      mutate("Reconfirmed an edited working copy", (draft) => {
        const item = draft.duplicates.rechecks.find((candidate) => candidate.id === recheckId);
        item.status = "Ready";
        item.confirmed = true;
        item.requiresReconfirmation = false;
        draft.duplicates.feedback = "Working copy reconfirmed. The unchanged older copies may be reviewed separately.";
      });
      return true;
    },

    consolidateDuplicates(groupId) {
      const group = duplicateGroupById(groupId);
      if (!group || !duplicateGroupIsActive(groupId) || group.status === "Complete") {
        throw new Error("This exact-copy group is already complete.");
      }
      const selected = state.selection.duplicateExtraFileIds.filter((id) => group?.memberFileIds.includes(id) && id !== group.workingCopyFileId);
      if (!group?.workingCopyFileId || !selected.length) throw new Error("Choose a working copy and at least one extra copy first.");
      const before = selected.map((fileId) => ({ fileId, beforeFolderId: fileById(fileId).folderId }));
      mutate("Consolidated selected extra copies", (draft) => {
        selected.forEach((fileId) => {
          const file = draft.files.find((item) => item.id === fileId);
          file.folderId = draft.duplicates.holdingAreaFolderId;
          file.inHoldingArea = true;
        });
        const item = draft.duplicates.groups.find((candidate) => candidate.id === groupId);
        item.status = "Complete";
        item.decision = "consolidated";
        item.heldFileIds = [...selected];
        item.selectedExtraFileIds = [];
        draft.activity.unshift({
          id: `activity-duplicate-${Date.now()}`,
          workflow: "duplicates",
          status: "Applied and verified",
          title: "Consolidated duplicate copies",
          occurredAt: now(),
          summary: `${selected.length} extra ${selected.length === 1 ? "copy" : "copies"} moved to the reversible holding area and verified.`,
          fileIds: [group.workingCopyFileId, ...selected],
          duplicateGroupId: group.id,
          verification: "The selected copies were rechecked before the sample operation completed.",
          reversibleChanges: before,
          undo: { status: "available", scope: "return-copy", label: "Return this copy", detail: "A narrow return is available for an unaffected extra copy." },
        });
        draft.expandedActivityIds = [draft.activity[0].id];
        selectNextDuplicateGroup(draft, groupId);
        draft.duplicates.feedback = "Consolidation applied and verified. Files were not deleted.";
      });
      return state.activity[0];
    },

    toggleActivity(activityId) {
      mutate("Changed Activity disclosure", (draft) => {
        const expanded = new Set(draft.expandedActivityIds);
        if (expanded.has(activityId)) expanded.delete(activityId);
        else expanded.add(activityId);
        draft.expandedActivityIds = [...expanded];
      });
    },

    undoActivity(activityId) {
      const activity = state.activity.find((item) => item.id === activityId);
      if (!activity || activity.undo?.status !== "available") return false;
      mutate("Used the short-term undo failsafe", (draft) => {
        const item = draft.activity.find((candidate) => candidate.id === activityId);
        [...(item.reversibleChanges || [])].reverse().forEach((change) => {
          const file = draft.files.find((candidate) => candidate.id === change.fileId);
          if (!file) return;
          if (change.beforeFolderId) file.folderId = change.beforeFolderId;
          if (change.beforeName) file.name = change.beforeName;
          file.inHoldingArea = false;
        });
        item.status = "Undone and verified";
        item.verification = "The reversible sample changes were returned and checked.";
        item.undo = { status: "used", scope: "none", label: "Already undone", detail: "The short-term failsafe has been used." };
        if (item.sourcePlanId && draft.plan.id === item.sourcePlanId) {
          draft.plan.status = "undone";
          draft.planUndo.snapshots = [];
          draft.selection.planChangeIds = [];
          draft.workflow.feedback = "The applied organization plan was undone and verified. Prepare a fresh plan before applying again.";
        }
      });
      return true;
    },

    returnDuplicateCopy(activityId) {
      const activity = state.activity.find((item) => item.id === activityId);
      const change = activity?.reversibleChanges?.[0];
      if (!activity || activity.undo?.scope !== "return-copy" || !change) return false;
      mutate("Returned one duplicate copy", (draft) => {
        const file = draft.files.find((item) => item.id === change.fileId);
        if (file) {
          file.folderId = change.beforeFolderId;
          file.inHoldingArea = false;
        }
        const item = draft.activity.find((candidate) => candidate.id === activityId);
        item.status = "Undone and verified";
        item.summary = "One selected sample copy returned from the holding area.";
        item.verification = "The selected sample copy returned and was checked. Other consolidation choices, if any, remain unchanged.";
        item.undo = { status: "used", scope: "none", label: "Copy returned", detail: "The narrow return has been used." };
        if (item.duplicateGroupId) {
          const group = draft.duplicates.groups.find((candidate) => candidate.id === item.duplicateGroupId);
          if (group) {
            group.heldFileIds = (group.heldFileIds || []).filter((fileId) => fileId !== change.fileId);
            group.selectedExtraFileIds = (group.selectedExtraFileIds || []).filter((fileId) => fileId !== change.fileId);
            group.status = "Returned";
            group.decision = group.heldFileIds.length ? "partially-returned" : "returned";
          }
          draft.duplicates.feedback = "One consolidated copy returned and was verified. The completed group record was updated.";
        }
      });
      return true;
    },

    updateSetting(path, value) {
      mutate("Updated a prototype setting", (draft) => {
        setNested(draft.settings, path, value);
      });
      return getNested(state.settings, path);
    },

    toggleRule(ruleId) {
      mutate("Changed a locked rule", (draft) => {
        const rule = draft.settings.rules.items.find((item) => item.id === ruleId);
        if (rule) rule.active = !rule.active;
      });
    },

    updateRule(ruleId, updates) {
      mutate("Updated a locked rule", (draft) => {
        const rule = draft.settings.rules.items.find((item) => item.id === ruleId);
        if (rule) Object.assign(rule, updates);
      });
    },

    removeRule(ruleId) {
      mutate("Removed a locked rule", (draft) => {
        draft.settings.rules.items = draft.settings.rules.items.filter((item) => item.id !== ruleId);
      });
    },

    reset() {
      if (storage) storage.removeItem(STORAGE_KEY);
      state = initialState(fixture);
      emit();
    },
  };

  persist();
  return api;
}

export { STORAGE_KEY };
