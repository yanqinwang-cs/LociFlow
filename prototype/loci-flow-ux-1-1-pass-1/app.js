import { createStore } from "./store.js";
import { dialogContent, escapeHtml, renderOnboarding, renderPage } from "./views.js";

const allowedRoutes = new Set(["home", "search", "files", "organize", "duplicates", "activity", "settings"]);
const elements = {
  shell: document.querySelector("#app-shell"),
  content: document.querySelector("#page-content"),
  main: document.querySelector("#main-content"),
  navigation: document.querySelector("#primary-nav"),
  onboarding: document.querySelector("#onboarding-root"),
  dialog: document.querySelector("#prototype-dialog"),
  dialogEyebrow: document.querySelector("#dialog-eyebrow"),
  dialogTitle: document.querySelector("#dialog-title"),
  dialogDescription: document.querySelector("#dialog-description"),
  dialogContent: document.querySelector("#dialog-content"),
  toast: document.querySelector("#toast-region"),
  progress: document.querySelector("#progress-region"),
};

let store;
let returnFocus = null;
let toastTimer = null;
let rendering = false;

function parseRoute() {
  const hash = window.location.hash || "#/home";
  const raw = hash.startsWith("#/") ? hash.slice(2) : "home";
  const [path, query = ""] = raw.split("?");
  const name = allowedRoutes.has(path) ? path : "home";
  return { name, params: new URLSearchParams(query) };
}

function navigate(path) {
  if (window.location.hash === path) render({ focusMain: true });
  else window.location.hash = path;
}

function updateNavigation(route) {
  elements.navigation.querySelectorAll("a[data-route]").forEach((link) => {
    const current = link.dataset.route === route.name;
    link.classList.toggle("active", current);
    if (current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const summary = store.planSummary();
  const planCount = elements.navigation.querySelector("[data-nav-count='plan']");
  if (planCount) {
    const count = summary.reviewRequired + summary.conflicts;
    planCount.textContent = String(count);
    planCount.hidden = count === 0;
    planCount.setAttribute("aria-label", `${count} organization plan ${count === 1 ? "item" : "items"} need attention`);
  }
  const duplicateCount = elements.navigation.querySelector("[data-nav-count='duplicates']");
  if (duplicateCount) {
    const count = store.getState().duplicates.groups.filter((group) => group.status === "active").length;
    duplicateCount.textContent = String(count);
    duplicateCount.hidden = count === 0;
    duplicateCount.setAttribute("aria-label", `${count} active exact-copy ${count === 1 ? "group" : "groups"}`);
  }
}

function render({ focusMain = false } = {}) {
  if (!store || rendering) return;
  rendering = true;
  const state = store.getState();
  const route = parseRoute();
  const onboardingVisible = !state.onboarding.completed;
  elements.onboarding.hidden = !onboardingVisible;
  elements.onboarding.innerHTML = onboardingVisible ? renderOnboarding(store) : "";
  elements.shell.toggleAttribute("inert", onboardingVisible);
  elements.shell.setAttribute("aria-hidden", onboardingVisible ? "true" : "false");

  const page = renderPage(store, route);
  elements.content.innerHTML = page.html;
  elements.content.setAttribute("aria-busy", "false");
  document.title = `${page.title} · Loci Flow`;
  updateNavigation(route);
  rendering = false;

  if (focusMain && !onboardingVisible) {
    window.requestAnimationFrame(() => elements.main.focus({ preventScroll: false }));
  } else if (onboardingVisible) {
    window.requestAnimationFrame(() => elements.onboarding.querySelector("button, input")?.focus());
  }
}

function toast(message, { tone = "success" } = {}) {
  window.clearTimeout(toastTimer);
  elements.toast.className = `toast-region toast-${tone}`;
  elements.toast.textContent = message;
  elements.toast.classList.add("is-visible");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 4200);
}

function setProgress(message) {
  elements.progress.textContent = message;
}

function setDialog(view, type, id = "") {
  elements.dialogEyebrow.textContent = view.eyebrow || "";
  elements.dialogTitle.textContent = view.title || "";
  elements.dialogDescription.textContent = view.description || "";
  elements.dialogContent.innerHTML = view.html || "";
  elements.dialog.dataset.dialogType = type;
  elements.dialog.dataset.dialogId = id;
}

function openDialog(type, id = "", trigger = document.activeElement) {
  returnFocus = trigger instanceof HTMLElement ? trigger : null;
  setDialog(dialogContent(store, type, id), type, id);
  if (!elements.dialog.open) elements.dialog.showModal();
  window.requestAnimationFrame(() => elements.dialogContent.querySelector("[autofocus], input, select, button")?.focus());
}

function openCustomDialog(view, type, id = "", trigger = document.activeElement) {
  returnFocus = trigger instanceof HTMLElement ? trigger : null;
  setDialog(view, type, id);
  if (!elements.dialog.open) elements.dialog.showModal();
  window.requestAnimationFrame(() => elements.dialogContent.querySelector("[autofocus], input, select, button")?.focus());
}

function closeDialog() {
  if (elements.dialog.open) elements.dialog.close();
}

elements.dialog.addEventListener("close", () => {
  elements.dialog.dataset.dialogType = "";
  elements.dialog.dataset.dialogId = "";
  window.requestAnimationFrame(() => returnFocus?.isConnected && returnFocus.focus());
});

elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) closeDialog();
});

const wait = (milliseconds) =>
  new Promise((resolve) => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.setTimeout(resolve, reduced ? Math.min(milliseconds, 80) : milliseconds);
  });

async function showStages(stages, finish, { finalTitle = "Verified" } = {}) {
  const stageList = stages.map((stage, index) => `<li data-stage-index="${index}"><span aria-hidden="true"></span><strong>${escapeHtml(stage.title)}</strong><small>${escapeHtml(stage.detail)}</small></li>`).join("");
  setDialog(
    {
      eyebrow: "Simulated operation",
      title: stages[0].title,
      description: "The prototype reports progress and verification separately.",
      html: `<ol class="operation-stages">${stageList}</ol><div class="stage-progress"><span></span></div>`,
    },
    "progress",
  );
  if (!elements.dialog.open) elements.dialog.showModal();
  const rows = [...elements.dialogContent.querySelectorAll("[data-stage-index]")];
  for (let index = 0; index < stages.length; index += 1) {
    rows.forEach((row, rowIndex) => {
      row.classList.toggle("is-current", rowIndex === index);
      row.classList.toggle("is-complete", rowIndex < index);
    });
    elements.dialogTitle.textContent = stages[index].title;
    setProgress(`${stages[index].title}. ${stages[index].detail}`);
    await wait(stages[index].duration || 900);
  }
  const result = await finish();
  rows.forEach((row) => row.classList.add("is-complete"));
  elements.dialogTitle.textContent = finalTitle;
  setProgress(`${finalTitle}.`);
  return result;
}

function formError(form, message) {
  const target = form.querySelector("[data-form-error]");
  if (target) target.textContent = message;
  else toast(message, { tone: "danger" });
}

function destinationChoiceDialog({ title, description, action, fileId = "", conflictId = "" }, trigger) {
  const folders = store.getState().folders.filter((folder) => ["folder-elarian", "folder-northstar", "folder-client-forms", "folder-receipts", "folder-cs1010s"].includes(folder.id));
  openCustomDialog(
    {
      eyebrow: "Choose destination",
      title,
      description,
      html: `<form data-action="${escapeHtml(action)}" ${fileId ? `data-file-id="${escapeHtml(fileId)}"` : ""} ${conflictId ? `data-conflict-id="${escapeHtml(conflictId)}"` : ""}><fieldset class="destination-options"><legend>Available sample folders</legend>${folders.map((folder, index) => `<label class="destination-option"><input type="radio" name="destinationFolderId" value="${escapeHtml(folder.id)}" ${index === 1 ? "checked" : ""}><span><strong>${escapeHtml(store.breadcrumb(folder.id).map((item) => item.name).join(" › "))}</strong><small>${folder.fileCount || 0} files already here</small></span></label>`).join("")}</fieldset><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="submit">Use destination</button></div></form>`,
    },
    action,
    fileId || conflictId,
    trigger,
  );
}

function groupChoiceDialog(fileId, trigger) {
  const groups = store.getState().plan.groups;
  openCustomDialog(
    {
      eyebrow: "Plan customization",
      title: "Move this file to another proposed group",
      description: "The same plan will update its destination, group counts, and annotations.",
      html: `<form data-action="confirm-plan-group-change" data-file-id="${escapeHtml(fileId)}"><fieldset class="destination-options"><legend>Prepared groups</legend>${groups.map((group, index) => `<label class="destination-option"><input type="radio" name="groupId" value="${escapeHtml(group.id)}" ${index === 0 ? "checked" : ""}><span><strong>${escapeHtml(group.label || group.name)}</strong><small>${escapeHtml(group.reason)}</small></span></label>`).join("")}</fieldset><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="submit">Move to group</button></div></form>`,
    },
    "plan-group-change",
    fileId,
    trigger,
  );
}

async function handleClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;

  if (action === "close-dialog") {
    if (!target.matches("a[href]")) event.preventDefault();
    closeDialog();
    return;
  }
  if (action === "onboarding-continue") store.setOnboardingStep(1);
  else if (action === "onboarding-back") store.setOnboardingStep(0);
  else if (action === "onboarding-complete") {
    store.completeOnboarding();
    navigate("#/home");
    toast("Sample Downloads is ready.");
  } else if (action === "file-details") openDialog("file-details", target.dataset.fileId, target);
  else if (action === "rename-file") openDialog("rename-file", target.dataset.fileId, target);
  else if (action === "move-file") openDialog("move-file", target.dataset.fileId, target);
  else if (action === "open-file") toast(`Prototype: “${store.fileById(target.dataset.fileId)?.name || "File"}” would open through the operating system.`, { tone: "neutral" });
  else if (action === "show-in-finder") toast(`Prototype: “${store.fileById(target.dataset.fileId)?.name || "File"}” would be revealed at its sample location.`, { tone: "neutral" });
  else if (action === "add-to-plan") {
    const added = store.addToPlan(target.dataset.fileId);
    toast(added ? "Added to the active organization plan. The placement needs review." : "This file is already included in the plan.", { tone: added ? "success" : "neutral" });
  } else if (action === "aggregate-preview" || action === "aggregate-files" || action === "unchanged-preview") {
    const folder = store.folderById(target.dataset.folderId);
    const files = folder ? store.filesInFolder(folder.parentId || folder.id).slice(0, 12) : store.getState().files.filter((file) => !store.planChangeForFile(file.id)).slice(0, 12);
    openCustomDialog({ eyebrow: "Bounded preview", title: folder?.name || "Unchanged sample files", description: folder?.description || "A filename-led subset stands in for a large collection.", html: `<div class="file-list">${files.map((file) => `<button class="preview-file-button" type="button" data-action="file-details" data-file-id="${escapeHtml(file.id)}"><strong>${escapeHtml(file.name)}</strong><span>${escapeHtml(file.type)} · ${escapeHtml(file.sizeLabel)}</span></button>`).join("")}</div>` }, "aggregate", folder?.id || "unchanged", target);
  } else if (action === "resolve-exception") {
    store.resolveException(target.dataset.exceptionId, target.dataset.resolution, target.dataset.groupId || null);
    toast("Review item resolved. The plan summary is up to date.");
  } else if (action === "resolve-conflict") {
    if (target.dataset.resolution === "change_destination") {
      destinationChoiceDialog({ title: "Choose another destination for Notes.docx", description: "Select a destination without the filename collision.", action: "confirm-conflict-destination", conflictId: target.dataset.conflictId }, target);
    } else {
      store.resolveConflict(target.dataset.conflictId, target.dataset.resolution, target.dataset.resolution === "rename" ? "Notes from Downloads.docx" : null);
      toast("Blocking conflict resolved. Apply becomes available when every review item is also resolved.");
    }
  } else if (action === "toggle-plan-group") {
    store.toggleGroup(target.dataset.groupId);
    toast("Plan group updated.");
  } else if (action === "plan-group-details") openDialog("plan-group", target.dataset.groupId, target);
  else if (action === "rename-plan-folder") {
    const group = store.groupById(target.dataset.groupId);
    openCustomDialog({ eyebrow: "Plan customization", title: "Rename proposed folder", description: "This changes the folder label in the active plan only.", html: `<form data-action="confirm-plan-folder-name" data-group-id="${escapeHtml(group.id)}"><label>Folder name<input name="folderName" value="${escapeHtml(group.proposedName || group.label || group.name)}" required autofocus></label><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="submit">Update folder</button></div><p class="form-error" data-form-error role="alert"></p></form>` }, "rename-plan-folder", group.id, target);
  } else if (action === "exclude-plan-file") {
    store.excludePlanFile(target.dataset.fileId);
    toast("File will be left unchanged.");
    closeDialog();
  } else if (action === "change-plan-destination") destinationChoiceDialog({ title: "Change planned destination", description: "Only this file changes destination; the plan remains one coherent plan.", action: "confirm-plan-destination", fileId: target.dataset.fileId }, target);
  else if (action === "move-plan-group") groupChoiceDialog(target.dataset.fileId, target);
  else if (action === "edit-suggested-name") {
    const file = store.fileById(target.dataset.fileId);
    const change = store.planChangeForFile(target.dataset.fileId);
    openCustomDialog({ eyebrow: "Filename suggestion", title: `Edit suggestion for ${file.name}`, description: "The existing extension is preserved.", html: `<form data-action="confirm-suggested-name" data-file-id="${escapeHtml(file.id)}"><label>Suggested filename<input name="suggestedName" value="${escapeHtml(change.suggestedName)}" required autofocus></label><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="reject-suggested-name" data-file-id="${escapeHtml(file.id)}">Leave unchanged</button><button class="button button-primary" type="submit">Use edited name</button></div><p class="form-error" data-form-error role="alert"></p></form>` }, "edit-suggested-name", file.id, target);
  } else if (action === "reject-suggested-name") {
    store.decideSuggestedName(target.dataset.fileId, "reject");
    closeDialog();
    toast("Filename will remain unchanged.");
  } else if (action === "undo-plan-edit") toast(store.undoPlanEdit() ? "Planning edit undone." : "There is no planning edit to undo.", { tone: "neutral" });
  else if (action === "check-plan") {
    store.checkPlanFreshness();
    toast("Plan checked. Harmless fixture changes were incorporated automatically.");
  } else if (action === "dismiss-plan") {
    openCustomDialog({ eyebrow: "Organization plan", title: "Dismiss this plan?", description: "It will disappear from Home and Organize, and can be regenerated later.", html: `<div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-danger" type="button" data-action="confirm-dismiss-plan">Dismiss plan</button></div>` }, "dismiss-plan", "", target);
  } else if (action === "confirm-dismiss-plan") {
    store.dismissPlan();
    closeDialog();
    toast("Organization plan dismissed.");
  } else if (action === "regenerate-plan") {
    store.regeneratePlan();
    toast("A refreshed sample plan is ready.");
  } else if (action === "confirm-apply-plan") openDialog("apply-plan", "", target);
  else if (action === "apply-plan") await applyPlanSequence();
  else if (action === "select-working-copy") {
    store.selectWorkingCopy(target.dataset.groupId, target.dataset.fileId);
    toast("Working copy updated. Related plan sections were marked for review where needed.");
  } else if (action === "keep-duplicates") {
    store.keepDuplicateGroup(target.dataset.groupId);
    toast("All exact copies will remain where they are.");
  } else if (action === "consolidate-duplicates") openDialog("consolidate-duplicates", target.dataset.groupId, target);
  else if (action === "confirm-consolidate") await consolidateSequence(target.dataset.groupId);
  else if (action === "activity-details") openDialog("activity-details", target.dataset.activityId, target);
  else if (action === "confirm-undo-activity") {
    const operation = store.getState().activity.find((item) => item.id === target.dataset.activityId);
    openCustomDialog({ eyebrow: "Short-term failsafe", title: operation?.undo?.scope === "limited" || operation?.undoAvailability === "limited" ? "Use limited recovery?" : "Undo this recent operation?", description: operation?.undo?.detail || "Only cleanly reversible sample changes will be returned.", html: `<div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="button" data-action="undo-activity" data-activity-id="${escapeHtml(target.dataset.activityId)}">Undo and verify</button></div>` }, "undo-activity", target.dataset.activityId, target);
  } else if (action === "undo-activity") await undoSequence(target.dataset.activityId);
  else if (action === "sample-location-control") toast("Prototype only: no real folder chooser or permission request opens.", { tone: "neutral" });
  else if (action === "reset-learned") {
    store.resetLearnedHabits();
    toast("Learned behavior reset for this prototype.");
  } else if (action === "index-placeholder") toast("Fixture index control simulated. No files were indexed.", { tone: "neutral" });
  else if (action === "export-placeholder") toast("Export is a placeholder in Pass 1. No file was created.", { tone: "neutral" });
  else if (action === "inspect-rule") openDialog("inspect-rule", target.dataset.ruleId, target);
  else if (action === "toggle-rule") {
    const rules = Array.isArray(store.getState().settings.lockedRules) ? store.getState().settings.lockedRules : store.getState().settings.lockedRules.items;
    const rule = rules.find((item) => item.id === target.dataset.ruleId);
    store.updateLockedRule(rule.id, { active: !rule.active });
    toast(rule.active ? "Locked rule resumed." : "Locked rule paused.");
  } else if (action === "edit-rule") {
    const rules = Array.isArray(store.getState().settings.lockedRules) ? store.getState().settings.lockedRules : store.getState().settings.lockedRules.items;
    const rule = rules.find((item) => item.id === target.dataset.ruleId);
    openCustomDialog({ eyebrow: "Locked rule", title: "Edit deterministic rule", description: "Preview remains available before this fixture rule affects future plans.", html: `<form data-action="confirm-rule-edit" data-rule-id="${escapeHtml(rule.id)}"><label>Filename match<input name="match" value="${escapeHtml(rule.match)}" required autofocus></label><label>Destination<select name="destinationFolderId">${["folder-cs1010s", "folder-cs1010x", "folder-study"].map((folderId) => { const folder = store.folderById(folderId); return `<option value="${folderId}" ${rule.destinationFolderId === folderId ? "selected" : ""}>${escapeHtml(store.breadcrumb(folderId).map((item) => item.name).join(" › "))}</option>`; }).join("")}</select></label><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="submit">Save rule</button></div></form>` }, "edit-rule", rule.id, target);
  } else if (action === "remove-rule") {
    openCustomDialog({ eyebrow: "Locked rule", title: "Remove this rule?", description: "The fixture plan will keep its current decisions; future regeneration will no longer use this rule.", html: `<div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-danger" type="button" data-action="confirm-remove-rule" data-rule-id="${escapeHtml(target.dataset.ruleId)}">Remove rule</button></div>` }, "remove-rule", target.dataset.ruleId, target);
  } else if (action === "confirm-remove-rule") {
    store.removeLockedRule(target.dataset.ruleId);
    closeDialog();
    toast("Locked rule removed.");
  } else if (action === "confirm-reset") openDialog("confirm-reset", "", target);
  else if (action === "reset-prototype") {
    store.reset();
    closeDialog();
    navigate("#/home");
    toast("Prototype restored to its initial fixture state.");
  }
}

async function applyPlanSequence() {
  const outcome = store.getState().demoOutcome;
  const stages = outcome === "failed"
    ? [
        { title: "Rechecking plan", detail: "Checking freshness, destinations, and resolved decisions." },
        { title: "Stopped before changes", detail: "The selected demo outcome fails before applying." },
      ]
    : [
        { title: "Rechecking plan", detail: "Checking freshness, destinations, and resolved decisions." },
        { title: "Applying", detail: "Updating the fixture plan in a controlled sequence." },
        { title: "Verifying", detail: "Comparing every simulated result with the intended plan." },
      ];
  let operation;
  await showStages(stages, async () => {
    operation = store.applyPlan(outcome);
  }, { finalTitle: outcome === "verified" ? "Applied and verified" : operation?.status || "Result recorded" });
  const current = operation || store.getState().activity[0];
  const completedStages = stages
    .map((stage) => `<li class="is-complete"><span aria-hidden="true"></span><strong>${escapeHtml(stage.title)}</strong><small>${escapeHtml(stage.detail)}</small></li>`)
    .join("");
  setDialog(
    {
      eyebrow: "Verified result",
      title: current.status,
      description: current.verification,
      html: `<ol class="operation-stages" aria-label="Completed operation stages">${completedStages}</ol><div class="result-state ${current.status === "Applied and verified" ? "is-success" : "is-warning"}"><span aria-hidden="true">${current.status === "Applied and verified" ? "✓" : "!"}</span><div><strong>${escapeHtml(current.summary)}</strong><p>${escapeHtml(current.verification)}</p></div></div><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Close</button><a class="button button-primary" href="#/activity" data-action="close-dialog">View Activity</a></div>`,
    },
    "apply-result",
    current.id,
  );
}

async function consolidateSequence(groupId) {
  await showStages(
    [
      { title: "Rechecking copies", detail: "Confirming the copies are still exact." },
      { title: "Consolidating", detail: "Moving extras to the reversible fixture holding area." },
      { title: "Verifying", detail: "Checking the working copy and held extras." },
    ],
    async () => store.consolidateDuplicateGroup(groupId),
    { finalTitle: "Applied and verified" },
  );
  closeDialog();
  toast("Extra copies consolidated and verified. Nothing was deleted.");
}

async function undoSequence(activityId) {
  await showStages(
    [
      { title: "Checking reversibility", detail: "Confirming the affected sample files have not diverged." },
      { title: "Reversing", detail: "Returning only cleanly reversible fixture changes." },
      { title: "Verifying", detail: "Checking the returned names and locations." },
    ],
    async () => store.undoActivity(activityId),
    { finalTitle: "Undone and verified" },
  );
  closeDialog();
  toast("The available sample changes were undone and verified.");
}

async function handleSubmit(event) {
  const form = event.target.closest("form[data-action]");
  if (!form) return;
  event.preventDefault();
  const action = form.dataset.action;
  const data = new FormData(form);

  if (action === "home-search" || action === "search-submit") {
    const query = String(data.get("q") || "").trim();
    navigate(query ? `#/search?q=${encodeURIComponent(query)}` : "#/search");
  } else if (action === "search-refine") {
    const params = new URLSearchParams();
    for (const key of ["q", "type", "date", "location", "size"]) {
      const value = String(data.get(key) || "");
      if (value && value !== "all") params.set(key, value);
    }
    navigate(`#/search?${params.toString()}`);
  } else if (action === "confirm-rename") {
    try {
      store.validateRename(form.dataset.fileId, data.get("newName"));
      await showStages(
        [
          { title: "Checking name", detail: "Preserving the extension and checking for collisions." },
          { title: "Applying rename", detail: "Updating the canonical fixture file." },
          { title: "Verifying", detail: "Rechecking Search, Files, related files, and the active plan." },
        ],
        async () => store.renameFile(form.dataset.fileId, data.get("newName")),
        { finalTitle: "Renamed and verified" },
      );
      closeDialog();
      toast("Filename updated everywhere and verified.");
    } catch (error) {
      if (elements.dialog.dataset.dialogType === "rename-file") formError(form, error.message);
      else {
        closeDialog();
        toast(error.message, { tone: "danger" });
      }
    }
  } else if (action === "confirm-move") {
    const destination = String(data.get("destinationFolderId") || "");
    try {
      const validation = store.validateMove(form.dataset.fileId, destination);
      if (validation.sameLocation) {
        formError(form, "This file is already in that destination.");
        return;
      }
      if (validation.collision) {
        const file = validation.file;
        openCustomDialog({ eyebrow: "Filename conflict", title: `${file.name} already exists there`, description: "Choose a new filename for the incoming sample file, then confirm the move.", html: `<form data-action="confirm-conflicted-move" data-file-id="${escapeHtml(file.id)}" data-destination-folder-id="${escapeHtml(destination)}"><label>New filename<input name="newName" value="${escapeHtml(file.name.replace(/(\.[^.]+)$/, " moved$1"))}" required autofocus></label><div class="move-preview"><span>Destination</span><strong>${escapeHtml(store.breadcrumb(destination).map((folder) => folder.name).join(" › "))}</strong></div><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="move-file" data-file-id="${escapeHtml(file.id)}">Back</button><button class="button button-primary" type="submit">Rename and move</button></div><p class="form-error" data-form-error role="alert"></p></form>` }, "move-conflict", file.id);
        return;
      }
      await runMove(form.dataset.fileId, destination);
    } catch (error) {
      formError(form, error.message);
    }
  } else if (action === "confirm-conflicted-move") {
    try {
      store.renameFile(form.dataset.fileId, data.get("newName"));
      await runMove(form.dataset.fileId, form.dataset.destinationFolderId);
    } catch (error) {
      formError(form, error.message);
    }
  } else if (action === "confirm-conflict-destination") {
    store.resolveConflict(form.dataset.conflictId, "change_destination", String(data.get("destinationFolderId")));
    closeDialog();
    toast("Blocking conflict resolved with another destination.");
  } else if (action === "confirm-plan-folder-name") {
    try {
      store.renameProposedFolder(form.dataset.groupId, data.get("folderName"));
      closeDialog();
      toast("Proposed folder name updated.");
    } catch (error) {
      formError(form, error.message);
    }
  } else if (action === "confirm-plan-destination") {
    store.changePlanDestination(form.dataset.fileId, String(data.get("destinationFolderId")));
    closeDialog();
    toast("Planned destination updated.");
  } else if (action === "confirm-plan-group-change") {
    store.movePlanFileToGroup(form.dataset.fileId, String(data.get("groupId")));
    closeDialog();
    toast("File moved to another proposed group.");
  } else if (action === "confirm-suggested-name") {
    try {
      store.decideSuggestedName(form.dataset.fileId, "edit", data.get("suggestedName"));
      closeDialog();
      toast("Filename suggestion updated.");
    } catch (error) {
      formError(form, error.message);
    }
  } else if (action === "confirm-rule-edit") {
    store.updateLockedRule(form.dataset.ruleId, { match: String(data.get("match")), destinationFolderId: String(data.get("destinationFolderId")) });
    closeDialog();
    toast("Locked rule updated. Matches remain previewable.");
  }
}

async function runMove(fileId, destinationFolderId) {
  await showStages(
    [
      { title: "Checking destination", detail: "Checking the sample path and filename for conflicts." },
      { title: "Moving file", detail: "Updating the canonical fixture location." },
      { title: "Verifying", detail: "Rechecking Search, Files, nearby context, and the active plan." },
    ],
    async () => store.moveFile(fileId, destinationFolderId),
    { finalTitle: "Moved and verified" },
  );
  closeDialog();
  toast("Sample location updated everywhere and verified.");
}

function handleChange(event) {
  const target = event.target;
  if (target.matches("[data-setting-path]")) {
    const value = target.type === "checkbox" ? target.checked : target.value;
    store.updateSetting(target.dataset.settingPath, value);
    toast("Setting saved for this prototype.");
  } else if (target.matches("[data-demo-outcome]")) {
    store.setDemoOutcome(target.value);
    toast("Apply demo outcome updated.");
  } else if (target.matches("[data-plan-freshness]")) {
    store.setPlanFreshness(target.value);
    toast("Plan freshness demo updated.");
  } else if (target.matches("input[name='destinationFolderId']") && elements.dialog.open) {
    const preview = elements.dialog.querySelector("[data-move-preview]");
    if (preview) preview.textContent = store.breadcrumb(target.value).map((folder) => folder.name).join(" › ");
  }
}

function handleInput(event) {
  if (!event.target.matches("[data-destination-search]")) return;
  const query = event.target.value.trim().toLocaleLowerCase();
  elements.dialog.querySelectorAll("[data-destination-name]").forEach((option) => {
    option.hidden = query && !option.dataset.destinationName.includes(query);
  });
}

document.addEventListener("click", (event) => {
  handleClick(event).catch((error) => {
    closeDialog();
    toast(error.message || "That sample interaction could not be completed.", { tone: "danger" });
  });
});
document.addEventListener("submit", (event) => {
  handleSubmit(event).catch((error) => {
    closeDialog();
    toast(error.message || "That sample interaction could not be completed.", { tone: "danger" });
  });
});
document.addEventListener("change", handleChange);
document.addEventListener("input", handleInput);
window.addEventListener("hashchange", () => render({ focusMain: true }));

async function start() {
  try {
    const response = await fetch("./fixtures.json", { cache: "no-store" });
    if (!response.ok) throw new Error("Fixture data could not be loaded.");
    const fixture = await response.json();
    store = createStore(fixture);
    store.subscribe(() => render());
    if (!window.location.hash) window.history.replaceState(null, "", "#/home");
    render();
  } catch (error) {
    elements.content.setAttribute("aria-busy", "false");
    elements.content.innerHTML = `<section class="empty-state"><h1 id="page-heading">Prototype unavailable</h1><p>${escapeHtml(error.message)}</p><p>Run this directory through the documented loopback-only static server.</p></section>`;
  }
}

start();
