import { createLociFlowApiClient } from "./api.js";
import {
  activityErrorMessage,
  createBackendDataSource,
  isUncertainMovementError,
  isUncertainUndoError,
  movementErrorMessage,
} from "./data_source.js";
import { renderDialog, renderRoute } from "./views.js";
import {
  buildDialogViewModel,
  buildMovementConfirmationViewModel,
  buildProductionStageB1Contract,
  buildRouteViewModel,
  buildShellViewModel,
} from "./view_models.js";

const routes = new Set(["home", "organize", "duplicate-bin", "activity", "standards", "settings"]);
const elements = {
  app: document.querySelector("#app"),
  main: document.querySelector("#main-content"),
  nav: document.querySelector(".primary-nav"),
  live: document.querySelector("#status-live"),
  dialog: document.querySelector("#dialog"),
  dialogEyebrow: document.querySelector("#dialog-eyebrow"),
  dialogTitle: document.querySelector("#dialog-title"),
  dialogDescription: document.querySelector("#dialog-description"),
  dialogContent: document.querySelector("#dialog-content"),
  dialogActions: document.querySelector("#dialog-actions"),
};

const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
const dataSource = createBackendDataSource({
  apiClient: createLociFlowApiClient(),
  csrfToken,
});
const state = {
  route: "home",
  theme: "light",
  settingsSection: "appearance",
  loadStatus: "loading",
  backendSnapshot: { status: "loading", backendState: null },
  selectedItemId: null,
  selectedRowIds: [],
  activeDialog: null,
  pendingMutation: false,
  scanPolling: false,
  workflowOverride: null,
  destinationDrafts: {},
  duplicateDrafts: {},
  activity: {
    loadStatus: "idle",
    entries: [],
    expandedIds: new Set(),
    activeUndoPreview: null,
    pendingUndo: false,
    errorMessage: null,
    noticeMessage: null,
    loadVersion: 0,
    previewVersion: 0,
  },
  movement: {
    previewStatus: "idle",
    preview: null,
    confirmationText: "",
    executionPending: false,
    execution: null,
    errorCode: null,
    errorMessage: null,
    previewVersion: 0,
  },
};
const viewSource = {
  getState() {
    return {
      route: state.route,
      theme: state.theme,
      selectedItemId: state.selectedItemId,
      settingsSection: state.settingsSection,
      settings: {
        notifications: {
          organizationReady: false,
          moveCompleted: false,
          moveFailed: false,
        },
      },
      stageB1: buildProductionStageB1Contract({
        snapshot: state.backendSnapshot,
        workflowOverride: state.workflowOverride,
        destinationDrafts: state.destinationDrafts,
        duplicateDrafts: state.duplicateDrafts,
        activity: state.activity,
        movement: state.movement,
      }),
    };
  },
  duplicateChoiceStatus(choiceId) {
    const choice = currentDuplicateChoice(choiceId);
    if (!choice) return { valid: false };
    const draft = duplicateDraft(choiceId);
    if (draft.optionId === "keep-all") return { valid: true };
    const selected = new Set(draft.selectedCopyIds);
    return {
      valid: selected.size > 0 && selected.size < choice.copies.length,
    };
  },
};

function currentDuplicateChoice(choiceId) {
  return viewSource.getState().stageB1.organization.choicesNeeded.find(
    (item) => item.id === choiceId && item.choiceType === "duplicate",
  ) || null;
}

let dialogPayload = {};
let dialogTrigger = null;

function routeFromHash() {
  const value = window.location.hash.startsWith("#/") ? window.location.hash.slice(2) : "home";
  return routes.has(value) ? value : "home";
}

function announce(message) {
  elements.live.textContent = "";
  window.requestAnimationFrame(() => { elements.live.textContent = message; });
}

function workflowOverride(status, message, action, label, staleKind = "revision") {
  return {
    stage: "stale",
    status,
    message,
    staleKind,
    primaryAction: { label, action, enabled: true },
  };
}

function allItemIds() {
  const backend = state.backendSnapshot.backendState;
  if (!backend) return new Set();
  return new Set([
    ...backend.recommended_changes.map((item) => item.id),
    ...backend.choices_needed.map((item) => item.id),
    ...backend.kept_by_standards.map((item) => item.item.id),
  ]);
}

function movementPlanKey(snapshot) {
  const backend = snapshot?.backendState;
  if (!backend) return null;
  return JSON.stringify([
    backend.scope.display_name,
    backend.freshness.scan_generation,
    backend.review.decision_revision,
    backend.review.status,
    backend.review.approval?.approved_at || null,
  ]);
}

function applySnapshot(snapshot, { preserveSelection = true } = {}) {
  const previousPlanKey = movementPlanKey(state.backendSnapshot);
  const nextPlanKey = movementPlanKey(snapshot);
  state.backendSnapshot = snapshot;
  state.loadStatus = "ready";
  if (previousPlanKey && previousPlanKey !== nextPlanKey) {
    discardMovementConfirmation();
    state.movement.execution = null;
    state.movement.errorCode = null;
    state.movement.errorMessage = null;
  }
  if (!preserveSelection || !allItemIds().has(state.selectedItemId)) {
    state.selectedItemId = null;
  }
  state.selectedRowIds = state.selectedRowIds.filter((id) => allItemIds().has(id));
}

function syncShell() {
  const shell = buildShellViewModel(viewSource);
  document.documentElement.dataset.theme = state.theme;
  document.querySelectorAll('[data-action="set-theme"]').forEach((control) => {
    control.setAttribute("aria-pressed", String(control.dataset.themeValue === state.theme));
  });
  elements.nav.querySelectorAll("[data-route]").forEach((link) => {
    link.setAttribute("aria-current", link.dataset.route === state.route ? "page" : "false");
  });
  const organizeCount = elements.nav.querySelector("[data-organize-count]");
  const binCount = elements.nav.querySelector("[data-bin-count]");
  const folderTitle = document.querySelector(".folder-title strong");
  const folderFreshness = document.querySelector(".folder-title span");
  if (folderTitle) folderTitle.textContent = shell.scope.displayName;
  if (folderFreshness) folderFreshness.textContent = shell.toolbarFreshnessLabel;
  if (organizeCount) organizeCount.textContent = String(shell.organizeCount);
  if (binCount) binCount.textContent = String(shell.duplicateBinCount);
  const activityBusy = state.route === "activity"
    && (state.activity.loadStatus === "loading" || state.activity.pendingUndo);
  const movementBusy = state.movement.previewStatus === "loading"
    || state.movement.executionPending;
  elements.app.inert = state.pendingMutation || movementBusy;
  elements.app.setAttribute("aria-busy", String(
    state.pendingMutation || state.loadStatus === "loading" || activityBusy || movementBusy,
  ));
  elements.dialog.inert = state.pendingMutation;
}

function replaceActivityEmptyState(title, description, { retry = false } = {}) {
  const empty = elements.app.querySelector(".activity-list .empty-state");
  if (!empty) return;
  const heading = empty.querySelector("h2");
  const copy = empty.querySelector("p");
  if (heading) heading.textContent = title;
  if (copy) copy.textContent = description;
  if (retry) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.action = "retry-activity";
    button.textContent = "Try again";
    empty.append(button);
  }
}

function decorateActivityPage() {
  if (state.route !== "activity") return;
  if (state.activity.loadStatus === "loading") {
    replaceActivityEmptyState("Loading Activity…", "Loading completed file moves.");
  } else if (state.activity.loadStatus === "error") {
    replaceActivityEmptyState(
      "Activity could not be loaded.",
      state.activity.errorMessage || "Try again.",
      { retry: true },
    );
  } else if (state.activity.loadStatus === "ready" && !state.activity.entries.length) {
    replaceActivityEmptyState("No file moves recorded.", "Completed moves will appear here.");
  }
  if (state.activity.noticeMessage && state.activity.loadStatus === "ready") {
    const list = elements.app.querySelector(".activity-list");
    if (list) {
      const notice = document.createElement("p");
      notice.className = "activity-notice";
      notice.setAttribute("role", "status");
      notice.textContent = state.activity.noticeMessage;
      list.prepend(notice);
    }
  }
  const undo = elements.app.querySelector('[data-action="undo-move"]');
  if (undo) undo.disabled = state.activity.pendingUndo;
}

function render(options = {}) {
  state.route = routeFromHash();
  elements.app.innerHTML = renderRoute(buildRouteViewModel(viewSource, state.route), state.route);
  elements.app.setAttribute("aria-busy", "false");
  decorateActivityPage();
  document.title = `${state.route === "duplicate-bin" ? "Duplicate Bin" : state.route[0].toUpperCase() + state.route.slice(1)} · Loci Flow`;
  syncShell();
  if (options.focusChanges) {
    window.requestAnimationFrame(() => document.querySelector("#changes-to-confirm")?.focus({ preventScroll: false }));
  }
  if (options.focusMain) elements.main.focus({ preventScroll: true });
  if (options.focusDetails) {
    window.requestAnimationFrame(() => document.querySelector("#details-heading")?.focus({ preventScroll: true }));
  }
}

function navigate(route, options = {}) {
  if (!routes.has(route)) return;
  const previousRoute = state.route;
  if (previousRoute === "activity" && route !== "activity") discardActivityPreview();
  if (previousRoute === "organize" && route !== "organize") discardMovementConfirmation();
  state.route = route;
  if (window.location.hash !== `#/${route}`) window.location.hash = `#/${route}`;
  render(options);
  if (route === "activity" && previousRoute !== "activity") loadActivity();
}

function setDialog(type, payload = {}) {
  const view = renderDialog(buildDialogViewModel(viewSource), type, payload);
  state.activeDialog = type;
  dialogPayload = payload;
  elements.dialogEyebrow.textContent = view.eyebrow;
  elements.dialogTitle.textContent = view.title;
  elements.dialogDescription.textContent = view.description;
  elements.dialogContent.innerHTML = view.content;
  elements.dialogActions.innerHTML = view.actions;
}

function openDialog(type, payload = {}, trigger = document.activeElement) {
  dialogTrigger = trigger;
  setDialog(type, payload);
  if (!elements.dialog.open) elements.dialog.showModal();
  window.requestAnimationFrame(() => {
    elements.dialog.querySelector("input:checked, select, button:not([disabled])")?.focus();
  });
}

function refreshDialog(focusAttribute, focusValue) {
  setDialog(state.activeDialog, dialogPayload);
  window.requestAnimationFrame(() => {
    const control = [...elements.dialog.querySelectorAll(`[${focusAttribute}]`)]
      .find((item) => item.getAttribute(focusAttribute) === focusValue);
    control?.focus();
  });
}

function closeDialog({ restoreFocus = true } = {}) {
  if (state.activeDialog === "activity-undo") state.activity.activeUndoPreview = null;
  if (state.activeDialog === "movement-confirmation") resetMovementConfirmation();
  if (elements.dialog.open) elements.dialog.close();
  state.activeDialog = null;
  dialogPayload = {};
  if (restoreFocus) dialogTrigger?.focus?.();
  dialogTrigger = null;
}

function resetMovementConfirmation() {
  state.movement.previewVersion += 1;
  state.movement.previewStatus = "idle";
  state.movement.preview = null;
  state.movement.confirmationText = "";
}

function discardMovementConfirmation() {
  resetMovementConfirmation();
  if (state.activeDialog !== "movement-confirmation") return;
  if (elements.dialog.open) elements.dialog.close();
  state.activeDialog = null;
  dialogPayload = {};
  dialogTrigger = null;
}

function setMovementDialog(preview, trigger) {
  const view = buildMovementConfirmationViewModel(
    preview,
    state.movement.confirmationText,
  );
  state.activeDialog = "movement-confirmation";
  state.movement.previewStatus = "ready";
  state.movement.preview = preview;
  dialogPayload = {};
  dialogTrigger = trigger;
  elements.dialogEyebrow.textContent = view.eyebrow;
  elements.dialogTitle.textContent = view.title;
  elements.dialogDescription.textContent = view.description;
  elements.dialogContent.replaceChildren();
  if (view.duplicateMessage) {
    const duplicateMessage = document.createElement("p");
    duplicateMessage.textContent = view.duplicateMessage;
    elements.dialogContent.append(duplicateMessage);
  }
  const instruction = document.createElement("p");
  instruction.textContent = view.instruction;
  const field = document.createElement("label");
  field.className = "builder-field";
  const label = document.createElement("span");
  label.textContent = "Confirmation phrase";
  const input = document.createElement("input");
  input.type = "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.dataset.action = "movement-confirmation-input";
  input.value = view.confirmationText;
  field.append(label, input);
  elements.dialogContent.append(instruction, field);
  elements.dialogActions.replaceChildren();
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.dataset.action = "cancel-move";
  cancel.textContent = view.cancelLabel;
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "button button-primary";
  confirm.dataset.action = "confirm-move";
  confirm.textContent = view.confirmLabel;
  confirm.disabled = !view.canConfirm;
  elements.dialogActions.append(cancel, confirm);
  if (!elements.dialog.open) elements.dialog.showModal();
  window.requestAnimationFrame(() => input.focus());
}

function setMovementDialogPending() {
  elements.dialog.querySelectorAll("button, input, select").forEach((control) => {
    control.disabled = true;
  });
  const confirm = elements.dialog.querySelector('[data-action="confirm-move"]');
  if (confirm) confirm.textContent = "Moving files…";
}

function discardActivityPreview() {
  state.activity.previewVersion += 1;
  state.activity.activeUndoPreview = null;
  state.activity.pendingUndo = false;
  if (state.activeDialog === "activity-undo") closeDialog();
}

function setUndoDialog(preview, trigger) {
  state.activeDialog = "activity-undo";
  state.activity.activeUndoPreview = preview;
  dialogPayload = {};
  dialogTrigger = trigger;
  elements.dialogEyebrow.textContent = "Activity";
  elements.dialogTitle.textContent = "Undo this move?";
  const noun = preview.files_to_restore === 1 ? "file" : "files";
  const destination = preview.files_to_restore === 1
    ? "its previous folder"
    : "their previous folders";
  elements.dialogDescription.textContent = `${preview.files_to_restore} ${noun} will return to ${destination}.`;
  elements.dialogContent.replaceChildren();
  elements.dialogActions.replaceChildren();
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.dataset.action = "close-dialog";
  cancel.textContent = "Cancel";
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "button button-primary";
  confirm.dataset.action = "confirm-activity-undo";
  confirm.textContent = "Undo move";
  elements.dialogActions.append(cancel, confirm);
  if (!elements.dialog.open) elements.dialog.showModal();
  window.requestAnimationFrame(() => cancel.focus());
}

function setUndoDialogPending(value) {
  state.activity.pendingUndo = value;
  elements.dialog.querySelectorAll("button, input, select").forEach((control) => {
    control.disabled = value;
  });
  syncShell();
}

function setPending(value) {
  state.pendingMutation = value;
  syncShell();
}

async function loadCurrent({ clearOverride = true } = {}) {
  if (clearOverride) state.workflowOverride = null;
  try {
    applySnapshot(await dataSource.loadCurrentState());
  } catch {
    state.loadStatus = "error";
    state.workflowOverride = workflowOverride(
      "Organization information could not be loaded.",
      "Try loading the latest organization information again.",
      "reload-choices",
      "Try again",
    );
  }
  render();
}

async function loadActivity({ noticeMessage = null, force = false } = {}) {
  if (state.route !== "activity" && !force) return;
  const visible = state.route === "activity";
  const version = ++state.activity.loadVersion;
  state.activity.loadStatus = "loading";
  state.activity.entries = [];
  state.activity.errorMessage = null;
  state.activity.noticeMessage = noticeMessage;
  if (visible) render();
  try {
    const payload = await dataSource.loadActivity();
    if (version !== state.activity.loadVersion) return;
    state.activity.entries = payload.activity;
    const ids = new Set(payload.activity.map((entry) => entry.id));
    state.activity.expandedIds = new Set(
      [...state.activity.expandedIds].filter((id) => ids.has(id)),
    );
    state.activity.loadStatus = "ready";
  } catch {
    if (version !== state.activity.loadVersion) return;
    state.activity.entries = [];
    state.activity.expandedIds = new Set();
    state.activity.loadStatus = "error";
    state.activity.errorMessage = "Try again.";
  }
  if (state.route === "activity") render();
  else syncShell();
}

function toggleActivity(activityId) {
  const ids = new Set(state.activity.expandedIds);
  if (ids.has(activityId)) ids.delete(activityId);
  else ids.add(activityId);
  state.activity.expandedIds = ids;
  render();
}

async function previewActivityUndo(target) {
  if (state.activity.pendingUndo) return;
  const entry = state.activity.entries.find((item) => item.undoable === true);
  if (!entry) return;
  const version = ++state.activity.previewVersion;
  state.activity.pendingUndo = true;
  state.activity.noticeMessage = null;
  render();
  const trigger = elements.app.querySelector('[data-action="undo-move"]') || target;
  try {
    const preview = await dataSource.previewUndo(entry.id);
    if (version !== state.activity.previewVersion || state.route !== "activity") return;
    state.activity.pendingUndo = false;
    render();
    setUndoDialog(preview, elements.app.querySelector('[data-action="undo-move"]') || trigger);
  } catch (error) {
    if (version !== state.activity.previewVersion || state.route !== "activity") return;
    state.activity.pendingUndo = false;
    const message = activityErrorMessage(error);
    announce(message);
    await loadActivity({ noticeMessage: message });
  }
}

async function confirmActivityUndo() {
  const preview = state.activity.activeUndoPreview;
  if (!preview || state.activity.pendingUndo) return;
  setUndoDialogPending(true);
  announce("Restoring files…");
  state.activity.activeUndoPreview = null;
  try {
    await dataSource.confirmUndo(preview.operation_id, preview.confirmation_token);
    closeDialog({ restoreFocus: false });
    await loadActivity({ noticeMessage: "Files restored." });
    announce("Files restored.");
  } catch (error) {
    const uncertain = isUncertainUndoError(error);
    const message = uncertain
      ? "The result could not be loaded. Reload Activity before trying again."
      : activityErrorMessage(error);
    closeDialog({ restoreFocus: false });
    await loadActivity({ noticeMessage: message });
    announce(message);
  } finally {
    state.activity.pendingUndo = false;
    if (state.route === "activity") render();
    else syncShell();
  }
}

function currentExecution() {
  const backendExecution = state.backendSnapshot.backendState?.execution || null;
  if (backendExecution && backendExecution.status !== "ready") return backendExecution;
  return state.movement.execution || backendExecution;
}

function executionAnnouncement(execution) {
  if (!execution) return "The move result could not be confirmed.";
  if (execution.status === "verified") {
    return execution.files_moved === 1 ? "1 file moved." : `${execution.files_moved} files moved.`;
  }
  if (new Set(["prepared", "moving", "moved_unverified"]).has(execution.status)) {
    return "The file move is still in progress.";
  }
  if (execution.status === "partial_movement") return "The move partly completed.";
  if (execution.status === "movement_failed") return "The files did not move.";
  return "The move needs attention.";
}

async function previewMove(target) {
  if (
    state.movement.previewStatus === "loading"
    || state.movement.executionPending
    || state.pendingMutation
    || state.activity.pendingUndo
  ) return;
  discardMovementConfirmation();
  state.movement.errorCode = null;
  state.movement.errorMessage = null;
  const version = ++state.movement.previewVersion;
  state.movement.previewStatus = "loading";
  render();
  announce("Preparing the approved move…");
  try {
    const preview = await dataSource.previewMovement();
    if (version !== state.movement.previewVersion || state.route !== "organize") return;
    state.movement.previewStatus = "ready";
    state.movement.preview = preview;
    state.movement.confirmationText = "";
    render();
    const trigger = elements.app.querySelector('[data-action="preview-move"]') || target;
    setMovementDialog(preview, trigger);
  } catch (error) {
    if (version !== state.movement.previewVersion) return;
    state.movement.previewStatus = "error";
    state.movement.preview = null;
    state.movement.errorCode = error?.code || "request_failed";
    state.movement.errorMessage = movementErrorMessage(error);
    announce(state.movement.errorMessage);
    render({ focusMain: true });
  }
}

async function confirmMove() {
  const preview = state.movement.preview;
  if (
    !preview
    || state.movement.executionPending
    || state.movement.confirmationText !== preview.confirmation
  ) return;
  state.movement.executionPending = true;
  state.movement.errorCode = null;
  state.movement.errorMessage = null;
  setMovementDialogPending();
  syncShell();
  announce("Moving files…");
  let requestError = null;
  try {
    const result = await dataSource.confirmMovement(
      preview.confirmation_token,
      state.movement.confirmationText,
    );
    state.movement.execution = result.operation;
  } catch (error) {
    requestError = error;
  }
  closeDialog({ restoreFocus: false });
  state.movement.executionPending = false;
  try {
    applySnapshot(await dataSource.loadCurrentState());
  } catch {
    // The authoritative execution response remains available if reloading fails.
  }
  const execution = currentExecution();
  const recovered = execution && execution.status !== "ready" && execution.status !== "not_ready";
  if (requestError && !recovered) {
    state.movement.errorCode = requestError?.code || "request_failed";
    state.movement.errorMessage = movementErrorMessage(requestError);
  } else {
    state.movement.errorCode = null;
    state.movement.errorMessage = null;
  }
  if (execution?.operation_id) {
    await loadActivity({ force: true });
  }
  if (requestError && isUncertainMovementError(requestError) && !recovered) {
    announce("The move result could not be confirmed. Load the latest organization before trying again.");
  } else if (requestError && !recovered) {
    announce(state.movement.errorMessage);
  } else {
    announce(executionAnnouncement(execution));
  }
  render({ focusMain: true });
}

async function checkFiles() {
  if (state.scanPolling || state.pendingMutation) return;
  discardMovementConfirmation();
  state.movement.execution = null;
  state.movement.errorCode = null;
  state.movement.errorMessage = null;
  state.workflowOverride = null;
  state.scanPolling = true;
  applySnapshot({ status: "checking", backendState: null });
  render({ focusMain: true });
  announce("Checking files…");
  try {
    const settled = await dataSource.startScan((snapshot) => {
      applySnapshot(snapshot, { preserveSelection: false });
      render();
    });
    applySnapshot(settled, { preserveSelection: false });
    state.destinationDrafts = {};
    state.duplicateDrafts = {};
    announce(settled.status === "completed" ? "File check complete." : "File check failed.");
  } catch {
    applySnapshot({ status: "failed", backendState: null }, { preserveSelection: false });
    announce("File check failed.");
  } finally {
    state.scanPolling = false;
    render({ focusMain: true });
  }
}

async function handleMutation(operation, successMessage, { close = false } = {}) {
  if (state.pendingMutation) return;
  setPending(true);
  announce("Saving organization choice…");
  try {
    const snapshot = await operation();
    applySnapshot(snapshot);
    state.workflowOverride = null;
    state.destinationDrafts = {};
    state.duplicateDrafts = {};
    if (close) closeDialog({ restoreFocus: false });
    announce(successMessage);
    render({ focusDetails: state.route === "organize" });
  } catch (error) {
    const code = error?.code;
    if (code === "scan_generation_mismatch") {
      state.selectedItemId = null;
      state.selectedRowIds = [];
      state.destinationDrafts = {};
      state.duplicateDrafts = {};
      state.workflowOverride = workflowOverride(
        "Files have changed",
        "Check the folder again before continuing.",
        "check-again",
        "Check again",
        "generation",
      );
    } else if (code === "decision_revision_mismatch") {
      await reloadAfterConflict();
      state.workflowOverride = workflowOverride(
        "Organization choices were updated",
        "Reload the latest choices.",
        "reload-choices",
        "Reload choices",
      );
    } else if (code === "recommendation_set_changed") {
      await reloadAfterConflict();
      state.workflowOverride = workflowOverride(
        "Organization changes were updated.",
        "Review the latest changes.",
        "reload-choices",
        "Review changes",
      );
    } else if (new Set(["unknown_change_id", "unknown_choice_id", "invalid_option"]).has(code)) {
      await reloadAfterConflict();
      state.workflowOverride = workflowOverride(
        "This choice is no longer available.",
        "Reload the latest choices.",
        "reload-choices",
        "Reload choices",
      );
    } else {
      state.workflowOverride = workflowOverride(
        "Organization information could not be loaded.",
        "No organization choice was changed.",
        "reload-choices",
        "Try again",
      );
    }
    announce(state.workflowOverride.status);
    render({ focusMain: true });
  } finally {
    setPending(false);
  }
}

async function reloadAfterConflict() {
  try {
    applySnapshot(await dataSource.loadCurrentState());
  } catch {
    // The safe conflict state below remains visible if reloading also fails.
  }
}

function recommendationDecision(target) {
  return handleMutation(
    () => dataSource.decideRecommendation(target.dataset.itemId, target.dataset.decision),
    target.dataset.decision === "keep" ? "The file will stay here." : "Change accepted. No file moved.",
  );
}

function acceptAllRecommendations() {
  const backend = state.backendSnapshot.backendState;
  if (!backend) return;
  const decisions = new Map(backend.review.recommendations.map((item) => [item.change_id, item.decision]));
  const pending = backend.recommended_changes
    .map((item) => item.id)
    .filter((id) => decisions.get(id) === "undecided");
  return handleMutation(
    () => dataSource.acceptRecommendations(pending),
    `${pending.length} recommended ${pending.length === 1 ? "change" : "changes"} accepted. No file moved.`,
  );
}

function selectDestination(target) {
  state.destinationDrafts = {
    ...state.destinationDrafts,
    [target.dataset.itemId]: target.dataset.optionId,
  };
  refreshDialog("data-option-id", target.dataset.optionId);
}

function confirmDestination(target) {
  const choiceId = target.dataset.itemId;
  const optionId = state.destinationDrafts[choiceId];
  if (!optionId) return;
  return handleMutation(
    () => dataSource.decideChoice(choiceId, optionId),
    "Folder choice saved. No file moved.",
    { close: true },
  );
}

function duplicateDraft(choiceId) {
  const backend = state.backendSnapshot.backendState;
  const choiceState = backend?.review.choices.find((item) => item.choice_id === choiceId);
  return state.duplicateDrafts[choiceId] || {
    optionId: choiceState?.option_id || "keep-all",
    selectedCopyIds: [...(choiceState?.selected_copy_ids || [])],
  };
}

function setDuplicateDecision(target) {
  const choiceId = target.dataset.itemId;
  const current = duplicateDraft(choiceId);
  state.duplicateDrafts = {
    ...state.duplicateDrafts,
    [choiceId]: {
      optionId: target.dataset.duplicateDecision === "keep_all" ? "keep-all" : "choose-for-bin",
      selectedCopyIds: current.selectedCopyIds,
    },
  };
  refreshDialog("data-duplicate-decision", target.dataset.duplicateDecision);
}

function toggleDuplicateCopy(target) {
  const choiceId = target.dataset.itemId;
  const current = duplicateDraft(choiceId);
  const selected = new Set(current.selectedCopyIds);
  if (selected.has(target.dataset.copyId)) selected.delete(target.dataset.copyId);
  else selected.add(target.dataset.copyId);
  state.duplicateDrafts = {
    ...state.duplicateDrafts,
    [choiceId]: { optionId: "choose-for-bin", selectedCopyIds: [...selected] },
  };
  refreshDialog("data-copy-id", target.dataset.copyId);
}

function confirmDuplicate(target) {
  const choiceId = target.dataset.itemId;
  const draft = duplicateDraft(choiceId);
  const choice = currentDuplicateChoice(choiceId);
  if (!choice) return invalidateStaleDuplicateChoice(choiceId);
  const selected = new Set(draft.selectedCopyIds);
  if (
    draft.optionId === "choose-for-bin"
    && (selected.size === 0 || selected.size >= choice.copies.length)
  ) {
    announce("Choose at least one copy, but not every copy.");
    return setDialog("duplicate", { itemId: choiceId });
  }
  return handleMutation(
    () => dataSource.decideChoice(
      choiceId,
      draft.optionId,
      draft.optionId === "choose-for-bin" ? [...selected] : undefined,
    ),
    draft.optionId === "keep-all" ? "All copies will stay where they are." : "Duplicate choice saved. No file moved.",
    { close: true },
  );
}

async function invalidateStaleDuplicateChoice(choiceId) {
  const drafts = { ...state.duplicateDrafts };
  delete drafts[choiceId];
  state.duplicateDrafts = drafts;
  closeDialog({ restoreFocus: false });
  announce("This duplicate choice is no longer available. Reloading the latest organization.");
  await loadCurrent();
}

function actionFor(target) {
  const action = target.dataset.action;
  if (!action) return;
  if (action === "history-back") return window.history.back();
  if (action === "history-forward") return window.history.forward();
  if (action === "none") return;
  if (action === "set-theme") {
    state.theme = target.dataset.themeValue === "dark" ? "dark" : "light";
    announce(`${target.textContent.trim()} appearance selected.`);
    return render();
  }
  if (action === "continue-organizing") return navigate("organize", { focusChanges: true });
  if (action === "open-duplicate-bin") return navigate("duplicate-bin", { focusMain: true });
  if (action === "open-activity") return navigate("activity", { focusMain: true });
  if (action === "open-standards") return navigate("standards", { focusMain: true });
  if (action === "check-files" || action === "check-again") return checkFiles();
  if (action === "reload-choices") {
    discardMovementConfirmation();
    state.movement.execution = null;
    state.movement.errorCode = null;
    state.movement.errorMessage = null;
    return loadCurrent();
  }
  if (action === "retry-activity") return loadActivity();
  if (action === "toggle-activity") return toggleActivity(target.dataset.activityId);
  if (action === "expand-all-activity") {
    state.activity.expandedIds = new Set(state.activity.entries.map((entry) => entry.id));
    return render();
  }
  if (action === "collapse-all-activity") {
    state.activity.expandedIds = new Set();
    return render();
  }
  if (action === "undo-move") return previewActivityUndo(target);
  if (action === "confirm-activity-undo") return confirmActivityUndo();
  if (action === "preview-move") return previewMove(target);
  if (action === "confirm-move") return confirmMove();
  if (action === "cancel-move") return closeDialog();
  if (action === "set-style") {
    announce("Only the current deterministic organization plan is available.");
    return;
  }
  if (action === "set-settings-section") {
    if (target.dataset.section !== "appearance") {
      announce("Only appearance is available in this stage.");
      return;
    }
    state.settingsSection = "appearance";
    return render();
  }
  if (action === "toggle-notification") {
    announce("Notifications are not connected in this stage.");
    return;
  }
  if (action === "accept-contract-recommended") return acceptAllRecommendations();
  if (action === "activate-contract-item") {
    state.selectedItemId = target.dataset.itemId;
    return render({ focusDetails: true });
  }
  if (action === "decide-contract-recommendation") return recommendationDecision(target);
  if (action === "open-destination-choice") {
    state.selectedItemId = target.dataset.itemId;
    const backendState = state.backendSnapshot.backendState?.review.choices.find(
      (item) => item.choice_id === target.dataset.itemId,
    );
    if (backendState?.option_id) {
      state.destinationDrafts = {
        ...state.destinationDrafts,
        [target.dataset.itemId]: backendState.option_id,
      };
    }
    return openDialog("destination", { itemId: target.dataset.itemId }, target);
  }
  if (action === "select-destination-option") return selectDestination(target);
  if (action === "confirm-destination-choice") return confirmDestination(target);
  if (action === "open-duplicate-choice") {
    state.selectedItemId = target.dataset.itemId;
    return openDialog("duplicate", { itemId: target.dataset.itemId }, target);
  }
  if (action === "set-duplicate-decision") return setDuplicateDecision(target);
  if (action === "toggle-duplicate-copy") return toggleDuplicateCopy(target);
  if (action === "confirm-duplicate-choice") return confirmDuplicate(target);
  if (action === "review-and-approve") return openDialog("approval", {}, target);
  if (action === "approve-organization") {
    return handleMutation(
      () => dataSource.approve(),
      "Organization approved. No files have moved yet.",
      { close: true },
    );
  }
  if (action === "close-dialog") return closeDialog();
}

document.addEventListener("click", (event) => {
  const routeLink = event.target.closest("[data-route]");
  if (routeLink?.dataset.route === "activity" && state.route === "activity") {
    event.preventDefault();
    loadActivity();
    return;
  }
  const target = event.target.closest("[data-action]");
  if (target) actionFor(target);
});

document.addEventListener("input", (event) => {
  const input = event.target.closest('[data-action="movement-confirmation-input"]');
  if (!input || state.activeDialog !== "movement-confirmation") return;
  state.movement.confirmationText = input.value;
  const view = buildMovementConfirmationViewModel(
    state.movement.preview,
    state.movement.confirmationText,
  );
  const confirm = elements.dialog.querySelector('[data-action="confirm-move"]');
  if (confirm) confirm.disabled = !view.canConfirm || state.movement.executionPending;
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && elements.dialog.open) {
    event.preventDefault();
    if (state.activeDialog === "activity-undo" && state.activity.pendingUndo) return;
    if (state.activeDialog === "movement-confirmation" && state.movement.executionPending) return;
    closeDialog();
    return;
  }
  if (elements.dialog.open && event.key === "Tab") {
    const focusable = [...elements.dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
    return;
  }
  const activeRow = event.target.closest("[data-change-row]");
  if (!activeRow || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
  const rows = [...document.querySelectorAll("[data-change-row]")];
  const index = rows.indexOf(activeRow);
  const next = event.key === "ArrowDown" ? rows[index + 1] : rows[index - 1];
  if (next) {
    event.preventDefault();
    next.querySelector(".change-name")?.focus();
  }
});

window.addEventListener("hashchange", () => {
  const previousRoute = state.route;
  const nextRoute = routeFromHash();
  if (previousRoute === "activity" && nextRoute !== "activity") discardActivityPreview();
  if (previousRoute === "organize" && nextRoute !== "organize") discardMovementConfirmation();
  state.route = nextRoute;
  render({ focusMain: true });
  if (nextRoute === "activity" && previousRoute !== "activity") loadActivity();
});
window.addEventListener("beforeunload", () => dataSource.dispose());

if (!window.location.hash) window.location.hash = "#/home";
state.route = routeFromHash();
render();
loadCurrent();
if (state.route === "activity") loadActivity();
