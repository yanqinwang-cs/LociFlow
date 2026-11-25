import os
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from organizer.models import FileMetadata
from organizer.safety import validate_under_root

DEFAULT_ORGANIZATION_EXTENSIONS = {
    ".pdf",
    ".md",
    ".markdown",
    ".txt",
    ".rtf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".html",
    ".htm",
}
ORPHAN_CODE_EXTENSIONS = {
    ".py",
    ".java",
    ".ipynb",
    ".c",
    ".cpp",
    ".js",
    ".ts",
    ".go",
    ".rs",
}
GENERATED_ASSET_EXTENSIONS = {
    ".css",
    ".js",
    ".map",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
}
GENERATED_ASSET_FILENAMES = {
    "saved_resource.html",
}
GENERATED_ASSET_DIRECTORY_SUFFIXES = {
    "_files",
}
PROJECT_OUTPUT_DIRECTORY_NAMES = {
    "archive_experiment_files",
    "experiment_outputs",
    "build",
    "dist",
}
CONTEXTUAL_PROJECT_OUTPUT_DIRECTORY_NAMES = {
    "resources",
    "src",
}
PROJECT_CONTEXT_DIRECTORY_HINTS = {
    "app",
    "application",
    "backend",
    "frontend",
    "package",
    "project",
    "repo",
    "repository",
    "site",
    "web",
}
EXCLUDED_ORGANIZATION_EXTENSIONS = {
    ".h",
    ".hpp",
    ".jsx",
    ".tsx",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".css",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mov",
    ".mp4",
    ".mp3",
    ".wav",
    ".zip",
    ".tar",
    ".gz",
    ".dmg",
    ".pkg",
    ".exe",
    ".bin",
}
PROTECTED_ROOT_NAMES = {"AI_Review", "Organized", "Protected_Workspaces"}
DUPLICATE_BIN_RELATIVE_ROOT = Path("AI_Review") / "duplicate_bin"
DUPLICATE_BIN_ITEMS_RELATIVE_ROOT = DUPLICATE_BIN_RELATIVE_ROOT / "items"
PROTECTED_DIRECTORY_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}
PROTECTED_DIRECTORY_SUFFIXES = {
    ".rtfd",
    ".app",
    ".framework",
    ".bundle",
    ".plugin",
    ".kext",
    ".xcodeproj",
    ".xcworkspace",
}
PROJECT_MARKER_NAMES = {
    ".git",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "poetry.lock",
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "setup.py",
    "setup.cfg",
    "CMakeLists.txt",
}
WEB_PROJECT_MARKER_NAMES = {
    "package.json",
    "tsconfig.json",
    "style.css",
    "styles.css",
    "app.js",
    "main.js",
    "index.js",
}
WEB_PROJECT_MARKER_PREFIXES = {
    "vite.config.",
    "webpack.config.",
    "next.config.",
}
WEB_PROJECT_DIRECTORY_NAMES = {
    "src",
    "assets",
    "static",
    "public",
    "node_modules",
}
SUBMISSION_DIRECTORY_NAMES = {"submission", "submissions"}
SHARED_DIRECTORY_NAMES = {"shared", "shared files", "shared_files"}

PROJECT_PROTECTION_REASON = "project_folder_protected"
SUBMISSION_PROTECTION_REASON = "submission_folder_protected"
SHARED_PROTECTION_REASON = "shared_folder_protected"
PROJECT_PROTECTION_STANDARD_ID = "builtin-protect-project-folders"
SUBMISSION_PROTECTION_STANDARD_ID = "builtin-protect-submission-folders"
SHARED_PROTECTION_STANDARD_ID = "builtin-protect-shared-folders"


@dataclass(frozen=True)
class ActionableEligibility:
    allowed: bool
    reason_code: str | None = None
    standard_id: str | None = None


def is_protected_context_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    if not parts:
        return False
    if parts[0] in PROTECTED_ROOT_NAMES:
        return True
    return _has_protected_path_part(parts)


def is_duplicate_bin_path(relative_path: Path) -> bool:
    """Return whether a root-relative path is in the reserved Duplicate Bin."""

    prefix = DUPLICATE_BIN_RELATIVE_ROOT.parts
    return relative_path.parts[: len(prefix)] == prefix


def validate_duplicate_bin_item_destination(destination: Path, root: Path) -> Path:
    """Validate one future Duplicate Bin-owned item destination without creating it."""

    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError("configured root is not a directory")
    if not destination.is_absolute():
        raise ValueError("duplicate bin destination must be absolute")
    validate_under_root(destination.resolve(strict=False), resolved_root)
    try:
        relative = destination.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(
            "duplicate bin destination is outside the configured root"
        ) from error
    parts = relative.parts
    prefix = DUPLICATE_BIN_ITEMS_RELATIVE_ROOT.parts
    if len(parts) != len(prefix) + 2 or parts[: len(prefix)] != prefix:
        raise ValueError("duplicate bin item destination has an invalid layout")

    item_id, basename = parts[-2:]
    try:
        parsed_item_id = uuid.UUID(item_id)
    except ValueError as error:
        raise ValueError("duplicate bin item ID must be an opaque UUID") from error
    if str(parsed_item_id) != item_id:
        raise ValueError("duplicate bin item ID must be a canonical UUID")
    if (
        not basename
        or basename in {".", ".."}
        or "/" in basename
        or "\\" in basename
        or Path(basename).name != basename
    ):
        raise ValueError("duplicate bin item basename is unsafe")

    item_directory = destination.parent
    if os.path.lexists(item_directory) or os.path.lexists(destination):
        raise ValueError("duplicate bin item destination already exists")

    current = resolved_root
    for component in parts[:-1]:
        current = current / component
        if not os.path.lexists(current):
            continue
        if current.is_symlink():
            raise ValueError("duplicate bin destination contains a symlink")
        if not current.is_dir():
            raise ValueError("duplicate bin destination parent is not a directory")

    items_directory = resolved_root / DUPLICATE_BIN_ITEMS_RELATIVE_ROOT
    if items_directory.exists():
        if items_directory.is_symlink() or not items_directory.is_dir():
            raise ValueError("duplicate bin items path is unsafe")
        normalized_item_id = unicodedata.normalize("NFC", item_id).casefold()
        for child in items_directory.iterdir():
            if (
                unicodedata.normalize("NFC", child.name).casefold()
                == normalized_item_id
            ):
                raise ValueError("duplicate bin item destination collides")

    _validate_platform_path_lengths(destination, basename, resolved_root)
    return destination


def _validate_platform_path_lengths(
    destination: Path, basename: str, root: Path
) -> None:
    try:
        name_max = os.pathconf(root, "PC_NAME_MAX")
    except (OSError, ValueError):
        name_max = -1
    if name_max >= 0 and len(os.fsencode(basename)) > name_max:
        raise ValueError("duplicate bin item basename is too long")
    try:
        path_max = os.pathconf(root, "PC_PATH_MAX")
    except (OSError, ValueError):
        path_max = -1
    if path_max >= 0 and len(os.fsencode(destination)) > path_max:
        raise ValueError("duplicate bin item destination is too long")


def is_project_context_path(
    relative_path: Path,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    if _scan_root_is_inside_repository(all_metadata):
        return True
    marker_dirs = _marker_directories(all_metadata, PROJECT_MARKER_NAMES)
    file_parent = relative_path.parent
    return any(_is_same_or_under(file_parent, marker_dir) for marker_dir in marker_dirs)


def _scan_root_is_inside_repository(all_metadata: Sequence[FileMetadata]) -> bool:
    # A scan started in a repository's subfolder never lists the .git above it,
    # so the markers inside the scan cannot show that the files belong to code.
    for metadata in all_metadata:
        depth = len(_normalize_relative_path(metadata.relative_path).parts)
        if metadata.relative_path.as_posix() in {"", "."}:
            depth = 0
        if len(metadata.path.parents) < depth + 1:
            continue
        root = metadata.path if depth == 0 else metadata.path.parents[depth - 1]
        return any(
            os.path.lexists(ancestor / ".git") for ancestor in (root, *root.parents)
        )
    return False


def is_actionable_plan_eligible(
    metadata: FileMetadata,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    return evaluate_actionable_plan_eligibility(metadata, all_metadata).allowed


def evaluate_actionable_plan_eligibility(
    metadata: FileMetadata,
    all_metadata: Sequence[FileMetadata],
) -> ActionableEligibility:
    if metadata.is_dir or metadata.path.is_symlink() or not metadata.path.is_file():
        return ActionableEligibility(allowed=False)
    return evaluate_actionable_source_path(metadata.relative_path, all_metadata)


def is_actionable_source_path(
    relative_path: Path,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    return evaluate_actionable_source_path(relative_path, all_metadata).allowed


def evaluate_actionable_source_path(
    relative_path: Path,
    all_metadata: Sequence[FileMetadata],
) -> ActionableEligibility:
    if _is_under_named_folder(relative_path, SUBMISSION_DIRECTORY_NAMES):
        return ActionableEligibility(
            allowed=False,
            reason_code=SUBMISSION_PROTECTION_REASON,
            standard_id=SUBMISSION_PROTECTION_STANDARD_ID,
        )
    if _is_under_named_folder(relative_path, SHARED_DIRECTORY_NAMES):
        return ActionableEligibility(
            allowed=False,
            reason_code=SHARED_PROTECTION_REASON,
            standard_id=SHARED_PROTECTION_STANDARD_ID,
        )
    if is_protected_context_path(relative_path):
        if _is_builtin_project_protection(relative_path):
            return ActionableEligibility(
                allowed=False,
                reason_code=PROJECT_PROTECTION_REASON,
                standard_id=PROJECT_PROTECTION_STANDARD_ID,
            )
        return ActionableEligibility(allowed=False)
    if is_project_context_path(relative_path, all_metadata):
        return ActionableEligibility(
            allowed=False,
            reason_code=PROJECT_PROTECTION_REASON,
            standard_id=PROJECT_PROTECTION_STANDARD_ID,
        )
    if is_generated_or_project_output_context_path(relative_path, all_metadata):
        return ActionableEligibility(allowed=False)
    return ActionableEligibility(allowed=True)


def is_actionable_destination_path(
    relative_path: Path,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    if is_duplicate_bin_path(relative_path):
        return False
    if relative_path.parts and relative_path.parts[0] in {"AI_Review", "Organized"}:
        return True
    return is_actionable_source_path(relative_path, all_metadata)


def is_generated_or_project_output_context_path(
    relative_path: Path,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    return (
        is_generated_asset_context_path(relative_path)
        or is_generated_asset_context(relative_path, all_metadata)
        or is_project_output_context_path(relative_path, all_metadata)
    )


def is_generated_asset_context_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    lowered_parts = [part.lower() for part in parts]
    if any(
        part.endswith(tuple(GENERATED_ASSET_DIRECTORY_SUFFIXES))
        for part in lowered_parts[:-1]
    ):
        return True
    if relative_path.name.lower() in GENERATED_ASSET_FILENAMES:
        return True
    if _looks_like_generated_asset_name(relative_path):
        return True
    return False


def is_generated_asset_context(
    relative_path: Path,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    if is_generated_asset_context_path(relative_path):
        return True
    if relative_path.suffix.lower() not in GENERATED_ASSET_EXTENSIONS:
        return False
    parent = relative_path.parent
    siblings = [
        metadata
        for metadata in all_metadata
        if not metadata.is_dir and metadata.relative_path.parent == parent
    ]
    if len(siblings) < 3:
        return False
    generated_siblings = [
        metadata
        for metadata in siblings
        if (
            metadata.extension.lower() in GENERATED_ASSET_EXTENSIONS
            or metadata.name.lower() in GENERATED_ASSET_FILENAMES
            or _looks_like_generated_asset_name(metadata.relative_path)
        )
    ]
    return len(generated_siblings) / len(siblings) >= 0.6


def is_project_output_context_path(
    relative_path: Path,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    parts = relative_path.parts
    lowered_parts = [part.lower() for part in parts[:-1]]
    if any(part in PROJECT_OUTPUT_DIRECTORY_NAMES for part in lowered_parts):
        return True
    for index, part in enumerate(lowered_parts):
        if part not in CONTEXTUAL_PROJECT_OUTPUT_DIRECTORY_NAMES:
            continue
        parent_parts = lowered_parts[:index]
        if any(parent in PROJECT_CONTEXT_DIRECTORY_HINTS for parent in parent_parts):
            return True
        if is_project_context_path(relative_path, all_metadata):
            return True
    return False


def is_allowed_organization_file(
    metadata: FileMetadata,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    extension = metadata.extension.lower()
    if not is_actionable_plan_eligible(metadata, all_metadata):
        return False
    if extension in EXCLUDED_ORGANIZATION_EXTENSIONS:
        return False
    if extension in {".html", ".htm"}:
        return is_standalone_html_document(metadata, all_metadata)
    return extension in DEFAULT_ORGANIZATION_EXTENSIONS


def is_standalone_html_document(
    metadata: FileMetadata,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    if metadata.extension.lower() not in {".html", ".htm"}:
        return False
    if not is_actionable_plan_eligible(metadata, all_metadata):
        return False
    return not _has_web_project_context(metadata, all_metadata)


def is_orphan_code_candidate(
    metadata: FileMetadata,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    if metadata.extension.lower() not in ORPHAN_CODE_EXTENSIONS:
        return False
    return is_actionable_plan_eligible(metadata, all_metadata)


def _has_web_project_context(
    metadata: FileMetadata,
    all_metadata: Sequence[FileMetadata],
) -> bool:
    marker_dirs = _marker_directories(all_metadata, WEB_PROJECT_MARKER_NAMES)
    marker_dirs.update(
        _prefixed_marker_directories(all_metadata, WEB_PROJECT_MARKER_PREFIXES)
    )
    marker_dirs.update(
        _named_directory_paths(all_metadata, WEB_PROJECT_DIRECTORY_NAMES)
    )
    file_parent = metadata.relative_path.parent
    return any(_is_same_or_under(file_parent, marker_dir) for marker_dir in marker_dirs)


def _has_protected_path_part(parts: tuple[str, ...]) -> bool:
    for part in parts[:-1]:
        if part in PROTECTED_DIRECTORY_NAMES:
            return True
        if any(part.endswith(suffix) for suffix in PROTECTED_DIRECTORY_SUFFIXES):
            return True
    return False


def _looks_like_generated_asset_name(relative_path: Path) -> bool:
    stem = relative_path.stem.lower()
    suffix = relative_path.suffix.lower()
    if suffix not in GENERATED_ASSET_EXTENSIONS:
        return False
    if ".min" in relative_path.name.lower():
        return True
    compact = stem.replace("-", "").replace("_", "").replace(".", "")
    return len(compact) >= 12 and all(
        character in "0123456789abcdef" for character in compact
    )


def _marker_directories(
    all_metadata: Sequence[FileMetadata],
    marker_names: set[str],
) -> set[Path]:
    return {
        metadata.relative_path.parent
        for metadata in all_metadata
        if metadata.name in marker_names
    }


def _prefixed_marker_directories(
    all_metadata: Sequence[FileMetadata],
    marker_prefixes: set[str],
) -> set[Path]:
    return {
        metadata.relative_path.parent
        for metadata in all_metadata
        if any(metadata.name.startswith(prefix) for prefix in marker_prefixes)
    }


def _named_directory_paths(
    all_metadata: Sequence[FileMetadata],
    directory_names: set[str],
) -> set[Path]:
    return {
        metadata.relative_path
        for metadata in all_metadata
        if metadata.is_dir and metadata.name in directory_names
    }


def _is_same_or_under(path: Path, possible_parent: Path) -> bool:
    normalized_path = _normalize_relative_path(path)
    normalized_parent = _normalize_relative_path(possible_parent)
    return (
        normalized_path == normalized_parent
        or normalized_parent in normalized_path.parents
    )


def _normalize_relative_path(path: Path) -> Path:
    return Path(".") if path.as_posix() in {"", "."} else path


def _is_under_named_folder(relative_path: Path, names: set[str]) -> bool:
    return any(part.casefold() in names for part in relative_path.parts[:-1])


def _is_builtin_project_protection(relative_path: Path) -> bool:
    parts = relative_path.parts
    if not parts:
        return False
    if parts[0] in {"AI_Review", "Organized"}:
        return False
    return True
