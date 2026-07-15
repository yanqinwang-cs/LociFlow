export const FILE_ICON_MAP = {
  folder: "folder",
  pdf: "pdf",
  txt: "txt",
  markdown: "markdown",
  document: "document",
  spreadsheet: "spreadsheet",
  image: "image",
  presentation: "presentation",
  code: "code",
  archive: "archive",
  unknown: "unknown",
};

const FILE_ICON_SPRITE = new URL("./assets/file-icons.svg", import.meta.url).href;

export function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function fileIcon(type, label = "") {
  const mapped = FILE_ICON_MAP[type] || FILE_ICON_MAP.unknown;
  return `<svg class="file-icon icon-${mapped}" role="img" aria-label="${escapeHtml(label || mapped)}"><use href="${FILE_ICON_SPRITE}#${mapped}"></use></svg>`;
}

export function breadcrumb(parts = []) {
  return `<span class="breadcrumb">${parts.map((part) => `<span>${escapeHtml(part)}</span>`).join('<span aria-hidden="true">›</span>')}</span>`;
}

function folderNode(name) {
  return { name, type: "folder", children: [], newFolder: false };
}

function ensureFolder(root, path) {
  let current = root;
  path.forEach((name) => {
    let child = current.children.find((item) => item.type === "folder" && item.name === name);
    if (!child) {
      child = folderNode(name);
      current.children.push(child);
    }
    current = child;
  });
  return current;
}

export function buildMovementTree(changes) {
  const root = folderNode("Files");
  changes.filter((change) => change.decision === "accepted").forEach((change) => {
    const path = change.newFolder?.length ? change.newFolder : change.currentFolder;
    const target = ensureFolder(root, path || ["Sample Downloads"]);
    if (change.type === "create_folder") {
      target.newFolder = true;
      return;
    }
    target.children.push({
      name: change.type === "rename" ? change.newName : change.name,
      type: "file",
      icon: change.icon,
      note: change.type === "duplicate"
        ? `${change.copiesToBin || 1} ${change.copiesToBin === 1 ? "copy" : "copies"}`
        : change.type === "rename" ? "New name" : "Move",
    });
  });
  return root;
}

function sortNodes(nodes) {
  return [...nodes].sort((a, b) => {
    if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

function renderNode(node, depth) {
  const icon = node.type === "folder" ? fileIcon("folder", "Folder") : fileIcon(node.icon, node.note || "File");
  const badge = node.newFolder ? '<span class="move-note">New folder</span>' : node.note ? `<span class="move-note">${escapeHtml(node.note)}</span>` : "";
  return `<li role="treeitem" aria-level="${depth}" class="move-tree-item" style="--tree-depth:${depth}"><div class="move-tree-row">${icon}<span class="move-tree-name">${escapeHtml(node.name)}</span>${badge}</div>${node.children?.length ? `<ul role="group">${sortNodes(node.children).map((child) => renderNode(child, depth + 1)).join("")}</ul>` : ""}</li>`;
}

function suppliedIcon(kind = "") {
  const normalized = String(kind).toLowerCase();
  const aliases = {
    "text document": "txt",
    "word document": "document",
    "csv spreadsheet": "spreadsheet",
    "source code": "code",
    "zip archive": "archive",
  };
  return FILE_ICON_MAP[aliases[normalized] || normalized] || FILE_ICON_MAP.unknown;
}

function renderSuppliedNode(node, depth) {
  const icon = node.type === "folder"
    ? fileIcon("folder", "Folder")
    : fileIcon(suppliedIcon(node.kind), node.kind || "File");
  const badge = node.note
    ? `<span class="move-note">${escapeHtml(node.note)}</span>`
    : "";
  const children = Array.isArray(node.children) ? node.children : [];
  return `<li role="treeitem" aria-level="${depth}" class="move-tree-item" style="--tree-depth:${depth}"><div class="move-tree-row">${icon}<span class="move-tree-name">${escapeHtml(node.name)}</span>${badge}</div>${children.length ? `<ul role="group">${children.map((child) => renderSuppliedNode(child, depth + 1)).join("")}</ul>` : ""}</li>`;
}

export function renderSuppliedMovementTree(treeModel) {
  const nodes = Array.isArray(treeModel?.nodes) ? treeModel.nodes : [];
  if (!nodes.length) return '<div class="empty-tree"><strong>No accepted changes yet</strong><span>Accepted moves, names, and folders will appear here.</span></div>';
  return `<ul class="move-tree" role="tree" aria-label="Where files will go">${nodes.map((node) => renderSuppliedNode(node, 1)).join("")}</ul>`;
}

export function renderMovementTree(changes) {
  if (!Array.isArray(changes)) return renderSuppliedMovementTree(changes);
  const tree = buildMovementTree(changes);
  if (!tree.children.length) return '<div class="empty-tree"><strong>No accepted changes yet</strong><span>Accepted moves, names, and folders will appear here.</span></div>';
  return `<ul class="move-tree" role="tree" aria-label="Where files will go">${sortNodes(tree.children).map((node) => renderNode(node, 1)).join("")}</ul>`;
}
