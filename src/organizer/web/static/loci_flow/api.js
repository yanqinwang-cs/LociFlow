const CURRENT_ORGANIZATION_URL = "/api/organization/current";
const CURRENT_RECOVERY_URL = "/api/recovery/current";
const RECOVERY_CHECK_CONTEXT_URL = "/api/recovery/check-context";
const RECOVERY_CHECK_RESULT_URL = "/api/recovery/check-result";
const DECISIONS_URL = "/api/organization/decisions";
const APPROVAL_URL = "/api/organization/approval";
const MOVEMENT_PREVIEW_URL = "/api/organization/move/preview";
const MOVEMENT_URL = "/api/organization/move";
const DUPLICATE_BIN_URL = "/api/duplicate-bin";
const DUPLICATE_BIN_MOVEMENT_PREVIEW_URL = "/api/duplicate-bin/move/preview";
const DUPLICATE_BIN_MOVEMENT_URL = "/api/duplicate-bin/move";
const DUPLICATE_BIN_RESTORE_PREVIEW_URL = "/api/duplicate-bin/restore/preview";
const DUPLICATE_BIN_RESTORE_URL = "/api/duplicate-bin/restore";
const SCAN_URL = "/scan?surface=home";
const ACTIVITY_URL = "/api/activity";
const STANDARDS_URL = "/api/standards";
const STANDARDS_CREATION_CONTEXT_URL = "/api/standards/creation-context";
const STANDARDS_STATE_URL = "/api/standards/state";
const STANDARDS_PREVIEW_URL = "/api/standards/preview";
const MOVEMENT_CONFIRMATION = "MOVE APPROVED FILES";
const UNDO_CONFIRMATION = "UNDO FILE MOVE";
const DUPLICATE_BIN_MOVEMENT_CONFIRMATION = "MOVE COPIES TO DUPLICATE BIN";
const DUPLICATE_BIN_RESTORE_CONFIRMATION = "RESTORE COPIES";

export class LociFlowApiError extends Error {
  constructor(code, message, status = 0) {
    super(message);
    this.name = "LociFlowApiError";
    this.code = code;
    this.status = status;
  }
}

function defaultFetch(...args) {
  return globalThis.fetch(...args);
}

function baseOptions() {
  return {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  };
}

function mutationOptions(fields) {
  return {
    ...baseOptions(),
    method: "POST",
    redirect: "follow",
    headers: {
      ...baseOptions().headers,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams(fields),
  };
}

async function responseJson(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new LociFlowApiError(
      "invalid_response",
      "Organization information could not be loaded.",
      response.status,
    );
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new LociFlowApiError(
      "invalid_response",
      "Organization information could not be loaded.",
      response.status,
    );
  }
  if (!response.ok) {
    const error = payload.error;
    const code = error && typeof error.code === "string" ? error.code : `http_${response.status}`;
    const message = error && typeof error.message === "string"
      ? error.message
      : "The organization request could not be completed.";
    throw new LociFlowApiError(code, message, response.status);
  }
  return payload;
}

async function postJson(url, fields, fetchImpl) {
  const response = await fetchImpl(url, mutationOptions(fields));
  return responseJson(response);
}

function requiredToken(csrfToken) {
  if (typeof csrfToken !== "string" || !csrfToken) {
    throw new LociFlowApiError("csrf_unavailable", "The secure session is unavailable.");
  }
  return csrfToken;
}

function mutationBase(payload, csrfToken) {
  return {
    csrf_token: requiredToken(csrfToken),
    scan_generation: String(payload.scanGeneration),
    expected_revision: String(payload.expectedRevision),
  };
}

export function isCanonicalOperationId(value) {
  return typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}

function requiredOperationId(operationId) {
  if (!isCanonicalOperationId(operationId)) {
    throw new LociFlowApiError("invalid_activity_contract", "Activity information is invalid.");
  }
  return operationId;
}

export async function getCurrentOrganization(fetchImpl = defaultFetch) {
  const response = await fetchImpl(CURRENT_ORGANIZATION_URL, {
    ...baseOptions(),
    method: "GET",
  });
  return responseJson(response);
}

export async function getCurrentRecovery(fetchImpl = defaultFetch) {
  const response = await fetchImpl(CURRENT_RECOVERY_URL, {
    ...baseOptions(),
    method: "GET",
  });
  return responseJson(response);
}

export async function createRecoveryCheckContext(
  fetchImpl = defaultFetch,
  csrfToken,
) {
  return postJson(
    RECOVERY_CHECK_CONTEXT_URL,
    { csrf_token: requiredToken(csrfToken) },
    fetchImpl,
  );
}

export async function checkRecoveryResult(
  payload,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  if (
    !isCanonicalOperationId(payload?.caseId)
    || !isCanonicalOperationId(payload?.actionId)
  ) {
    throw new LociFlowApiError(
      "invalid_recovery_contract",
      "Recovery information is invalid.",
    );
  }
  return postJson(
    RECOVERY_CHECK_RESULT_URL,
    {
      csrf_token: requiredToken(csrfToken),
      case_id: payload.caseId,
      action_id: payload.actionId,
    },
    fetchImpl,
  );
}

export async function startScan({ csrfToken }, fetchImpl = defaultFetch) {
  const response = await fetchImpl(
    SCAN_URL,
    mutationOptions({ csrf_token: requiredToken(csrfToken) }),
  );
  if (!response.ok) {
    throw new LociFlowApiError(
      response.status === 409 ? "scan_conflict" : `http_${response.status}`,
      "The file check could not be started.",
      response.status,
    );
  }
  return Object.freeze({ accepted: true });
}

export async function updateOrganizationDecision(
  payload,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  const fields = {
    ...mutationBase(payload, csrfToken),
    kind: String(payload.kind),
  };
  if (payload.kind === "recommendation") {
    fields.change_id = String(payload.changeId);
    fields.decision = String(payload.decision);
  } else if (payload.kind === "choice") {
    fields.choice_id = String(payload.choiceId);
    fields.option_id = String(payload.optionId);
    if (payload.optionId === "choose-for-bin") {
      fields.copy_ids = JSON.stringify(payload.copyIds);
    }
  } else {
    throw new LociFlowApiError("invalid_request", "The organization request is invalid.");
  }
  return postJson(DECISIONS_URL, fields, fetchImpl);
}

export function updateRecommendationDecision(payload, fetchImpl = defaultFetch, csrfToken) {
  return updateOrganizationDecision(
    { ...payload, kind: "recommendation" },
    fetchImpl,
    csrfToken,
  );
}

export function updateChoiceDecision(payload, fetchImpl = defaultFetch, csrfToken) {
  return updateOrganizationDecision(
    { ...payload, kind: "choice" },
    fetchImpl,
    csrfToken,
  );
}

export async function acceptRecommendedChanges(
  payload,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  return postJson(
    DECISIONS_URL,
    {
      ...mutationBase(payload, csrfToken),
      kind: "bulk_recommendations",
      decision: "accept",
      expected_change_ids: JSON.stringify(payload.expectedChangeIds),
    },
    fetchImpl,
  );
}

export const acceptAllRecommendedChanges = acceptRecommendedChanges;

export async function approveOrganizationPlan(
  payload,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  return postJson(APPROVAL_URL, mutationBase(payload, csrfToken), fetchImpl);
}

export async function previewOrganizationMove(
  fetchImpl = defaultFetch,
  csrfToken,
) {
  return postJson(
    MOVEMENT_PREVIEW_URL,
    { csrf_token: requiredToken(csrfToken) },
    fetchImpl,
  );
}

export async function moveApprovedOrganization(
  payload,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  if (typeof payload?.confirmationToken !== "string" || !payload.confirmationToken) {
    throw new LociFlowApiError("invalid_movement_contract", "Move information is invalid.");
  }
  if (payload.confirmation !== MOVEMENT_CONFIRMATION) {
    throw new LociFlowApiError("invalid_confirmation", "The confirmation phrase does not match.");
  }
  return postJson(
    MOVEMENT_URL,
    {
      csrf_token: requiredToken(csrfToken),
      confirmation_token: payload.confirmationToken,
      confirmation: payload.confirmation,
    },
    fetchImpl,
  );
}

export async function getActivity(fetchImpl = defaultFetch) {
  const response = await fetchImpl(ACTIVITY_URL, {
    ...baseOptions(),
    method: "GET",
  });
  return responseJson(response);
}

export async function getStandards(fetchImpl = defaultFetch) {
  const response = await fetchImpl(STANDARDS_URL, {
    ...baseOptions(),
    method: "GET",
  });
  return responseJson(response);
}

export async function getStandardsCreationContext(
  expectedRevision,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
    throw new LociFlowApiError("invalid_standards_contract", "Standards information is invalid.");
  }
  return postJson(
    STANDARDS_CREATION_CONTEXT_URL,
    {
      csrf_token: requiredToken(csrfToken),
      expected_revision: String(expectedRevision),
    },
    fetchImpl,
  );
}

export async function setStandardActive(
  payload,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  if (
    typeof payload?.standardId !== "string"
    || !payload.standardId
    || !Number.isSafeInteger(payload.expectedRevision)
    || payload.expectedRevision < 0
    || typeof payload.active !== "boolean"
  ) {
    throw new LociFlowApiError("invalid_standards_contract", "Standards information is invalid.");
  }
  return postJson(
    STANDARDS_STATE_URL,
    {
      csrf_token: requiredToken(csrfToken),
      expected_revision: String(payload.expectedRevision),
      standard_id: payload.standardId,
      active: String(payload.active),
    },
    fetchImpl,
  );
}

export async function previewStandardCreation(
  payload,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  const presetType = payload?.presetType;
  if (
    !new Set(["keep_folder_unchanged", "keep_matching_files_together"]).has(presetType)
    || typeof payload.scopeId !== "string"
    || !payload.scopeId
    || !Number.isSafeInteger(payload.expectedRevision)
    || payload.expectedRevision < 0
  ) {
    throw new LociFlowApiError("invalid_standards_contract", "Standards information is invalid.");
  }
  const fields = {
    csrf_token: requiredToken(csrfToken),
    expected_revision: String(payload.expectedRevision),
    preset_type: presetType,
    scope_id: payload.scopeId,
  };
  if (presetType === "keep_matching_files_together") {
    if (
      !new Set(["course_code", "project_code"]).has(payload.matcherKind)
      || typeof payload.matcherValue !== "string"
      || !payload.matcherValue.trim()
    ) {
      throw new LociFlowApiError("invalid_standards_contract", "Standards information is invalid.");
    }
    fields.matcher_kind = payload.matcherKind;
    fields.matcher_value = payload.matcherValue;
  }
  return postJson(STANDARDS_PREVIEW_URL, fields, fetchImpl);
}

export async function saveStandardCreation(
  payload,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  if (
    typeof payload?.previewToken !== "string"
    || !payload.previewToken
    || !Number.isSafeInteger(payload.expectedRevision)
    || payload.expectedRevision < 0
  ) {
    throw new LociFlowApiError("invalid_standards_contract", "Standards information is invalid.");
  }
  return postJson(
    STANDARDS_URL,
    {
      csrf_token: requiredToken(csrfToken),
      expected_revision: String(payload.expectedRevision),
      preview_token: payload.previewToken,
    },
    fetchImpl,
  );
}

function requiredServerId(value, code = "invalid_duplicate_bin_contract") {
  if (!isCanonicalOperationId(value)) {
    throw new LociFlowApiError(code, "Duplicate Bin information is invalid.");
  }
  return value;
}

function requiredConfirmation(payload, expected, code) {
  if (typeof payload?.confirmationToken !== "string" || !payload.confirmationToken) {
    throw new LociFlowApiError(code, "Duplicate Bin confirmation is invalid.");
  }
  if (payload.confirmation !== expected) {
    throw new LociFlowApiError("invalid_confirmation", "The confirmation phrase does not match.");
  }
}

export function previewDuplicateBinMove(batchId, fetchImpl = defaultFetch, csrfToken) {
  return postJson(
    DUPLICATE_BIN_MOVEMENT_PREVIEW_URL,
    {
      csrf_token: requiredToken(csrfToken),
      batch_id: requiredServerId(batchId),
    },
    fetchImpl,
  );
}

export function confirmDuplicateBinMove(payload, fetchImpl = defaultFetch, csrfToken) {
  requiredConfirmation(
    payload,
    DUPLICATE_BIN_MOVEMENT_CONFIRMATION,
    "invalid_duplicate_bin_movement_contract",
  );
  return postJson(
    DUPLICATE_BIN_MOVEMENT_URL,
    {
      csrf_token: requiredToken(csrfToken),
      confirmation_token: payload.confirmationToken,
      confirmation: payload.confirmation,
    },
    fetchImpl,
  );
}

export async function getDuplicateBin(fetchImpl = defaultFetch) {
  const response = await fetchImpl(DUPLICATE_BIN_URL, {
    ...baseOptions(),
    method: "GET",
  });
  return responseJson(response);
}

export function previewDuplicateBinRestore(itemIds, fetchImpl = defaultFetch, csrfToken) {
  if (
    !Array.isArray(itemIds)
    || itemIds.length === 0
    || new Set(itemIds).size !== itemIds.length
  ) {
    throw new LociFlowApiError(
      "invalid_duplicate_bin_restore_contract",
      "Select at least one copy to restore.",
    );
  }
  itemIds.forEach((itemId) => requiredServerId(
    itemId,
    "invalid_duplicate_bin_restore_contract",
  ));
  return postJson(
    DUPLICATE_BIN_RESTORE_PREVIEW_URL,
    {
      csrf_token: requiredToken(csrfToken),
      item_ids: JSON.stringify(itemIds),
    },
    fetchImpl,
  );
}

export function confirmDuplicateBinRestore(payload, fetchImpl = defaultFetch, csrfToken) {
  requiredConfirmation(
    payload,
    DUPLICATE_BIN_RESTORE_CONFIRMATION,
    "invalid_duplicate_bin_restore_contract",
  );
  return postJson(
    DUPLICATE_BIN_RESTORE_URL,
    {
      csrf_token: requiredToken(csrfToken),
      confirmation_token: payload.confirmationToken,
      confirmation: payload.confirmation,
    },
    fetchImpl,
  );
}

export const fetchActivity = getActivity;

export async function previewActivityUndo(
  operationId,
  fetchImpl = defaultFetch,
  csrfToken,
) {
  const id = requiredOperationId(operationId);
  return postJson(
    `${ACTIVITY_URL}/${encodeURIComponent(id)}/undo/preview`,
    { csrf_token: requiredToken(csrfToken) },
    fetchImpl,
  );
}

export async function undoActivityOperation(
  operationId,
  confirmationToken,
  fetchImpl = defaultFetch,
  csrfToken,
  confirmation = UNDO_CONFIRMATION,
) {
  const id = requiredOperationId(operationId);
  if (typeof confirmationToken !== "string" || !confirmationToken) {
    throw new LociFlowApiError("invalid_activity_contract", "Activity information is invalid.");
  }
  if (!new Set([UNDO_CONFIRMATION, DUPLICATE_BIN_RESTORE_CONFIRMATION]).has(confirmation)) {
    throw new LociFlowApiError("invalid_activity_contract", "Activity information is invalid.");
  }
  return postJson(
    `${ACTIVITY_URL}/${encodeURIComponent(id)}/undo`,
    {
      csrf_token: requiredToken(csrfToken),
      confirmation_token: confirmationToken,
      confirmation,
    },
    fetchImpl,
  );
}

export function executeActivityUndo(payload, fetchImpl = defaultFetch, csrfToken) {
  return undoActivityOperation(
    payload.operationId,
    payload.confirmationToken,
    fetchImpl,
    csrfToken,
    payload.confirmation || UNDO_CONFIRMATION,
  );
}

export function createLociFlowApiClient(fetchImpl = defaultFetch) {
  return Object.freeze({
    getCurrentOrganization: () => getCurrentOrganization(fetchImpl),
    getCurrentRecovery: () => getCurrentRecovery(fetchImpl),
    createRecoveryCheckContext: (csrfToken) => (
      createRecoveryCheckContext(fetchImpl, csrfToken)
    ),
    checkRecoveryResult: (payload, csrfToken) => (
      checkRecoveryResult(payload, fetchImpl, csrfToken)
    ),
    startScan: (payload) => startScan(payload, fetchImpl),
    updateRecommendationDecision: (payload, csrfToken) => (
      updateRecommendationDecision(payload, fetchImpl, csrfToken)
    ),
    updateChoiceDecision: (payload, csrfToken) => (
      updateChoiceDecision(payload, fetchImpl, csrfToken)
    ),
    acceptRecommendedChanges: (payload, csrfToken) => (
      acceptRecommendedChanges(payload, fetchImpl, csrfToken)
    ),
    approveOrganizationPlan: (payload, csrfToken) => (
      approveOrganizationPlan(payload, fetchImpl, csrfToken)
    ),
    previewOrganizationMove: (csrfToken) => (
      previewOrganizationMove(fetchImpl, csrfToken)
    ),
    moveApprovedOrganization: (payload, csrfToken) => (
      moveApprovedOrganization(payload, fetchImpl, csrfToken)
    ),
    previewDuplicateBinMove: (batchId, csrfToken) => (
      previewDuplicateBinMove(batchId, fetchImpl, csrfToken)
    ),
    confirmDuplicateBinMove: (payload, csrfToken) => (
      confirmDuplicateBinMove(payload, fetchImpl, csrfToken)
    ),
    getDuplicateBin: () => getDuplicateBin(fetchImpl),
    previewDuplicateBinRestore: (itemIds, csrfToken) => (
      previewDuplicateBinRestore(itemIds, fetchImpl, csrfToken)
    ),
    confirmDuplicateBinRestore: (payload, csrfToken) => (
      confirmDuplicateBinRestore(payload, fetchImpl, csrfToken)
    ),
    getActivity: () => getActivity(fetchImpl),
    getStandards: () => getStandards(fetchImpl),
    getStandardsCreationContext: (expectedRevision, csrfToken) => (
      getStandardsCreationContext(expectedRevision, fetchImpl, csrfToken)
    ),
    setStandardActive: (payload, csrfToken) => (
      setStandardActive(payload, fetchImpl, csrfToken)
    ),
    previewStandardCreation: (payload, csrfToken) => (
      previewStandardCreation(payload, fetchImpl, csrfToken)
    ),
    saveStandardCreation: (payload, csrfToken) => (
      saveStandardCreation(payload, fetchImpl, csrfToken)
    ),
    previewActivityUndo: (operationId, csrfToken) => (
      previewActivityUndo(operationId, fetchImpl, csrfToken)
    ),
    undoActivityOperation: (operationId, confirmationToken, csrfToken, confirmation) => (
      undoActivityOperation(
        operationId,
        confirmationToken,
        fetchImpl,
        csrfToken,
        confirmation,
      )
    ),
  });
}
