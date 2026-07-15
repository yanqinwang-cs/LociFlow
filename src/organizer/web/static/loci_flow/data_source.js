import { LociFlowApiError } from "./api.js";

const KNOWN_KINDS = new Set([
  "folder", "pdf", "text", "markdown", "word", "spreadsheet", "image",
  "presentation", "source_code", "archive", "document",
]);
const REVIEW_STATUSES = new Set(["in_progress", "ready_for_approval", "ready_to_move"]);
const RECOMMENDATION_DECISIONS = new Set(["undecided", "accept", "keep"]);
const MAX_TEXT_LENGTH = 2048;

export class OrganizationDataError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "OrganizationDataError";
    this.code = code;
  }
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  Object.values(value).forEach(deepFreeze);
  return value;
}

function contractError() {
  return new OrganizationDataError(
    "invalid_contract",
    "Organization information could not be loaded.",
  );
}

function requireObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw contractError();
  return value;
}

function requireArray(value) {
  if (!Array.isArray(value)) throw contractError();
  return value;
}

function requireText(value, { allowDot = false } = {}) {
  if (
    typeof value !== "string"
    || value.length === 0
    || value.length > MAX_TEXT_LENGTH
    || (!allowDot && value === ".")
  ) {
    throw contractError();
  }
  return value;
}

function requireCount(value) {
  if (!Number.isSafeInteger(value) || value < 0) throw contractError();
  return value;
}

export function isSafeRelativePath(value, { allowDot = true } = {}) {
  if (typeof value !== "string" || !value || value.length > MAX_TEXT_LENGTH) return false;
  if (value.includes("\0") || value.includes("\\")) return false;
  if (value.startsWith("/") || value.startsWith("\\")) return false;
  if (/^[A-Za-z]:/.test(value) || /^[A-Za-z][A-Za-z0-9+.-]*:/.test(value)) return false;
  if (value === ".") return allowDot;
  const parts = value.split("/");
  return parts.every((part) => part && part !== "." && part !== "..");
}

function requirePath(value, options) {
  if (!isSafeRelativePath(value, options)) throw contractError();
  return value;
}

function validateFileSnapshot(raw) {
  const item = requireObject(raw);
  requireText(item.id);
  requireText(item.name);
  if (!new Set(["file", "folder"]).has(item.entity_type)) throw contractError();
  requireText(item.kind);
  requireCount(item.size_bytes);
  requirePath(item.relative_path, { allowDot: false });
  requirePath(item.current_folder, { allowDot: true });
  if (item.modified_at !== undefined) requireText(item.modified_at);
  if (item.extension !== undefined && typeof item.extension !== "string") throw contractError();
  return item;
}

function validateOption(raw) {
  const option = requireObject(raw);
  requireText(option.id);
  if (!new Set(["move", "keep", "keep_all_copies", "choose_copies_for_duplicate_bin"]).has(option.action)) {
    throw contractError();
  }
  if (option.destination_folder !== undefined) {
    requirePath(option.destination_folder, { allowDot: true });
  }
  if (option.action === "move" && option.destination_folder === undefined) throw contractError();
  return option;
}

function validateSummary(summary, payload) {
  const value = requireObject(summary);
  for (const field of [
    "recommended_changes", "choices_needed", "kept_by_standards", "files_to_move",
    "names_to_change", "folders_to_create", "duplicate_copies",
  ]) {
    requireCount(value[field]);
  }
  if (
    value.recommended_changes !== payload.recommended_changes.length
    || value.choices_needed !== payload.choices_needed.length
    || value.kept_by_standards !== payload.kept_by_standards.length
  ) {
    throw contractError();
  }
  const treePaths = [];
  for (const rawEntry of requireArray(value.movement_tree)) {
    const entry = requireObject(rawEntry);
    requirePath(entry.folder, { allowDot: true });
    for (const path of requireArray(entry.files)) {
      treePaths.push(requirePath(path, { allowDot: false }));
    }
  }
  return treePaths;
}

function validateReview(review, payload) {
  const value = requireObject(review);
  if (!REVIEW_STATUSES.has(value.status)) throw contractError();
  if (value.scan_generation !== payload.freshness.scan_generation) throw contractError();
  requireCount(value.decision_revision);

  const recommendations = requireArray(value.recommendations);
  const decisionsById = new Map();
  for (const rawDecision of recommendations) {
    const decision = requireObject(rawDecision);
    requireText(decision.change_id);
    if (!RECOMMENDATION_DECISIONS.has(decision.decision) || decisionsById.has(decision.change_id)) {
      throw contractError();
    }
    decisionsById.set(decision.change_id, decision.decision);
  }
  const recommendationIds = payload.recommended_changes.map((item) => item.id);
  if (
    recommendations.length !== recommendationIds.length
    || recommendationIds.some((id) => !decisionsById.has(id))
  ) {
    throw contractError();
  }

  const choices = requireArray(value.choices);
  const choiceStateById = new Map();
  const publicChoices = new Map(payload.choices_needed.map((choice) => [choice.id, choice]));
  for (const rawChoice of choices) {
    const choice = requireObject(rawChoice);
    requireText(choice.choice_id);
    if (choiceStateById.has(choice.choice_id) || !publicChoices.has(choice.choice_id)) throw contractError();
    if (choice.option_id !== null && typeof choice.option_id !== "string") throw contractError();
    const publicChoice = publicChoices.get(choice.choice_id);
    const optionIds = new Set(publicChoice.options.map((option) => option.id));
    if (choice.option_id !== null && !optionIds.has(choice.option_id)) throw contractError();
    const selectedIds = requireArray(choice.selected_copy_ids);
    if (!selectedIds.every((id) => typeof id === "string") || new Set(selectedIds).size !== selectedIds.length) {
      throw contractError();
    }
    const offeredCopyIds = new Set((publicChoice.copies || []).map((copy) => copy.id));
    if (selectedIds.some((id) => !offeredCopyIds.has(id))) throw contractError();
    if (choice.option_id !== "choose-for-bin" && selectedIds.length) throw contractError();
    choiceStateById.set(choice.choice_id, choice);
  }
  if (choices.length !== publicChoices.size) throw contractError();

  const summary = requireObject(value.summary);
  for (const field of [
    "pending_recommendations", "pending_choices", "accepted_moves",
    "kept_recommendations", "duplicate_copies_for_future_bin", "choices_remaining",
    "files_to_move", "names_to_change", "folders_to_create",
  ]) {
    requireCount(summary[field]);
  }
  if (typeof summary.ready_for_approval !== "boolean") throw contractError();
  const pendingRecommendations = [...decisionsById.values()].filter((item) => item === "undecided").length;
  const pendingChoices = [...choiceStateById.values()].filter((item) => item.option_id === null).length;
  const acceptedRecommendations = [...decisionsById.values()].filter((item) => item === "accept").length;
  const acceptedDestinationChoices = [...choiceStateById].filter(([id, state]) => {
    if (state.option_id === null) return false;
    return publicChoices.get(id).options.some(
      (option) => option.id === state.option_id && option.action === "move",
    );
  }).length;
  const selectedCopies = [...choiceStateById.values()].reduce(
    (total, item) => total + item.selected_copy_ids.length,
    0,
  );
  const remaining = pendingRecommendations + pendingChoices;
  if (
    summary.pending_recommendations !== pendingRecommendations
    || summary.pending_choices !== pendingChoices
    || summary.choices_remaining !== remaining
    || summary.ready_for_approval !== (remaining === 0)
    || summary.files_to_move !== acceptedRecommendations + acceptedDestinationChoices
    || summary.accepted_moves !== summary.files_to_move
    || summary.duplicate_copies_for_future_bin !== selectedCopies
  ) {
    throw contractError();
  }
  if (value.status === "ready_for_approval" && !summary.ready_for_approval) throw contractError();
  if (value.status === "ready_to_move" && (!summary.ready_for_approval || !value.approval)) {
    throw contractError();
  }
  if (value.approval !== null && value.approval !== undefined) {
    const approval = requireObject(value.approval);
    requireText(approval.approved_at);
    requireObject(approval.summary);
  }
  return value;
}

export function validateOrganizationResponse(rawPayload) {
  const payload = clone(requireObject(rawPayload));
  const scope = requireObject(payload.scope);
  requireText(scope.display_name);
  const freshness = requireObject(payload.freshness);
  requireText(freshness.scan_generation);
  requireText(freshness.last_checked_at);
  requireObject(payload.planning);
  requireArray(payload.recommended_changes);
  requireArray(payload.choices_needed);
  requireArray(payload.kept_by_standards);

  const actionableItemIds = new Set();
  const recommendationPaths = new Set();
  for (const rawChange of payload.recommended_changes) {
    const change = requireObject(rawChange);
    requireText(change.id);
    if (change.action !== "move") throw contractError();
    const item = validateFileSnapshot(change.item);
    requirePath(change.destination_folder, { allowDot: true });
    requireText(change.reason_code);
    if (actionableItemIds.has(item.id)) throw contractError();
    actionableItemIds.add(item.id);
    recommendationPaths.add(item.relative_path);
  }

  for (const rawChoice of payload.choices_needed) {
    const choice = requireObject(rawChoice);
    requireText(choice.id);
    if (!new Set(["choose_destination", "exact_duplicate"]).has(choice.choice_type)) throw contractError();
    requireText(choice.reason_code);
    const options = requireArray(choice.options).map(validateOption);
    if (new Set(options.map((option) => option.id)).size !== options.length) throw contractError();
    const items = choice.choice_type === "choose_destination"
      ? [validateFileSnapshot(choice.item)]
      : requireArray(choice.copies).map(validateFileSnapshot);
    if (choice.choice_type === "exact_duplicate" && items.length < 2) throw contractError();
    for (const item of items) {
      if (actionableItemIds.has(item.id)) throw contractError();
      actionableItemIds.add(item.id);
    }
  }

  const protectedItemIds = new Set();
  const protectedPaths = new Set();
  for (const rawKept of payload.kept_by_standards) {
    const kept = requireObject(rawKept);
    const item = validateFileSnapshot(kept.item);
    const standard = requireObject(kept.standard);
    requireText(standard.id);
    requireText(standard.name);
    requireText(kept.reason_code);
    if (actionableItemIds.has(item.id) || protectedItemIds.has(item.id)) throw contractError();
    protectedItemIds.add(item.id);
    protectedPaths.add(item.relative_path);
  }

  const treePaths = validateSummary(payload.summary, payload);
  if (
    treePaths.some((path) => !recommendationPaths.has(path) || protectedPaths.has(path))
    || new Set(treePaths).size !== treePaths.length
    || treePaths.length !== recommendationPaths.size
  ) {
    throw contractError();
  }
  validateReview(payload.review, payload);
  return deepFreeze(payload);
}

function snapshot(status, backendState = null) {
  return Object.freeze({ status, backendState });
}

function normalizeApiError(error) {
  if (error instanceof OrganizationDataError || error instanceof LociFlowApiError) return error;
  return new OrganizationDataError("request_failed", "Organization information could not be loaded.");
}

let fixtureModulesPromise;

async function fixtureModules() {
  if (!fixtureModulesPromise) {
    fixtureModulesPromise = Promise.all([
      import("./fixtures.json", { with: { type: "json" } }),
      import("./stage_b1_scenarios.js"),
    ]);
  }
  return fixtureModulesPromise;
}

export function createFixtureDataSource(options = {}) {
  const scenarioId = options.scenarioId || "accepted_default";
  return Object.freeze({
    kind: "fixture",
    scenarioId,
    async load() {
      const [{ default: fixtureState }, scenarios] = await fixtureModules();
      return scenarios.createStageB1Fixture(clone(fixtureState), scenarioId);
    },
  });
}

export function createBackendDataSource({
  apiClient,
  csrfToken,
  pollIntervalMs = 1000,
  schedule = globalThis.setTimeout.bind(globalThis),
  cancel = globalThis.clearTimeout.bind(globalThis),
}) {
  if (!apiClient || typeof apiClient.getCurrentOrganization !== "function") {
    throw new TypeError("an organization API client is required");
  }
  const interval = Math.max(1000, pollIntervalMs);
  let disposed = false;
  let current = snapshot("not_checked");
  let mutationPending = false;
  let scanPending = false;
  let pollTimer = null;
  let wakePoll = null;

  function assertActive() {
    if (disposed) throw new OrganizationDataError("disposed", "The organization view is unavailable.");
  }

  async function loadCurrentState() {
    assertActive();
    try {
      const payload = validateOrganizationResponse(await apiClient.getCurrentOrganization());
      current = snapshot("completed", payload);
    } catch (rawError) {
      const error = normalizeApiError(rawError);
      if (error.code === "scan_not_ready") current = snapshot("not_checked");
      else if (error.code === "scan_in_progress") current = snapshot("checking");
      else if (error.code === "scan_failed") current = snapshot("failed");
      else throw error;
    }
    return current;
  }

  function waitForNextPoll() {
    return new Promise((resolve) => {
      wakePoll = resolve;
      pollTimer = schedule(() => {
        pollTimer = null;
        wakePoll = null;
        resolve();
      }, interval);
    });
  }

  async function pollUntilSettled(onStatus) {
    while (!disposed) {
      await waitForNextPoll();
      if (disposed) break;
      const next = await loadCurrentState();
      onStatus?.(next);
      if (next.status === "completed" || next.status === "failed") return next;
    }
    throw new OrganizationDataError("disposed", "The organization view is unavailable.");
  }

  async function startScan(onStatus) {
    assertActive();
    if (scanPending || current.status === "checking") {
      throw new OrganizationDataError("scan_in_progress", "A file check is already running.");
    }
    scanPending = true;
    current = snapshot("checking");
    onStatus?.(current);
    try {
      await apiClient.startScan({ csrfToken });
      return await pollUntilSettled(onStatus);
    } finally {
      scanPending = false;
    }
  }

  function mutationContext() {
    assertActive();
    if (current.status !== "completed" || !current.backendState) {
      throw new OrganizationDataError("review_unavailable", "Organization choices are unavailable.");
    }
    if (current.backendState.review.status === "ready_to_move") {
      throw new OrganizationDataError("approval_already_completed", "This organization plan is already approved.");
    }
    return {
      scanGeneration: current.backendState.freshness.scan_generation,
      expectedRevision: current.backendState.review.decision_revision,
    };
  }

  async function mutate(call) {
    if (mutationPending) {
      throw new OrganizationDataError("mutation_in_progress", "Another organization choice is being saved.");
    }
    mutationPending = true;
    try {
      const payload = validateOrganizationResponse(await call(mutationContext()));
      current = snapshot("completed", payload);
      return current;
    } catch (error) {
      throw normalizeApiError(error);
    } finally {
      mutationPending = false;
    }
  }

  function decideRecommendation(changeId, decision) {
    if (!new Set(["accept", "keep"]).has(decision)) throw contractError();
    return mutate((context) => apiClient.updateRecommendationDecision(
      { ...context, changeId, decision },
      csrfToken,
    ));
  }

  function decideChoice(choiceId, optionId, copyIds = undefined) {
    const state = current.backendState;
    const choice = state?.choices_needed.find((item) => item.id === choiceId);
    if (!choice || !choice.options.some((option) => option.id === optionId)) throw contractError();
    const ids = copyIds === undefined ? undefined : [...copyIds];
    if (optionId === "choose-for-bin") {
      const offered = new Set(choice.copies.map((copy) => copy.id));
      if (
        !ids
        || ids.length === 0
        || ids.length >= offered.size
        || new Set(ids).size !== ids.length
        || ids.some((id) => !offered.has(id))
      ) {
        throw contractError();
      }
    } else if (ids !== undefined) {
      throw contractError();
    }
    return mutate((context) => apiClient.updateChoiceDecision(
      { ...context, choiceId, optionId, ...(ids ? { copyIds: ids } : {}) },
      csrfToken,
    ));
  }

  function acceptRecommendations(expectedChangeIds) {
    const state = current.backendState;
    const decisions = new Map(
      (state?.review.recommendations || []).map((item) => [item.change_id, item.decision]),
    );
    const pending = [...decisions]
      .filter(([, decision]) => decision === "undecided")
      .map(([id]) => id);
    if (
      !Array.isArray(expectedChangeIds)
      || expectedChangeIds.length === 0
      || new Set(expectedChangeIds).size !== expectedChangeIds.length
      || expectedChangeIds.length !== pending.length
      || expectedChangeIds.some((id) => !pending.includes(id))
    ) {
      throw contractError();
    }
    return mutate((context) => apiClient.acceptRecommendedChanges(
      { ...context, expectedChangeIds: [...expectedChangeIds] },
      csrfToken,
    ));
  }

  function approve() {
    return mutate((context) => apiClient.approveOrganizationPlan(context, csrfToken));
  }

  function dispose() {
    disposed = true;
    if (pollTimer !== null) cancel(pollTimer);
    pollTimer = null;
    wakePoll?.();
    wakePoll = null;
  }

  return Object.freeze({
    kind: "backend",
    loadCurrentState,
    startScan,
    decideRecommendation,
    decideChoice,
    acceptRecommendations,
    approve,
    dispose,
    currentState: () => current,
    isMutationPending: () => mutationPending,
  });
}
