const STORAGE_KEY = "loci-flow:ux-1-1-pass-1:v1";
const MAX_PLAN_HISTORY = 12;

const clone = (value) => JSON.parse(JSON.stringify(value));

function safeStorage() {
  try {
    const probe = `${STORAGE_KEY}:probe`;
    window.localStorage.setItem(probe, "1");
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch (_error) {
    return null;
  }
}

function createInitialState(fixture) {
  return {
    stateVersion: fixture.fixtureVersion,
    files: clone(fixture.files),
    folders: clone(fixture.folders),
    onboarding: clone(fixture.initialState.onboarding),
    location: clone(fixture.initialState.location),
    plan: clone(fixture.initialState.plan),
    duplicates: clone(fixture.initialState.duplicates),
    activity: clone(fixture.initialState.activity),
    settings: clone(fixture.initialState.settings),
    learnedHabits: clone(fixture.initialState.learnedHabits),
    demoOutcome: fixture.initialState.demoOutcome || "verified",
    planUndo: [],
    lastMutation: null,
  };
}

function isUsableState(candidate, fixtureVersion) {
  return Boolean(
    candidate &&
      candidate.stateVersion === fixtureVersion &&
      Array.isArray(candidate.files) &&
      Array.isArray(candidate.folders) &&
      Array.isArray(candidate.activity) &&
      candidate.plan &&
      candidate.duplicates &&
      candidate.settings,
  );
}

function normalized(value) {
  return String(value || "")
    .normalize("NFKD")
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function splitExtension(filename) {
  const index = filename.lastIndexOf(".");
  if (index <= 0 || index === filename.length - 1) {
    return { stem: filename, extension: "" };
  }
  return { stem: filename.slice(0, index), extension: filename.slice(index) };
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

export function createStore(fixture) {
  const storage = safeStorage();
  let state = createInitialState(fixture);
  const listeners = new Set();

  if (storage) {
    try {
      const persisted = JSON.parse(storage.getItem(STORAGE_KEY) || "null");
      if (isUsableState(persisted, fixture.fixtureVersion)) state = persisted;
    } catch (_error) {
      storage.removeItem(STORAGE_KEY);
    }
  }

  const persist = () => {
    if (!storage) return;
    try {
      storage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (_error) {
      // The prototype remains usable in memory when browser storage is full.
    }
  };

  const emit = () => listeners.forEach((listener) => listener(state));

  const mutate = (label, operation, { planEdit = false } = {}) => {
    if (planEdit && state.plan) {
      state.planUndo ||= [];
      state.planUndo.push(clone(state.plan));
      if (state.planUndo.length > MAX_PLAN_HISTORY) state.planUndo.shift();
    }
    operation(state);
    state.lastMutation = { label, at: new Date().toISOString() };
    persist();
    emit();
  };

  const fileById = (fileId) => state.files.find((file) => file.id === fileId);
  const folderById = (folderId) => state.folders.find((folder) => folder.id === folderId);
  const groupById = (groupId) => state.plan?.groups?.find((group) => group.id === groupId);

  const breadcrumb = (folderId) => {
    const parts = [];
    const seen = new Set();
    let folder = folderById(folderId);
    while (folder && !seen.has(folder.id)) {
      seen.add(folder.id);
      parts.unshift(folder);
      folder = folder.parentId ? folderById(folder.parentId) : null;
    }
    return parts;
  };

  const filePath = (fileId, { exact = false } = {}) => {
    const file = fileById(fileId);
    if (!file) return "Unknown file";
    const parts = breadcrumb(file.folderId).map((folder) => folder.name);
    parts.push(file.name);
    return exact ? `/${parts.join("/")}` : parts.join(" › ");
  };

  const planChangeForFile = (fileId) =>
    state.plan?.changes?.find((change) => change.fileId === fileId && change.status !== "excluded");

  const isGroupIncluded = (groupId) => groupById(groupId)?.included !== false;

  const actionablePlanChanges = () => {
    if (!state.plan || state.plan.status !== "active") return [];
    return state.plan.changes.filter(
      (change) =>
        change.status !== "excluded" &&
        change.status !== "satisfied_directly" &&
        isGroupIncluded(change.groupId),
    );
  };

  const planSummary = () => {
    if (!state.plan || state.plan.status !== "active") {
      return { files: 0, destinations: 0, reviewRequired: 0, conflicts: 0, canApply: false };
    }
    const changes = actionablePlanChanges();
    const destinations = new Set(changes.map((change) => change.destinationFolderId));
    const reviewRequired = state.plan.exceptions.filter((item) => item.status === "unresolved").length;
    const conflicts = state.plan.conflicts.filter((item) => item.status === "unresolved").length;
    return {
      files: changes.length,
      destinations: destinations.size,
      reviewRequired,
      conflicts,
      canApply: reviewRequired === 0 && conflicts === 0 && changes.length > 0,
    };
  };

  const reconcilePlanFile = (fileId, { ambiguous = false, reason = "A direct action changed this file." } = {}) => {
    if (!state.plan || state.plan.status !== "active") return;
    const file = fileById(fileId);
    const change = planChangeForFile(fileId);
    if (!file || !change) return;

    if (ambiguous) {
      const group = groupById(change.groupId);
      if (group) {
        group.needsReview = true;
        group.reviewReason = reason;
      }
      state.plan.freshness = "needs_review";
      return;
    }

    if (change.suggestedName && normalized(change.suggestedName) === normalized(file.name)) {
      change.suggestedName = null;
      change.renameDecision = "completed_directly";
    }
    if (change.destinationFolderId === file.folderId) change.moveCompletedDirectly = true;
    if (!change.suggestedName && change.moveCompletedDirectly) {
      change.status = "satisfied_directly";
      change.annotation = "Completed directly";
    } else if (change.moveCompletedDirectly) {
      change.kind = "rename";
      change.annotation = "Rename planned";
    } else if (!change.suggestedName) {
      change.kind = "move";
      change.annotation = "Moving here";
    }
    state.plan.freshness = "updated_automatically";
  };

  const addActivity = (operation) => {
    state.activity.unshift({
      id: `activity-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      occurredAt: new Date().toISOString(),
      ...operation,
    });
  };

  const normalizeRequestedFilename = (file, requestedName) => {
    const trimmed = String(requestedName || "").trim();
    if (!trimmed) throw new Error("Enter a new filename.");
    if (/[\\/\u0000-\u001f]/.test(trimmed) || trimmed === "." || trimmed === "..") {
      throw new Error("Use a filename without slashes or control characters.");
    }
    const current = splitExtension(file.name);
    const requested = splitExtension(trimmed);
    const newStem = requested.extension ? requested.stem : trimmed;
    if (!newStem.trim()) throw new Error("Enter a filename before the extension.");
    return `${newStem.trim()}${current.extension}`;
  };

  const api = {
    getState: () => state,
    getFixture: () => fixture,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    fileById,
    folderById,
    groupById,
    breadcrumb,
    filePath,
    planChangeForFile,
    planSummary,
    filesInFolder(folderId) {
      return state.files.filter((file) => file.folderId === folderId && !file.inHoldingArea);
    },
    childFolders(folderId) {
      return state.folders.filter((folder) => folder.parentId === folderId && !folder.hidden);
    },
    nearbyFiles(fileId) {
      const file = fileById(fileId);
      return file ? state.files.filter((candidate) => candidate.folderId === file.folderId && candidate.id !== fileId).slice(0, 6) : [];
    },
    relatedFiles(fileId) {
      const file = fileById(fileId);
      return (file?.related || [])
        .map((relationship) => ({ ...relationship, file: fileById(relationship.fileId) }))
        .filter((relationship) => relationship.file);
    },
    completeOnboarding() {
      mutate("Completed onboarding", (draft) => {
        draft.onboarding.completed = true;
        draft.onboarding.step = 2;
      });
    },
    setOnboardingStep(step) {
      mutate("Changed onboarding step", (draft) => {
        draft.onboarding.step = Math.max(0, Math.min(1, Number(step) || 0));
      });
    },
    search(query, refinements = {}) {
      const queryKey = normalized(query);
      if (!queryKey) return [];
      const scenario = fixture.searchScenarios.find((item) => normalized(item.query) === queryKey);
      let results;
      if (scenario) {
        results = scenario.results.map((result) => ({ ...result, file: fileById(result.fileId) })).filter((result) => result.file);
      } else {
        const tokens = queryKey.split(" ").filter(Boolean);
        results = state.files
          .map((file) => {
            const filename = normalized(file.name);
            const path = normalized(filePath(file.id));
            const keywords = normalized((file.keywords || []).join(" "));
            const exact = filename === queryKey;
            const phrase = filename.includes(queryKey);
            const filenameMatches = tokens.filter((token) => filename.includes(token)).length;
            const contextMatches = tokens.filter((token) => `${path} ${keywords}`.includes(token)).length;
            if (!exact && !phrase && filenameMatches === 0 && contextMatches === 0) return null;
            let band = "Weak match";
            let explanation = "A folder or collection label is similar to this query.";
            let rank = contextMatches;
            if (exact) {
              band = "Strong match";
              explanation = "Exact filename match.";
              rank = 100;
            } else if (phrase || filenameMatches === tokens.length) {
              band = "Strong match";
              explanation = "Filename phrase and tokens match.";
              rank = 80 + filenameMatches;
            } else if (filenameMatches > 0) {
              band = "Possible match";
              explanation = "Some filename tokens match.";
              rank = 40 + filenameMatches;
            }
            return { fileId: file.id, file, band, explanation, rank };
          })
          .filter(Boolean)
          .sort((left, right) => right.rank - left.rank || left.file.name.localeCompare(right.file.name));
      }

      return results.filter(({ file }) => {
        if (refinements.type && refinements.type !== "all" && file.type !== refinements.type) return false;
        if (refinements.location && refinements.location !== "all" && file.folderId !== refinements.location) return false;
        if (refinements.size === "small" && parseFloat(file.sizeLabel) >= 5) return false;
        if (refinements.size === "large" && parseFloat(file.sizeLabel) < 5) return false;
        if (refinements.date === "recent" && new Date(file.modifiedAt) < new Date("2026-06-01")) return false;
        if (refinements.date === "older" && new Date(file.modifiedAt) >= new Date("2026-06-01")) return false;
        return true;
      });
    },
    validateRename(fileId, requestedName) {
      const file = fileById(fileId);
      if (!file) throw new Error("That sample file is no longer available.");
      const name = normalizeRequestedFilename(file, requestedName);
      const collision = state.files.find(
        (candidate) => candidate.id !== fileId && candidate.folderId === file.folderId && normalized(candidate.name) === normalized(name),
      );
      if (collision) throw new Error(`A file named “${name}” is already here.`);
      return { name, extensionPreserved: splitExtension(requestedName).extension !== splitExtension(file.name).extension };
    },
    renameFile(fileId, requestedName) {
      const validation = api.validateRename(fileId, requestedName);
      const file = fileById(fileId);
      const previousName = file.name;
      mutate("Renamed a sample file", () => {
        file.name = validation.name;
        file.modifiedAt = new Date().toISOString();
        reconcilePlanFile(fileId);
        addActivity({
          title: `Renamed ${previousName}`,
          kind: "rename",
          status: "Applied and verified",
          summary: `${previousName} is now ${file.name}.`,
          verification: "The sample filename was rechecked across the prototype.",
          undoAvailability: "limited",
          changes: [`${previousName} → ${file.name}`],
          reversibleChanges: [{ type: "rename", fileId, beforeName: previousName, afterName: file.name }],
        });
      });
      return validation;
    },
    validateMove(fileId, destinationFolderId) {
      const file = fileById(fileId);
      const destination = folderById(destinationFolderId);
      if (!file || !destination) throw new Error("That sample destination is unavailable.");
      if (file.folderId === destinationFolderId) return { sameLocation: true, file, destination };
      const collision = state.files.find(
        (candidate) => candidate.id !== fileId && candidate.folderId === destinationFolderId && normalized(candidate.name) === normalized(file.name),
      );
      return { sameLocation: false, file, destination, collision };
    },
    moveFile(fileId, destinationFolderId) {
      const validation = api.validateMove(fileId, destinationFolderId);
      if (validation.collision) throw new Error(`“${validation.collision.name}” already exists in that destination.`);
      if (validation.sameLocation) return validation;
      const previousFolderId = validation.file.folderId;
      const previousPath = filePath(fileId);
      mutate("Moved a sample file", () => {
        validation.file.folderId = destinationFolderId;
        validation.file.modifiedAt = new Date().toISOString();
        reconcilePlanFile(fileId);
        addActivity({
          title: `Moved ${validation.file.name}`,
          kind: "move",
          status: "Applied and verified",
          summary: `${validation.file.name} moved to ${filePath(fileId)}.`,
          verification: "The sample location was rechecked after the move.",
          undoAvailability: "limited",
          changes: [`${previousPath} → ${filePath(fileId)}`],
          reversibleChanges: [{ type: "move", fileId, beforeFolderId: previousFolderId, afterFolderId: destinationFolderId }],
        });
      });
      return validation;
    },
    addToPlan(fileId) {
      if (!state.plan || state.plan.status !== "active") api.regeneratePlan();
      if (state.plan.changes.some((change) => change.fileId === fileId && change.status !== "excluded")) return false;
      const file = fileById(fileId);
      const matchingGroup = state.plan.groups.find((group) => (file.keywords || []).some((keyword) => normalized(group.name).includes(normalized(keyword)))) || state.plan.groups[0];
      mutate("Added a file to the organization plan", (draft) => {
        draft.plan.changes.push({
          id: `change-added-${Date.now()}`,
          fileId,
          sourceFolderId: file.folderId,
          destinationFolderId: matchingGroup.destinationFolderId,
          groupId: matchingGroup.id,
          kind: "move",
          status: "planned",
          annotation: "Added to plan",
        });
        matchingGroup.fileIds ||= [];
        matchingGroup.fileIds.push(fileId);
        draft.plan.freshness = "needs_review";
        matchingGroup.needsReview = true;
        matchingGroup.reviewReason = "This file was added directly and needs a quick placement check.";
      }, { planEdit: true });
      return true;
    },
    resolveException(exceptionId, resolution, targetGroupId = null) {
      mutate("Resolved a review item", (draft) => {
        const exception = draft.plan.exceptions.find((item) => item.id === exceptionId);
        if (!exception) return;
        exception.status = "resolved";
        exception.resolution = resolution;
        if (targetGroupId) {
          const change = draft.plan.changes.find((item) => item.fileId === exception.fileId);
          const group = draft.plan.groups.find((item) => item.id === targetGroupId);
          if (change && group) {
            change.groupId = group.id;
            change.destinationFolderId = group.destinationFolderId;
            change.annotation = change.suggestedName ? "Moving + renamed" : "Moving here";
          }
        }
        const resolvedChange = draft.plan.changes.find((item) => item.fileId === exception.fileId);
        if (resolvedChange && resolution !== "leave_unchanged") {
          resolvedChange.annotation = resolvedChange.suggestedName ? "Moving + renamed" : "Moving here";
        }
        if (resolution === "leave_unchanged") {
          const change = draft.plan.changes.find((item) => item.fileId === exception.fileId);
          if (change) change.status = "excluded";
        }
      }, { planEdit: true });
    },
    resolveConflict(conflictId, resolution, value = null) {
      mutate("Resolved a blocking conflict", (draft) => {
        const conflict = draft.plan.conflicts.find((item) => item.id === conflictId);
        if (!conflict) return;
        const change = draft.plan.changes.find((item) => item.fileId === conflict.fileId);
        if (change && resolution === "rename") change.suggestedName = value || conflict.suggestedName || "Notes from Downloads.docx";
        if (change && resolution === "change_destination" && value) change.destinationFolderId = value;
        if (change && resolution === "leave_unchanged") change.status = "excluded";
        conflict.status = "resolved";
        conflict.resolution = resolution;
        if (change && resolution !== "leave_unchanged") {
          change.annotation = change.suggestedName ? "Moving + renamed" : "Moving here";
        }
      }, { planEdit: true });
    },
    toggleGroup(groupId) {
      mutate("Changed a plan group", () => {
        const group = groupById(groupId);
        if (group) group.included = group.included === false;
      }, { planEdit: true });
    },
    excludePlanFile(fileId) {
      mutate("Excluded a file from the plan", (draft) => {
        const change = draft.plan.changes.find((item) => item.fileId === fileId);
        if (change) {
          change.status = "excluded";
          change.annotation = "Excluded";
        }
      }, { planEdit: true });
    },
    changePlanDestination(fileId, folderId) {
      mutate("Changed a planned destination", (draft) => {
        const change = draft.plan.changes.find((item) => item.fileId === fileId);
        if (change && folderById(folderId)) {
          change.destinationFolderId = folderId;
          change.annotation = "Destination changed";
        }
      }, { planEdit: true });
    },
    movePlanFileToGroup(fileId, groupId) {
      const group = groupById(groupId);
      if (!group) return;
      mutate("Moved a file to another proposed group", (draft) => {
        const change = draft.plan.changes.find((item) => item.fileId === fileId);
        if (change) {
          change.groupId = groupId;
          change.destinationFolderId = group.destinationFolderId;
          change.annotation = "Group changed";
        }
      }, { planEdit: true });
    },
    renameProposedFolder(groupId, name) {
      const trimmed = String(name || "").trim();
      if (!trimmed || /[\\/]/.test(trimmed)) throw new Error("Enter a simple folder name without slashes.");
      mutate("Renamed a proposed folder", () => {
        const group = groupById(groupId);
        if (group) group.proposedName = trimmed;
      }, { planEdit: true });
    },
    decideSuggestedName(fileId, decision, editedName = null) {
      mutate("Changed a filename suggestion", (draft) => {
        const change = draft.plan.changes.find((item) => item.fileId === fileId);
        const file = fileById(fileId);
        if (!change || !file) return;
        if (decision === "reject") change.suggestedName = null;
        if (decision === "edit") change.suggestedName = normalizeRequestedFilename(file, editedName);
        change.renameDecision = decision;
      }, { planEdit: true });
    },
    undoPlanEdit() {
      if (!state.planUndo?.length) return false;
      const previous = state.planUndo.pop();
      mutate("Undid a planning edit", (draft) => {
        draft.plan = previous;
      });
      return true;
    },
    checkPlanFreshness() {
      mutate("Checked the organization plan", (draft) => {
        if (draft.plan.status === "active") draft.plan.freshness = "updated_automatically";
      });
    },
    setPlanFreshness(freshness) {
      mutate("Changed the plan freshness demo", (draft) => {
        if (draft.plan.status === "active") draft.plan.freshness = freshness;
      });
    },
    dismissPlan() {
      mutate("Dismissed the organization plan", (draft) => {
        draft.plan.status = "dismissed";
      });
    },
    regeneratePlan() {
      mutate("Regenerated the organization plan", (draft) => {
        draft.plan = clone(fixture.initialState.plan);
        draft.plan.freshness = "updated_automatically";
        draft.plan.status = "active";
        draft.planUndo = [];
        draft.files.forEach((file) => reconcilePlanFile(file.id));
      });
    },
    applyPlan(outcome = state.demoOutcome) {
      if (!planSummary().canApply) throw new Error("Resolve every review item and conflict before applying.");
      const changes = clone(actionablePlanChanges());
      const snapshots = [];
      mutate("Applied the sample organization plan", (draft) => {
        const applyCount = outcome === "partial" ? Math.max(1, Math.floor(changes.length / 2)) : outcome === "failed" ? 0 : changes.length;
        changes.slice(0, applyCount).forEach((change) => {
          const file = draft.files.find((item) => item.id === change.fileId);
          if (!file) return;
          snapshots.push({ fileId: file.id, beforeFolderId: file.folderId, beforeName: file.name, afterFolderId: change.destinationFolderId, afterName: change.suggestedName || file.name });
          file.folderId = change.destinationFolderId;
          if (change.suggestedName) file.name = change.suggestedName;
          file.modifiedAt = new Date().toISOString();
        });

        let status = "Applied and verified";
        let verification = "All planned sample changes were rechecked successfully.";
        if (outcome === "partial") {
          status = "Partially completed";
          verification = `${applyCount} of ${changes.length} sample changes were verified. Prepare a new plan for the remainder.`;
          draft.plan.status = "partial";
        } else if (outcome === "failed") {
          status = "Failed with no changes";
          verification = "The recheck stopped the operation before any sample change was made.";
          draft.plan.status = "active";
          draft.plan.freshness = "needs_review";
        } else if (outcome === "verification_incomplete") {
          status = "Verification needed";
          verification = "The sample changes finished, but the final recheck did not complete.";
          draft.plan.status = "verification_incomplete";
        } else {
          draft.plan.status = "applied";
        }

        addActivity({
          title: "Organized Downloads",
          kind: "organization",
          status,
          summary: outcome === "failed" ? "No sample files changed." : `${applyCount} sample file changes completed.`,
          verification,
          undoAvailability: outcome === "verified" ? "available" : outcome === "failed" ? "unavailable" : "limited",
          changes: snapshots.slice(0, 12).map((item) => {
            const beforeLocation = breadcrumb(item.beforeFolderId).map((folder) => folder.name).join(" › ");
            const afterLocation = breadcrumb(item.afterFolderId).map((folder) => folder.name).join(" › ");
            const before = `${beforeLocation} › ${item.beforeName}`;
            const after = `${afterLocation} › ${item.afterName}`;
            return `${before} → ${after}`;
          }),
          reversibleChanges: snapshots,
        });
      });
      return state.activity[0];
    },
    selectWorkingCopy(groupId, fileId) {
      mutate("Selected a duplicate working copy", (draft) => {
        const group = draft.duplicates.groups.find((item) => item.id === groupId);
        if (!group || !group.fileIds.includes(fileId)) return;
        group.preferredFileId = fileId;
        group.decision = "working_copy_selected";
        group.recommendationReason = "You selected this sample file as the working copy.";
        group.fileIds.forEach((id) => reconcilePlanFile(id, { ambiguous: true, reason: "The preferred duplicate copy changed." }));
      });
    },
    keepDuplicateGroup(groupId) {
      mutate("Kept all duplicate copies", (draft) => {
        const group = draft.duplicates.groups.find((item) => item.id === groupId);
        if (group) group.decision = "keep_all";
      });
    },
    consolidateDuplicateGroup(groupId) {
      const group = state.duplicates.groups.find((item) => item.id === groupId);
      if (!group || !group.preferredFileId) throw new Error("Choose a working copy first.");
      const extras = group.fileIds.filter((fileId) => fileId !== group.preferredFileId);
      const moved = [];
      mutate("Consolidated sample duplicate copies", (draft) => {
        const currentGroup = draft.duplicates.groups.find((item) => item.id === groupId);
        extras.forEach((fileId) => {
          const file = draft.files.find((item) => item.id === fileId);
          if (!file) return;
          moved.push({ fileId, beforeFolderId: file.folderId });
          file.inHoldingArea = true;
          file.holdingOriginalFolderId = file.folderId;
          file.folderId = draft.duplicates.holdingArea.folderId;
          reconcilePlanFile(fileId, { ambiguous: true, reason: "An exact copy moved to the reversible holding area." });
        });
        currentGroup.decision = "consolidated";
        currentGroup.status = "consolidated";
        addActivity({
          title: `Consolidated ${extras.length} extra ${extras.length === 1 ? "copy" : "copies"}`,
          kind: "duplicate",
          status: "Applied and verified",
          summary: "Extra sample copies moved to a reversible holding area. They were not deleted.",
          verification: "The preferred working copy remains available and each extra was rechecked.",
          undoAvailability: "limited",
          changes: moved.map((item) => `${fileById(item.fileId)?.name || "Copy"} → Reversible holding area`),
          reversibleChanges: moved.map((item) => ({ type: "holding", ...item })),
        });
      });
      return moved;
    },
    undoActivity(activityId) {
      const operation = state.activity.find((item) => item.id === activityId);
      if (!operation || !["available", "limited"].includes(operation.undoAvailability)) return false;
      mutate("Used the undo failsafe", (draft) => {
        [...(operation.reversibleChanges || [])].reverse().forEach((change) => {
          const file = draft.files.find((item) => item.id === change.fileId);
          if (!file) return;
          if (change.type === "rename") file.name = change.beforeName;
          if (change.type === "move") file.folderId = change.beforeFolderId;
          if (change.type === "holding") {
            file.folderId = change.beforeFolderId;
            file.inHoldingArea = false;
          }
          if (change.beforeName) file.name = change.beforeName;
          if (change.beforeFolderId) file.folderId = change.beforeFolderId;
        });
        operation.status = "Undone and verified";
        operation.undoAvailability = "unavailable";
        operation.verification = "The reversible sample changes were returned and rechecked.";
      });
      return true;
    },
    updateSetting(path, value) {
      mutate("Updated a prototype setting", (draft) => setNested(draft.settings, path, value));
      return getNested(state.settings, path);
    },
    updateLockedRule(ruleId, updates) {
      mutate("Updated a locked rule", (draft) => {
        const rule = draft.settings.lockedRules.items.find((item) => item.id === ruleId);
        if (rule) Object.assign(rule, updates);
      });
    },
    removeLockedRule(ruleId) {
      mutate("Removed a locked rule", (draft) => {
        draft.settings.lockedRules.items = draft.settings.lockedRules.items.filter((item) => item.id !== ruleId);
      });
    },
    resetLearnedHabits() {
      mutate("Reset learned behavior", (draft) => {
        draft.learnedHabits = { summary: "No learned habits yet.", signals: [], resetAt: new Date().toISOString() };
      });
    },
    setDemoOutcome(outcome) {
      const allowed = new Set(["verified", "partial", "failed", "verification_incomplete"]);
      if (!allowed.has(outcome)) return;
      mutate("Changed the Apply demo outcome", (draft) => {
        draft.demoOutcome = outcome;
        draft.settings.advanced.demoOutcome = outcome;
      });
    },
    reset() {
      if (storage) storage.removeItem(STORAGE_KEY);
      state = createInitialState(fixture);
      state.onboarding.completed = false;
      state.onboarding.step = 0;
      emit();
    },
  };

  persist();
  return api;
}

export { STORAGE_KEY };
