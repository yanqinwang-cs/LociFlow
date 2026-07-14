export const ORGANIZATION_ENDPOINT = "/api/organization/current";
export const ORGANIZATION_DECISIONS_ENDPOINT = "/api/organization/decisions";
export const ORGANIZATION_APPROVAL_ENDPOINT = "/api/organization/approval";

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
  return validateOrganizationSlice(payload);
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
  return submitForm(ORGANIZATION_DECISIONS_ENDPOINT, fields, fetchImpl);
}

export function acceptRecommendedChanges(request, fetchImpl = globalThis.fetch, csrfToken = csrfTokenFromDom()) {
  return submitForm(ORGANIZATION_DECISIONS_ENDPOINT, {
    csrf_token: csrfToken,
    scan_generation: request.scanGeneration,
    expected_revision: request.expectedRevision,
    kind: "bulk_recommendations",
    decision: "accept",
    expected_change_ids: JSON.stringify(request.expectedChangeIds),
  }, fetchImpl);
}

export function approveOrganizationPlan(request, fetchImpl = globalThis.fetch, csrfToken = csrfTokenFromDom()) {
  return submitForm(ORGANIZATION_APPROVAL_ENDPOINT, {
    csrf_token: csrfToken,
    scan_generation: request.scanGeneration,
    expected_revision: request.expectedRevision,
  }, fetchImpl);
}
