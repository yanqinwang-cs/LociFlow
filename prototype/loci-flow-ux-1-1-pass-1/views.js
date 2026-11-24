const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const slug = (value) => String(value || "").toLocaleLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");

const humanize = (value) =>
  String(value || "")
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());

const dateLabel = (value) => {
  if (!value) return "Unknown";
  return new Intl.DateTimeFormat("en-SG", { day: "numeric", month: "short", year: "numeric" }).format(new Date(value));
};

const relativeDate = (value) => {
  if (!value) return "Unknown";
  const days = Math.round((new Date("2026-07-12T12:00:00+08:00") - new Date(value)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 14) return `${days} days ago`;
  return dateLabel(value);
};

const freshnessLabel = (value) => {
  const key = String(value || "up_to_date").toLocaleLowerCase().replaceAll(" ", "_");
  return {
    up_to_date: "Up to date",
    updated_automatically: "Updated automatically",
    needs_review: "Needs review",
    no_longer_reliable: "No longer reliable",
  }[key] || humanize(value);
};

const statusClass = (value) => {
  const text = String(value || "").toLocaleLowerCase();
  if (text.includes("verified") || text.includes("ready") || text.includes("up to date")) return "status-success";
  if (text.includes("conflict") || text.includes("failed") || text.includes("unavailable")) return "status-danger";
  if (text.includes("review") || text.includes("partial") || text.includes("verification")) return "status-warning";
  return "status-neutral";
};

const fileGlyph = (file) => {
  const type = String(file?.type || "file").toLocaleLowerCase();
  if (type.includes("image") || type.includes("png") || type.includes("jpeg") || type.includes("svg")) return "IMG";
  if (type.includes("spreadsheet") || type.includes("csv")) return "SHT";
  if (type.includes("presentation")) return "SLD";
  if (type.includes("pdf")) return "PDF";
  if (type.includes("word")) return "DOC";
  return "FILE";
};

function pageHeader(eyebrow, title, intro, actions = "") {
  return `
    <header class="page-header">
      <div>
        <p class="eyebrow">${escapeHtml(eyebrow)}</p>
        <h1 id="page-heading">${escapeHtml(title)}</h1>
        <p class="page-intro">${escapeHtml(intro)}</p>
      </div>
      ${actions ? `<div class="page-actions">${actions}</div>` : ""}
    </header>`;
}

function button(label, action, options = {}) {
  const { kind = "secondary", attrs = "", disabled = false } = options;
  return `<button class="button button-${kind}" type="button" data-action="${escapeHtml(action)}" ${attrs} ${disabled ? "disabled" : ""}>${escapeHtml(label)}</button>`;
}

function emptyState(title, description, action = "") {
  return `<section class="empty-state" role="status"><span class="empty-mark" aria-hidden="true">◇</span><h2>${escapeHtml(title)}</h2><p>${escapeHtml(description)}</p>${action}</section>`;
}

function breadcrumbHtml(store, folderId) {
  return `<nav class="breadcrumbs" aria-label="Folder breadcrumb">${store
    .breadcrumb(folderId)
    .map((folder, index, all) =>
      index === all.length - 1
        ? `<span aria-current="page">${escapeHtml(folder.name)}</span>`
        : `<a href="#/files?folder=${encodeURIComponent(folder.id)}">${escapeHtml(folder.name)}</a>`,
    )
    .join("")}</nav>`;
}

function stat(label, value, detail = "") {
  return `<div class="stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</div>`;
}

function fileRow(store, file, { annotation = "", planned = false } = {}) {
  return `
    <article class="file-row ${planned ? "is-planned" : ""}">
      <button class="file-leading" type="button" data-action="file-details" data-file-id="${escapeHtml(file.id)}" aria-label="View details for ${escapeHtml(file.name)}">
        <span class="file-icon" aria-hidden="true">${fileGlyph(file)}</span>
        <span><strong>${escapeHtml(file.name)}</strong><small>${escapeHtml(file.type)} · ${escapeHtml(file.sizeLabel)} · ${escapeHtml(relativeDate(file.modifiedAt))}</small></span>
      </button>
      ${annotation ? `<span class="badge ${statusClass(annotation)}">${escapeHtml(annotation)}</span>` : ""}
      <button class="icon-button" type="button" data-action="file-details" data-file-id="${escapeHtml(file.id)}" aria-label="More about ${escapeHtml(file.name)}">•••</button>
    </article>`;
}

function planAnnotation(store, change) {
  const plan = store.getState().plan;
  if (change.status === "excluded") return "Excluded";
  if (change.status === "satisfied_directly") return "Completed directly";
  if (plan.exceptions.some((item) => item.fileId === change.fileId && item.status === "unresolved")) return "Needs review";
  if (plan.conflicts.some((item) => item.fileId === change.fileId && item.status === "unresolved")) return "Conflict";
  if (change.suggestedName && !change.moveCompletedDirectly) return "Moving + renamed";
  if (change.suggestedName && change.moveCompletedDirectly) return "Rename planned";
  return "Moving here";
}

function renderHome(store) {
  const state = store.getState();
  const root = store.folderById(state.location.folderId);
  const plan = store.planSummary();
  const planActive = state.plan.status === "active";
  const childFolders = store.childFolders(root.id).filter((folder) => folder.kind !== "aggregate").slice(0, 7);
  const recent = state.activity[0];

  return {
    title: "Home",
    html: `
      ${pageHeader("Sample Downloads", "Find what you need. Shape what comes next.", "Search your sample files, understand their place, and act through visible plans.")}
      <section class="search-hero" aria-labelledby="home-search-heading">
        <div><p class="eyebrow">Start here</p><h2 id="home-search-heading">What are you looking for?</h2></div>
        <form class="search-form" data-action="home-search">
          <label class="visually-hidden" for="home-search-input">Search sample files</label>
          <span class="search-symbol" aria-hidden="true"></span>
          <input id="home-search-input" name="q" type="search" placeholder="Try CS1010X 2016 Finals" autocomplete="off">
          <button class="button button-primary" type="submit">Search</button>
        </form>
        <div class="query-suggestions" aria-label="Example searches">
          ${["CS1010X 2016 Finals", "Elarian architecture", "INV-2048", "V1_5"].map((query) => `<a href="#/search?q=${encodeURIComponent(query)}">${escapeHtml(query)}</a>`).join("")}
        </div>
      </section>

      <div class="dashboard-grid home-priority-grid">
        ${
          planActive
            ? `<section class="card plan-summary feature-card" aria-labelledby="home-plan-heading">
                <div class="card-heading"><div><p class="eyebrow">Active organization plan</p><h2 id="home-plan-heading">Downloads, with a clearer shape</h2></div><span class="badge ${statusClass(state.plan.freshness)}">${escapeHtml(freshnessLabel(state.plan.freshness))}</span></div>
                <div class="summary-grid">${stat("Files", String(plan.files))}${stat("Destinations", String(plan.destinations))}${stat("Review required", String(plan.reviewRequired))}${stat("Conflict", String(plan.conflicts))}</div>
                <p class="muted">Prepared manually. Nothing applies without your confirmation.</p>
                <a class="button button-primary" href="#/organize">Review organization plan</a>
              </section>`
            : `<section class="card feature-card">${emptyState("No active organization plan", "The last plan was applied or dismissed. Prepare another whenever it is useful.", `<a class="button button-primary" href="#/organize">Open Organize</a>`)}</section>`
        }
        <section class="card location-card" aria-labelledby="location-heading">
          <div class="card-heading"><div><p class="eyebrow">Current location</p><h2 id="location-heading">${escapeHtml(root.name)}</h2></div><span class="badge status-success">Up to date</span></div>
          <p>One approved sample location, checked quietly in this fixture.</p>
          <dl class="compact-facts"><div><dt>Loose files</dt><dd>84</dd></div><div><dt>Last checked</dt><dd>${escapeHtml(relativeDate(state.location.lastCheckedAt))}</dd></div></dl>
        </section>
      </div>

      <div class="content-grid home-secondary-grid">
        <section class="card folder-map" aria-labelledby="folder-map-heading">
          <div class="card-heading"><div><p class="eyebrow">Folder orientation</p><h2 id="folder-map-heading">A broad view of Downloads</h2></div><a href="#/files?folder=${encodeURIComponent(root.id)}">Explore Files</a></div>
          <div class="map-root"><span class="map-root-mark" aria-hidden="true"></span><div><strong>Downloads</strong><small>7 major branches · 84 loose files</small></div></div>
          <div class="map-branches">
            ${childFolders.map((folder) => `<a class="map-branch" href="#/files?folder=${encodeURIComponent(folder.id)}"><span class="folder-tab" aria-hidden="true"></span><strong>${escapeHtml(folder.name)}</strong><small>${escapeHtml(String(folder.fileCount || 0))} files${folder.folderCount ? ` · ${folder.folderCount} folders` : ""}</small></a>`).join("")}
          </div>
          <p class="map-note">This map shows hierarchy and concentration, not conceptual distance.</p>
        </section>
        <section class="card recent-activity" aria-labelledby="recent-heading">
          <div class="card-heading"><div><p class="eyebrow">Recent activity</p><h2 id="recent-heading">${escapeHtml(recent?.summary || "Nothing recent")}</h2></div><span class="badge ${statusClass(recent?.status)}">${escapeHtml(recent?.status || "Quiet")}</span></div>
          ${recent ? `<p>${escapeHtml(recent.verification)}</p><p class="muted">${escapeHtml(relativeDate(recent.occurredAt))}</p><button class="text-button" type="button" data-action="activity-details" data-activity-id="${escapeHtml(recent.id)}">View operation details</button>` : ""}
          <a class="button button-secondary" href="#/activity">See all activity</a>
        </section>
      </div>`,
  };
}

function renderSearch(store, route) {
  const fixture = store.getFixture();
  const query = route.params.get("q") || "";
  const refinements = {
    type: route.params.get("type") || "all",
    location: route.params.get("location") || "all",
    size: route.params.get("size") || "all",
    date: route.params.get("date") || "all",
  };
  const results = query ? store.search(query, refinements) : [];
  const folders = store.getState().folders.filter((folder) => folder.kind !== "aggregate" && !folder.hidden);
  const types = [...new Set(store.getState().files.map((file) => file.type))].sort();

  return {
    title: "Search",
    html: `
      ${pageHeader("Search", query ? `Results for “${query}”` : "Find a file without tracing folders", query ? `${results.length} ${results.length === 1 ? "result" : "results"}, ranked by the evidence visible in this sample.` : "Start with a filename, phrase, identifier, or project you remember.")}
      <section class="search-workspace" aria-labelledby="search-field-label">
        <form class="search-form search-form-large" data-action="search-submit">
          <label id="search-field-label" class="visually-hidden" for="search-page-input">Search sample files</label>
          <span class="search-symbol" aria-hidden="true"></span>
          <input id="search-page-input" name="q" type="search" value="${escapeHtml(query)}" placeholder="Filename, identifier, phrase, or project" autofocus>
          <button class="button button-primary" type="submit">Search</button>
        </form>
        ${
          query
            ? `<details class="advanced-search" ${Object.values(refinements).some((value) => value !== "all") ? "open" : ""}>
                <summary>Advanced search</summary>
                <form class="refinement-grid" data-action="search-refine">
                  <input type="hidden" name="q" value="${escapeHtml(query)}">
                  <label>File type<select name="type"><option value="all">Any type</option>${types.map((type) => `<option value="${escapeHtml(type)}" ${refinements.type === type ? "selected" : ""}>${escapeHtml(type)}</option>`).join("")}</select></label>
                  <label>Date<select name="date"><option value="all">Any date</option><option value="recent" ${refinements.date === "recent" ? "selected" : ""}>Recent</option><option value="older" ${refinements.date === "older" ? "selected" : ""}>Older</option></select></label>
                  <label>Location<select name="location"><option value="all">Anywhere in Downloads</option>${folders.map((folder) => `<option value="${escapeHtml(folder.id)}" ${refinements.location === folder.id ? "selected" : ""}>${escapeHtml(store.breadcrumb(folder.id).map((part) => part.name).join(" › "))}</option>`).join("")}</select></label>
                  <label>Size<select name="size"><option value="all">Any size</option><option value="small" ${refinements.size === "small" ? "selected" : ""}>Under about 5 MB</option><option value="large" ${refinements.size === "large" ? "selected" : ""}>About 5 MB or larger</option></select></label>
                  <button class="button button-secondary" type="submit">Refine results</button>
                </form>
              </details>`
            : ""
        }
      </section>
      ${
        !query
          ? `<section class="search-start card"><h2>Try a sample search</h2><div class="scenario-grid">${fixture.searchScenarios.slice(0, 7).map((scenario) => `<a href="#/search?q=${encodeURIComponent(scenario.query)}"><span class="badge status-neutral">${escapeHtml(humanize(scenario.kind))}</span><strong>${escapeHtml(scenario.query)}</strong></a>`).join("")}</div></section>`
          : results.length
            ? `<section class="search-results" aria-label="Search results">${results.map((result, index) => {
                const file = result.file;
                return `<article class="result-card ${slug(result.band)}">
                  <div class="result-rank" aria-label="Result ${index + 1}">${index + 1}</div>
                  <div class="result-main"><div class="result-title-row"><button class="result-title" type="button" data-action="file-details" data-file-id="${escapeHtml(file.id)}">${escapeHtml(file.name)}</button><span class="match-band ${slug(result.band)}"><span aria-hidden="true"></span>${escapeHtml(result.band)}</span></div>
                  <p class="breadcrumb-text">${escapeHtml(store.filePath(file.id))}</p><p>${escapeHtml(result.explanation)}</p><ul class="inline-facts"><li>${escapeHtml(file.type)}</li><li>${escapeHtml(file.sizeLabel)}</li><li>Modified ${escapeHtml(dateLabel(file.modifiedAt))}</li></ul></div>
                  <div class="result-actions">${button("Open", "open-file", { attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("Show in Finder", "show-in-finder", { attrs: `data-file-id="${escapeHtml(file.id)}"` })}<div class="result-more">${button("Rename", "rename-file", { kind: "quiet", attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("Move", "move-file", { kind: "quiet", attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("File details", "file-details", { kind: "quiet", attrs: `data-file-id="${escapeHtml(file.id)}"` })}</div></div>
                </article>`;
              }).join("")}</section>`
            : emptyState("No matching sample files", "Try a broader filename, identifier, or project. Weak evidence is not promoted just to fill this page.", `<a class="button button-secondary" href="#/search">Clear search</a>`)
      }`,
  };
}

function renderFiles(store, route) {
  const state = store.getState();
  const folderId = route.params.get("folder") || state.location.folderId;
  const folder = store.folderById(folderId) || store.folderById(state.location.folderId);
  const view = route.params.get("view") || "current";
  const childFolders = store.childFolders(folder.id);
  const directFiles = store.filesInFolder(folder.id);
  const changes = state.plan?.status === "active" ? state.plan.changes.filter((change) => change.status !== "excluded") : [];
  const relevantChanges = changes.filter((change) => change.sourceFolderId === folder.id || change.destinationFolderId === folder.id);
  const plannedFileIds = new Set(relevantChanges.map((change) => change.fileId));
  const files = view === "changes" ? state.files.filter((file) => plannedFileIds.has(file.id)) : directFiles;
  const displayFiles = files.slice(0, 14);

  return {
    title: "Files",
    html: `
      ${breadcrumbHtml(store, folder.id)}
      ${pageHeader("Files", folder.name, "Focus one level at a time, inspect bounded contents, then narrow into a branch.", `<span class="badge status-neutral">${escapeHtml(String(folder.fileCount || directFiles.length))} files</span>`)}
      <div class="segmented-control" role="group" aria-label="File structure view">
        ${["current", "planned", "changes"].map((mode) => `<a href="#/files?folder=${encodeURIComponent(folder.id)}&view=${mode}" class="${view === mode ? "active" : ""}" ${view === mode ? 'aria-current="page"' : ""}>${mode === "changes" ? "Changes only" : humanize(mode)}</a>`).join("")}
      </div>
      ${view === "planned" && state.plan.status !== "active" ? `<div class="inline-message">There is no active plan to overlay. Current structure is shown instead.</div>` : ""}
      <section class="folder-focus" aria-labelledby="branches-heading">
        <div class="section-heading"><div><p class="eyebrow">Current level</p><h2 id="branches-heading">Branches in ${escapeHtml(folder.name)}</h2></div><span>${childFolders.length} visible branches</span></div>
        <div class="folder-grid">
          ${
            childFolders.length
              ? childFolders.map((child) => child.kind === "aggregate"
                  ? `<button class="folder-card aggregate-card" type="button" data-action="aggregate-preview" data-folder-id="${escapeHtml(child.id)}"><span class="aggregate-mark" aria-hidden="true">＋</span><strong>${escapeHtml(child.name)}</strong><small>${escapeHtml(child.description || "Open a bounded preview")}</small></button>`
                  : `<a class="folder-card" href="#/files?folder=${encodeURIComponent(child.id)}&view=${encodeURIComponent(view)}"><span class="folder-shape" aria-hidden="true"></span><strong>${escapeHtml(child.name)}</strong><small>${escapeHtml(String(child.fileCount || 0))} files${child.folderCount ? ` · ${child.folderCount} folders` : ""}</small>${view !== "current" && changes.some((change) => change.destinationFolderId === child.id) ? `<span class="badge status-success">Moving here</span>` : ""}</a>`).join("")
              : `<p class="muted">No child folders at this level.</p>`
          }
        </div>
      </section>
      <section class="card contents-preview" aria-labelledby="contents-heading">
        <div class="section-heading"><div><p class="eyebrow">Bounded preview</p><h2 id="contents-heading">${view === "changes" ? "Affected files" : `Contents of ${escapeHtml(folder.name)}`}</h2></div><span>${files.length} represented</span></div>
        <div class="file-list" tabindex="0" aria-label="Scrollable filename list">
          ${displayFiles.length ? displayFiles.map((file) => {
            const change = changes.find((item) => item.fileId === file.id);
            const annotation = change ? planAnnotation(store, change) : "Unchanged";
            return fileRow(store, file, { annotation: view === "current" ? "" : annotation, planned: Boolean(change) });
          }).join("") : `<p class="empty-copy">No files in this bounded view.</p>`}
          ${files.length > displayFiles.length ? `<button class="aggregate-row" type="button" data-action="aggregate-files" data-folder-id="${escapeHtml(folder.id)}">${files.length - displayFiles.length} other files not shown <span>Open preview</span></button>` : ""}
        </div>
        <p class="map-note">Physical hierarchy is primary. Relationships appear inside file details with a plain-language reason.</p>
      </section>`,
  };
}

function renderException(store, exception) {
  const file = store.fileById(exception.fileId);
  const resolved = exception.status === "resolved";
  const groups = store.getState().plan.groups;
  return `<article class="exception-card ${resolved ? "is-resolved" : ""}">
    <div class="card-heading"><div><span class="tier-label">Tier 2 · Review required</span><h3>${escapeHtml(file?.name || exception.title)}</h3></div><span class="badge ${resolved ? "status-success" : "status-warning"}">${resolved ? "Resolved" : "Review required"}</span></div>
    <p>${escapeHtml(exception.detail)}</p>
    ${resolved ? `<p><strong>Decision:</strong> ${escapeHtml(humanize(exception.resolution))}</p>` : `<div class="decision-actions">
      ${exception.id.includes("final") ? groups.slice(0, 2).map((group) => button(`Place with ${group.label || group.name}`, "resolve-exception", { attrs: `data-exception-id="${escapeHtml(exception.id)}" data-resolution="choose_group" data-group-id="${escapeHtml(group.id)}"` })).join("") : button("Accept suggested group", "resolve-exception", { attrs: `data-exception-id="${escapeHtml(exception.id)}" data-resolution="accept"` })}
      ${button("Leave unchanged", "resolve-exception", { kind: "quiet", attrs: `data-exception-id="${escapeHtml(exception.id)}" data-resolution="leave_unchanged"` })}
    </div>`}
  </article>`;
}

function renderConflict(store, conflict) {
  const file = store.fileById(conflict.fileId);
  const resolved = conflict.status === "resolved";
  return `<article class="conflict-card ${resolved ? "is-resolved" : ""}">
    <div class="card-heading"><div><span class="tier-label">Tier 1 · Blocking</span><h3>${escapeHtml(file?.name || conflict.title)}</h3></div><span class="badge ${resolved ? "status-success" : "status-danger"}">${resolved ? "Resolved" : "Conflict"}</span></div>
    <p>${escapeHtml(conflict.detail)}</p>
    ${resolved ? `<p><strong>Decision:</strong> ${escapeHtml(humanize(conflict.resolution))}</p>` : `<div class="decision-actions">${button("Rename incoming file", "resolve-conflict", { kind: "primary", attrs: `data-conflict-id="${escapeHtml(conflict.id)}" data-resolution="rename"` })}${button("Choose another destination", "resolve-conflict", { attrs: `data-conflict-id="${escapeHtml(conflict.id)}" data-resolution="change_destination"` })}${button("Leave unchanged", "resolve-conflict", { kind: "quiet", attrs: `data-conflict-id="${escapeHtml(conflict.id)}" data-resolution="leave_unchanged"` })}</div>`}
  </article>`;
}

function renderOrganize(store) {
  const state = store.getState();
  const plan = state.plan;
  const summary = store.planSummary();
  if (plan.status !== "active") {
    const message = plan.status === "dismissed" ? "The plan was dismissed." : plan.status === "applied" ? "The plan was applied and verified." : "This plan is no longer active.";
    return { title: "Organize", html: `${pageHeader("Organize", "No active organization plan", message)}${emptyState("Downloads has no active plan", "Prepare one fixture plan at a time. Applied plans remain in Activity.", button("Regenerate sample plan", "regenerate-plan", { kind: "primary" }))}` };
  }
  const changes = plan.changes.filter((change) => change.status !== "excluded" && change.status !== "satisfied_directly");

  return {
    title: "Organize",
    html: `
      ${pageHeader("Organization plan", "A clearer Downloads, prepared for review", "Review the shape, focus on exceptions, and adjust only where useful.", `<span class="badge ${statusClass(plan.freshness)}">${escapeHtml(freshnessLabel(plan.freshness))}</span>`)}
      <section class="plan-overview card" aria-labelledby="plan-summary-heading">
        <div class="card-heading"><div><p class="eyebrow">Prepared manually</p><h2 id="plan-summary-heading">${escapeHtml(plan.name || "Organization plan for Downloads")}</h2></div><div class="compact-actions">${button("Check files", "check-plan", { kind: "quiet" })}${button("Dismiss plan", "dismiss-plan", { kind: "quiet" })}</div></div>
        <div class="summary-grid">${stat("Files changing", String(summary.files))}${stat("Destinations", String(summary.destinations))}${stat("Review required", String(summary.reviewRequired))}${stat("Blocking conflicts", String(summary.conflicts))}</div>
        <div class="plan-readiness ${summary.canApply ? "ready" : "blocked"}"><span class="readiness-mark" aria-hidden="true"></span><div><strong>${summary.canApply ? "Ready to apply" : "Finish the highlighted decisions"}</strong><p>${summary.canApply ? "A final recheck and one concise confirmation come next." : "Both review items and the blocking conflict can be handled in any order."}</p></div>${button("Apply plan", "confirm-apply-plan", { kind: "primary", disabled: !summary.canApply })}</div>
        ${state.planUndo?.length ? `<div class="undo-strip" role="status"><span>Your latest planning edit is saved.</span>${button("Undo planning edit", "undo-plan-edit", { kind: "quiet" })}</div>` : ""}
      </section>

      ${summary.conflicts || plan.conflicts.some((item) => item.status === "resolved") ? `<section aria-labelledby="conflicts-heading"><div class="section-heading"><div><p class="eyebrow">Blocking issues</p><h2 id="conflicts-heading">Destination conflict</h2></div></div><div class="exception-stack">${plan.conflicts.map((item) => renderConflict(store, item)).join("")}</div></section>` : ""}
      <section aria-labelledby="exceptions-heading"><div class="section-heading"><div><p class="eyebrow">Review required</p><h2 id="exceptions-heading">Two focused decisions</h2></div><span>Resolve in any order</span></div><div class="exception-stack">${plan.exceptions.map((item) => renderException(store, item)).join("")}</div></section>

      <section class="card planned-structure" aria-labelledby="planned-tree-heading">
        <div class="section-heading"><div><p class="eyebrow">Focused planned structure</p><h2 id="planned-tree-heading">Only affected branches</h2></div><span>${summary.destinations} destinations</span></div>
        <div class="plan-tree"><div class="tree-root"><span class="tree-node-icon" aria-hidden="true"></span><strong>Downloads</strong><span>${summary.files} included in this plan</span></div>
          <div class="tree-branches">${plan.groups.map((group) => {
            const groupChanges = changes.filter((change) => change.groupId === group.id);
            const destination = store.folderById(group.destinationFolderId);
            return `<article class="tree-branch ${group.included === false ? "is-excluded" : ""}"><div class="tree-row"><span class="tree-connector" aria-hidden="true"></span><div><strong>${escapeHtml(group.proposedName || group.label || group.name)}</strong><small>${escapeHtml(store.breadcrumb(destination.id).map((part) => part.name).join(" › "))}</small></div><span class="badge ${group.needsReview ? "status-warning" : group.included === false ? "status-neutral" : "status-success"}">${group.included === false ? "Excluded" : group.needsReview ? "Needs review" : `${groupChanges.length} moving here`}</span></div>
              <p>${escapeHtml(group.reviewReason || group.reason)}</p><div class="tree-samples">${groupChanges.slice(0, 3).map((change) => { const file = store.fileById(change.fileId); return file ? `<span>${escapeHtml(change.suggestedName || file.name)} <small>${escapeHtml(planAnnotation(store, change))}</small></span>` : ""; }).join("")}${groupChanges.length > 3 ? `<button type="button" data-action="plan-group-details" data-group-id="${escapeHtml(group.id)}">${groupChanges.length - 3} more changes</button>` : ""}</div>
              <div class="compact-actions">${button(group.included === false ? "Include group" : "Exclude group", "toggle-plan-group", { kind: "quiet", attrs: `data-group-id="${escapeHtml(group.id)}"` })}${button("Inspect group", "plan-group-details", { attrs: `data-group-id="${escapeHtml(group.id)}"` })}${button("Rename proposed folder", "rename-plan-folder", { kind: "quiet", attrs: `data-group-id="${escapeHtml(group.id)}"` })}</div></article>`;
          }).join("")}</div>
          <button class="aggregate-row" type="button" data-action="unchanged-preview">79 other files not shown <span>Unchanged</span></button>
        </div>
        <details class="change-list-alternative"><summary>View exact changes as a list</summary><div class="file-list">${changes.map((change) => { const file = store.fileById(change.fileId); return file ? fileRow(store, file, { annotation: planAnnotation(store, change), planned: true }) : ""; }).join("")}</div></details>
      </section>`,
  };
}

function duplicateFileChoice(store, group, fileId) {
  const file = store.fileById(fileId);
  if (!file) return "";
  const preferred = group.preferredFileId === fileId;
  return `<article class="copy-row ${preferred ? "is-preferred" : ""}"><span class="file-icon" aria-hidden="true">${fileGlyph(file)}</span><div><strong>${escapeHtml(file.name)}</strong><p>${escapeHtml(store.filePath(file.id))}</p><small>${escapeHtml(file.sizeLabel)} · modified ${escapeHtml(dateLabel(file.modifiedAt))}</small></div><div>${preferred ? `<span class="badge status-success">Recommended working copy</span>` : button("Choose as working copy", "select-working-copy", { kind: "quiet", attrs: `data-group-id="${escapeHtml(group.id)}" data-file-id="${escapeHtml(file.id)}"` })}</div></article>`;
}

function renderDuplicates(store) {
  const state = store.getState();
  const groups = state.duplicates.groups;
  const active = groups.filter((group) => group.status === "active");
  return {
    title: "Duplicates",
    html: `
      ${pageHeader("Exact copies", "Choose the copy you want to work from", "Exact-copy decisions stay separate from the organization plan and never delete sample files.")}
      <div class="guidance-banner"><span aria-hidden="true">i</span><p><strong>Need to make changes?</strong> Open the preferred working copy. Loci Flow will recheck all copies before applying.</p></div>
      <section class="summary-strip" aria-label="Duplicate summary">${stat("Active exact-copy groups", String(active.length))}${stat("Decisions made", String(groups.filter((group) => group.decision && group.decision !== "undecided").length))}${stat("Copies in holding area", String(state.duplicates.holdingArea.items?.length || state.files.filter((file) => file.inHoldingArea).length), "Holding copies does not reclaim space")}</section>
      <section class="duplicate-stack" aria-label="Exact-copy groups">
        ${groups.map((group) => `<article class="duplicate-card ${group.status !== "active" ? "is-complete" : ""}"><div class="card-heading"><div><p class="eyebrow">Exact-copy group</p><h2>${escapeHtml(group.title)}</h2></div><span class="badge ${group.decision && group.decision !== "undecided" ? "status-success" : "status-warning"}">${escapeHtml(group.decision && group.decision !== "undecided" ? humanize(group.decision) : "Decision needed")}</span></div><p>${escapeHtml(group.recommendationReason || group.reason)}</p><div class="copy-list">${group.fileIds.map((fileId) => duplicateFileChoice(store, group, fileId)).join("")}</div><div class="duplicate-actions">${group.preferredFileId ? button("Open preferred copy", "open-file", { attrs: `data-file-id="${escapeHtml(group.preferredFileId)}"` }) : ""}${button("Keep all", "keep-duplicates", { kind: "quiet", attrs: `data-group-id="${escapeHtml(group.id)}"` })}${button("Consolidate extra copies", "consolidate-duplicates", { kind: "primary", attrs: `data-group-id="${escapeHtml(group.id)}"`, disabled: !group.preferredFileId || group.status !== "active" })}</div><p class="safety-copy">Extra copies move to a reversible holding area. They are not deleted, and storage is not recovered.</p></article>`).join("")}
      </section>
      <section class="recheck-section" aria-labelledby="rechecks-heading"><div class="section-heading"><div><p class="eyebrow">Recheck examples</p><h2 id="rechecks-heading">When a copy changes</h2></div></div><div class="content-grid">${state.duplicates.rechecks.map((item) => `<article class="card recheck-card"><span class="badge status-neutral">Rechecked</span><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.message)}</p>${item.guidance ? `<p class="muted">${escapeHtml(item.guidance)}</p>` : ""}${item.changedFileId ? `<button class="text-button" type="button" data-action="file-details" data-file-id="${escapeHtml(item.changedFileId)}">Inspect changed file</button>` : ""}</article>`).join("")}</div></section>`,
  };
}

function renderActivity(store, route) {
  const state = store.getState();
  const filter = route.params.get("filter") || "all";
  const operations = filter === "all" ? state.activity : state.activity.filter((item) => slug(item.status) === filter);
  const statuses = [...new Set(state.activity.map((item) => item.status))];
  return {
    title: "Activity",
    html: `
      ${pageHeader("Activity", "What changed, and what was verified", "Readable operation history with a short-term failsafe where reversal remains truthful.")}
      <nav class="filter-pills" aria-label="Activity filters"><a href="#/activity" class="${filter === "all" ? "active" : ""}">All</a>${statuses.map((status) => `<a href="#/activity?filter=${encodeURIComponent(slug(status))}" class="${filter === slug(status) ? "active" : ""}">${escapeHtml(status)}</a>`).join("")}</nav>
      <section class="timeline" aria-label="Operation history">${operations.map((operation) => {
        const undo = operation.undo || { available: ["available", "limited"].includes(operation.undoAvailability), label: operation.undoAvailability === "limited" ? "Limited recovery" : "Undo changes", detail: "This operation has a short-term recovery option." };
        return `<article class="activity-item"><span class="timeline-dot ${statusClass(operation.status)}" aria-hidden="true"></span><div class="activity-card"><div class="card-heading"><div><p class="eyebrow">${escapeHtml(relativeDate(operation.occurredAt))}</p><h2>${escapeHtml(operation.title || operation.summary)}</h2></div><span class="badge ${statusClass(operation.status)}">${escapeHtml(operation.status)}</span></div><p>${escapeHtml(operation.summary)}</p><p class="verification-copy"><strong>Verification:</strong> ${escapeHtml(operation.verification)}</p><div class="activity-actions">${button("View details", "activity-details", { attrs: `data-activity-id="${escapeHtml(operation.id)}"` })}${undo.available ? button(undo.label || "Undo changes", "confirm-undo-activity", { kind: "quiet", attrs: `data-activity-id="${escapeHtml(operation.id)}"` }) : ""}</div>${!undo.available && operation.status === "Undo unavailable" ? `<div class="unavailable-note"><strong>Undo is no longer available.</strong><span>Prepare a new organization plan.</span><a href="#/organize">Open Organize</a></div>` : ""}</div></article>`;
      }).join("")}</section>`,
  };
}

const settingsSections = [
  ["locations", "Locations"],
  ["organization", "Organization"],
  ["automation", "Automation"],
  ["locked-rules", "Locked rules"],
  ["duplicates", "Duplicates"],
  ["search-indexing", "Search and indexing"],
  ["notifications", "Notifications"],
  ["privacy", "Privacy and telemetry"],
  ["advanced", "Advanced"],
];

function toggleSetting(label, path, checked, description = "") {
  return `<div class="setting-row"><div><strong>${escapeHtml(label)}</strong>${description ? `<p>${escapeHtml(description)}</p>` : ""}</div><label class="switch"><input type="checkbox" data-setting-path="${escapeHtml(path)}" ${checked ? "checked" : ""}><span aria-hidden="true"></span><span class="visually-hidden">${checked ? "Disable" : "Enable"} ${escapeHtml(label)}</span></label></div>`;
}

function radioGroup(legend, path, value, options) {
  return `<fieldset class="setting-fieldset"><legend>${escapeHtml(legend)}</legend>${options.map(([optionValue, label, note]) => `<label class="radio-card"><input type="radio" name="${escapeHtml(path)}" data-setting-path="${escapeHtml(path)}" value="${escapeHtml(optionValue)}" ${value === optionValue ? "checked" : ""}><span><strong>${escapeHtml(label)}</strong>${note ? `<small>${escapeHtml(note)}</small>` : ""}</span></label>`).join("")}</fieldset>`;
}

function renderSettingsContent(store, section) {
  const state = store.getState();
  const settings = state.settings;
  if (section === "locations") return `<section class="settings-section"><p class="eyebrow">Locations</p><h2>Where Loci Flow is allowed to help</h2><div class="approved-location"><span class="folder-shape" aria-hidden="true"></span><div><strong>Downloads</strong><p>Approved sample location · Up to date</p></div><span class="badge status-success">Active</span></div><div class="compact-actions">${button("Change sample location", "sample-location-control")}${button("Remove sample location", "sample-location-control", { kind: "quiet" })}</div><p class="safety-copy">These controls are conceptual. No real folder chooser or permission request opens in this prototype.</p></section>`;
  if (section === "organization") return `<section class="settings-section"><p class="eyebrow">Organization</p><h2>Choose how much review detail you see</h2>${radioGroup("Review detail", "organization.reviewDetail", settings.organization.reviewDetail, [["recommended", "Recommended review detail", "Focus on structure, conflicts, and exceptions."], ["detailed", "Detailed review", "Show more evidence and sample changes."], ["manual-file-level", "Manual file-level review", "Expand every represented file."]])}${toggleSetting("Rename suggestions", "organization.renameSuggestions", settings.organization.renameSuggestions, "Show conservative filename suggestions inside plans.")}<div class="learned-summary"><h3>Learned habits</h3><p>${escapeHtml(state.learnedHabits.summary)}</p>${button("Reset learned behavior", "reset-learned", { kind: "quiet" })}</div></section>`;
  if (section === "automation") return `<section class="settings-section"><p class="eyebrow">Automation</p><h2>Separate preparation from application</h2>${radioGroup("Index maintenance", "automation.indexMaintenance", settings.automation.indexMaintenance, [["automatic-while-running", "Automatic while running", "Keep the local sample index current."], ["reconcile-at-launch", "Reconcile at launch", "Check once when Loci Flow opens."], ["manual-only", "Manual only", "Update only when requested."]])}${radioGroup("Plan preparation", "automation.planPreparation", settings.automation.planPreparation, [["manual", "Manual", "Prepare a plan only when requested."], ["automatic-when-useful", "Automatic when useful", "Prepare quietly when enough evidence exists."], ["scheduled", "Scheduled", "Use a future schedule."]])}<div class="setting-row fixed-setting"><div><strong>Application</strong><p>Every plan requires confirmation.</p></div><span class="badge status-success">Always ask</span></div></section>`;
  if (section === "locked-rules") {
    const rules = Array.isArray(settings.lockedRules) ? settings.lockedRules : settings.lockedRules.items;
    return `<section class="settings-section"><p class="eyebrow">Locked rules</p><h2>Deterministic and previewable</h2>${rules.length ? rules.map((rule) => `<article class="rule-card"><div><span class="badge ${rule.active ? "status-success" : "status-neutral"}">${rule.active ? "Active" : "Paused"}</span><h3>${escapeHtml(rule.match)}</h3><p>→ ${escapeHtml(store.breadcrumb(rule.destinationFolderId).map((folder) => folder.name).join(" › "))}</p></div><div class="compact-actions">${button("Inspect matches", "inspect-rule", { attrs: `data-rule-id="${escapeHtml(rule.id)}"` })}${button("Edit", "edit-rule", { kind: "quiet", attrs: `data-rule-id="${escapeHtml(rule.id)}"` })}${button(rule.active ? "Pause" : "Resume", "toggle-rule", { kind: "quiet", attrs: `data-rule-id="${escapeHtml(rule.id)}"` })}${button("Remove", "remove-rule", { kind: "quiet", attrs: `data-rule-id="${escapeHtml(rule.id)}"` })}</div></article>`).join("") : emptyState("No active locked rules", "Reset the prototype to restore the sample CS1010 rule.")}</section>`;
  }
  if (section === "duplicates") return `<section class="settings-section"><p class="eyebrow">Duplicates</p><h2>Exact-copy handling</h2>${toggleSetting("Ask before consolidation", "duplicates.askBeforeConsolidation", settings.duplicates.askBeforeConsolidation, "Show the preferred copy, extras, and holding-area preview first.")}<div class="setting-row fixed-setting"><div><strong>Reversible holding area</strong><p>Extra copies move here; they are not deleted and storage is not recovered.</p></div><span class="badge status-success">On</span></div><p class="safety-copy">Operating-system Trash is not available in this prototype.</p></section>`;
  if (section === "search-indexing") return `<section class="settings-section"><p class="eyebrow">Search and indexing</p><h2>Local fixture metadata</h2>${toggleSetting("Local metadata indexing", "searchAndIndexing.localMetadataIndexing", settings.searchAndIndexing.localMetadataIndexing)}${toggleSetting("Preview generation", "searchAndIndexing.previewGeneration", settings.searchAndIndexing.previewGeneration)}<div class="setting-row fixed-setting"><div><strong>Content indexing</strong><p>Research-dependent and deliberately deferred.</p></div><span class="badge status-neutral">Off · Future</span></div><div class="compact-actions">${button("Clear fixture index", "index-placeholder")}${button("Rebuild fixture index", "index-placeholder", { kind: "quiet" })}</div></section>`;
  if (section === "notifications") return `<section class="settings-section"><p class="eyebrow">Notifications</p><h2>Low-frequency and factual</h2><p>Home remains authoritative even when every notification is off.</p>${toggleSetting("Organization plan ready", "notifications.organizationPlanReady", settings.notifications.organizationPlanReady, "“Organization plan for Downloads is ready.”")}${toggleSetting("Duplicate review ready", "notifications.duplicateReviewReady", settings.notifications.duplicateReviewReady)}${toggleSetting("Apply or verification problem", "notifications.applyOrVerificationProblem", settings.notifications.applyOrVerificationProblem)}${toggleSetting("Index problem", "notifications.indexProblem", settings.notifications.indexProblem)}<p class="safety-copy">No real notifications are sent.</p></section>`;
  if (section === "privacy") return `<section class="settings-section"><p class="eyebrow">Privacy and telemetry</p><h2>Local by design</h2><div class="privacy-grid"><article><strong>Local-only fixture and index</strong><p>All sample state stays in this browser.</p></article><article><strong>Telemetry off</strong><p>No product events are collected.</p></article><article><strong>No sensitive uploads</strong><p>No query text, filenames, paths, or document contents leave this prototype.</p></article><article><strong>Future contribution</strong><p>Aggregate metrics and reviewed contributions would require a separate explicit workflow.</p></article></div></section>`;
  return `<section class="settings-section"><p class="eyebrow">Advanced</p><h2>Prototype controls and diagnostics</h2><div class="diagnostic-grid">${stat("Fixture", settings.advanced.fixtureStatus || "Ready")}${stat("References", settings.advanced.diagnostics || "Loaded")}${stat("State version", String(state.stateVersion))}${stat("Working name", "Loci Flow")}</div><label class="select-setting"><span><strong>Apply demo outcome</strong><small>Choose the truth shown after the simulated sequence.</small></span><select data-demo-outcome><option value="verified" ${state.demoOutcome === "verified" ? "selected" : ""}>Applied and verified</option><option value="partial" ${state.demoOutcome === "partial" ? "selected" : ""}>Partially applied</option><option value="failed" ${state.demoOutcome === "failed" ? "selected" : ""}>Failed before changes</option><option value="verification_incomplete" ${state.demoOutcome === "verification_incomplete" ? "selected" : ""}>Verification incomplete</option></select></label><label class="select-setting"><span><strong>Plan freshness demo</strong><small>Change the active plan’s sample freshness.</small></span><select data-plan-freshness><option value="up_to_date">Up to date</option><option value="updated_automatically">Updated automatically</option><option value="needs_review">Needs review</option><option value="no_longer_reliable">No longer reliable</option></select></label><div class="advanced-actions">${button("Export prototype summary", "export-placeholder")}${button("Export acceptance notes", "export-placeholder", { kind: "quiet" })}${button("Reset prototype", "confirm-reset", { kind: "danger" })}</div><p class="working-name-note">${escapeHtml(settings.advanced.workingNameNotice || "Loci Flow is a temporary prototype-facing name.")}</p></section>`;
}

function renderSettings(store, route) {
  const section = route.params.get("section") || "locations";
  const valid = settingsSections.some(([id]) => id === section) ? section : "locations";
  return {
    title: "Settings",
    html: `${pageHeader("Settings", "Make Loci Flow fit your habits", "Presentation preferences never weaken confirmation or verification.")}<div class="settings-layout"><nav class="settings-nav" aria-label="Settings sections">${settingsSections.map(([id, label]) => `<a href="#/settings?section=${encodeURIComponent(id)}" class="${valid === id ? "active" : ""}" ${valid === id ? 'aria-current="page"' : ""}>${escapeHtml(label)}</a>`).join("")}</nav><div class="settings-content">${renderSettingsContent(store, valid)}</div></div>`,
  };
}

export function renderPage(store, route) {
  if (route.name === "search") return renderSearch(store, route);
  if (route.name === "files") return renderFiles(store, route);
  if (route.name === "organize") return renderOrganize(store);
  if (route.name === "duplicates") return renderDuplicates(store);
  if (route.name === "activity") return renderActivity(store, route);
  if (route.name === "settings") return renderSettings(store, route);
  return renderHome(store);
}

export function renderOnboarding(store) {
  const state = store.getState();
  const step = Number(state.onboarding.step) || 0;
  if (state.onboarding.completed) return "";
  if (step <= 0) {
    return `<section class="onboarding-shell" role="dialog" aria-modal="true" aria-labelledby="onboarding-heading">
      <aside class="onboarding-aside">
        <div><span class="brand-mark large" aria-hidden="true"><span></span><span></span><span></span></span><p class="eyebrow">Loci Flow</p><h2>File management through visible intentions.</h2><p>Find what you remember, understand where it belongs, and review a plan before anything changes.</p></div>
        <ul class="trust-points"><li>One coherent sample workspace</li><li>No real folder permission</li><li>Offline fixture data</li></ul>
      </aside>
      <div class="onboarding-main">
        <div class="onboarding-progress" aria-label="Step 1 of 2"><span class="is-active"></span><span></span></div>
        <p class="eyebrow">Welcome</p><h1 id="onboarding-heading">Start with a safe sample location</h1><p>This structural prototype helps you find files and prepare understandable organization plans.</p>
        <div class="onboarding-options"><button class="location-option is-selected" type="button" aria-pressed="true"><span class="folder-shape" aria-hidden="true"></span><span><strong>Downloads</strong><small>Sample location · no permission requested</small></span><span class="choice-check" aria-hidden="true">✓</span></button></div>
        <ul class="onboarding-points"><li><span><strong>Analysis stays local.</strong><br>This prototype loads only bundled fixture data and never inspects your computer.</span></li></ul>
        <div class="onboarding-actions">${button("Continue", "onboarding-continue", { kind: "primary" })}</div><p class="step-count">1 of 2</p>
      </div>
    </section>`;
  }
  return `<section class="onboarding-shell" role="dialog" aria-modal="true" aria-labelledby="onboarding-heading">
    <aside class="onboarding-aside">
      <div><span class="brand-mark large" aria-hidden="true"><span></span><span></span><span></span></span><p class="eyebrow">One safe default</p><h2>Understand → confirm → verify.</h2><p>Applying and verification remain visibly separate, so progress never masquerades as truth.</p></div>
      <ul class="trust-points"><li>No typed phrase for routine plans</li><li>No hidden sample actions</li><li>Short-term recovery where truthful</li></ul>
    </aside>
    <div class="onboarding-main">
      <div class="onboarding-progress" aria-label="Step 2 of 2"><span class="is-complete"></span><span class="is-active"></span></div>
      <p class="eyebrow">Ready to begin</p><h1 id="onboarding-heading">Nothing moves without confirmation</h1><p>Loci Flow presents visible plans first. Applying a plan always asks, then verifies before reporting success.</p>
      <ul class="onboarding-points"><li><span><strong>Understand the plan</strong><br>See destinations, exceptions, and conflicts before acting.</span></li><li><span><strong>Confirm once</strong><br>Routine sample changes use one concise confirmation.</span></li><li><span><strong>Verify the result</strong><br>Success appears only after the simulated recheck completes.</span></li></ul>
      <div class="onboarding-actions">${button("Back", "onboarding-back", { kind: "quiet" })}${button("Start using Loci Flow", "onboarding-complete", { kind: "primary" })}</div><p class="step-count">2 of 2</p>
    </div>
  </section>`;
}

function detailFacts(store, file) {
  return `<dl class="detail-facts"><div><dt>Type</dt><dd>${escapeHtml(file.type)}</dd></div><div><dt>Size</dt><dd>${escapeHtml(file.sizeLabel)}</dd></div><div><dt>Created</dt><dd>${escapeHtml(dateLabel(file.createdAt))}</dd></div><div><dt>Modified</dt><dd>${escapeHtml(dateLabel(file.modifiedAt))}</dd></div><div class="wide"><dt>Where</dt><dd>${escapeHtml(store.filePath(file.id))}</dd></div></dl>`;
}

export function dialogContent(store, type, id) {
  const state = store.getState();
  if (type === "file-details") {
    const file = store.fileById(id);
    if (!file) return { eyebrow: "File details", title: "File unavailable", description: "This sample record could not be found.", html: "" };
    const related = store.relatedFiles(file.id);
    const nearby = store.nearbyFiles(file.id);
    return { eyebrow: "File details", title: file.name, description: file.title || "Sample file", html: `<div class="file-detail"><div class="preview-panel"><span class="preview-type">${fileGlyph(file)}</span><div><strong>${escapeHtml(file.title || file.name)}</strong><p>${escapeHtml(file.preview || "A safe fixture preview placeholder.")}</p></div></div>${detailFacts(store, file)}<details class="technical-details"><summary>Exact path and secondary details</summary><code>${escapeHtml(store.filePath(file.id, { exact: true }))}</code><p>Fixture ID: ${escapeHtml(file.id)}</p></details><div class="dialog-actions">${button("Open", "open-file", { kind: "primary", attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("Show in Finder", "show-in-finder", { attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("Rename", "rename-file", { attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("Move", "move-file", { attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("Add to organization plan", "add-to-plan", { kind: "quiet", attrs: `data-file-id="${escapeHtml(file.id)}"` })}</div><section class="relationship-section"><h3>Related files</h3><p>Connected by title, identifier, timing, grouping, or exact content.</p>${related.length ? `<div class="related-list">${related.map((item) => `<button type="button" data-action="file-details" data-file-id="${escapeHtml(item.file.id)}"><strong>${escapeHtml(item.file.name)}</strong><span>${escapeHtml(item.reason)}</span></button>`).join("")}</div>` : `<p class="muted">No fixture relationships for this file.</p>`}</section><section class="relationship-section"><h3>Nearby files</h3><p>Physically in the same folder; this does not imply a conceptual relationship.</p><div class="nearby-list">${nearby.slice(0, 5).map((item) => `<button type="button" data-action="file-details" data-file-id="${escapeHtml(item.id)}">${escapeHtml(item.name)}</button>`).join("") || `<span class="muted">No nearby sample files.</span>`}</div></section></div>` };
  }
  if (type === "rename-file") {
    const file = store.fileById(id);
    const suggested = file?.id === "file-53538976" ? "Elarian Architecture Diagram.jpg" : file?.name;
    return { eyebrow: "Direct rename", title: `Rename ${file?.name || "file"}`, description: "The original extension is preserved and collisions are checked before applying.", html: `<form data-action="confirm-rename" data-file-id="${escapeHtml(id)}"><label>Current<input type="text" value="${escapeHtml(file?.name)}" disabled></label><label>New<input name="newName" type="text" value="${escapeHtml(suggested)}" required autofocus></label><p class="form-help">Extension preservation and verification are automatic in this sample.</p><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="submit">Rename</button></div><p class="form-error" data-form-error role="alert"></p></form>` };
  }
  if (type === "move-file") {
    const file = store.fileById(id);
    const destinations = state.folders.filter((folder) => ["folder-elarian", "folder-northstar", "folder-images", "folder-receipts", "folder-client-forms"].includes(folder.id));
    const suggested = (file?.keywords || []).includes("Northstar") ? "folder-northstar" : (file?.keywords || []).includes("receipt") ? "folder-receipts" : "folder-elarian";
    return { eyebrow: "Direct move", title: `Move “${file?.name || "file"}”`, description: "Choose one destination, preview the path, and confirm once.", html: `<form data-action="confirm-move" data-file-id="${escapeHtml(id)}"><div class="move-current"><span>Current</span><strong>${escapeHtml(store.filePath(id))}</strong></div><label class="destination-search">Search destinations<input type="search" data-destination-search placeholder="Project or folder"></label><fieldset class="destination-options"><legend>Suggested destinations</legend>${destinations.map((folder) => `<label class="destination-option" data-destination-name="${escapeHtml(folder.name.toLocaleLowerCase())}"><input type="radio" name="destinationFolderId" value="${escapeHtml(folder.id)}" ${folder.id === suggested ? "checked" : ""}><span><strong>${escapeHtml(store.breadcrumb(folder.id).map((part) => part.name).join(" › "))}</strong><small>${folder.id === suggested ? "Related project files and matching identifiers" : `${folder.fileCount || 0} files already here`}</small></span></label>`).join("")}</fieldset><div class="move-preview"><span>Preview</span><strong>${escapeHtml(store.breadcrumb(file.folderId).map((folder) => folder.name).join(" › "))} → <span data-move-preview>${escapeHtml(store.breadcrumb(suggested).map((folder) => folder.name).join(" › "))}</span></strong></div><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="submit">Move file</button></div><p class="form-error" data-form-error role="alert"></p></form>` };
  }
  if (type === "plan-group") {
    const group = store.groupById(id);
    const changes = state.plan.changes.filter((change) => change.groupId === id && change.status !== "excluded" && change.status !== "satisfied_directly");
    return { eyebrow: "Prepared group", title: group?.label || group?.name || "Plan group", description: group?.reason || "Inspect represented changes and customize individual files.", html: `<div class="group-detail-list">${changes.map((change) => { const file = store.fileById(change.fileId); return file ? `<article class="group-change"><div><strong>${escapeHtml(file.name)}</strong><p>${escapeHtml(store.filePath(file.id))} → ${escapeHtml(store.breadcrumb(change.destinationFolderId).map((folder) => folder.name).join(" › "))}</p>${change.suggestedName ? `<p><span class="badge status-neutral">Suggested rename</span> ${escapeHtml(change.suggestedName)}</p>` : ""}</div><div class="compact-actions">${button("Leave unchanged", "exclude-plan-file", { kind: "quiet", attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("Change destination", "change-plan-destination", { kind: "quiet", attrs: `data-file-id="${escapeHtml(file.id)}"` })}${button("Move to another group", "move-plan-group", { kind: "quiet", attrs: `data-file-id="${escapeHtml(file.id)}"` })}${change.suggestedName ? button("Edit suggested name", "edit-suggested-name", { attrs: `data-file-id="${escapeHtml(file.id)}"` }) : ""}</div></article>` : ""; }).join("")}</div>` };
  }
  if (type === "apply-plan") {
    const summary = store.planSummary();
    return { eyebrow: "Final confirmation", title: "Apply this sample organization plan?", description: "Loci Flow will recheck first, then show Applying and Verifying as separate stages.", html: `<div class="confirmation-summary">${stat("Files moving", String(summary.files))}${stat("Destinations", String(summary.destinations))}${stat("Review items", "Resolved")}${stat("Conflicts", "Resolved")}</div><dl class="confirmation-facts"><div><dt>Folders created</dt><dd>${escapeHtml(String(state.plan.summary?.foldersCreated || 0))}</dd></div><div><dt>Renames</dt><dd>${escapeHtml(String(state.plan.summary?.renames || state.plan.changes.filter((change) => change.suggestedName).length))}</dd></div><div><dt>Freshness</dt><dd>${escapeHtml(freshnessLabel(state.plan.freshness))}</dd></div></dl><p class="safety-copy">This applies only to fixture state. No files on your computer are accessed or changed.</p><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="button" data-action="apply-plan">Apply and verify</button></div>` };
  }
  if (type === "consolidate-duplicates") {
    const group = state.duplicates.groups.find((item) => item.id === id);
    const preferred = store.fileById(group?.preferredFileId);
    const extras = (group?.fileIds || []).filter((fileId) => fileId !== group.preferredFileId).map((fileId) => store.fileById(fileId)).filter(Boolean);
    return { eyebrow: "Consolidation preview", title: "Consolidate extra copies?", description: "The preferred working copy stays in place.", html: `<div class="preferred-preview"><span class="badge status-success">Preferred working copy</span><strong>${escapeHtml(preferred?.name)}</strong><p>${escapeHtml(preferred ? store.filePath(preferred.id) : "")}</p></div><div class="holding-preview"><h3>Moving to reversible holding area</h3>${extras.map((file) => `<p>${escapeHtml(file.name)} · ${escapeHtml(store.filePath(file.id))}</p>`).join("")}</div><p class="safety-copy"><strong>These copies are not deleted.</strong> Storage is not recovered.</p><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="button" data-action="confirm-consolidate" data-group-id="${escapeHtml(id)}">Consolidate extra copies</button></div>` };
  }
  if (type === "activity-details") {
    const operation = state.activity.find((item) => item.id === id);
    const undo = operation?.undo || {
      available: ["available", "limited"].includes(operation?.undoAvailability),
      label: operation?.undoAvailability === "limited" ? "Limited recovery" : "Undo changes",
      detail: operation?.undoAvailability === "available"
        ? "This recent operation remains cleanly reversible."
        : "A limited recovery is available.",
    };
    return { eyebrow: "Operation detail", title: operation?.title || operation?.summary || "Activity", description: operation?.verification || "", html: `<div class="activity-detail-status"><span class="badge ${statusClass(operation?.status)}">${escapeHtml(operation?.status)}</span><span>${escapeHtml(dateLabel(operation?.occurredAt))}</span></div><h3>Changes</h3><div class="change-scroll" tabindex="0"><ol>${(operation?.changes || []).map((change) => `<li>${escapeHtml(change)}</li>`).join("") || "<li>No changes were made.</li>"}</ol></div>${operation?.problems?.length ? `<h3>Problems</h3><ul>${operation.problems.map((problem) => `<li>${escapeHtml(problem)}</li>`).join("")}</ul>` : ""}<div class="failsafe-panel"><strong>${undo.available ? "Failsafe available" : operation?.status === "Undo unavailable" ? "Undo is no longer available" : "No undo needed"}</strong><p>${escapeHtml(undo.detail || "Prepare a new organization plan.")}</p>${undo.available ? button(undo.label || "Undo changes", "confirm-undo-activity", { kind: "primary", attrs: `data-activity-id="${escapeHtml(id)}"` }) : operation?.status === "Undo unavailable" ? `<a class="button button-secondary" href="#/organize" data-action="close-dialog">Prepare a new plan</a>` : ""}</div>` };
  }
  if (type === "confirm-reset") return { eyebrow: "Advanced", title: "Reset the prototype?", description: "This restores all fixture files, plans, decisions, settings, and onboarding.", html: `<p>Only browser-local prototype state is cleared. No files on your computer are involved.</p><div class="dialog-actions"><button class="button button-quiet" type="button" data-action="close-dialog">Cancel</button><button class="button button-danger" type="button" data-action="reset-prototype">Reset prototype</button></div>` };
  if (type === "inspect-rule") {
    const rules = Array.isArray(state.settings.lockedRules) ? state.settings.lockedRules : state.settings.lockedRules.items;
    const rule = rules.find((item) => item.id === id);
    return { eyebrow: "Locked rule", title: rule?.match || "Rule", description: "A deterministic rule with previewable matches.", html: `<p><strong>Destination:</strong> ${escapeHtml(store.breadcrumb(rule.destinationFolderId).map((folder) => folder.name).join(" › "))}</p><h3>Current sample matches</h3><div class="file-list">${(rule.previewFileIds || []).map((fileId) => { const file = store.fileById(fileId); return file ? fileRow(store, file) : ""; }).join("")}</div><p class="safety-copy">Locked rules remain previewable and never bypass confirmation.</p>` };
  }
  return { eyebrow: "Prototype", title: "Sample interaction", description: "This area is simulated.", html: "" };
}

export { escapeHtml, humanize, freshnessLabel };
