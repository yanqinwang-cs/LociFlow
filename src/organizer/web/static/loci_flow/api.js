const CURRENT_ORGANIZATION_URL = "/api/organization/current";
const DECISIONS_URL = "/api/organization/decisions";
const APPROVAL_URL = "/api/organization/approval";
const SCAN_URL = "/scan?surface=home";

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

export async function getCurrentOrganization(fetchImpl = defaultFetch) {
  const response = await fetchImpl(CURRENT_ORGANIZATION_URL, {
    ...baseOptions(),
    method: "GET",
  });
  return responseJson(response);
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

export function createLociFlowApiClient(fetchImpl = defaultFetch) {
  return Object.freeze({
    getCurrentOrganization: () => getCurrentOrganization(fetchImpl),
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
  });
}
