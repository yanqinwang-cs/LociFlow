import { LociFlowApiError } from "./api.js";

const KNOWN_KINDS = new Set([
  "folder", "pdf", "text", "markdown", "word", "spreadsheet", "image",
  "presentation", "source_code", "archive", "document",
]);
const REVIEW_STATUSES = new Set(["in_progress", "ready_for_approval", "ready_to_move"]);
const RECOMMENDATION_DECISIONS = new Set(["undecided", "accept", "keep"]);
const MAX_TEXT_LENGTH = 2048;
const MAX_CONFIRMATION_TOKEN_LENGTH = 512;
const UNDO_CONFIRMATION = "UNDO FILE MOVE";
const ACTIVITY_TYPES = new Set(["organization", "restore"]);
const MOVEMENT_ACTIVITY_STATUSES = new Set([
  "verified", "partial_movement", "movement_failed", "verification_failed",
  "prepared", "moving", "moved_unverified", "interrupted", "recovery_required",
]);
const RESTORE_ACTIVITY_STATUSES = new Set([
  "restored", "partial_undo", "undo_failed", "undo_verification_failed",
  "prepared", "undoing", "restored_unverified", "interrupted",
]);
const ACTIVITY_ERROR_MESSAGES = Object.freeze({
  operation_not_found: "This move is no longer available.",
  operation_not_undoable: "This move can no longer be undone.",
  operation_superseded: "A newer file move must be handled first.",
  undo_already_started: "This move is already being restored.",
  undo_already_completed: "This move has already been restored.",
  undo_destination_occupied: "A file now exists in one of the previous folders.",
  undo_source_missing: "A moved file could not be found.",
  undo_source_changed: "A moved file changed after it was moved.",
  unsafe_undo_path: "This move cannot be undone safely.",
  movement_or_undo_in_progress: "Another file action is running.",
});

export class OrganizationDataError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "OrganizationDataError";
    this.code = code;
  }
}

export class ActivityDataError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ActivityDataError";
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

function activityContractError() {
  return new ActivityDataError(
    "invalid_activity_contract",
    "Activity could not be loaded.",
  );
}

function requireActivityObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw activityContractError();
  return value;
}

function requireActivityArray(value) {
  if (!Array.isArray(value)) throw activityContractError();
  return value;
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

export function isCanonicalActivityId(value) {
  return typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}

function requireActivityId(value) {
  if (!isCanonicalActivityId(value)) throw activityContractError();
  return value;
}

function requireActivityText(value) {
  if (typeof value !== "string" || !value || value.length > MAX_TEXT_LENGTH) {
    throw activityContractError();
  }
  return value;
}

function requireActivityCount(value) {
  if (!Number.isSafeInteger(value) || value < 0) throw activityContractError();
  return value;
}

function requireIsoTimestamp(value) {
  if (value === null) return null;
  if (typeof value !== "string" || value.length > 64) throw activityContractError();
  const match = value.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/,
  );
  if (!match) throw activityContractError();
  const [, year, month, day, hour, minute, second, zone] = match;
  const numeric = [year, month, day, hour, minute, second].map(Number);
  const [yearValue, monthValue, dayValue, hourValue, minuteValue, secondValue] = numeric;
  const daysInMonth = new Date(Date.UTC(yearValue, monthValue, 0)).getUTCDate();
  if (
    monthValue < 1 || monthValue > 12 || dayValue < 1 || dayValue > daysInMonth
    || hourValue > 23 || minuteValue > 59 || secondValue > 59
  ) {
    throw activityContractError();
  }
  if (zone !== "Z") {
    const [zoneHour, zoneMinute] = zone.slice(1).split(":").map(Number);
    if (zoneHour > 23 || zoneMinute > 59) throw activityContractError();
  }
  if (!Number.isFinite(Date.parse(value))) throw activityContractError();
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

function validateActivityDetail(rawDetail, type) {
  const detail = requireActivityObject(rawDetail);
  const status = requireActivityText(detail.status);
  if (type === "organization") {
    if (!isSafeRelativePath(detail.source, { allowDot: false })) throw activityContractError();
    if (!isSafeRelativePath(detail.destination, { allowDot: false })) throw activityContractError();
    return { source: detail.source, destination: detail.destination, status };
  }
  if (!isSafeRelativePath(detail.restored_to, { allowDot: false })) throw activityContractError();
  if (!isSafeRelativePath(detail.moved_from, { allowDot: false })) throw activityContractError();
  return { restored_to: detail.restored_to, moved_from: detail.moved_from, status };
}

function validateActivityEntry(rawEntry) {
  const entry = requireActivityObject(rawEntry);
  const id = requireActivityId(entry.id);
  if (!ACTIVITY_TYPES.has(entry.type)) throw activityContractError();
  const title = requireActivityText(entry.title);
  const status = requireActivityText(entry.status);
  const allowedStatuses = entry.type === "organization"
    ? MOVEMENT_ACTIVITY_STATUSES
    : RESTORE_ACTIVITY_STATUSES;
  if (!allowedStatuses.has(status)) throw activityContractError();
  const startedAt = requireIsoTimestamp(entry.started_at);
  const completedAt = requireIsoTimestamp(entry.completed_at);
  for (const field of [
    "files_expected", "files_moved", "files_restored", "files_failed", "files_unattempted",
  ]) {
    requireActivityCount(entry[field]);
  }
  const verificationStatus = entry.verification_status === null
    ? null
    : requireActivityText(entry.verification_status);
  if (typeof entry.undoable !== "boolean") throw activityContractError();
  const relatedOperationId = entry.related_operation_id === null
    ? null
    : requireActivityId(entry.related_operation_id);
  const completed = entry.type === "organization" ? entry.files_moved : entry.files_restored;
  if (completed + entry.files_failed + entry.files_unattempted > entry.files_expected) {
    throw activityContractError();
  }
  if (entry.type === "organization") {
    if (entry.files_restored !== 0 || relatedOperationId !== null) throw activityContractError();
    if (
      entry.undoable
      && (status !== "verified" || verificationStatus !== "passed" || entry.files_moved < 1)
    ) {
      throw activityContractError();
    }
  } else if (entry.files_moved !== 0 || entry.undoable || relatedOperationId === null) {
    throw activityContractError();
  }
  return {
    id,
    type: entry.type,
    title,
    status,
    started_at: startedAt,
    completed_at: completedAt,
    files_expected: entry.files_expected,
    files_moved: entry.files_moved,
    files_restored: entry.files_restored,
    files_failed: entry.files_failed,
    files_unattempted: entry.files_unattempted,
    verification_status: verificationStatus,
    undoable: entry.undoable,
    related_operation_id: relatedOperationId,
    details: requireActivityArray(entry.details).map(
      (detail) => validateActivityDetail(detail, entry.type),
    ),
  };
}

export function validateActivityResponse(rawPayload) {
  const payload = requireActivityObject(rawPayload);
  const ids = new Set();
  let undoableCount = 0;
  const activity = requireActivityArray(payload.activity).map((rawEntry) => {
    const entry = validateActivityEntry(rawEntry);
    if (ids.has(entry.id)) throw activityContractError();
    ids.add(entry.id);
    if (entry.undoable) undoableCount += 1;
    return entry;
  });
  if (undoableCount > 1) throw activityContractError();
  return deepFreeze({ activity });
}

export function validateUndoPreview(rawPreview, requestedOperationId) {
  const preview = requireActivityObject(rawPreview);
  requireActivityId(requestedOperationId);
  if (requireActivityId(preview.operation_id) !== requestedOperationId) throw activityContractError();
  if (!Number.isSafeInteger(preview.files_to_restore) || preview.files_to_restore < 1) {
    throw activityContractError();
  }
  if (preview.confirmation !== UNDO_CONFIRMATION) throw activityContractError();
  if (
    typeof preview.confirmation_token !== "string"
    || !preview.confirmation_token
    || preview.confirmation_token.length > MAX_CONFIRMATION_TOKEN_LENGTH
  ) {
    throw activityContractError();
  }
  return deepFreeze({
    operation_id: preview.operation_id,
    files_to_restore: preview.files_to_restore,
    confirmation: preview.confirmation,
    confirmation_token: preview.confirmation_token,
  });
}

export function activityErrorMessage(error) {
  return ACTIVITY_ERROR_MESSAGES[error?.code]
    || (error?.code === "activity_unavailable" ? "Activity could not be loaded." : null)
    || "The Activity request could not be completed.";
}

export function isUncertainUndoError(error) {
  const code = error?.code || "";
  return code === "request_failed"
    || code === "invalid_response"
    || /^http_5\d\d$/.test(code)
    || !Object.hasOwn(ACTIVITY_ERROR_MESSAGES, code);
}

function snapshot(status, backendState = null) {
  return Object.freeze({ status, backendState });
}

function normalizeApiError(error) {
  if (error instanceof OrganizationDataError || error instanceof LociFlowApiError) return error;
  return new OrganizationDataError("request_failed", "Organization information could not be loaded.");
}

function normalizeActivityError(error, fallbackMessage) {
  if (error instanceof ActivityDataError) return error;
  if (error instanceof LociFlowApiError) {
    return new ActivityDataError(error.code, ACTIVITY_ERROR_MESSAGES[error.code] || fallbackMessage);
  }
  return new ActivityDataError("request_failed", fallbackMessage);
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
  let activityState = null;
  let undoMutationPending = false;

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

  async function loadActivity() {
    assertActive();
    try {
      if (typeof apiClient.getActivity !== "function") throw new TypeError("Activity API is unavailable");
      activityState = validateActivityResponse(await apiClient.getActivity());
      return activityState;
    } catch (error) {
      throw normalizeActivityError(error, "Activity could not be loaded.");
    }
  }

  async function withUndoMutation(operation) {
    assertActive();
    if (undoMutationPending) {
      throw new ActivityDataError("undo_in_progress", "Another Undo request is already running.");
    }
    undoMutationPending = true;
    try {
      return await operation();
    } finally {
      undoMutationPending = false;
    }
  }

  function currentUndoableEntry(operationId) {
    requireActivityId(operationId);
    const entry = activityState?.activity.find((item) => item.id === operationId);
    if (!entry || entry.undoable !== true) {
      throw new ActivityDataError("operation_not_undoable", ACTIVITY_ERROR_MESSAGES.operation_not_undoable);
    }
    return entry;
  }

  function previewUndo(operationId) {
    currentUndoableEntry(operationId);
    return withUndoMutation(async () => {
      try {
        if (typeof apiClient.previewActivityUndo !== "function") throw new TypeError("Undo API is unavailable");
        return validateUndoPreview(
          await apiClient.previewActivityUndo(operationId, csrfToken),
          operationId,
        );
      } catch (error) {
        throw normalizeActivityError(error, "The Undo preview could not be loaded.");
      }
    });
  }

  function confirmUndo(operationId, confirmationToken) {
    currentUndoableEntry(operationId);
    if (
      typeof confirmationToken !== "string"
      || !confirmationToken
      || confirmationToken.length > MAX_CONFIRMATION_TOKEN_LENGTH
    ) {
      throw activityContractError();
    }
    return withUndoMutation(async () => {
      try {
        if (typeof apiClient.undoActivityOperation !== "function") throw new TypeError("Undo API is unavailable");
        return await apiClient.undoActivityOperation(operationId, confirmationToken, csrfToken);
      } catch (error) {
        throw normalizeActivityError(error, "The Undo result could not be loaded.");
      }
    });
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
    loadActivity,
    previewUndo,
    confirmUndo,
    dispose,
    currentState: () => current,
    isMutationPending: () => mutationPending,
    isUndoPending: () => undoMutationPending,
  });
}
