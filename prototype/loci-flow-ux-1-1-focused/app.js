import { createStore } from "./store.js";
import {
  escapeHtml,
  renderDialog,
  renderOutcomeDialog,
  renderRoute,
  renderStageDialog,
} from "./views.js";

const routes = new Set(["home", "organize", "duplicates", "activity", "settings"]);
const workflowForRoute = { organize: "organization", duplicates: "duplicates" };
const routeForWorkflow = { organization: "organize", duplicates: "duplicates" };

const elements = {
  app: document.querySelector("#app"),
  main: document.querySelector("#main-content"),
  nav: document.querySelector(".primary-nav"),
  status: document.querySelector("#status-live"),
  alert: document.querySelector("#alert-live"),
  dialog: document.querySelector("#dialog-host"),
  dialogEyebrow: document.querySelector("#dialog-eyebrow"),
  dialogTitle: document.querySelector("#dialog-title"),
  dialogDescription: document.querySelector("#dialog-description"),
  dialogContent: document.querySelector("#dialog-content"),
  dialogActions: document.querySelector("#dialog-actions"),
  dialogClose: document.querySelector("#dialog-host .dialog-close"),
  freshness: document.querySelector("[data-location-freshness]"),
};

let store;
let lastDialogTrigger = null;
let lastDialogTriggerDescriptor = null;
let lastResponsiveInspectorTriggerDescriptor = null;
let routeGuardBusy = false;
const responsiveInspectorQuery = window.matchMedia("(max-width: 51.25rem)");

const focusIdentityAttributes = [
  "data-focus-key",
  "data-action",
  "data-change-id",
  "data-group-id",
  "data-file-id",
  "data-activity-id",
  "data-rule-id",
  "data-recheck-id",
  "data-setting-path",
  "data-settings-section",
  "data-pane",
  "name",
  "value",
];

function focusDescriptor(element) {
  if (!(element instanceof HTMLElement)) return null;
  const attributes = focusIdentityAttributes
    .filter((name) => element.hasAttribute(name))
    .map((name) => [name, element.getAttribute(name)]);
  if (!element.id && !attributes.length) return null;
  return { id: element.id || "", tagName: element.tagName, attributes };
}

function findFocusTarget(descriptor, root = document) {
  if (!descriptor) return null;
  if (descriptor.id) {
    const byId = document.getElementById(descriptor.id);
    if (byId && root.contains(byId)) return byId;
  }
  return [...root.querySelectorAll(descriptor.tagName.toLocaleLowerCase())].find((candidate) =>
    descriptor.attributes.every(([name, value]) => candidate.getAttribute(name) === value),
  ) || null;
}

function activeResponsiveInspector() {
  if (!responsiveInspectorQuery.matches) return null;
  const organizeInspector = elements.app.querySelector('.comparison-workspace .inspector-pane[data-open="true"][data-has-selection="true"]:not([hidden])');
  if (organizeInspector) return organizeInspector;
  return elements.app.querySelector('.duplicates-workspace .duplicate-inspector[data-open="true"][data-has-selection="true"]');
}

function syncResponsiveInspector() {
  elements.app.querySelectorAll("[data-responsive-inert]").forEach((element) => {
    element.removeAttribute("inert");
    element.removeAttribute("data-responsive-inert");
  });
  elements.nav.removeAttribute("inert");
  const drawer = activeResponsiveInspector();
  if (!drawer) return null;

  const shell = drawer.closest(".organize-shell, .duplicates-shell");
  const workspace = drawer.parentElement;
  [...shell.children].forEach((child) => {
    if (child === workspace) {
      [...workspace.children].forEach((pane) => {
        if (pane !== drawer) {
          pane.setAttribute("inert", "");
          pane.setAttribute("data-responsive-inert", "true");
        }
      });
    } else {
      child.setAttribute("inert", "");
      child.setAttribute("data-responsive-inert", "true");
    }
  });
  elements.nav.setAttribute("inert", "");
  drawer.setAttribute("role", "dialog");
  drawer.setAttribute("aria-modal", "true");
  return drawer;
}

function rememberResponsiveInspectorTrigger(trigger) {
  if (responsiveInspectorQuery.matches && trigger) lastResponsiveInspectorTriggerDescriptor = focusDescriptor(trigger);
}

function restoreResponsiveInspectorFocus() {
  const descriptor = lastResponsiveInspectorTriggerDescriptor;
  lastResponsiveInspectorTriggerDescriptor = null;
  window.requestAnimationFrame(() => (findFocusTarget(descriptor, elements.app) || elements.main).focus({ preventScroll: false }));
}

function parseRoute() {
  const raw = window.location.hash.startsWith("#/") ? window.location.hash.slice(2) : "home";
  const [candidate, query = ""] = raw.split("?", 2);
  const name = routes.has(candidate) ? candidate : "home";
  return { name, params: new URLSearchParams(query) };
}

function routeHash(name, params = "") {
  return `#/${name}${params ? `?${params}` : ""}`;
}

function navigate(hash) {
  if (window.location.hash === hash) render({ focusMain: true });
  else window.location.hash = hash;
}

function setStatus(message) {
  elements.status.textContent = "";
  window.requestAnimationFrame(() => {
    elements.status.textContent = message;
  });
}

function setAlert(message) {
  elements.alert.textContent = "";
  window.requestAnimationFrame(() => {
    elements.alert.textContent = message;
  });
}

function showDialogError(message) {
  elements.dialogContent.querySelector("[data-dialog-error]")?.remove();
  const error = document.createElement("p");
  error.className = "form-error";
  error.dataset.dialogError = "true";
  error.setAttribute("role", "alert");
  error.textContent = message;
  elements.dialogContent.append(error);
  setAlert(message);
}

function updateShell(route) {
  const state = store.getState();
  const summary = store.planSummary();
  elements.freshness.textContent = state.location.statusLabel;
  elements.nav.querySelectorAll("[data-route]").forEach((link) => {
    const active = link.dataset.route === route.name;
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
    const targetWorkflow = workflowForRoute[link.dataset.route];
    const operationLocked = state.stagedOperation?.status === "working" || ["applying", "verifying"].includes(state.workflow.active);
    const locked = Boolean(targetWorkflow && (operationLocked || (["organization", "duplicates"].includes(state.workflow.active) && state.workflow.active !== targetWorkflow)));
    link.dataset.locked = String(locked);
    link.title = locked
      ? operationLocked
        ? "A sample operation is in progress"
        : `${state.workflow.active === "organization" ? "Organization" : "Duplicate review"} is active`
      : "";
  });
  const planCount = state.plan.status === "ready" ? summary.review + summary.conflicts : 0;
  const planBadge = elements.nav.querySelector('[data-nav-count="organization"]');
  const duplicateBadge = elements.nav.querySelector('[data-nav-count="duplicates"]');
  planBadge.textContent = String(planCount);
  planBadge.setAttribute("aria-label", `${planCount} unresolved ${planCount === 1 ? "issue" : "issues"}`);
  duplicateBadge.textContent = String(state.duplicates.activeGroupIds.length);
  duplicateBadge.setAttribute("aria-label", `${state.duplicates.activeGroupIds.length} exact-copy groups`);
}

function render({ focusMain = false } = {}) {
  if (!store) return;
  const activeDescriptor = focusMain || elements.dialog.open || !elements.app.contains(document.activeElement)
    ? null
    : focusDescriptor(document.activeElement);
  const route = parseRoute();
  const view = renderRoute(store, route);
  document.title = `${view.title} · Loci Flow focused prototype`;
  elements.app.setAttribute("aria-busy", "false");
  elements.app.dataset.route = route.name;
  elements.app.innerHTML = view.html;
  updateShell(route);
  const responsiveInspector = syncResponsiveInspector();
  if (focusMain) {
    window.requestAnimationFrame(() => {
      const target = responsiveInspector?.querySelector("[data-inspector-close], h2") || elements.main;
      if (!target.hasAttribute("tabindex") && !target.matches("button, [tabindex]")) target.tabIndex = -1;
      target.focus({ preventScroll: false });
    });
  }
  else if (activeDescriptor) {
    window.requestAnimationFrame(() => {
      const target = findFocusTarget(activeDescriptor, responsiveInspector || elements.app)
        || responsiveInspector?.querySelector("[data-inspector-close], h2")
        || elements.main;
      if (!target.hasAttribute("tabindex") && !target.matches("button, a, input, select, textarea, [tabindex]")) target.tabIndex = -1;
      target.focus({ preventScroll: true });
    });
  }
}

function setDialogView(view, { locked = false, trigger = null } = {}) {
  if (trigger && !elements.dialog.open) {
    lastDialogTrigger = trigger;
    lastDialogTriggerDescriptor = focusDescriptor(trigger);
  }
  elements.dialog.dataset.locked = String(locked);
  elements.dialogEyebrow.textContent = view.eyebrow || "";
  elements.dialogTitle.textContent = view.title || "";
  elements.dialogDescription.textContent = view.description || "";
  elements.dialogContent.innerHTML = view.content || "";
  elements.dialogActions.innerHTML = view.actions || "";
  elements.dialogClose.disabled = locked;
  elements.dialogClose.hidden = locked;
  if (!elements.dialog.open) elements.dialog.showModal();
  window.requestAnimationFrame(() => {
    if (locked) {
      const activeStage = elements.dialogContent.querySelector("[data-stage-focus]");
      elements.dialogTitle.tabIndex = -1;
      (activeStage || elements.dialogTitle).focus();
      return;
    }
    elements.dialogTitle.removeAttribute("tabindex");
    const focusTarget =
      elements.dialog.querySelector("[autofocus]") ||
      elements.dialogContent.querySelector("input:not([type='hidden']):not(:disabled), select:not(:disabled), textarea:not(:disabled)") ||
      elements.dialogActions.querySelector(".button-primary:not(:disabled), .button-danger:not(:disabled), button:not(:disabled), a[href]") ||
      elements.dialogContent.querySelector("button:not(:disabled), a[href]") ||
      elements.dialogClose;
    focusTarget?.focus();
  });
}

function rememberDialogTrigger(trigger) {
  if (!trigger || elements.dialog.open) return;
  lastDialogTrigger = trigger;
  lastDialogTriggerDescriptor = focusDescriptor(trigger);
}

function openDialog(type, payload = {}, trigger = null) {
  setDialogView(renderDialog(store, type, payload), { trigger });
  elements.dialog.dataset.dialogType = type;
  elements.dialog.dataset.payload = JSON.stringify(payload);
}

function closeDialog({ restoreFocus = true } = {}) {
  if (elements.dialog.open) elements.dialog.close();
  elements.dialog.dataset.locked = "false";
  elements.dialogClose.disabled = false;
  elements.dialogClose.hidden = false;
  if (restoreFocus) {
    const target = lastDialogTrigger?.isConnected
      ? lastDialogTrigger
      : findFocusTarget(lastDialogTriggerDescriptor, elements.app) || elements.app.querySelector("h1") || elements.main;
    window.requestAnimationFrame(() => {
      if (!target.hasAttribute("tabindex") && !target.matches("button, a, input, select, textarea, [tabindex]")) target.tabIndex = -1;
      target.focus();
    });
  }
  lastDialogTrigger = null;
  lastDialogTriggerDescriptor = null;
}

function workflowHasWork(target, state = store.getState()) {
  if (target === "organization") return state.plan.status === "ready";
  if (target === "duplicates") return state.duplicates.activeGroupIds.length > 0;
  return false;
}

function requestWorkflow(target, trigger = null, { navigateOnSuccess = true } = {}) {
  if (store.getState().workflow.active === target && !workflowHasWork(target)) store.finishWorkflowIfEmpty();
  const state = store.getState();
  if (state.workflow.active === "none" && !workflowHasWork(target, state)) {
    if (navigateOnSuccess) navigate(routeHash(routeForWorkflow[target]));
    return true;
  }
  const result = store.beginWorkflow(target);
  if (result.status === "switch-required") {
    openDialog("switch-workflow", result, trigger);
    return false;
  }
  if (result.status === "blocked") {
    setAlert("The current sample operation must finish before switching workflows.");
    return false;
  }
  if (navigateOnSuccess) navigate(routeHash(routeForWorkflow[target]));
  return true;
}

function guardHashRoute() {
  if (!store || routeGuardBusy) return false;
  const route = parseRoute();
  const target = workflowForRoute[route.name];
  if (!target) return false;
  const state = store.getState();
  let active = state.workflow.active;
  if (["organization", "duplicates"].includes(active) && !workflowHasWork(active, state)) {
    store.finishWorkflowIfEmpty();
    active = "none";
  }
  const operationLocked = state.stagedOperation?.status === "working" || ["applying", "verifying"].includes(active);
  if (operationLocked) {
    const definition = state.stagedOperation?.definition;
    const returnWorkflow = state.stagedOperation?.returnWorkflow;
    const safeRoute = routeForWorkflow[returnWorkflow]
      || (definition === "duplicateConsolidation" ? "duplicates" : definition === "undo" ? "activity" : definition === "freshnessCheck" ? "home" : "organize");
    routeGuardBusy = true;
    window.history.replaceState(null, "", routeHash(safeRoute));
    routeGuardBusy = false;
    setAlert("The active sample operation must finish before another mutable workflow opens.");
    return true;
  }
  if (active === "none") {
    if (workflowHasWork(target, state)) store.beginWorkflow(target);
    return false;
  }
  if (["organization", "duplicates"].includes(active) && active !== target) {
    routeGuardBusy = true;
    window.history.replaceState(null, "", routeHash(routeForWorkflow[active]));
    render({ focusMain: true });
    openDialog("switch-workflow", { status: "switch-required", active, target });
    routeGuardBusy = false;
    return true;
  }
  return false;
}

const stageMessages = {
  "checking-files": "Checking changed sample files…",
  "finding-groups": "Finding bounded organization groups…",
  "checking-conflicts": "Reviewing proposed destinations…",
  "preparing-plan": "Preparing the editable comparison…",
  rechecking: "Checking freshness, destinations, and resolved decisions…",
  applying: "Applying the approved fixture changes…",
  consolidating: "Moving selected extras to the reversible holding area…",
  "rechecking-copies": "Confirming the selected files still match…",
  verifying: "Comparing the sample result with the approved intent…",
  complete: "Recording the final sample truth…",
  "checking-reversibility": "Checking which sample changes remain cleanly reversible…",
  reversing: "Returning the reversible sample changes…",
};

const stageDurations = {
  "checking-files": 420,
  "finding-groups": 610,
  "checking-conflicts": 470,
  "preparing-plan": 360,
  rechecking: 480,
  applying: 670,
  consolidating: 620,
  "rechecking-copies": 440,
  verifying: 590,
  complete: 240,
  "checking-reversibility": 430,
  reversing: 540,
};

function waitForStage(milliseconds = 520) {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return new Promise((resolve) => window.setTimeout(resolve, reduced ? 70 : milliseconds));
}

async function runStages(stages, definition, finish, { nextWorkflow = "none", trigger = null } = {}) {
  rememberDialogTrigger(trigger);
  store.setOperation(definition, stages[0]?.id || null, nextWorkflow);
  let result;
  let activeIndex = 0;
  try {
    for (activeIndex = 0; activeIndex < stages.length; activeIndex += 1) {
      const stage = stages[activeIndex];
      const message = stageMessages[stage.id] || "Working with the sample state…";
      if (activeIndex === stages.length - 1) result = await finish();
      store.advanceOperation(stage.id, message);
      setDialogView(renderStageDialog(stages, activeIndex, { title: stage.label, message }), { locked: true });
      setAlert(`${stage.label}. ${message}`);
      await waitForStage(stageDurations[stage.id] || 520);
    }
    store.finishOperation(result?.status || "Complete", result?.nextWorkflow || nextWorkflow);
    return result;
  } catch (error) {
    store.finishOperation("Operation stopped", nextWorkflow);
    const failed = renderStageDialog(stages, activeIndex, { title: "Operation stopped", message: `${error.message} No files moved.`, failedIndex: activeIndex });
    failed.actions = '<button class="button button-primary" type="button" data-action="close-dialog">Close</button>';
    setDialogView(failed, { locked: false });
    setAlert(`Operation stopped. ${error.message} No files moved.`);
    return { error };
  }
}

async function preparePlan(trigger = null) {
  const stages = store.getState().stageDefinitions.planPreparation;
  const result = await runStages(stages, "planPreparation", async () => {
    const summary = store.preparePlan();
    return {
      status: summary.changes ? "Plan ready" : "Up to date",
      nextWorkflow: summary.changes ? "organization" : "none",
      summary,
    };
  }, { nextWorkflow: "organization", trigger });
  if (result?.error) return;
  store.clearOperation();
  closeDialog({ restoreFocus: false });
  navigate("#/organize");
  setStatus(result.summary.changes
    ? `Organization plan ready with ${result.summary.changes} proposed ${result.summary.changes === 1 ? "change" : "changes"}. No files moved.`
    : "Sample Downloads is already consistent with the latest approved organization intent. No new changes were proposed.");
}

async function requestPlanPreparation(trigger = null) {
  rememberDialogTrigger(trigger);
  if (!requestWorkflow("organization", trigger, { navigateOnSuccess: false })) return;
  if (store.getState().plan.status === "ready") {
    navigate("#/organize");
    setStatus("The existing organization plan opened. Its edits were preserved.");
    return;
  }
  await preparePlan(trigger);
}

async function checkFiles(trigger = null) {
  const previousWorkflow = store.getState().workflow.active;
  if (["applying", "verifying"].includes(previousWorkflow) || store.getState().stagedOperation?.status === "working") {
    setAlert("The active sample operation must finish before checking freshness.");
    return;
  }
  const stages = [
    { id: "checking-files", label: "Checking sample files" },
    { id: "checking-conflicts", label: "Checking plan freshness" },
    { id: "complete", label: "Up to date" },
  ];
  const result = await runStages(
    stages,
    "freshnessCheck",
    async () => ({ status: "Up to date" }),
    { nextWorkflow: previousWorkflow, trigger },
  );
  if (result?.error) return;
  store.clearOperation();
  closeDialog();
  setStatus("Sample Downloads is up to date. The current plan and choices were preserved.");
}

async function applyPlan() {
  const state = store.getState();
  const outcome = state.settings.advanced.demoOutcome;
  const defaultStages = state.stageDefinitions.organizationApply;
  const stages = outcome === "failed-before-changes"
    ? [defaultStages[0], { id: "complete", label: "Stopped before changes" }]
    : defaultStages;
  const operation = await runStages(
    stages,
    "organizationApply",
    async () => store.applyPlan(outcome),
    { nextWorkflow: outcome === "failed-before-changes" ? "organization" : "none" },
  );
  if (operation?.error) return;
  store.clearOperation();
  setDialogView(renderOutcomeDialog(operation, stages), { locked: false });
  setStatus(`${operation.status}. ${operation.summary}`);
}

async function consolidateDuplicates(groupId) {
  const stages = store.getState().stageDefinitions.duplicateConsolidation;
  const remainingAfterSuccess = Math.max(0, store.getState().duplicates.activeGroupIds.length - 1);
  const operation = await runStages(
    stages,
    "duplicateConsolidation",
    async () => store.consolidateDuplicates(groupId),
    { nextWorkflow: remainingAfterSuccess ? "duplicates" : "none" },
  );
  if (operation?.error) return;
  store.clearOperation();
  setDialogView(renderOutcomeDialog(operation, stages), { locked: false });
  setStatus(`${operation.status}. ${operation.summary}`);
}

function focusSelectedPlanIssue() {
  window.requestAnimationFrame(() => {
    const changeId = store.getState().selection.inspectorItemId;
    const row = [...elements.app.querySelectorAll('[data-action="focus-plan-change"]')]
      .find((candidate) => candidate.dataset.changeId === changeId && candidate.closest(".planned-pane"));
    const responsiveInspector = activeResponsiveInspector();
    const target = responsiveInspector?.querySelector("[data-inspector-close], #inspector-heading")
      || row
      || elements.app.querySelector("#inspector-heading")
      || elements.main;
    if (!target.hasAttribute("tabindex") && !target.matches("button, a, input, select, textarea, [tabindex]")) target.tabIndex = -1;
    target.focus({ preventScroll: false });
  });
}

async function undoActivity(activityId, narrow = false) {
  const activeWorkflow = store.getState().workflow.active;
  if (["organization", "duplicates"].includes(activeWorkflow)) {
    closeDialog();
    setAlert(`Finish or discard the active ${activeWorkflow === "organization" ? "organization plan" : "duplicate review"} before using recovery.`);
    return;
  }
  const stages = [
    { id: "checking-reversibility", label: "Checking reversibility" },
    { id: "reversing", label: narrow ? "Returning copy" : "Reversing changes" },
    { id: "verifying", label: "Verifying" },
    { id: "complete", label: "Complete" },
  ];
  const result = await runStages(stages, "undo", async () => {
    const changed = narrow ? store.returnDuplicateCopy(activityId) : store.undoActivity(activityId);
    if (!changed) throw new Error("This sample recovery is no longer available.");
    return store.getState().activity.find((item) => item.id === activityId);
  });
  if (result?.error) return;
  store.clearOperation();
  closeDialog({ restoreFocus: false });
  navigate("#/activity");
  setStatus(`${result.status}. ${result.verification}`);
}

function attemptApply(trigger) {
  const summary = store.planSummary();
  if (!summary.changes) {
    setAlert("No planned changes remain to apply. Undo the latest plan edit or discard this plan.");
    return;
  }
  if (summary.review) {
    openDialog("review-gate", { count: summary.review }, trigger);
    return;
  }
  if (summary.conflicts) {
    openDialog("conflict-gate", { count: summary.conflicts }, trigger);
    return;
  }
  openDialog("apply-confirm", {}, trigger);
}

async function handleAction(target, event) {
  const action = target.dataset.action;
  if (!action) return;
  event.preventDefault();

  if (action === "close-dialog") closeDialog();
  else if (action === "open-workflow") requestWorkflow(target.dataset.workflow, target);
  else if (action === "continue-workflow") {
    const workflow = target.dataset.workflow;
    closeDialog({ restoreFocus: false });
    navigate(routeHash(routeForWorkflow[workflow]));
  } else if (action === "discard-and-switch") {
    const workflow = target.dataset.workflow;
    store.discardAndSwitch(workflow);
    store.finishWorkflowIfEmpty();
    closeDialog({ restoreFocus: false });
    navigate(routeHash(routeForWorkflow[workflow]));
    setStatus(store.getState().workflow.active === workflow
      ? "Previous workflow edits discarded. New workflow opened."
      : "Previous workflow edits discarded. The destination opened with no mutable work waiting.");
  } else if (action === "view-activity") {
    closeDialog({ restoreFocus: false });
    navigate("#/activity");
  } else if (action === "check-files") await checkFiles(target);
  else if (action === "prepare-plan") await requestPlanPreparation(target);
  else if (action === "set-narrow-pane") store.setNarrowPane(target.dataset.pane);
  else if (action === "toggle-inspector") {
    const opening = !store.getState().selection.inspectorOpen;
    const responsiveWasOpen = Boolean(activeResponsiveInspector());
    if (opening) rememberResponsiveInspectorTrigger(target);
    store.toggleInspector();
    if (!opening && responsiveWasOpen) restoreResponsiveInspectorFocus();
  }
  else if (action === "toggle-plan-group") store.togglePlanGroup(target.dataset.groupId);
  else if (action === "focus-plan-change") {
    rememberResponsiveInspectorTrigger(target);
    store.focusPlanChange(target.dataset.changeId);
  }
  else if (action === "focus-plan-group") {
    rememberResponsiveInspectorTrigger(target);
    store.focusPlanGroup(target.dataset.groupId);
  }
  else if (action === "toggle-plan-selection") {
    store.selectPlanChange(target.dataset.changeId, { checked: target.checked });
    if (responsiveInspectorQuery.matches && store.getState().selection.inspectorOpen) store.toggleInspector(false);
  }
  else if (action === "select-all-visible") {
    store.selectVisiblePlanChanges((target.dataset.changeIds || "").split(",").filter(Boolean));
    if (responsiveInspectorQuery.matches && store.getState().selection.inspectorOpen) store.toggleInspector(false);
  }
  else if (action === "clear-plan-selection") store.clearPlanSelection();
  else if (action === "move-selected") openDialog("destination", {}, target);
  else if (action === "select-and-move") {
    store.selectPlanChange(target.dataset.changeId, { checked: true, additive: false });
    openDialog("destination", {}, target);
  } else if (action === "leave-selected" || action === "remove-selected") {
    const inspectorWasOpen = Boolean(activeResponsiveInspector());
    store.leaveSelected();
    if (inspectorWasOpen) restoreResponsiveInspectorFocus();
    setStatus("Plan updated. No files moved.");
  } else if (action === "select-and-leave") {
    const inspectorWasOpen = Boolean(activeResponsiveInspector());
    store.selectPlanChange(target.dataset.changeId, { checked: true, additive: false });
    store.leaveSelected();
    if (inspectorWasOpen) restoreResponsiveInspectorFocus();
    setStatus("Plan updated. The file will remain in place.");
  } else if (action === "confirm-remove-group") openDialog("remove-group", { groupId: target.dataset.groupId }, target);
  else if (action === "remove-group") {
    store.removeGroup(target.dataset.groupId);
    closeDialog();
    setStatus("Proposed folder removed from the plan. No files moved.");
  } else if (action === "undo-plan-edit") {
    store.undoPlanEdit();
    setStatus("Latest plan edit undone. No files moved.");
  } else if (action === "accept-review") {
    store.acceptReview(target.dataset.reviewId);
    setStatus("Review accepted. Plan updated. No files moved.");
  } else if (action === "resolve-conflict") {
    store.resolveConflict(target.dataset.conflictId);
    setStatus("Conflict resolved. Plan updated. No files moved.");
  } else if (action === "show-issues") {
    const summary = store.planSummary();
    rememberResponsiveInspectorTrigger(target);
    store.showPlanIssues(summary.review ? "review" : summary.conflicts ? "conflict" : "all");
    focusSelectedPlanIssue();
    setStatus(summary.review ? "Review items shown." : summary.conflicts ? "Conflict shown." : "No unresolved issues remain.");
  } else if (action === "clear-issue-filter") store.clearIssueFilter();
  else if (action === "focus-review-items") {
    if (responsiveInspectorQuery.matches) lastResponsiveInspectorTriggerDescriptor = lastDialogTriggerDescriptor;
    closeDialog({ restoreFocus: false });
    store.showPlanIssues("review");
    focusSelectedPlanIssue();
    setStatus("Review items shown in the Planned organization.");
  } else if (action === "focus-conflicts") {
    if (responsiveInspectorQuery.matches) lastResponsiveInspectorTriggerDescriptor = lastDialogTriggerDescriptor;
    closeDialog({ restoreFocus: false });
    store.showPlanIssues("conflict");
    focusSelectedPlanIssue();
    setStatus("Conflict shown in the Planned organization.");
  } else if (action === "attempt-apply") attemptApply(target);
  else if (action === "apply-plan") await applyPlan();
  else if (action === "confirm-discard-plan") openDialog("discard-plan", {}, target);
  else if (action === "discard-plan") {
    store.discardPlan();
    closeDialog({ restoreFocus: false });
    navigate("#/home");
    setStatus("Organization plan discarded. No files moved.");
  } else if (action === "reveal-plan-file") setStatus(`Prototype only: ${store.fileById(target.dataset.fileId)?.name || "This file"} would be revealed in Finder or File Explorer.`);
  else if (action === "select-duplicate-group") store.selectDuplicateGroup(target.dataset.groupId);
  else if (action === "focus-duplicate-file") {
    rememberResponsiveInspectorTrigger(target);
    store.focusDuplicateFile(target.dataset.fileId);
  }
  else if (action === "toggle-duplicate-inspector") {
    const opening = !store.getState().selection.duplicateInspectorOpen;
    const responsiveWasOpen = Boolean(activeResponsiveInspector());
    if (opening) rememberResponsiveInspectorTrigger(target);
    store.toggleDuplicateInspector();
    if (!opening && responsiveWasOpen) restoreResponsiveInspectorFocus();
  }
  else if (action === "choose-working-copy") {
    store.chooseWorkingCopy(target.dataset.groupId, target.dataset.fileId);
    setStatus("Working copy chosen.");
  } else if (action === "focus-working-copy-choice") setStatus("Choose a file from the group as the working copy.");
  else if (action === "toggle-duplicate-extra") store.toggleDuplicateExtra(target.dataset.fileId, target.checked);
  else if (action === "select-duplicate-extras") {
    if (!store.selectDuplicateExtras(target.dataset.groupId)) setAlert("Choose a working copy first.");
  } else if (action === "keep-all-duplicates") {
    const inspectorWasOpen = Boolean(activeResponsiveInspector());
    if (store.keepAllDuplicates(target.dataset.groupId)) {
      const complete = store.finishDuplicateReview();
      if (inspectorWasOpen) restoreResponsiveInspectorFocus();
      setStatus(complete ? "Duplicate review complete. All exact copies will stay where they are." : "All exact copies in this group will stay where they are.");
    }
  } else if (action === "reconfirm-duplicate") {
    store.reconfirmDuplicateRecheck(target.dataset.recheckId);
    setStatus("Edited working copy reconfirmed.");
  } else if (action === "confirm-consolidate") openDialog("consolidate", { groupId: target.dataset.groupId }, target);
  else if (action === "consolidate-duplicates") await consolidateDuplicates(target.dataset.groupId);
  else if (action === "toggle-activity") store.toggleActivity(target.dataset.activityId);
  else if (action === "confirm-undo") openDialog("undo", { activityId: target.dataset.activityId }, target);
  else if (action === "undo-activity") await undoActivity(target.dataset.activityId);
  else if (action === "confirm-return-copy") openDialog("return-copy", { activityId: target.dataset.activityId }, target);
  else if (action === "return-copy") await undoActivity(target.dataset.activityId, true);
  else if (action === "toggle-rule") {
    store.toggleRule(target.dataset.ruleId);
    setStatus("Rule status updated for future fixture plans.");
  } else if (action === "inspect-rule") openDialog("inspect-rule", { ruleId: target.dataset.ruleId }, target);
  else if (action === "edit-rule") openDialog("edit-rule", { ruleId: target.dataset.ruleId }, target);
  else if (action === "confirm-remove-rule") openDialog("remove-rule", { ruleId: target.dataset.ruleId }, target);
  else if (action === "remove-rule") {
    store.removeRule(target.dataset.ruleId);
    closeDialog();
    setStatus("Locked rule removed from the fixture.");
  } else if (action === "change-location") setStatus("Prototype only: no real folder chooser opens.");
  else if (action === "export-placeholder") setStatus("Export is a placeholder. No file was created.");
  else if (action === "confirm-reset") openDialog("reset", {}, target);
  else if (action === "reset-prototype") {
    store.reset();
    closeDialog({ restoreFocus: false });
    window.history.replaceState(null, "", "#/home");
    render({ focusMain: true });
    setStatus("Prototype reset to the complete initial fixture.");
  }
}

async function handleSubmit(event) {
  const form = event.target.closest("form[data-submit]");
  if (!form) return;
  event.preventDefault();
  const data = new FormData(form);
  if (form.dataset.submit === "move-selected") {
    const destination = String(data.get("destinationFolderId") || "");
    if (!destination) {
      setAlert("Choose a planned destination.");
      return;
    }
    if (!store.moveSelected(destination)) {
      showDialogError("Choose a different destination for Review items and avoid filename collisions in the planned result.");
      return;
    }
    closeDialog();
    setStatus("Plan updated. No files moved.");
  } else if (form.dataset.submit === "edit-rule") {
    store.updateRule(form.dataset.ruleId, {
      match: String(data.get("match") || ""),
      destinationFolderId: String(data.get("destinationFolderId") || ""),
    });
    closeDialog();
    setStatus("Locked rule updated for future fixture plans.");
  }
}

function handleChange(event) {
  const target = event.target;
  if (target.matches("[data-setting-path]")) {
    const value = target.type === "checkbox" ? target.checked : target.value;
    store.updateSetting(target.dataset.settingPath, value);
    setStatus("Setting saved in this browser.");
  } else if (target.matches("[data-settings-section]")) {
    navigate(`#/settings?section=${encodeURIComponent(target.value)}`);
  }
}

document.addEventListener("click", (event) => {
  const primaryLink = event.target.closest(".primary-nav [data-route]");
  if (primaryLink) {
    const workflow = workflowForRoute[primaryLink.dataset.route];
    if (workflow) {
      event.preventDefault();
      requestWorkflow(workflow, primaryLink);
      return;
    }
  }
  const actionTarget = event.target.closest("[data-action]");
  if (!actionTarget) return;
  handleAction(actionTarget, event).catch((error) => {
    setAlert(error.message || "The sample interaction could not be completed.");
  });
});

document.addEventListener("submit", (event) => {
  handleSubmit(event).catch((error) => setAlert(error.message || "The form could not be completed."));
});
document.addEventListener("change", handleChange);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && elements.dialog.open) {
    if (elements.dialog.dataset.locked === "true") {
      event.preventDefault();
      setAlert("The active sample operation must finish first.");
    } else {
      event.preventDefault();
      closeDialog();
    }
    return;
  }

  if (elements.dialog.open) return;

  const responsiveInspector = activeResponsiveInspector();
  if (!responsiveInspector) return;
  if (event.key === "Escape") {
    event.preventDefault();
    if (responsiveInspector.classList.contains("duplicate-inspector")) store.toggleDuplicateInspector(false);
    else store.toggleInspector(false);
    restoreResponsiveInspectorFocus();
    setStatus("Inspector closed.");
    return;
  }
  if (event.key === "Tab") {
    const focusable = [...responsiveInspector.querySelectorAll("button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])")]
      .filter((element) => !element.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && (document.activeElement === first || !responsiveInspector.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !responsiveInspector.contains(document.activeElement))) {
      event.preventDefault();
      first.focus();
    }
  }
});

elements.dialog.addEventListener("cancel", (event) => {
  if (elements.dialog.dataset.locked === "true") {
    event.preventDefault();
    setAlert("The active sample operation must finish first.");
  }
});

elements.dialog.addEventListener("close", () => {
  if (lastDialogTrigger?.isConnected) lastDialogTrigger.focus();
  lastDialogTrigger = null;
});

window.addEventListener("hashchange", () => {
  if (!guardHashRoute()) render({ focusMain: true });
});

responsiveInspectorQuery.addEventListener("change", () => {
  if (store) render({ focusMain: true });
});

async function start() {
  try {
    const response = await fetch("./fixtures.json", { cache: "no-store" });
    if (!response.ok) throw new Error("Fixture data could not be loaded.");
    const fixture = await response.json();
    store = createStore(fixture);
    const recoveredMessage = /^Recovered|^Cleared a completed/.test(store.getState().lastMutation?.label || "")
      ? store.getState().workflow.feedback
      : "";
    store.subscribe(() => render());
    if (!window.location.hash) window.history.replaceState(null, "", "#/home");
    guardHashRoute();
    render();
    if (recoveredMessage) setStatus(recoveredMessage);
  } catch (error) {
    elements.app.setAttribute("aria-busy", "false");
    elements.app.innerHTML = `<section class="empty-state" role="alert"><h1>Prototype unavailable</h1><p>${escapeHtml(error.message)}</p><p>Run the focused prototype through the documented loopback-only static server.</p></section>`;
  }
}

start();
