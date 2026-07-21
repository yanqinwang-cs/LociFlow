import { createLociFlowApiClient } from "./api.js";
import {
  activityErrorMessage,
  createBackendDataSource,
  duplicateBinErrorMessage,
  isUncertainDuplicateBinError,
  isUncertainMovementError,
  isUncertainUndoError,
  movementErrorMessage,
  standardsErrorMessage,
} from "./data_source.js";
import { renderDialog, renderRoute } from "./views.js";
import {
  buildDialogViewModel,
  buildDuplicateBinConfirmationViewModel,
  buildMovementConfirmationViewModel,
  buildProductionStageB1Contract,
  buildRecoveryDialogViewModel,
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
const recoveryAttention = (
  document.querySelector('meta[name="recovery-attention"]')?.content === "true"
);
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
  duplicateMovement: {
    previewStatus: "idle",
    preview: null,
    confirmationText: "",
    executionPending: false,
    execution: null,
    errorCode: null,
    errorMessage: null,
    previewVersion: 0,
  },
  duplicateBin: {
    loadStatus: "loading",
    items: [],
    selectedIds: new Set(),
    previewStatus: "idle",
    preview: null,
    confirmationText: "",
    restorePending: false,
    errorMessage: null,
    noticeMessage: null,
    loadVersion: 0,
    previewVersion: 0,
  },
  standards: {
    loadStatus: "idle",
    revision: null,
    items: [],
    presets: [],
    scopeChoices: [],
    creationAvailable: false,
    creationMessage: null,
    mutationPending: false,
    pendingStandardId: null,
    errorMessage: null,
    conflictMessage: null,
    freshScanRequired: false,
    noticeMessage: null,
    loadVersion: 0,
    builder: {
      open: false,
      presetId: "",
      scopeId: "",
      matcherKind: "course_code",
      matcherValue: "",
      status: "idle",
      preview: null,
      previewToken: null,
      expiresAt: null,
      contextExpiresAt: null,
      contextRevision: null,
      errorMessage: null,
    },
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
        duplicateMovement: state.duplicateMovement,
        duplicateBin: state.duplicateBin,
        standards: state.standards,
        recoveryAttention,
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
let activityLoadPromise = null;
let duplicateBinLoadPromise = null;
let standardsLoadPromise = null;
let recoveryLoadPending = false;

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
    backend.duplicate_batch?.id || null,
    backend.duplicate_batch?.status || null,
  ]);
}

function applySnapshot(snapshot, { preserveSelection = true } = {}) {
  const previousPlanKey = movementPlanKey(state.backendSnapshot);
  const nextPlanKey = movementPlanKey(snapshot);
  state.backendSnapshot = snapshot;
  state.loadStatus = "ready";
  if (previousPlanKey && previousPlanKey !== nextPlanKey) {
    discardMovementConfirmation();
    discardDuplicateMovementConfirmation();
    state.movement.execution = null;
    state.movement.errorCode = null;
    state.movement.errorMessage = null;
    state.duplicateMovement.execution = null;
    state.duplicateMovement.errorCode = null;
    state.duplicateMovement.errorMessage = null;
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
  const bannerBadge = document.querySelector(".prototype-notice .prototype-badge");
  const bannerStatus = document.querySelector(".prototype-notice span:last-child");
  const movementStatus = document.querySelector(".sample-label");
  if (folderTitle) folderTitle.textContent = shell.scope.displayName;
  if (folderFreshness) folderFreshness.textContent = shell.toolbarFreshnessLabel;
  if (bannerBadge && shell.bannerStatusLabel) bannerBadge.textContent = "Status";
  if (bannerStatus && shell.bannerStatusLabel) bannerStatus.textContent = shell.bannerStatusLabel;
  if (movementStatus) movementStatus.textContent = shell.movementStatusLabel;
  if (organizeCount) organizeCount.textContent = String(shell.organizeCount);
  if (binCount) binCount.textContent = String(shell.duplicateBinCount);
  const activityBusy = state.route === "activity"
    && (state.activity.loadStatus === "loading" || state.activity.pendingUndo);
  const movementBusy = state.movement.previewStatus === "loading"
    || state.movement.executionPending
    || state.duplicateMovement.previewStatus === "loading"
    || state.duplicateMovement.executionPending
    || state.duplicateBin.previewStatus === "loading"
    || state.duplicateBin.restorePending;
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
  if (previousRoute === "organize" && route !== "organize") {
    discardDuplicateMovementConfirmation();
  }
  if (previousRoute === "duplicate-bin" && route !== "duplicate-bin") {
    discardDuplicateRestoreConfirmation();
  }
  if (previousRoute === "standards" && route !== "standards") {
    discardStandardBuilder({ close: true });
  }
  if (state.activeDialog === "recovery" && route !== previousRoute) {
    closeDialog({ restoreFocus: false });
  }
  state.route = route;
  if (window.location.hash !== `#/${route}`) window.location.hash = `#/${route}`;
  render(options);
  if (route === "activity" && previousRoute !== "activity") loadActivity();
  if (route === "duplicate-bin" && previousRoute !== "duplicate-bin") {
    loadDuplicateBin({ refresh: true });
  }
  if (route === "standards" && previousRoute !== "standards") loadStandards();
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
  if (state.activeDialog === "duplicate-movement-confirmation") {
    resetDuplicateMovementConfirmation();
  }
  if (state.activeDialog === "duplicate-restore-confirmation") {
    resetDuplicateRestoreConfirmation();
  }
  if (state.activeDialog === "standard-builder") resetStandardBuilder();
  if (elements.dialog.open) elements.dialog.close();
  state.activeDialog = null;
  dialogPayload = {};
  if (restoreFocus) dialogTrigger?.focus?.();
  dialogTrigger = null;
}

const SAFE_RECOVERY_FAILURE = Object.freeze({
  state: "cannot_continue_automatically",
  title: "Loci Flow can’t continue this file operation automatically.",
  message: "New file changes remain paused. Get help before changing these files.",
  affected_items: 0,
});

async function reviewRecovery(trigger) {
  if (recoveryLoadPending) return;
  recoveryLoadPending = true;
  trigger.disabled = true;
  const openingRoute = state.route;
  try {
    const projection = await dataSource.loadRecoveryProjection();
    if (state.route === openingRoute && state.route === "home") {
      openRecoveryDialog(projection, trigger);
    }
  } catch {
    if (state.route === openingRoute && state.route === "home") {
      openRecoveryDialog(SAFE_RECOVERY_FAILURE, trigger);
    }
  } finally {
    recoveryLoadPending = false;
    trigger.disabled = false;
  }
}

function openRecoveryDialog(projection, trigger = document.activeElement) {
  const view = buildRecoveryDialogViewModel(projection);
  state.activeDialog = "recovery";
  dialogPayload = projection;
  dialogTrigger = trigger;
  elements.dialogEyebrow.textContent = view.eyebrow;
  elements.dialogTitle.textContent = view.title;
  elements.dialogDescription.textContent = view.description;
  elements.dialogContent.replaceChildren();
  if (view.affectedItems > 0) {
    const count = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = String(view.affectedItems);
    count.append(
      strong,
      ` ${view.affectedItems === 1 ? "item needs" : "items need"} review.`,
    );
    elements.dialogContent.append(count);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "button button-secondary";
  close.dataset.action = "close-dialog";
  close.textContent = view.closeLabel;
  elements.dialogActions.replaceChildren(close);
  if (!elements.dialog.open) elements.dialog.showModal();
  window.requestAnimationFrame(() => close.focus());
}

function resetStandardBuilder() {
  dataSource.clearStandardsCreationContext?.();
  state.standards.scopeChoices = [];
  state.standards.builder = {
    open: false,
    presetId: "",
    scopeId: "",
    matcherKind: "course_code",
    matcherValue: "",
    status: "idle",
    preview: null,
    previewToken: null,
    expiresAt: null,
    contextExpiresAt: null,
    contextRevision: null,
    errorMessage: null,
  };
}

function discardStandardBuilder({ close = false } = {}) {
  resetStandardBuilder();
  if (!close || state.activeDialog !== "standard-builder") return;
  if (elements.dialog.open) elements.dialog.close();
  state.activeDialog = null;
  dialogPayload = {};
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

function resetDuplicateMovementConfirmation() {
  state.duplicateMovement.previewVersion += 1;
  state.duplicateMovement.previewStatus = "idle";
  state.duplicateMovement.preview = null;
  state.duplicateMovement.confirmationText = "";
}

function discardDuplicateMovementConfirmation() {
  resetDuplicateMovementConfirmation();
  if (state.activeDialog !== "duplicate-movement-confirmation") return;
  if (elements.dialog.open) elements.dialog.close();
  state.activeDialog = null;
  dialogPayload = {};
  dialogTrigger = null;
}

function resetDuplicateRestoreConfirmation() {
  state.duplicateBin.previewVersion += 1;
  state.duplicateBin.previewStatus = "idle";
  state.duplicateBin.preview = null;
  state.duplicateBin.confirmationText = "";
}

function discardDuplicateRestoreConfirmation() {
  resetDuplicateRestoreConfirmation();
  if (state.activeDialog !== "duplicate-restore-confirmation") return;
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

function setDuplicateBinDialog(preview, kind, trigger) {
  const restore = kind === "restore";
  const owner = restore ? state.duplicateBin : state.duplicateMovement;
  const type = restore ? "duplicate-restore-confirmation" : "duplicate-movement-confirmation";
  const view = buildDuplicateBinConfirmationViewModel(
    preview,
    owner.confirmationText,
    kind,
  );
  state.activeDialog = type;
  owner.previewStatus = "ready";
  owner.preview = preview;
  dialogPayload = {};
  dialogTrigger = trigger;
  elements.dialogEyebrow.textContent = view.eyebrow;
  elements.dialogTitle.textContent = view.title;
  elements.dialogDescription.textContent = view.description;
  elements.dialogContent.replaceChildren();
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
  input.dataset.action = restore
    ? "duplicate-restore-confirmation-input"
    : "duplicate-movement-confirmation-input";
  input.value = view.confirmationText;
  field.append(label, input);
  elements.dialogContent.append(instruction, field);
  elements.dialogActions.replaceChildren();
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.dataset.action = restore ? "cancel-duplicate-restore" : "cancel-duplicate-move";
  cancel.textContent = view.cancelLabel;
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "button button-primary";
  confirm.dataset.action = restore ? "confirm-duplicate-restore" : "confirm-duplicate-move";
  confirm.textContent = view.confirmLabel;
  confirm.disabled = !view.canConfirm;
  elements.dialogActions.append(cancel, confirm);
  if (!elements.dialog.open) elements.dialog.showModal();
  window.requestAnimationFrame(() => input.focus());
}

function setDuplicateBinDialogPending(kind) {
  elements.dialog.querySelectorAll("button, input, select").forEach((control) => {
    control.disabled = true;
  });
  const action = kind === "restore" ? "confirm-duplicate-restore" : "confirm-duplicate-move";
  const confirm = elements.dialog.querySelector(`[data-action="${action}"]`);
  if (confirm) confirm.textContent = kind === "restore" ? "Restoring…" : "Moving copies…";
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
  const copies = preview.copies_to_restore !== undefined;
  const count = copies ? preview.copies_to_restore : preview.files_to_restore;
  elements.dialogTitle.textContent = copies ? "Restore these copies?" : "Undo this move?";
  const noun = count === 1 ? (copies ? "copy" : "file") : (copies ? "copies" : "files");
  const destination = count === 1
    ? "its previous folder"
    : "their previous folders";
  elements.dialogDescription.textContent = `${count} ${noun} will return to ${destination}.`;
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
  confirm.textContent = copies ? "Restore copies" : "Undo move";
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
  if (clearOverride && !state.standards.freshScanRequired) state.workflowOverride = null;
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

async function loadStandards({ refresh = false } = {}) {
  if (standardsLoadPromise) {
    await standardsLoadPromise.catch(() => {});
    return;
  }
  if (state.standards.loadStatus === "ready" && !refresh) {
    if (state.route === "standards") render();
    return;
  }
  const version = ++state.standards.loadVersion;
  state.standards.loadStatus = "loading";
  state.standards.errorMessage = null;
  if (state.route === "standards") render();
  standardsLoadPromise = dataSource.loadStandards();
  try {
    const payload = await standardsLoadPromise;
    if (version !== state.standards.loadVersion) return;
    const revisionChanged = state.standards.revision !== null
      && state.standards.revision !== payload.revision;
    if (revisionChanged && state.standards.builder.open) {
      discardStandardBuilder({ close: true });
    }
    state.standards.revision = payload.revision;
    state.standards.items = payload.items;
    state.standards.presets = payload.presets;
    state.standards.creationAvailable = payload.creation_available;
    state.standards.creationMessage = payload.creation_message;
    state.standards.loadStatus = "ready";
  } catch (error) {
    if (version !== state.standards.loadVersion) return;
    state.standards.revision = null;
    state.standards.items = [];
    state.standards.presets = [];
    state.standards.scopeChoices = [];
    state.standards.creationAvailable = false;
    state.standards.creationMessage = "Standards could not be loaded.";
    state.standards.loadStatus = "error";
    state.standards.errorMessage = standardsErrorMessage(error);
  } finally {
    standardsLoadPromise = null;
  }
  if (state.route === "standards") render();
  else syncShell();
}

async function setStandardActive(target) {
  if (state.standards.mutationPending || state.standards.loadStatus !== "ready") return;
  const item = state.standards.items.find(
    (candidate) => candidate.id === target.dataset.standardId && candidate.mutable,
  );
  const active = target.dataset.active === "true";
  if (!item || !new Set(["true", "false"]).has(target.dataset.active)) return;
  state.standards.mutationPending = true;
  state.standards.pendingStandardId = item.id;
  state.standards.errorMessage = null;
  state.standards.conflictMessage = null;
  state.standards.noticeMessage = null;
  announce(`Saving ${item.name}…`);
  render();
  try {
    const result = await dataSource.setStandardActive(
      item.id,
      active,
      state.standards.revision,
    );
    state.standards.freshScanRequired = result.fresh_scan_required
      || state.standards.freshScanRequired;
    if (result.fresh_scan_required) {
      state.selectedItemId = null;
      state.selectedRowIds = [];
      state.destinationDrafts = {};
      state.duplicateDrafts = {};
      discardMovementConfirmation();
      discardDuplicateMovementConfirmation();
      state.workflowOverride = workflowOverride(
        "Check files again",
        "Standards changed. Check the folder again before organizing.",
        "check-again",
        "Check again",
        "generation",
      );
    }
    await loadStandards({ refresh: true });
    announce(active ? "Standard turned on. Check files again before organizing." : "Standard paused. Check files again before organizing.");
  } catch (error) {
    const reloadCodes = new Set([
      "standard_revision_mismatch",
      "standard_conflict",
      "unknown_standard",
      "immutable_standard",
    ]);
    if (reloadCodes.has(error?.code)) await loadStandards({ refresh: true });
    state.standards.conflictMessage = standardsErrorMessage(error);
    announce(state.standards.conflictMessage);
  } finally {
    state.standards.mutationPending = false;
    state.standards.pendingStandardId = null;
    render();
  }
}

async function openStandardBuilder(trigger) {
  if (
    state.standards.loadStatus !== "ready"
    || !state.standards.creationAvailable
    || state.standards.mutationPending
  ) {
    announce(state.standards.creationMessage || "A Standard cannot be added right now.");
    return;
  }
  resetStandardBuilder();
  state.standards.conflictMessage = null;
  state.standards.noticeMessage = null;
  state.standards.mutationPending = true;
  state.standards.errorMessage = null;
  announce("Loading folder choices…");
  render();
  try {
    const context = await dataSource.loadStandardsCreationContext(
      state.standards.revision,
    );
    if (context.revision !== state.standards.revision) {
      throw new Error("Standards changed while folder choices loaded.");
    }
    state.standards.scopeChoices = context.scope_choices;
    state.standards.builder.open = true;
    state.standards.builder.contextExpiresAt = context.expires_at;
    state.standards.builder.contextRevision = context.revision;
    openDialog("standard-builder", {}, trigger);
  } catch (error) {
    resetStandardBuilder();
    state.standards.errorMessage = standardsErrorMessage(error);
    announce(state.standards.errorMessage);
  } finally {
    state.standards.mutationPending = false;
    render();
  }
}

function standardBuilderInputComplete(builder) {
  const matching = builder.presetId === "keep_matching_files_together";
  return Boolean(
    builder.presetId
    && builder.scopeId
    && (!matching || (builder.matcherKind && builder.matcherValue.trim())),
  );
}

function invalidateStandardBuilderPreview() {
  const builder = state.standards.builder;
  builder.status = "idle";
  builder.preview = null;
  builder.previewToken = null;
  builder.expiresAt = null;
  builder.errorMessage = null;
}

function syncStandardBuilderInputControls() {
  elements.dialogContent?.querySelectorAll?.(
    ".interpretation-result, .activity-notice",
  ).forEach((item) => item.remove());
  const preview = elements.dialogActions?.querySelector?.(
    '[data-action="preview-standard"]',
  );
  const save = elements.dialogActions?.querySelector?.(
    '[data-action="save-standard"]',
  );
  if (preview) preview.disabled = !standardBuilderInputComplete(state.standards.builder);
  if (save) save.disabled = true;
}

function updateStandardBuilderField(control) {
  if (state.activeDialog !== "standard-builder") return;
  const builder = state.standards.builder;
  if (new Set(["preview_loading", "save_pending"]).has(builder.status)) return;
  const field = control.dataset.standardBuilderField;
  if (field === "preset") builder.presetId = control.value;
  else if (field === "scope") builder.scopeId = control.value;
  else if (field === "matcher-kind") builder.matcherKind = control.value;
  else if (field === "matcher-value") builder.matcherValue = control.value;
  else return;
  if (field === "preset" && builder.presetId !== "keep_matching_files_together") {
    builder.matcherValue = "";
  }
  invalidateStandardBuilderPreview();
  if (field === "matcher-value") {
    syncStandardBuilderInputControls();
    return;
  }
  refreshDialog("id", control.id);
}

async function previewStandard() {
  const builder = state.standards.builder;
  if (
    state.activeDialog !== "standard-builder"
    || new Set(["preview_loading", "save_pending"]).has(builder.status)
  ) return;
  builder.status = "preview_loading";
  builder.preview = null;
  builder.previewToken = null;
  builder.errorMessage = null;
  setDialog("standard-builder");
  announce("Preparing Standard preview…");
  try {
    const result = await dataSource.previewStandardCreation(
      {
        presetId: builder.presetId,
        scopeId: builder.scopeId,
        matcherKind: builder.matcherKind,
        matcherValue: builder.matcherValue,
      },
      state.standards.revision,
    );
    builder.status = "preview_ready";
    builder.preview = result.preview;
    builder.previewToken = result.preview_token;
    builder.expiresAt = result.expires_at;
    announce("Standard preview ready.");
    setDialog("standard-builder");
  } catch (error) {
    builder.status = error?.code === "preview_expired" ? "preview_expired" : "preview_attention";
    builder.preview = null;
    builder.previewToken = null;
    builder.expiresAt = null;
    builder.errorMessage = standardsErrorMessage(error);
    const reloadCodes = new Set([
      "standard_revision_mismatch", "standard_conflict", "folder_scope_stale",
      "wrong_root_scope", "active_plan_conflict", "standards_storage_unavailable",
    ]);
    announce(builder.errorMessage);
    if (reloadCodes.has(error?.code)) {
      discardStandardBuilder({ close: true });
      await loadStandards({ refresh: true });
    } else if (state.activeDialog === "standard-builder") {
      setDialog("standard-builder");
    }
  }
}

async function saveStandard() {
  const builder = state.standards.builder;
  if (
    state.activeDialog !== "standard-builder"
    || builder.status !== "preview_ready"
    || !builder.previewToken
  ) return;
  builder.status = "save_pending";
  builder.errorMessage = null;
  setDialog("standard-builder");
  announce("Saving Standard…");
  try {
    const result = await dataSource.saveStandardCreation(
      builder.previewToken,
      state.standards.revision,
    );
    state.standards.freshScanRequired = result.fresh_scan_required
      || state.standards.freshScanRequired;
    if (result.fresh_scan_required) {
      state.selectedItemId = null;
      state.selectedRowIds = [];
      state.destinationDrafts = {};
      state.duplicateDrafts = {};
      discardMovementConfirmation();
      discardDuplicateMovementConfirmation();
      state.workflowOverride = workflowOverride(
        "Check files again",
        "Standards changed. Check the folder again before organizing.",
        "check-again",
        "Check again",
        "generation",
      );
    }
    discardStandardBuilder({ close: true });
    await loadStandards({ refresh: true });
    state.standards.noticeMessage = result.save_message;
    announce(result.fresh_scan_required
      ? `${result.save_message} Check files again before organizing.`
      : result.save_message);
  } catch (error) {
    const message = standardsErrorMessage(error);
    const repreviewCodes = new Set(["preview_expired", "preview_superseded"]);
    const reloadCodes = new Set([
      "standard_revision_mismatch", "standard_conflict", "folder_scope_stale",
      "wrong_root_scope", "active_plan_conflict", "standards_storage_unavailable",
    ]);
    if (repreviewCodes.has(error?.code)) {
      builder.status = "preview_expired";
      builder.preview = null;
      builder.previewToken = null;
      builder.expiresAt = null;
      builder.errorMessage = message;
      if (state.activeDialog === "standard-builder") setDialog("standard-builder");
    } else if (reloadCodes.has(error?.code)) {
      discardStandardBuilder({ close: true });
      await loadStandards({ refresh: true });
      state.standards.conflictMessage = message;
    } else {
      builder.status = "preview_attention";
      builder.errorMessage = message;
      if (state.activeDialog === "standard-builder") setDialog("standard-builder");
    }
    announce(message);
  }
  render();
}

async function loadActivity({ noticeMessage = null, force = false, refresh = false } = {}) {
  if (state.route !== "activity" && !force) return;
  if (activityLoadPromise) {
    if (!refresh && noticeMessage === null) return activityLoadPromise;
    await activityLoadPromise.catch(() => {});
    return loadActivity({ noticeMessage, force, refresh: true });
  }
  const visible = state.route === "activity";
  if (state.activity.loadStatus === "ready" && !refresh && noticeMessage === null) {
    if (visible || state.route === "home") render();
    else syncShell();
    return;
  }
  const version = ++state.activity.loadVersion;
  state.activity.loadStatus = "loading";
  state.activity.entries = [];
  state.activity.errorMessage = null;
  state.activity.noticeMessage = noticeMessage;
  if (visible) render();
  try {
    activityLoadPromise = dataSource.loadActivity();
    const payload = await activityLoadPromise;
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
  } finally {
    activityLoadPromise = null;
  }
  if (state.route === "activity" || state.route === "home") render();
  else syncShell();
}

async function loadDuplicateBin({ noticeMessage = null, refresh = false } = {}) {
  if (duplicateBinLoadPromise) {
    if (!refresh && noticeMessage === null) return duplicateBinLoadPromise;
    await duplicateBinLoadPromise.catch(() => {});
    return loadDuplicateBin({ noticeMessage, refresh: true });
  }
  if (state.duplicateBin.loadStatus === "ready" && !refresh && noticeMessage === null) {
    if (state.route === "duplicate-bin" || state.route === "home") render();
    else syncShell();
    return;
  }
  const version = ++state.duplicateBin.loadVersion;
  state.duplicateBin.loadStatus = "loading";
  state.duplicateBin.errorMessage = null;
  state.duplicateBin.noticeMessage = noticeMessage;
  if (state.route === "duplicate-bin") render();
  try {
    duplicateBinLoadPromise = dataSource.loadDuplicateBin();
    const payload = await duplicateBinLoadPromise;
    if (version !== state.duplicateBin.loadVersion) return;
    state.duplicateBin.items = payload.items;
    const available = new Set(
      payload.items.filter((item) => item.restore_available).map((item) => item.id),
    );
    const selected = new Set(
      [...state.duplicateBin.selectedIds].filter((id) => available.has(id)),
    );
    if (
      selected.size !== state.duplicateBin.selectedIds.size
      || [...selected].some((id) => !state.duplicateBin.selectedIds.has(id))
    ) {
      state.duplicateBin.selectedIds = selected;
      discardDuplicateRestoreConfirmation();
    }
    state.duplicateBin.loadStatus = "ready";
  } catch (error) {
    if (version !== state.duplicateBin.loadVersion) return;
    state.duplicateBin.items = [];
    state.duplicateBin.selectedIds = new Set();
    state.duplicateBin.loadStatus = "error";
    state.duplicateBin.errorMessage = duplicateBinErrorMessage(error);
    discardDuplicateRestoreConfirmation();
  } finally {
    duplicateBinLoadPromise = null;
  }
  if (state.route === "duplicate-bin" || state.route === "home") render();
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
  const requestedId = target?.dataset.activityId;
  const entry = state.activity.entries.find(
    (item) => item.id === requestedId && item.undoable === true,
  );
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
    await dataSource.confirmUndo(
      preview.operation_id,
      preview.confirmation_token,
      preview.confirmation,
    );
    closeDialog({ restoreFocus: false });
    await Promise.allSettled([
      loadActivity({ noticeMessage: "Files restored.", force: true, refresh: true }),
      loadDuplicateBin({ refresh: true }),
    ]);
    if (preview.copies_to_restore !== undefined) announce("Copies restored.");
    else announce("Files restored.");
  } catch (error) {
    const uncertain = isUncertainUndoError(error);
    const message = uncertain
      ? "The result could not be loaded. Reload Activity before trying again."
      : activityErrorMessage(error);
    closeDialog({ restoreFocus: false });
    await Promise.allSettled([
      loadActivity({ noticeMessage: message, force: true, refresh: true }),
      loadDuplicateBin({ refresh: true }),
    ]);
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

function duplicateMovementAnnouncement(execution) {
  if (!execution) return "The Duplicate Bin move result could not be confirmed.";
  if (execution.status === "moved") {
    return execution.copies_moved === 1
      ? "1 copy moved to Duplicate Bin."
      : `${execution.copies_moved} copies moved to Duplicate Bin.`;
  }
  if (execution.status === "partially_moved") {
    return "Some copies were moved. Review the result in Duplicate Bin and Activity.";
  }
  if (new Set(["prepared", "moving", "moved_unverified"]).has(execution.status)) {
    return "The Duplicate Bin move is still in progress.";
  }
  return "The Duplicate Bin move needs attention in Activity.";
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
    await loadActivity({ force: true, refresh: true });
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

async function previewDuplicateBinMove(target) {
  const batch = state.backendSnapshot.backendState?.duplicate_batch;
  if (
    !batch
    || state.duplicateMovement.previewStatus === "loading"
    || state.duplicateMovement.executionPending
    || state.pendingMutation
    || state.activity.pendingUndo
    || state.duplicateBin.restorePending
  ) return;
  discardDuplicateMovementConfirmation();
  state.duplicateMovement.errorCode = null;
  state.duplicateMovement.errorMessage = null;
  const version = ++state.duplicateMovement.previewVersion;
  state.duplicateMovement.previewStatus = "loading";
  render();
  announce("Preparing the Duplicate Bin move…");
  try {
    const preview = await dataSource.previewDuplicateBinMove(batch.id);
    if (version !== state.duplicateMovement.previewVersion || state.route !== "organize") return;
    state.duplicateMovement.previewStatus = "ready";
    state.duplicateMovement.preview = preview;
    state.duplicateMovement.confirmationText = "";
    render();
    const trigger = elements.app.querySelector(
      '[data-action="preview-duplicate-bin-move"]',
    ) || target;
    setDuplicateBinDialog(preview, "movement", trigger);
  } catch (error) {
    if (version !== state.duplicateMovement.previewVersion) return;
    state.duplicateMovement.previewStatus = "error";
    state.duplicateMovement.preview = null;
    state.duplicateMovement.errorCode = error?.code || "request_failed";
    state.duplicateMovement.errorMessage = duplicateBinErrorMessage(error);
    announce(state.duplicateMovement.errorMessage);
    render({ focusMain: true });
  }
}

async function confirmDuplicateBinMove() {
  const preview = state.duplicateMovement.preview;
  if (
    !preview
    || state.duplicateMovement.executionPending
    || state.duplicateMovement.confirmationText !== preview.confirmation
  ) return;
  state.duplicateMovement.executionPending = true;
  state.duplicateMovement.errorCode = null;
  state.duplicateMovement.errorMessage = null;
  setDuplicateBinDialogPending("movement");
  syncShell();
  announce("Moving copies to Duplicate Bin…");
  let requestError = null;
  try {
    const result = await dataSource.confirmDuplicateBinMove(
      preview.confirmation_token,
      state.duplicateMovement.confirmationText,
    );
    state.duplicateMovement.execution = result.operation;
  } catch (error) {
    requestError = error;
  }
  closeDialog({ restoreFocus: false });
  state.duplicateMovement.executionPending = false;
  await Promise.allSettled([
    loadCurrent(),
    loadDuplicateBin({ refresh: true }),
    loadActivity({ force: true, refresh: true }),
  ]);
  const batchStatus = state.backendSnapshot.backendState?.duplicate_batch?.status;
  const recovered = state.duplicateMovement.execution
    || (batchStatus && batchStatus !== "approved");
  if (requestError && !recovered) {
    state.duplicateMovement.errorCode = requestError?.code || "request_failed";
    state.duplicateMovement.errorMessage = duplicateBinErrorMessage(requestError);
  }
  if (requestError && isUncertainDuplicateBinError(requestError) && !recovered) {
    announce("The move result could not be loaded. Reload Organize before trying again.");
  } else if (requestError && !recovered) {
    announce(state.duplicateMovement.errorMessage);
  } else {
    announce(duplicateMovementAnnouncement(state.duplicateMovement.execution));
  }
  render({ focusMain: true });
}

function setDuplicateBinSelection(ids) {
  const available = new Set(
    state.duplicateBin.items.filter((item) => item.restore_available).map((item) => item.id),
  );
  const selected = new Set(ids);
  if ([...selected].some((id) => !available.has(id))) return;
  state.duplicateBin.selectedIds = selected;
  discardDuplicateRestoreConfirmation();
  render();
}

function toggleDuplicateBinItem(itemId) {
  const selected = new Set(state.duplicateBin.selectedIds);
  if (selected.has(itemId)) selected.delete(itemId);
  else selected.add(itemId);
  setDuplicateBinSelection(selected);
}

function selectAllDuplicateBinItems() {
  const available = state.duplicateBin.items
    .filter((item) => item.restore_available)
    .map((item) => item.id);
  const allSelected = available.length > 0
    && available.every((id) => state.duplicateBin.selectedIds.has(id));
  setDuplicateBinSelection(allSelected ? [] : available);
}

async function previewDuplicateBinRestore(target, itemId = null) {
  if (itemId) setDuplicateBinSelection([itemId]);
  const selected = [...state.duplicateBin.selectedIds].sort();
  if (!selected.length || state.duplicateBin.previewStatus === "loading" || state.duplicateBin.restorePending) {
    return;
  }
  discardDuplicateRestoreConfirmation();
  const version = ++state.duplicateBin.previewVersion;
  state.duplicateBin.previewStatus = "loading";
  state.duplicateBin.noticeMessage = null;
  render();
  announce("Preparing to restore selected copies…");
  try {
    const preview = await dataSource.previewDuplicateBinRestore(selected);
    if (version !== state.duplicateBin.previewVersion || state.route !== "duplicate-bin") return;
    state.duplicateBin.previewStatus = "ready";
    state.duplicateBin.preview = preview;
    state.duplicateBin.confirmationText = "";
    render();
    const trigger = elements.app.querySelector('[data-action="restore-selected"]') || target;
    setDuplicateBinDialog(preview, "restore", trigger);
  } catch (error) {
    if (version !== state.duplicateBin.previewVersion) return;
    state.duplicateBin.previewStatus = "error";
    state.duplicateBin.noticeMessage = duplicateBinErrorMessage(error);
    await loadDuplicateBin({ noticeMessage: state.duplicateBin.noticeMessage, refresh: true });
    announce(state.duplicateBin.noticeMessage);
  }
}

async function confirmDuplicateBinRestore() {
  const preview = state.duplicateBin.preview;
  if (
    !preview
    || state.duplicateBin.restorePending
    || state.duplicateBin.confirmationText !== preview.confirmation
  ) return;
  state.duplicateBin.restorePending = true;
  setDuplicateBinDialogPending("restore");
  syncShell();
  announce("Restoring selected copies…");
  let result = null;
  let requestError = null;
  try {
    result = await dataSource.confirmDuplicateBinRestore(
      preview.confirmation_token,
      state.duplicateBin.confirmationText,
    );
  } catch (error) {
    requestError = error;
  }
  closeDialog({ restoreFocus: false });
  state.duplicateBin.restorePending = false;
  state.duplicateBin.selectedIds = new Set();
  const message = requestError
    ? duplicateBinErrorMessage(requestError)
    : result?.restore.status === "restored"
      ? "Selected copies were restored."
      : "Some copies could not be restored. Review the result.";
  await Promise.allSettled([
    loadDuplicateBin({ noticeMessage: message, refresh: true }),
    loadActivity({ force: true, refresh: true }),
  ]);
  announce(
    requestError && isUncertainDuplicateBinError(requestError)
      ? "The restore result could not be loaded. Reload Duplicate Bin before trying again."
      : message,
  );
  render({ focusMain: true });
}

async function checkFiles() {
  if (state.scanPolling || state.pendingMutation) return;
  discardMovementConfirmation();
  discardDuplicateMovementConfirmation();
  discardDuplicateRestoreConfirmation();
  discardStandardBuilder({ close: true });
  state.movement.execution = null;
  state.movement.errorCode = null;
  state.movement.errorMessage = null;
  state.duplicateMovement.execution = null;
  state.duplicateMovement.errorCode = null;
  state.duplicateMovement.errorMessage = null;
  state.destinationDrafts = {};
  state.duplicateDrafts = {};
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
    if (settled.status === "completed") {
      state.standards.freshScanRequired = false;
      await loadStandards({ refresh: true });
    }
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
    } else if (code === "duplicate_batch_conflict") {
      await reloadAfterConflict();
      if (state.activeDialog === "approval") closeDialog({ restoreFocus: false });
      state.workflowOverride = workflowOverride(
        "These copies are already part of another approved action.",
        "Review the latest duplicate choices.",
        "reload-choices",
        "Review latest choices",
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
  if (action === "review-recovery") return reviewRecovery(target);
  if (action === "check-files" || action === "check-again") return checkFiles();
  if (action === "reload-choices") {
    discardMovementConfirmation();
    discardDuplicateMovementConfirmation();
    state.movement.execution = null;
    state.movement.errorCode = null;
    state.movement.errorMessage = null;
    return loadCurrent();
  }
  if (action === "retry-activity") return loadActivity();
  if (action === "reload-duplicate-bin") return loadDuplicateBin({ refresh: true });
  if (action === "reload-standards") return loadStandards({ refresh: true });
  if (action === "set-standard-active") return setStandardActive(target);
  if (action === "open-standard-builder") return openStandardBuilder(target);
  if (action === "preview-standard") return previewStandard();
  if (action === "save-standard") return saveStandard();
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
  if (action === "preview-duplicate-bin-move") return previewDuplicateBinMove(target);
  if (action === "confirm-duplicate-move") return confirmDuplicateBinMove();
  if (action === "cancel-duplicate-move") return closeDialog();
  if (action === "toggle-bin") return toggleDuplicateBinItem(target.dataset.binId);
  if (action === "select-all-bin") return selectAllDuplicateBinItems();
  if (action === "restore-selected") return previewDuplicateBinRestore(target);
  if (action === "restore-one") {
    return previewDuplicateBinRestore(target, target.dataset.binId);
  }
  if (action === "confirm-duplicate-restore") return confirmDuplicateBinRestore();
  if (action === "cancel-duplicate-restore") return closeDialog();
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
  const builderInput = event.target.closest('[data-standard-builder-field="matcher-value"]');
  if (builderInput) {
    updateStandardBuilderField(builderInput);
    return;
  }
  const input = event.target.closest("[data-action$='confirmation-input']");
  if (!input) return;
  if (state.activeDialog === "movement-confirmation") {
    state.movement.confirmationText = input.value;
    const view = buildMovementConfirmationViewModel(
      state.movement.preview,
      state.movement.confirmationText,
    );
    const confirm = elements.dialog.querySelector('[data-action="confirm-move"]');
    if (confirm) confirm.disabled = !view.canConfirm || state.movement.executionPending;
    return;
  }
  const restore = state.activeDialog === "duplicate-restore-confirmation";
  const movement = state.activeDialog === "duplicate-movement-confirmation";
  if (!restore && !movement) return;
  const owner = restore ? state.duplicateBin : state.duplicateMovement;
  owner.confirmationText = input.value;
  const view = buildDuplicateBinConfirmationViewModel(
    owner.preview,
    owner.confirmationText,
    restore ? "restore" : "movement",
  );
  const action = restore ? "confirm-duplicate-restore" : "confirm-duplicate-move";
  const confirm = elements.dialog.querySelector(`[data-action="${action}"]`);
  if (confirm) confirm.disabled = !view.canConfirm || owner.executionPending || owner.restorePending;
});

document.addEventListener("change", (event) => {
  const control = event.target.closest("[data-standard-builder-field]");
  if (!control || control.dataset.standardBuilderField === "matcher-value") return;
  updateStandardBuilderField(control);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && elements.dialog.open) {
    event.preventDefault();
    if (state.activeDialog === "activity-undo" && state.activity.pendingUndo) return;
    if (state.activeDialog === "movement-confirmation" && state.movement.executionPending) return;
    if (
      state.activeDialog === "duplicate-movement-confirmation"
      && state.duplicateMovement.executionPending
    ) return;
    if (
      state.activeDialog === "duplicate-restore-confirmation"
      && state.duplicateBin.restorePending
    ) return;
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
  if (previousRoute === "organize" && nextRoute !== "organize") {
    discardDuplicateMovementConfirmation();
  }
  if (previousRoute === "duplicate-bin" && nextRoute !== "duplicate-bin") {
    discardDuplicateRestoreConfirmation();
  }
  if (previousRoute === "standards" && nextRoute !== "standards") {
    discardStandardBuilder({ close: true });
  }
  if (state.activeDialog === "recovery" && nextRoute !== previousRoute) {
    closeDialog({ restoreFocus: false });
  }
  state.route = nextRoute;
  render({ focusMain: true });
  if (nextRoute === "activity" && previousRoute !== "activity") loadActivity();
  if (nextRoute === "duplicate-bin" && previousRoute !== "duplicate-bin") {
    loadDuplicateBin({ refresh: true });
  }
  if (nextRoute === "standards" && previousRoute !== "standards") loadStandards();
});
window.addEventListener("beforeunload", () => dataSource.dispose());

if (!window.location.hash) window.location.hash = "#/home";
state.route = routeFromHash();
render();
loadCurrent();
loadActivity({ force: true });
loadDuplicateBin();
loadStandards();
