from __future__ import annotations


_KINDS_BY_EXTENSION = {
    ".pdf": "pdf",
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".doc": "word",
    ".docx": "word",
    ".odt": "word",
    ".csv": "spreadsheet",
    ".xls": "spreadsheet",
    ".xlsx": "spreadsheet",
    ".ods": "spreadsheet",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".heic": "image",
    ".ppt": "presentation",
    ".pptx": "presentation",
    ".odp": "presentation",
    ".zip": "archive",
    ".tar": "archive",
    ".gz": "archive",
    ".tgz": "archive",
    ".bz2": "archive",
    ".7z": "archive",
    ".rar": "archive",
}
_SOURCE_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".sh",
    ".bash",
    ".zsh",
    ".html",
    ".css",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
}


def consumer_file_kind(extension: str, *, is_dir: bool = False) -> str:
    if is_dir:
        return "folder"
    normalized = extension.casefold()
    if normalized and not normalized.startswith("."):
        normalized = f".{normalized}"
    if normalized in _SOURCE_CODE_EXTENSIONS:
        return "source_code"
    return _KINDS_BY_EXTENSION.get(normalized, "document")
