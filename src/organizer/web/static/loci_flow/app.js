import {
  acceptRecommendedChanges,
  approveOrganizationPlan,
  executeActivityUndo,
  fetchActivity,
  fetchOrganizationSlice,
  OrganizationApiError,
  previewActivityUndo,
  updateOrganizationDecision,
} from "./api.js";
import {
  formatDate,
  renderApproval,
  renderActivity,
  renderHome,
  renderLoadState,
  renderOrganize,
  renderReady,
  renderSettings,
  renderUnintegrated,
} from "./views.js";

const POLL_INTERVAL_MS = 1500;
const ROUTES = new Set(["home", "organize", "duplicate-bin", "activity", "standards", "settings"]);
const state = {
  source: "backend_read_only",
  loadStatus: "loading",
  organizationSlice: null,
  selectedItemId: null,
  route: "home",
  theme: "light",
  busy: false,
  approvalOpen: false,
  showApprovedSummary: false,
  mutationError: "",
  duplicateDrafts: new Map(),
  activityStatus: "idle",
  activity: [],
  expandedActivityIds: new Set(),
  activityBusy: false,
  undoPreview: null,
  activityError: "",
};

let errorCode = "";
let pollTimer = null;
const app = document.querySelector("#app");
const liveRegion = document.querySelector("#status-live");

function routeFromHash() {
  const value = window.location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
  return ROUTES.has(value) ? value : "home";
}

function clearPoll() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function schedulePoll() {
  clearPoll();
  if (errorCode !== "scan_in_progress" || state.route !== "home") return;
  pollTimer = window.setTimeout(() => loadOrganization({ announce: false }), POLL_INTERVAL_MS);
}

function announce(message) {
  liveRegion.textContent = "";
  window.requestAnimationFrame(() => { liveRegion.textContent = message; });
}

function firstVisibleId() {
  return state.organizationSlice?.recommended_changes[0]?.id
    || state.organizationSlice?.choices_needed[0]?.id
    || null;
}

function ensureValidSelection() {
  if (!state.organizationSlice || !state.selectedItemId) return;
  const exists = state.organizationSlice.recommended_changes.some((entry) => entry.id === state.selectedItemId)
    || state.organizationSlice.choices_needed.some((entry) => entry.id === state.selectedItemId)
    || state.organizationSlice.kept_by_standards.some((entry) => `standard:${entry.item.id}` === state.selectedItemId);
  if (!exists) state.selectedItemId = null;
}

function syncShell() {
  const titles = {
    home: "Home", organize: "Organize", "duplicate-bin": "Duplicate Bin",
    activity: "Activity", standards: "Standards", settings: "Settings",
  };
  document.title = `${titles[state.route]} · Loci Flow`;
  document.documentElement.dataset.theme = state.theme;
  document.querySelectorAll("[data-route]").forEach((link) => {
    if (link.dataset.route === state.route) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-action='set-theme']").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.themeValue === state.theme ? "true" : "false");
  });
  const title = document.querySelector("[data-folder-title]");
  const freshness = document.querySelector("[data-folder-freshness]");
  const count = document.querySelector("[data-organize-count]");
  if (state.organizationSlice) {
    title.textContent = state.organizationSlice.scope.display_name;
    freshness.textContent = `Last checked ${formatDate(state.organizationSlice.freshness.last_checked_at)}`;
    const total = state.organizationSlice.review?.summary.choices_remaining
      ?? state.organizationSlice.summary.recommended_changes + state.organizationSlice.summary.choices_needed;
    count.textContent = String(total);
    count.hidden = total === 0;
  } else {
    title.textContent = "Selected folder";
    freshness.textContent = state.loadStatus === "loading" ? "Loading organization information…" : "Organization information unavailable";
    count.hidden = true;
  }
}

function render() {
  syncShell();
  app.setAttribute("aria-busy", state.loadStatus === "loading" || state.busy || state.activityBusy ? "true" : "false");
  if (state.route === "activity") {
    app.replaceChildren(renderActivity(state.activity, {
      status: state.activityStatus,
      expandedIds: state.expandedActivityIds,
      busy: state.activityBusy,
      preview: state.undoPreview,
      errorMessage: state.activityError,
    }));
    return;
  }
  if (state.loadStatus !== "ready" || !state.organizationSlice) {
    app.replaceChildren(renderLoadState(state.loadStatus, errorCode));
    return;
  }
  ensureValidSelection();
  if (state.route === "home") app.replaceChildren(renderHome(state.organizationSlice));
  else if (state.route === "organize" && state.organizationSlice.review.status === "ready_to_move") {
    app.replaceChildren(renderReady(state.organizationSlice, state.showApprovedSummary));
  } else if (state.route === "organize" && state.approvalOpen) {
    app.replaceChildren(renderApproval(state.organizationSlice, state.busy, state.mutationError));
  } else if (state.route === "organize") {
    app.replaceChildren(renderOrganize(state.organizationSlice, state.selectedItemId, {
      busy: state.busy,
      duplicateDrafts: state.duplicateDrafts,
      errorMessage: state.mutationError,
    }));
  } else if (state.route === "settings") app.replaceChildren(renderSettings(state.theme));
  else app.replaceChildren(renderUnintegrated(state.route));
}

async function loadActivity({ announce: shouldAnnounce = false } = {}) {
  state.activityStatus = "loading";
  state.activityError = "";
  render();
  try {
    const result = await fetchActivity();
    state.activity = result.activity;
    state.activityStatus = "ready";
    state.expandedActivityIds = new Set(
      [...state.expandedActivityIds].filter((id) => state.activity.some((entry) => entry.id === id)),
    );
    if (shouldAnnounce) announce("Activity loaded.");
  } catch (error) {
    state.activity = [];
    state.activityStatus = "error";
    state.activityError = error instanceof Error ? error.message : "Activity is unavailable.";
    if (shouldAnnounce) announce("Activity is unavailable.");
  }
  render();
}

async function openUndoPreview(operationId) {
  if (state.activityBusy) return;
  state.activityBusy = true;
  state.activityError = "";
  render();
  try {
    state.undoPreview = await previewActivityUndo(operationId);
  } catch (error) {
    state.activityError = error instanceof Error ? error.message : "Undo is unavailable.";
    await loadActivity();
    announce("Undo is unavailable for this move.");
  } finally {
    state.activityBusy = false;
    render();
    if (state.undoPreview) window.requestAnimationFrame(() => document.querySelector("[data-action='confirm-undo']")?.focus());
  }
}

async function confirmUndo() {
  if (state.activityBusy || !state.undoPreview) return;
  state.activityBusy = true;
  state.activityError = "";
  render();
  try {
    await executeActivityUndo({
      operationId: state.undoPreview.operation_id,
      confirmationToken: state.undoPreview.confirmation_token,
    });
    state.undoPreview = null;
    await loadActivity();
    announce("File move undone. Activity has been updated.");
  } catch (error) {
    state.activityError = error instanceof Error ? error.message : "Undo could not be completed.";
    announce("Undo could not be completed.");
  } finally {
    state.activityBusy = false;
    render();
  }
}

async function loadOrganization({ announce: shouldAnnounce = true } = {}) {
  clearPoll();
  state.loadStatus = "loading";
  state.mutationError = "";
  errorCode = "";
  render();
  try {
    state.organizationSlice = await fetchOrganizationSlice();
    state.loadStatus = "ready";
    state.approvalOpen = false;
    state.duplicateDrafts.clear();
    if (shouldAnnounce) announce("Current organization information loaded.");
  } catch (error) {
    state.organizationSlice = null;
    errorCode = error instanceof OrganizationApiError ? error.code : "organization_unavailable";
    state.loadStatus = ["scan_not_ready", "scan_in_progress", "scan_failed"].includes(errorCode)
      ? errorCode
      : "error";
    if (shouldAnnounce) announce("Organization information is unavailable.");
  }
  render();
  schedulePoll();
}

async function mutate(operation, successMessage) {
  if (state.busy) return;
  state.busy = true;
  state.mutationError = "";
  render();
  try {
    state.organizationSlice = await operation();
    state.duplicateDrafts.clear();
    announce(successMessage);
  } catch (error) {
    const code = error instanceof OrganizationApiError ? error.code : "organization_unavailable";
    const reloadCodes = new Set([
      "scan_generation_mismatch", "decision_revision_mismatch", "source_changed",
      "overwrite_detected", "unsafe_destination", "duplicate_group_changed",
    ]);
    if (reloadCodes.has(code)) {
      errorCode = code;
      state.organizationSlice = null;
      state.loadStatus = "error";
    } else {
      state.mutationError = error instanceof Error ? error.message : "The organization choice was not saved.";
    }
    announce("The organization request was not accepted.");
  } finally {
    state.busy = false;
    render();
  }
}

function requestContext() {
  return {
    scanGeneration: state.organizationSlice.freshness.scan_generation,
    expectedRevision: state.organizationSlice.review.decision_revision,
  };
}

function focusSelected() {
  window.requestAnimationFrame(() => {
    const target = Array.from(document.querySelectorAll("[data-select-id]"))
      .find((element) => element.dataset.selectId === state.selectedItemId);
    target?.focus();
  });
}

function continueOrganizing() {
  state.selectedItemId = firstVisibleId();
  if (state.route === "organize") {
    render();
    focusSelected();
    return;
  }
  window.location.hash = "/organize";
}

document.addEventListener("click", (event) => {
  const control = event.target.closest("[data-action]");
  if (control) {
    const action = control.dataset.action;
    if (action === "history-back") window.history.back();
    else if (action === "history-forward") window.history.forward();
    else if (action === "set-theme") {
      state.theme = control.dataset.themeValue === "dark" ? "dark" : "light";
      render();
      announce(`${state.theme === "dark" ? "Dark" : "Light"} theme enabled.`);
    } else if (action === "continue-organizing") continueOrganizing();
    else if (action === "retry-load") loadOrganization();
    else if (action === "retry-activity") loadActivity({ announce: true });
    else if (action === "toggle-activity") {
      const id = control.dataset.activityId;
      if (state.expandedActivityIds.has(id)) state.expandedActivityIds.delete(id);
      else state.expandedActivityIds.add(id);
      render();
    } else if (action === "expand-all-activity") {
      state.expandedActivityIds = new Set(state.activity.map((entry) => entry.id));
      render();
    } else if (action === "collapse-all-activity") {
      state.expandedActivityIds.clear();
      render();
    } else if (action === "preview-undo") openUndoPreview(control.dataset.operationId);
    else if (action === "cancel-undo") {
      state.undoPreview = null;
      state.activityError = "";
      render();
    } else if (action === "confirm-undo") confirmUndo();
    else if (action === "recommendation-decision") {
      mutate(() => updateOrganizationDecision({
        ...requestContext(), kind: "recommendation",
        changeId: control.dataset.changeId, decision: control.dataset.decision,
      }), "Recommendation decision saved.");
    } else if (action === "choice-option") {
      mutate(() => updateOrganizationDecision({
        ...requestContext(), kind: "choice",
        choiceId: control.dataset.choiceId, optionId: control.dataset.optionId,
      }), "Organization choice saved.");
    } else if (action === "start-duplicate-choice") {
      const existing = state.organizationSlice.review.choices.find((item) => item.choice_id === control.dataset.choiceId);
      state.duplicateDrafts.set(control.dataset.choiceId, new Set(existing?.selected_copy_ids || []));
      render();
    } else if (action === "save-duplicate-choice") {
      const selected = state.duplicateDrafts.get(control.dataset.choiceId) || new Set();
      mutate(() => updateOrganizationDecision({
        ...requestContext(), kind: "choice", choiceId: control.dataset.choiceId,
        optionId: "choose-for-bin", copyIds: [...selected],
      }), "Duplicate choice saved.");
    } else if (action === "accept-all-recommended") {
      const expectedChangeIds = state.organizationSlice.review.recommendations
        .filter((item) => item.decision === "undecided")
        .map((item) => item.change_id);
      mutate(() => acceptRecommendedChanges({ ...requestContext(), expectedChangeIds }), "Recommended changes accepted.");
    } else if (action === "open-approval") {
      state.approvalOpen = true;
      state.mutationError = "";
      render();
    } else if (action === "close-approval") {
      state.approvalOpen = false;
      state.mutationError = "";
      render();
    } else if (action === "approve-organization") {
      mutate(() => approveOrganizationPlan(requestContext()), "Organization plan approved.");
    } else if (action === "toggle-approved-summary") {
      state.showApprovedSummary = !state.showApprovedSummary;
      render();
    }
    return;
  }
  const selected = event.target.closest("[data-select-id]");
  if (selected) {
    state.selectedItemId = selected.dataset.selectId;
    render();
    focusSelected();
  }
});

document.addEventListener("change", (event) => {
  const input = event.target.closest("[data-action='toggle-duplicate-copy']");
  if (!input) return;
  const choice = state.organizationSlice.choices_needed.find((item) => item.id === input.dataset.choiceId);
  const selected = state.duplicateDrafts.get(input.dataset.choiceId) || new Set();
  if (input.checked) selected.add(input.dataset.copyId);
  else selected.delete(input.dataset.copyId);
  if (choice && selected.size >= choice.copies.length) {
    selected.delete(input.dataset.copyId);
    input.checked = false;
    announce("Leave at least one duplicate copy unselected.");
  }
  state.duplicateDrafts.set(input.dataset.choiceId, selected);
  render();
});

window.addEventListener("hashchange", () => {
  clearPoll();
  state.route = routeFromHash();
  state.approvalOpen = false;
  render();
  if (state.route === "activity") loadActivity();
  if (state.route === "organize" && state.selectedItemId) focusSelected();
  schedulePoll();
  document.querySelector("#main-content")?.focus();
});
window.addEventListener("beforeunload", clearPoll);

state.route = routeFromHash();
render();
loadOrganization();
if (state.route === "activity") loadActivity();
