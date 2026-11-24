import sample from "./fixtures.json" with { type: "json" };
import { createStore } from "./store.js";
import { renderDialog, renderRoute } from "./views.js";

const store = createStore(sample);
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

let dialogType = null;
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

function syncShell() {
  const state = store.getState();
  document.documentElement.dataset.theme = state.theme;
  document.querySelectorAll('[data-action="set-theme"]').forEach((control) => {
    control.setAttribute("aria-pressed", String(control.dataset.themeValue === state.theme));
  });
  elements.nav.querySelectorAll("[data-route]").forEach((link) => {
    link.setAttribute("aria-current", link.dataset.route === state.route ? "page" : "false");
  });
  const organizeCount = elements.nav.querySelector("[data-organize-count]");
  const binCount = elements.nav.querySelector("[data-bin-count]");
  if (organizeCount) organizeCount.textContent = String(state.changes.filter((change) => change.decision === "pending").length);
  if (binCount) binCount.textContent = String(state.duplicateBinItems.length);
}

function render(options = {}) {
  const route = routeFromHash();
  if (store.getState().route !== route) store.setRoute(route);
  elements.app.innerHTML = renderRoute(store, route);
  elements.app.setAttribute("aria-busy", "false");
  document.title = `${route === "duplicate-bin" ? "Duplicate Bin" : route[0].toUpperCase() + route.slice(1)} · Loci Flow`;
  syncShell();
  if (options.focusChanges) {
    window.requestAnimationFrame(() => document.querySelector("#changes-to-confirm")?.focus({ preventScroll: false }));
  }
  if (options.focusMain) elements.main.focus({ preventScroll: true });
  if (options.focusDetails) window.requestAnimationFrame(() => document.querySelector("#details-heading")?.focus({ preventScroll: true }));
}

function navigate(route, options = {}) {
  if (!routes.has(route)) return;
  store.setRoute(route);
  if (window.location.hash !== `#/${route}`) window.location.hash = `#/${route}`;
  render(options);
}

function setDialog(type, payload = {}) {
  const view = renderDialog(store, type, payload);
  dialogType = type;
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

function closeDialog({ restoreFocus = true } = {}) {
  if (elements.dialog.open) elements.dialog.close();
  dialogType = null;
  dialogPayload = {};
  if (restoreFocus) dialogTrigger?.focus?.();
  dialogTrigger = null;
}

function checkedFolder(name) {
  const value = elements.dialog.querySelector(`input[name="${name}"]:checked`)?.value || "Sample Downloads";
  return value.split("|");
}

function standardBuilderValue() {
  return {
    action: elements.dialog.querySelector('input[name="standard-action"]:checked')?.value || "Sort similar files",
    recognize: elements.dialog.querySelector("#standard-recognize")?.value || "Person name",
    grouping: elements.dialog.querySelector('input[name="standard-grouping"]:checked')?.value || "Create a folder for each value",
    fileType: Boolean(elements.dialog.querySelector("#standard-file-type")?.checked),
    folder: checkedFolder("standard-folder"),
  };
}

function suggestedStandardValue() {
  return {
    name: elements.dialog.querySelector("#suggestion-name")?.value.trim() || "Untitled Standard",
    description: elements.dialog.querySelector("#suggestion-description")?.value.trim() || "Review this organization instruction.",
    appliesTo: (elements.dialog.querySelector("#suggestion-folder")?.value || "Sample Downloads")
      .split("›")
      .map((part) => part.trim())
      .filter(Boolean),
  };
}

function afterChoice(message) {
  announce(message);
  render({ focusDetails: true });
}

function actionFor(target, event) {
  const action = target.dataset.action;
  if (!action) return;

  if (action === "history-back") return window.history.back();
  if (action === "history-forward") return window.history.forward();
  if (action === "set-theme") {
    store.setTheme(target.dataset.themeValue);
    announce(`${target.textContent.trim()} appearance selected.`);
    return render();
  }
  if (action === "continue-organizing") {
    store.openOrganize();
    return navigate("organize", { focusChanges: true });
  }
  if (action === "open-duplicate-bin") return navigate("duplicate-bin", { focusMain: true });
  if (action === "open-activity") return navigate("activity", { focusMain: true });
  if (action === "open-standards") return navigate("standards", { focusMain: true });
  if (action === "check-files" || action === "check-again") {
    store.startCheck();
    announce(`${store.waitingChanges().length} sample changes are ready to organize.`);
    return navigate("home", { focusMain: true });
  }
  if (action === "set-style") {
    store.setOrganizationStyle(target.dataset.style);
    announce(`${target.textContent.trim().split("\n")[0]} style selected. No file moved.`);
    return render({ focusChanges: true });
  }
  if (action === "filter-changes") {
    store.setActionFilter(target.dataset.type);
    return render({ focusChanges: true });
  }
  if (action === "accept-recommended") {
    const ids = store.acceptRecommended();
    announce(`${ids.length} recommended changes accepted. No file moved.`);
    return render({ focusChanges: true });
  }
  if (action === "toggle-change") {
    store.selectChange(target.dataset.changeId, { additive: true, range: event.shiftKey });
    return render({ focusDetails: true });
  }
  if (action === "activate-change") {
    store.selectChange(target.dataset.changeId, { additive: event.metaKey || event.ctrlKey, range: event.shiftKey });
    return render({ focusDetails: true });
  }
  if (action === "select-all-visible") {
    target.checked ? store.selectAllVisible() : store.clearSelection();
    return render({ focusDetails: true });
  }
  if (action === "bulk-accept") {
    const ids = store.bulkAction("accept");
    return afterChoice(`${ids.length} compatible changes accepted together. No file moved.`);
  }
  if (action === "bulk-keep") {
    const ids = store.bulkAction("keep");
    return afterChoice(`${ids.length} changes kept together. No file moved.`);
  }
  if (action === "bulk-remove") {
    const ids = store.bulkAction("remove");
    return afterChoice(`${ids.length} changes removed together. No file moved.`);
  }
  if (action === "bulk-folder") return openDialog("folder", { bulk: true }, target);
  if (action === "decide-change") {
    store.decideChange(target.dataset.changeId, target.dataset.choice);
    return afterChoice("Choice saved. No file moved.");
  }
  if (action === "open-folder") {
    store.selectChange(target.dataset.changeId);
    return openDialog("folder", { changeId: target.dataset.changeId }, target);
  }
  if (action === "confirm-folder") {
    const folder = checkedFolder("change-folder-choice");
    const ids = store.bulkAction("change-folder", folder);
    closeDialog({ restoreFocus: false });
    announce(`${ids.length} ${ids.length === 1 ? "change uses" : "changes use"} the chosen folder. No file moved.`);
    return render({ focusDetails: true });
  }
  if (action === "dismiss-reuse") {
    store.dismissReuseSuggestion();
    return render({ focusDetails: true });
  }
  if (action === "review-and-approve") {
    if (!store.beginApproval()) return;
    render({ focusChanges: true });
    return openDialog("approval", {}, target);
  }
  if (action === "approve-test-move") {
    const batch = store.approveTestMove();
    closeDialog({ restoreFocus: false });
    if (!batch) return;
    announce("Test move complete. No real files were changed.");
    return navigate("organize", { focusMain: true });
  }
  if (action === "verify-test-move") {
    const result = store.verifyTestMove();
    if (!result) return;
    announce(result.passed ? "Test move verified. No real files were changed." : "The sample checks found a problem.");
    return render({ focusMain: true });
  }
  if (action === "complete-workflow") {
    if (!store.completeWorkflow()) return;
    announce("Everything is organized.");
    return navigate("home", { focusMain: true });
  }
  if (action === "toggle-bin") {
    store.toggleBinSelection(target.dataset.binId);
    return render();
  }
  if (action === "select-all-bin") {
    target.checked ? store.selectAllBin() : store.clearBinSelection();
    return render();
  }
  if (action === "restore-one") {
    const ids = store.restoreDuplicates([target.dataset.binId]);
    announce(`${ids.length} file restored to its stored folder.`);
    return render();
  }
  if (action === "restore-selected") {
    const ids = store.restoreDuplicates();
    announce(`${ids.length} ${ids.length === 1 ? "file" : "files"} restored to stored folders.`);
    return render();
  }
  if (action === "toggle-activity") {
    store.toggleActivity(target.dataset.activityId);
    return render();
  }
  if (action === "expand-all-activity") {
    store.setAllActivityExpanded(true);
    return render();
  }
  if (action === "collapse-all-activity") {
    store.setAllActivityExpanded(false);
    return render();
  }
  if (action === "undo-move") {
    if (!store.undoLatestMove()) return;
    announce("The latest file move was undone in the sample.");
    return navigate("home", { focusMain: true });
  }
  if (action === "interpret-preference") {
    const text = document.querySelector("#preference-input")?.value || "";
    store.interpretPreference(text);
    announce("Preference interpreted for review. No Standard was activated.");
    return render();
  }
  if (action === "clarify-preference") {
    store.clarifyInterpretation(target.dataset.interpretChoice);
    announce("Clarification saved. Suggested Standards are ready to review.");
    return render();
  }
  if (action === "remove-suggested-standard") {
    store.removeSuggestedStandard(target.dataset.suggestionId);
    return render();
  }
  if (action === "edit-suggested-standard") return openDialog("standard-builder", { suggestedStandardId: target.dataset.suggestionId }, target);
  if (action === "save-suggested-standard") {
    store.updateSuggestedStandard(target.dataset.suggestionId, suggestedStandardValue());
    closeDialog({ restoreFocus: false });
    announce("Suggested Standard updated. It is not active yet.");
    return render();
  }
  if (action === "confirm-suggested-standards") {
    const count = store.confirmSuggestedStandards();
    announce(`${count} Standards added. No file moved.`);
    return render();
  }
  if (action === "cancel-interpretation") {
    store.cancelInterpretation();
    return render();
  }
  if (action === "open-standard-builder") return openDialog("standard-builder", { standardId: target.dataset.standardId || null }, target);
  if (action === "save-standard") {
    const standard = store.saveStandard(standardBuilderValue());
    closeDialog({ restoreFocus: false });
    announce(`${standard.name} saved. No file moved.`);
    return navigate("standards", { focusMain: true });
  }
  if (action === "toggle-standard") {
    store.toggleStandard(target.dataset.standardId);
    announce("Standard updated. No file moved.");
    return render();
  }
  if (action === "open-standard-conflict") return openDialog("standard-conflict", {}, target);
  if (action === "set-settings-section") {
    store.setSettingsSection(target.dataset.section);
    return render();
  }
  if (action === "toggle-notification") {
    store.toggleNotification(target.dataset.settingKey);
    return render();
  }
  if (action === "reset-prototype") {
    store.reset();
    closeDialog({ restoreFocus: false });
    announce("The starting sample has been restored.");
    return navigate("home", { focusMain: true });
  }
  if (action === "close-dialog") return closeDialog();
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (target) actionFor(target, event);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && elements.dialog.open) {
    event.preventDefault();
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
  store.setRoute(routeFromHash());
  render({ focusMain: true });
});

store.subscribe(syncShell);
if (!window.location.hash) window.location.hash = "#/home";
store.setRoute(routeFromHash());
render();
