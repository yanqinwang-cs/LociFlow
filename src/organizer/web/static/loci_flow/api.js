export const ORGANIZATION_ENDPOINT = "/api/organization/current";
export const ORGANIZATION_DECISIONS_ENDPOINT = "/api/organization/decisions";
export const ORGANIZATION_APPROVAL_ENDPOINT = "/api/organization/approval";
export const ACTIVITY_ENDPOINT = "/api/activity";
export const UNDO_CONFIRMATION = "UNDO FILE MOVE";

export class OrganizationApiError extends Error {
  constructor(code, message, status = 0) {
    super(message);
    this.name = "OrganizationApiError";
    this.code = code;
    this.status = status;
  }
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isText = (value) => typeof value === "string" && value.length > 0;
const isCount = (value) => Number.isInteger(value) && value >= 0;

export function isSafeRelativePath(value) {
  if (typeof value !== "string" || value.length === 0 || value.includes("\\")) return false;
  if (value.startsWith("/") || /^[A-Za-z]:/.test(value)) return false;
  const parts = value.split("/");
  return !parts.some((part) => part === ".." || part.length === 0);
}

function assert(condition, message) {
  if (!condition) throw new OrganizationApiError("contract_error", message);
}

function validateTimestamp(value, field, nullable = false) {
  if (nullable && value === null) return;
  assert(isText(value) && !Number.isNaN(Date.parse(value)), `${field} must be an ISO timestamp`);
}

function validateSnapshot(item, field) {
  assert(isObject(item), `${field} must be an object`);
  for (const key of ["id", "name", "entity_type", "kind", "extension", "modified_at", "current_folder"]) {
    assert(typeof item[key] === "string", `${field}.${key} must be a string`);
  }
  assert(isText(item.id) && isText(item.name), `${field} must identify a named item`);
  assert(!item.name.includes("/") && !item.name.includes("\\") && item.name !== "." && item.name !== "..", `${field}.name must be a leaf name`);
  assert(item.entity_type === "file" || item.entity_type === "folder", `${field}.entity_type is unsupported`);
  assert(isCount(item.size_bytes), `${field}.size_bytes must be a non-negative integer`);
  assert(isSafeRelativePath(item.relative_path), `${field}.relative_path must be root-relative`);
  assert(item.current_folder === "." || isSafeRelativePath(item.current_folder), `${field}.current_folder must be root-relative`);
  validateTimestamp(item.modified_at, `${field}.modified_at`);
}

function validateOption(option, field) {
  assert(isObject(option) && isText(option.id) && isText(option.action), `${field} is invalid`);
  if (Object.hasOwn(option, "destination_folder")) {
    assert(option.destination_folder === "." || isSafeRelativePath(option.destination_folder), `${field}.destination_folder must be root-relative`);
  }
}

function validateSummary(summary, expected, recommendedPaths) {
  assert(isObject(summary), "summary must be an object");
  for (const field of [
    "recommended_changes", "choices_needed", "kept_by_standards", "files_to_move",
    "names_to_change", "folders_to_create", "duplicate_copies",
  ]) assert(isCount(summary[field]), `summary.${field} must be a non-negative integer`);
  assert(summary.recommended_changes === expected.recommended, "recommended summary count is inconsistent");
  assert(summary.choices_needed === expected.choices, "choice summary count is inconsistent");
  assert(summary.kept_by_standards === expected.kept, "Standards summary count is inconsistent");
  assert(Array.isArray(summary.movement_tree), "summary.movement_tree must be an array");
  summary.movement_tree.forEach((entry, index) => {
    assert(isObject(entry), `summary.movement_tree[${index}] must be an object`);
    assert(entry.folder === "." || isSafeRelativePath(entry.folder), `summary.movement_tree[${index}].folder must be root-relative`);
    assert(Array.isArray(entry.files), `summary.movement_tree[${index}].files must be an array`);
    entry.files.forEach((path, fileIndex) => {
      assert(isSafeRelativePath(path), `summary.movement_tree[${index}].files[${fileIndex}] must be root-relative`);
      assert(recommendedPaths.has(path), `summary.movement_tree[${index}].files[${fileIndex}] must reference a recommended change`);
    });
  });
}

function validateReview(review, generation) {
  assert(isObject(review), "review must be an object");
  assert(["in_progress", "ready_for_approval", "ready_to_move"].includes(review.status), "review.status is invalid");
  assert(review.scan_generation === generation, "review.scan_generation is inconsistent");
  assert(isCount(review.decision_revision), "review.decision_revision is invalid");
  assert(Array.isArray(review.recommendations) && Array.isArray(review.choices), "review decisions must be arrays");
  review.recommendations.forEach((item, index) => {
    assert(isObject(item) && isText(item.change_id), `review.recommendations[${index}] is invalid`);
    assert(["undecided", "accept", "keep"].includes(item.decision), `review.recommendations[${index}].decision is invalid`);
  });
  review.choices.forEach((item, index) => {
    assert(isObject(item) && isText(item.choice_id), `review.choices[${index}] is invalid`);
    assert(item.option_id === null || isText(item.option_id), `review.choices[${index}].option_id is invalid`);
    assert(Array.isArray(item.selected_copy_ids) && item.selected_copy_ids.every(isText), `review.choices[${index}].selected_copy_ids is invalid`);
  });
  const summary = review.summary;
  assert(isObject(summary), "review.summary must be an object");
  for (const field of [
    "pending_recommendations", "pending_choices", "accepted_moves", "kept_recommendations",
    "duplicate_copies_for_future_bin", "choices_remaining", "files_to_move",
    "names_to_change", "folders_to_create",
  ]) assert(isCount(summary[field]), `review.summary.${field} is invalid`);
  assert(typeof summary.ready_for_approval === "boolean", "review.summary.ready_for_approval is invalid");
  assert(review.approval === null || isObject(review.approval), "review.approval is invalid");
  if (review.status === "ready_to_move") {
    assert(isObject(review.approval), "ready review requires approval");
    validateTimestamp(review.approval.approved_at, "review.approval.approved_at");
  }
}

export function validateOrganizationSlice(data) {
  assert(isObject(data), "organization response must be an object");
  assert(isObject(data.scope) && isText(data.scope.display_name), "scope.display_name is required");
  assert(!data.scope.display_name.includes("/") && !data.scope.display_name.includes("\\") && !/^[A-Za-z]:/.test(data.scope.display_name), "scope.display_name must not expose a path");
  assert(data.scope.root_label === ".", "scope.root_label must not expose an absolute path");
  assert(isObject(data.freshness) && isText(data.freshness.scan_generation), "freshness.scan_generation is required");
  validateTimestamp(data.freshness.last_checked_at, "freshness.last_checked_at");
  validateTimestamp(data.freshness.last_organized_at, "freshness.last_organized_at", true);
  assert(isObject(data.planning) && isText(data.planning.strategy_id), "planning.strategy_id is required");
  assert(data.planning.organization_style === null || typeof data.planning.organization_style === "string", "planning.organization_style is invalid");

  for (const field of ["recommended_changes", "choices_needed", "kept_by_standards"]) {
    assert(Array.isArray(data[field]), `${field} must be an array`);
  }

  const visibleIds = new Set();
  const visiblePaths = new Set();
  data.recommended_changes.forEach((change, index) => {
    const field = `recommended_changes[${index}]`;
    assert(isObject(change) && isText(change.id), `${field}.id is required`);
    assert(change.action === "move", `${field}.action is unsupported`);
    validateSnapshot(change.item, `${field}.item`);
    assert(change.destination_folder === "." || isSafeRelativePath(change.destination_folder), `${field}.destination_folder must be root-relative`);
    assert(isText(change.reason_code), `${field}.reason_code is required`);
    visibleIds.add(change.item.id);
    visiblePaths.add(change.item.relative_path);
  });

  data.choices_needed.forEach((choice, index) => {
    const field = `choices_needed[${index}]`;
    assert(isObject(choice) && isText(choice.id) && isText(choice.choice_type) && isText(choice.reason_code), `${field} is invalid`);
    assert(Array.isArray(choice.options) && choice.options.length > 0, `${field}.options must be non-empty`);
    choice.options.forEach((option, optionIndex) => validateOption(option, `${field}.options[${optionIndex}]`));
    if (Object.hasOwn(choice, "item")) {
      validateSnapshot(choice.item, `${field}.item`);
      visibleIds.add(choice.item.id);
      visiblePaths.add(choice.item.relative_path);
    }
    if (Object.hasOwn(choice, "copies")) {
      assert(Array.isArray(choice.copies), `${field}.copies must be an array`);
      choice.copies.forEach((copy, copyIndex) => {
        validateSnapshot(copy, `${field}.copies[${copyIndex}]`);
        visibleIds.add(copy.id);
        visiblePaths.add(copy.relative_path);
      });
      assert(choice.identical_copy_count === choice.copies.length, `${field}.identical_copy_count is inconsistent`);
    }
    assert(Object.hasOwn(choice, "item") || (Array.isArray(choice.copies) && choice.copies.length > 0), `${field} must identify an item or copies`);
  });

  data.kept_by_standards.forEach((kept, index) => {
    const field = `kept_by_standards[${index}]`;
    assert(isObject(kept), `${field} must be an object`);
    validateSnapshot(kept.item, `${field}.item`);
    assert(isObject(kept.standard) && isText(kept.standard.id) && isText(kept.standard.name), `${field}.standard is invalid`);
    assert(isText(kept.reason_code), `${field}.reason_code is required`);
    assert(!visibleIds.has(kept.item.id) && !visiblePaths.has(kept.item.relative_path), `${field}.item must not also be a proposed change`);
  });

  validateSummary(data.summary, {
    recommended: data.recommended_changes.length,
    choices: data.choices_needed.length,
    kept: data.kept_by_standards.length,
  }, new Set(data.recommended_changes.map((change) => change.item.relative_path)));
  if (Object.hasOwn(data, "review")) validateReview(data.review, data.freshness.scan_generation);
  return data;
}

const ACTIVITY_TYPES = new Set(["organization", "restore"]);
const ACTIVITY_STATUSES = new Set([
  "prepared", "moving", "moved_unverified", "verified", "partial_movement",
  "movement_failed", "verification_failed", "interrupted", "recovery_required",
  "undoing", "restored_unverified", "restored", "partial_undo", "undo_failed",
  "undo_verification_failed",
]);

function validateActivityDetail(detail, field, type) {
  assert(isObject(detail) && isText(detail.status), `${field} is invalid`);
  const paths = type === "organization" ? ["source", "destination"] : ["restored_to", "moved_from"];
  for (const path of paths) assert(isSafeRelativePath(detail[path]), `${field}.${path} must be root-relative`);
}

function validateActivityEntry(entry, field) {
  assert(isObject(entry), `${field} must be an object`);
  assert(/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(entry.id), `${field}.id must be an opaque UUID`);
  assert(ACTIVITY_TYPES.has(entry.type), `${field}.type is unsupported`);
  assert(isText(entry.title) && ACTIVITY_STATUSES.has(entry.status), `${field} status is invalid`);
  validateTimestamp(entry.started_at, `${field}.started_at`, true);
  validateTimestamp(entry.completed_at, `${field}.completed_at`, true);
  for (const count of ["files_expected", "files_moved", "files_restored", "files_failed", "files_unattempted"]) {
    assert(isCount(entry[count]), `${field}.${count} must be a non-negative integer`);
  }
  assert(isText(entry.verification_status), `${field}.verification_status is required`);
  assert(typeof entry.undoable === "boolean", `${field}.undoable must be boolean`);
  assert(entry.related_operation_id === null || isText(entry.related_operation_id), `${field}.related_operation_id is invalid`);
  assert(Array.isArray(entry.details), `${field}.details must be an array`);
  entry.details.forEach((detail, index) => validateActivityDetail(detail, `${field}.details[${index}]`, entry.type));
}

export function validateActivityList(data) {
  assert(isObject(data) && Array.isArray(data.activity), "activity response must contain an array");
  data.activity.forEach((entry, index) => validateActivityEntry(entry, `activity[${index}]`));
  assert(data.activity.filter((entry) => entry.undoable).length <= 1, "only one Activity entry may be undoable");
  return data;
}

export function validateUndoPreview(data) {
  assert(isObject(data) && isText(data.operation_id), "Undo preview operation is required");
  assert(isCount(data.files_to_restore) && data.files_to_restore > 0, "Undo preview count is invalid");
  assert(data.confirmation === UNDO_CONFIRMATION, "Undo confirmation is invalid");
  assert(isText(data.confirmation_token), "Undo confirmation token is required");
  return data;
}

function validateUndoOutcome(data) {
  assert(isObject(data) && isObject(data.undo) && isObject(data.activity), "Undo response is invalid");
  validateActivityEntry(data.activity, "activity");
  assert(isText(data.undo.undo_id) && data.undo.undo_id === data.activity.id, "Undo response IDs are inconsistent");
  return data;
}

export async function fetchOrganizationSlice(fetchImpl = globalThis.fetch) {
  let response;
  try {
    response = await fetchImpl(ORGANIZATION_ENDPOINT, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
  } catch (_error) {
    throw new OrganizationApiError("network_error", "Loci Flow could not reach bootAI.");
  }

  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new OrganizationApiError("contract_error", "bootAI returned an unreadable organization response.", response.status);
  }
  if (!response.ok) {
    const error = isObject(payload.error) ? payload.error : {};
    const code = isText(error.code) ? error.code : "organization_unavailable";
    const message = isText(error.message) ? error.message : "Organization information is unavailable.";
    throw new OrganizationApiError(code, message, response.status);
  }
  return validateOrganizationSlice(payload);
}

function csrfTokenFromDom() {
  const token = globalThis.document?.querySelector('meta[name="csrf-token"]')?.content;
  if (!isText(token)) throw new OrganizationApiError("authentication_required", "The bootAI session token is unavailable.");
  return token;
}

async function submitForm(endpoint, fields, fetchImpl) {
  const body = new URLSearchParams();
  for (const [name, value] of Object.entries(fields)) body.set(name, String(value));
  let response;
  try {
    response = await fetchImpl(endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
  } catch (_error) {
    throw new OrganizationApiError("network_error", "Loci Flow could not reach bootAI.");
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new OrganizationApiError("contract_error", "bootAI returned an unreadable organization response.", response.status);
  }
  if (!response.ok) {
    const error = isObject(payload.error) ? payload.error : {};
    throw new OrganizationApiError(
      isText(error.code) ? error.code : "organization_unavailable",
      isText(error.message) ? error.message : "The organization request was not accepted.",
      response.status,
    );
  }
  return payload;
}

export function updateOrganizationDecision(request, fetchImpl = globalThis.fetch, csrfToken = csrfTokenFromDom()) {
  const fields = {
    csrf_token: csrfToken,
    scan_generation: request.scanGeneration,
    expected_revision: request.expectedRevision,
    kind: request.kind,
  };
  if (request.kind === "recommendation") {
    fields.change_id = request.changeId;
    fields.decision = request.decision;
  } else if (request.kind === "choice") {
    fields.choice_id = request.choiceId;
    fields.option_id = request.optionId;
    if (request.optionId === "choose-for-bin") fields.copy_ids = JSON.stringify(request.copyIds);
  } else {
    throw new OrganizationApiError("invalid_option", "That organization action is unsupported.");
  }
  return submitForm(ORGANIZATION_DECISIONS_ENDPOINT, fields, fetchImpl).then(validateOrganizationSlice);
}

export function acceptRecommendedChanges(request, fetchImpl = globalThis.fetch, csrfToken = csrfTokenFromDom()) {
  return submitForm(ORGANIZATION_DECISIONS_ENDPOINT, {
    csrf_token: csrfToken,
    scan_generation: request.scanGeneration,
    expected_revision: request.expectedRevision,
    kind: "bulk_recommendations",
    decision: "accept",
    expected_change_ids: JSON.stringify(request.expectedChangeIds),
  }, fetchImpl).then(validateOrganizationSlice);
}

export function approveOrganizationPlan(request, fetchImpl = globalThis.fetch, csrfToken = csrfTokenFromDom()) {
  return submitForm(ORGANIZATION_APPROVAL_ENDPOINT, {
    csrf_token: csrfToken,
    scan_generation: request.scanGeneration,
    expected_revision: request.expectedRevision,
  }, fetchImpl).then(validateOrganizationSlice);
}

export async function fetchActivity(fetchImpl = globalThis.fetch) {
  let response;
  try {
    response = await fetchImpl(ACTIVITY_ENDPOINT, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
  } catch (_error) {
    throw new OrganizationApiError("network_error", "Loci Flow could not reach Activity.");
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new OrganizationApiError("contract_error", "bootAI returned unreadable Activity.", response.status);
  }
  if (!response.ok) {
    const error = isObject(payload.error) ? payload.error : {};
    throw new OrganizationApiError(isText(error.code) ? error.code : "activity_unavailable", isText(error.message) ? error.message : "Activity is unavailable.", response.status);
  }
  return validateActivityList(payload);
}

export function previewActivityUndo(operationId, fetchImpl = globalThis.fetch, csrfToken = csrfTokenFromDom()) {
  return submitForm(`/api/activity/${encodeURIComponent(operationId)}/undo/preview`, {
    csrf_token: csrfToken,
  }, fetchImpl).then(validateUndoPreview);
}

export function executeActivityUndo(request, fetchImpl = globalThis.fetch, csrfToken = csrfTokenFromDom()) {
  return submitForm(`/api/activity/${encodeURIComponent(request.operationId)}/undo`, {
    csrf_token: csrfToken,
    confirmation_token: request.confirmationToken,
    confirmation: UNDO_CONFIRMATION,
  }, fetchImpl).then(validateUndoOutcome);
}
