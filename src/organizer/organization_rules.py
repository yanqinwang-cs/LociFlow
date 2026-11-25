from __future__ import annotations

import hashlib
import json
import os
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from organizer.json_values import is_json_version

ORGANIZATION_RULES_VERSION = 1
STANDARDS_CATALOG_VERSION = 2
ORGANIZATION_RULES_RELATIVE_PATH = Path("AI_Review") / "config" / "organization_rules.json"
ORGANIZATION_RULES_STATUS_MISSING = "missing"
ORGANIZATION_RULES_STATUS_LOADED = "loaded"
ORGANIZATION_RULES_STATUS_UNAVAILABLE = "unavailable"
ORGANIZATION_RULES_UNAVAILABLE_CODE = "standards_unavailable"
ORGANIZATION_RULES_UNAVAILABLE_MESSAGE = (
    "Standards could not be loaded. Fix them before organizing."
)
ORGANIZATION_RULES_FIELDS = {
    "version",
    "locked_anchors",
    "ignored_terms",
    "anchor_aliases",
    "preferred_granularities",
}
STANDARDS_CATALOG_FIELDS = {
    "version",
    "configured_root_identity",
    "revision",
    "standards",
}
STANDARD_RECORD_FIELDS = {
    "id",
    "type",
    "active",
    "scope",
    "parameters",
    "created_at",
    "updated_at",
    "record_revision",
    "archived_at",
}
STANDARD_KEEP_FOLDER_UNCHANGED = "keep_folder_unchanged"
STANDARD_KEEP_MATCHING_FILES_TOGETHER = "keep_matching_files_together"
STANDARD_LEGACY_IGNORED_TERM = "legacy_ignored_term"
STANDARD_LEGACY_ALIAS = "legacy_alias"
WRITABLE_STANDARD_TYPES = frozenset(
    {STANDARD_KEEP_FOLDER_UNCHANGED, STANDARD_KEEP_MATCHING_FILES_TOGETHER}
)
STORED_STANDARD_TYPES = WRITABLE_STANDARD_TYPES | {
    STANDARD_LEGACY_IGNORED_TERM,
    STANDARD_LEGACY_ALIAS,
}
STANDARD_MATCHER_KINDS = frozenset({"course_code", "project_code"})
DEFAULT_IGNORED_TERMS = frozenset(
    {
        "appendix",
        "balanced",
        "chat",
        "copy",
        "data",
        "debug",
        "draft",
        "field",
        "final",
        "full",
        "image",
        "index",
        "instructions",
        "module",
        "modules",
        "new",
        "old",
        "other",
        "output",
        "pasted",
        "report",
        "resource",
        "resources",
        "results",
        "run",
        "summary",
        "untitled",
        "updated",
        "v1",
        "v2",
        "v3",
    }
)


@dataclass(frozen=True)
class OrganizationRules:
    locked_anchors: frozenset[str]
    ignored_terms: frozenset[str]
    anchor_aliases: dict[str, str]
    anchor_display_names: dict[str, str]
    preferred_granularities: frozenset[str] = frozenset()
    locked_anchor_scopes: dict[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class StandardScope:
    kind: str
    relative_path: str | None = None


@dataclass(frozen=True)
class StandardRecord:
    id: str
    type: str
    active: bool
    scope: StandardScope
    parameters: tuple[tuple[str, str], ...]
    created_at: str
    updated_at: str
    record_revision: int
    archived_at: str | None = None

    def parameter(self, name: str) -> str | None:
        return dict(self.parameters).get(name)


@dataclass(frozen=True)
class StandardsCatalog:
    configured_root_identity: str
    revision: int
    standards: tuple[StandardRecord, ...]
    version: int = STANDARDS_CATALOG_VERSION


@dataclass(frozen=True)
class OrganizationRulesLoadResult:
    rules: OrganizationRules | None
    source_path: Path | None
    status: str
    message: str
    warnings: list[str]
    error_code: str | None = None
    raw_data: dict[str, Any] | None = None
    catalog: StandardsCatalog | None = None

    @property
    def available(self) -> bool:
        return self.status != ORGANIZATION_RULES_STATUS_UNAVAILABLE

    def require_rules(self) -> OrganizationRules:
        if self.rules is None:
            raise OrganizationRulesUnavailableError(
                self.error_code or ORGANIZATION_RULES_UNAVAILABLE_CODE
            )
        return self.rules


class OrganizationRulesUnavailableError(ValueError):
    def __init__(self, code: str = ORGANIZATION_RULES_UNAVAILABLE_CODE) -> None:
        super().__init__(code)
        self.code = code


def default_organization_rules() -> OrganizationRules:
    return OrganizationRules(
        locked_anchors=frozenset(),
        ignored_terms=DEFAULT_IGNORED_TERMS,
        anchor_aliases={},
        anchor_display_names={},
        preferred_granularities=frozenset(),
    )


def load_organization_rules(root: Path) -> OrganizationRulesLoadResult:
    resolved_root = root.resolve()
    rules_path = resolved_root / ORGANIZATION_RULES_RELATIVE_PATH
    if _rules_path_is_unsafe(resolved_root, rules_path):
        return _unavailable_result(rules_path, "Saved Standards storage is unsafe.")
    if not os.path.lexists(rules_path):
        return OrganizationRulesLoadResult(
            rules=default_organization_rules(),
            source_path=None,
            status=ORGANIZATION_RULES_STATUS_MISSING,
            message="No saved Standards were found. Using conservative built-in defaults.",
            warnings=[],
        )
    if not rules_path.is_file():
        return _unavailable_result(rules_path, "Saved Standards storage is unsafe.")

    try:
        with rules_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, UnicodeError):
        return _unavailable_result(rules_path, "Saved Standards JSON is invalid.")
    except OSError:
        return _unavailable_result(rules_path, "Saved Standards could not be read.")

    if not isinstance(data, dict):
        return _unavailable_result(rules_path, "Saved Standards must be a JSON object.")
    if is_json_version(data.get("version"), STANDARDS_CATALOG_VERSION):
        try:
            catalog = standards_catalog_from_data(data)
        except ValueError:
            return _unavailable_result(rules_path, "Saved Standards catalog is malformed.")
        expected_identity = hashlib.sha256(str(resolved_root).encode("utf-8")).hexdigest()
        if catalog.configured_root_identity != expected_identity:
            return _unavailable_result(
                rules_path,
                "Saved Standards belong to another configured folder.",
                code="wrong_root_scope",
            )
        rules = organization_rules_from_catalog(catalog)
        return OrganizationRulesLoadResult(
            rules=rules,
            source_path=rules_path,
            status=ORGANIZATION_RULES_STATUS_LOADED,
            message=f"Loaded saved Standards from {ORGANIZATION_RULES_RELATIVE_PATH.as_posix()}.",
            warnings=[],
            raw_data=data,
            catalog=catalog,
        )
    if not is_json_version(data.get("version"), ORGANIZATION_RULES_VERSION):
        return _unavailable_result(rules_path, "Saved Standards version is unsupported.")
    if set(data) - ORGANIZATION_RULES_FIELDS:
        return _unavailable_result(rules_path, "Saved Standards contain unsupported fields.")

    rules, warnings = organization_rules_from_data(data)
    if warnings:
        return _unavailable_result(rules_path, "Saved Standards are malformed.")
    return OrganizationRulesLoadResult(
        rules=rules,
        source_path=rules_path,
        status=ORGANIZATION_RULES_STATUS_LOADED,
        message=f"Loaded organization rules from {ORGANIZATION_RULES_RELATIVE_PATH.as_posix()}.",
        warnings=[],
        raw_data=data,
    )


def _unavailable_result(
    path: Path,
    warning: str,
    *,
    code: str = ORGANIZATION_RULES_UNAVAILABLE_CODE,
) -> OrganizationRulesLoadResult:
    return OrganizationRulesLoadResult(
        rules=None,
        source_path=path,
        status=ORGANIZATION_RULES_STATUS_UNAVAILABLE,
        message=ORGANIZATION_RULES_UNAVAILABLE_MESSAGE,
        warnings=[warning],
        error_code=code,
    )


def _rules_path_is_unsafe(root: Path, rules_path: Path) -> bool:
    current = root
    for part in ORGANIZATION_RULES_RELATIVE_PATH.parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            return True
    try:
        rules_path.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return True
    return False


def organization_rules_from_data(data: object) -> tuple[OrganizationRules, list[str]]:
    warnings: list[str] = []
    if not isinstance(data, dict):
        return (
            default_organization_rules(),
            ["Organization rules must be a JSON object. Using conservative built-in defaults."],
        )
    if not is_json_version(data.get("version"), ORGANIZATION_RULES_VERSION):
        return (
            default_organization_rules(),
            ["Organization rules version must be 1. Using conservative built-in defaults."],
        )

    locked, locked_display = _string_list_to_anchor_set(
        data.get("locked_anchors", []),
        "locked_anchors",
        warnings,
    )
    ignored, ignored_display = _string_list_to_anchor_set(
        data.get("ignored_terms", []),
        "ignored_terms",
        warnings,
    )
    ignored = frozenset(set(DEFAULT_IGNORED_TERMS) | set(ignored))

    aliases, alias_display = _aliases_from_data(data.get("anchor_aliases", {}), warnings)
    aliases = _reject_alias_cycles(aliases, warnings)
    aliases = _resolve_alias_chains(aliases, warnings)
    preferred_granularities = _string_list_to_plain_set(
        data.get("preferred_granularities", []),
        "preferred_granularities",
        warnings,
    )

    normalized_ignored = {_resolve_anchor(term, aliases) for term in ignored}
    normalized_locked = {_resolve_anchor(term, aliases) for term in locked}
    alias_targets_to_ignored = {
        alias
        for alias, target in aliases.items()
        if target in normalized_ignored
    }
    if alias_targets_to_ignored:
        for alias in sorted(alias_targets_to_ignored):
            warnings.append(
                f"anchor_aliases entry {alias!r} points to an ignored term and was ignored"
            )
            aliases.pop(alias, None)

    normalized_ignored = {_resolve_anchor(term, aliases) for term in ignored}
    normalized_locked = {_resolve_anchor(term, aliases) for term in locked}
    conflicts = normalized_locked & normalized_ignored
    if conflicts:
        for anchor in sorted(conflicts):
            warnings.append(
                f"locked_anchors entry {anchor!r} is also ignored; ignored term wins"
            )
        normalized_locked -= conflicts

    display_names: dict[str, str] = {}
    for mapping in (locked_display, ignored_display, alias_display):
        for key, display in mapping.items():
            canonical = _resolve_anchor(key, aliases)
            display_names.setdefault(canonical, display)
    for target in aliases.values():
        display_names.setdefault(target, _default_display_name(target))

    return (
        OrganizationRules(
            locked_anchors=frozenset(normalized_locked),
            ignored_terms=frozenset(normalized_ignored),
            anchor_aliases=dict(sorted(aliases.items())),
            anchor_display_names=dict(sorted(display_names.items())),
            preferred_granularities=frozenset(preferred_granularities),
        ),
        warnings,
    )


def organization_rules_from_catalog(catalog: StandardsCatalog) -> OrganizationRules:
    locked: set[str] = set()
    ignored = set(DEFAULT_IGNORED_TERMS)
    aliases: dict[str, str] = {}
    display_names: dict[str, str] = {}
    locked_scopes: dict[str, set[str]] = {}
    for record in catalog.standards:
        if not record.active or record.archived_at is not None:
            continue
        if record.type == STANDARD_KEEP_MATCHING_FILES_TOGETHER:
            value = record.parameter("matcher_value")
            if value is None:
                continue
            anchor = normalize_anchor(value)
            locked.add(anchor)
            display_names.setdefault(anchor, value)
            scope = record.scope.relative_path or "."
            locked_scopes.setdefault(anchor, set()).add(scope)
        elif record.type == STANDARD_LEGACY_IGNORED_TERM:
            term = record.parameter("term")
            if term is not None:
                ignored.add(normalize_anchor(term))
                display_names.setdefault(normalize_anchor(term), term)
        elif record.type == STANDARD_LEGACY_ALIAS:
            alias = record.parameter("alias")
            target = record.parameter("target")
            if alias is not None and target is not None:
                aliases[normalize_anchor(alias)] = normalize_anchor(target)
                display_names.setdefault(normalize_anchor(target), target)
    aliases = _resolve_alias_chains(_reject_alias_cycles(aliases, []), [])
    return OrganizationRules(
        locked_anchors=frozenset(_resolve_anchor(value, aliases) for value in locked),
        ignored_terms=frozenset(_resolve_anchor(value, aliases) for value in ignored),
        anchor_aliases=dict(sorted(aliases.items())),
        anchor_display_names=dict(sorted(display_names.items())),
        preferred_granularities=frozenset(),
        locked_anchor_scopes={
            _resolve_anchor(anchor, aliases): tuple(sorted(scopes))
            for anchor, scopes in sorted(locked_scopes.items())
        },
    )


def standards_catalog_to_data(catalog: StandardsCatalog) -> dict[str, Any]:
    return {
        "version": STANDARDS_CATALOG_VERSION,
        "configured_root_identity": catalog.configured_root_identity,
        "revision": catalog.revision,
        "standards": [standard_record_to_data(record) for record in catalog.standards],
    }


def standard_record_to_data(record: StandardRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "type": record.type,
        "active": record.active,
        "scope": {
            "kind": record.scope.kind,
            "relative_path": record.scope.relative_path,
        },
        "parameters": dict(record.parameters),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "record_revision": record.record_revision,
        "archived_at": record.archived_at,
    }


def standards_catalog_from_data(data: object) -> StandardsCatalog:
    if not isinstance(data, dict) or set(data) != STANDARDS_CATALOG_FIELDS:
        raise ValueError("Standards catalog fields do not match version 2")
    if not is_json_version(data["version"], STANDARDS_CATALOG_VERSION):
        raise ValueError("unsupported Standards catalog version")
    identity = _catalog_string(data["configured_root_identity"], "configured root identity")
    if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
        raise ValueError("invalid configured root identity")
    revision = _catalog_integer(data["revision"], "catalog revision", allow_zero=True)
    raw_standards = data["standards"]
    if not isinstance(raw_standards, list):
        raise ValueError("Standards must be a list")
    standards = tuple(sorted((_standard_record_from_data(item) for item in raw_standards), key=lambda item: item.id))
    if len({item.id for item in standards}) != len(standards):
        raise ValueError("duplicate Standard ID")
    for index, record in enumerate(standards):
        others = standards[:index] + standards[index + 1 :]
        if equivalent_standard_record(others, record) is not None:
            raise ValueError("duplicate Standard")
        if conflicting_standard_records(standards[:index], record):
            raise ValueError("conflicting active Standards")
    return StandardsCatalog(
        configured_root_identity=identity,
        revision=revision,
        standards=standards,
    )


def _standard_record_from_data(data: object) -> StandardRecord:
    if not isinstance(data, dict) or set(data) != STANDARD_RECORD_FIELDS:
        raise ValueError("Standard fields do not match version 2")
    standard_id = _canonical_uuid(data["id"], "Standard ID")
    standard_type = _catalog_string(data["type"], "Standard type")
    if standard_type not in STORED_STANDARD_TYPES:
        raise ValueError("unsupported Standard type")
    if type(data["active"]) is not bool:
        raise ValueError("Standard active must be a boolean")
    scope = _standard_scope_from_data(data["scope"])
    parameters = _standard_parameters(standard_type, data["parameters"])
    created_at = _catalog_timestamp(data["created_at"], "created_at")
    updated_at = _catalog_timestamp(data["updated_at"], "updated_at")
    record_revision = _catalog_integer(data["record_revision"], "record revision")
    archived_at = data["archived_at"]
    if archived_at is not None:
        archived_at = _catalog_timestamp(archived_at, "archived_at")
        if data["active"]:
            raise ValueError("archived Standard cannot be active")
    return StandardRecord(
        id=standard_id,
        type=standard_type,
        active=data["active"],
        scope=scope,
        parameters=parameters,
        created_at=created_at,
        updated_at=updated_at,
        record_revision=record_revision,
        archived_at=archived_at,
    )


def _standard_scope_from_data(data: object) -> StandardScope:
    if not isinstance(data, dict) or set(data) != {"kind", "relative_path"}:
        raise ValueError("Standard scope fields are invalid")
    kind = data["kind"]
    relative_path = data["relative_path"]
    if kind == "root" and relative_path is None:
        return StandardScope("root", None)
    if kind != "folder" or not isinstance(relative_path, str):
        raise ValueError("unsupported Standard scope")
    return StandardScope("folder", normalize_standard_relative_path(relative_path))


def _standard_parameters(standard_type: str, data: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(data, dict):
        raise ValueError("Standard parameters must be an object")
    if standard_type == STANDARD_KEEP_FOLDER_UNCHANGED:
        if data:
            raise ValueError("keep-folder parameters must be empty")
        return ()
    if standard_type == STANDARD_KEEP_MATCHING_FILES_TOGETHER:
        if set(data) != {"matcher_kind", "matcher_value"}:
            raise ValueError("keep-together parameters are invalid")
        kind = _catalog_string(data["matcher_kind"], "matcher kind")
        value = normalize_matcher_value(data["matcher_value"])
        if kind not in STANDARD_MATCHER_KINDS:
            raise ValueError("unsupported matcher kind")
        return (("matcher_kind", kind), ("matcher_value", value))
    if standard_type == STANDARD_LEGACY_IGNORED_TERM:
        if set(data) != {"term"}:
            raise ValueError("legacy ignored-term parameters are invalid")
        return (("term", normalize_matcher_value(data["term"])),)
    if set(data) != {"alias", "target"}:
        raise ValueError("legacy alias parameters are invalid")
    return (
        ("alias", normalize_matcher_value(data["alias"])),
        ("target", normalize_matcher_value(data["target"])),
    )


def normalize_standard_relative_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip().replace("\\", "/"))
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or normalized in {".", ".."}:
        raise ValueError("invalid folder scope")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid folder scope")
    return path.as_posix()


def normalize_matcher_value(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("matcher value must be a string")
    normalized = unicodedata.normalize("NFC", " ".join(value.strip().split()))
    if not normalized or len(normalized) > 128 or not _valid_anchor(normalize_anchor(normalized)):
        raise ValueError("invalid matcher value")
    return normalized


def _catalog_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a string")
    return value


def _catalog_integer(value: object, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} is invalid")
    return value


def _canonical_uuid(value: object, label: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if str(parsed) != value:
        raise ValueError(f"{label} is invalid")
    return str(parsed)


def _catalog_timestamp(value: object, label: str) -> str:
    text = _catalog_string(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include timezone")
    return parsed.isoformat()


def equivalent_standard_record(
    records: tuple[StandardRecord, ...],
    candidate: StandardRecord,
) -> StandardRecord | None:
    return next(
        (
            item
            for item in records
            if item.archived_at is None
            and item.type == candidate.type
            and item.scope == candidate.scope
            and item.parameters == candidate.parameters
        ),
        None,
    )


def conflicting_standard_records(
    records: tuple[StandardRecord, ...],
    candidate: StandardRecord,
) -> tuple[StandardRecord, ...]:
    if not candidate.active:
        return ()
    conflicts: list[StandardRecord] = []
    for item in records:
        if (
            not item.active
            or item.archived_at is not None
            or equivalent_standard_record((item,), candidate)
        ):
            continue
        if standard_scopes_overlap(item.scope, candidate.scope) and {
            item.type,
            candidate.type,
        } == {STANDARD_KEEP_FOLDER_UNCHANGED, STANDARD_KEEP_MATCHING_FILES_TOGETHER}:
            conflicts.append(item)
            continue
        if (
            item.type == candidate.type == STANDARD_KEEP_FOLDER_UNCHANGED
            and standard_scopes_overlap(item.scope, candidate.scope)
        ):
            conflicts.append(item)
            continue
        ignored, locked = (
            (item, candidate)
            if item.type == STANDARD_LEGACY_IGNORED_TERM
            else (candidate, item)
        )
        if (
            ignored.type == STANDARD_LEGACY_IGNORED_TERM
            and locked.type == STANDARD_KEEP_MATCHING_FILES_TOGETHER
            and normalize_anchor(ignored.parameter("term") or "")
            == normalize_anchor(locked.parameter("matcher_value") or "")
        ):
            conflicts.append(item)
    return tuple(sorted(conflicts, key=lambda item: item.id))


def standard_scopes_overlap(left: StandardScope, right: StandardScope) -> bool:
    if left.kind == "root" or right.kind == "root":
        return True
    left_path = Path(left.relative_path or ".")
    right_path = Path(right.relative_path or ".")
    return (
        left_path == right_path
        or left_path in right_path.parents
        or right_path in left_path.parents
    )


def normalize_anchor(text: str) -> str:
    return text.strip().lower()


def canonical_anchor(anchor: str, rules: OrganizationRules) -> str:
    return _resolve_anchor(normalize_anchor(anchor), rules.anchor_aliases)


def display_anchor(anchor: str, rules: OrganizationRules, fallback: str | None = None) -> str:
    canonical = canonical_anchor(anchor, rules)
    return rules.anchor_display_names.get(canonical, fallback or _default_display_name(canonical))


def _string_list_to_anchor_set(
    value: object,
    field_name: str,
    warnings: list[str],
) -> tuple[frozenset[str], dict[str, str]]:
    if value is None:
        return frozenset(), {}
    if not isinstance(value, list):
        warnings.append(f"{field_name} must be a list; ignoring this section")
        return frozenset(), {}

    anchors: set[str] = set()
    display_names: dict[str, str] = {}
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str):
            warnings.append(f"{field_name}[{index}] must be a string and was ignored")
            continue
        normalized = normalize_anchor(item)
        if not _valid_anchor(normalized):
            warnings.append(f"{field_name}[{index}] is not a valid anchor and was ignored")
            continue
        anchors.add(normalized)
        display_names.setdefault(normalized, item.strip())
    return frozenset(anchors), display_names


def _string_list_to_plain_set(
    value: object,
    field_name: str,
    warnings: list[str],
) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        warnings.append(f"{field_name} must be a list; ignoring this section")
        return frozenset()

    items: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str):
            warnings.append(f"{field_name}[{index}] must be a string and was ignored")
            continue
        normalized = normalize_anchor(item)
        if not _valid_anchor(normalized):
            warnings.append(f"{field_name}[{index}] is not valid and was ignored")
            continue
        items.add(normalized)
    return frozenset(items)


def _aliases_from_data(value: object, warnings: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    if value is None:
        return {}, {}
    if not isinstance(value, dict):
        warnings.append("anchor_aliases must be an object; ignoring this section")
        return {}, {}

    aliases: dict[str, str] = {}
    display_names: dict[str, str] = {}
    for raw_alias, raw_target in value.items():
        if not isinstance(raw_alias, str) or not isinstance(raw_target, str):
            warnings.append("anchor_aliases entries must map strings to strings and one entry was ignored")
            continue
        alias = normalize_anchor(raw_alias)
        target = normalize_anchor(raw_target)
        if not _valid_anchor(alias) or not _valid_anchor(target):
            warnings.append(f"anchor_aliases entry {raw_alias!r} is invalid and was ignored")
            continue
        if alias == target:
            display_names.setdefault(target, raw_target.strip())
            continue
        aliases[alias] = target
        display_names.setdefault(target, raw_target.strip())
    return aliases, display_names


def _reject_alias_cycles(aliases: dict[str, str], warnings: list[str]) -> dict[str, str]:
    valid = dict(aliases)
    for alias in sorted(aliases):
        seen: set[str] = set()
        current = alias
        while current in aliases:
            if current in seen:
                warnings.append(f"anchor_aliases entry {alias!r} is part of a cycle and was ignored")
                valid.pop(alias, None)
                break
            seen.add(current)
            current = aliases[current]
    return valid


def _resolve_alias_chains(aliases: dict[str, str], warnings: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for alias in sorted(aliases):
        target = aliases[alias]
        seen = {alias}
        while target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        if target in seen:
            warnings.append(f"anchor_aliases entry {alias!r} could not be resolved and was ignored")
            continue
        resolved[alias] = target
    return resolved


def _resolve_anchor(anchor: str, aliases: dict[str, str]) -> str:
    current = anchor
    seen: set[str] = set()
    while current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def _valid_anchor(anchor: str) -> bool:
    return (
        bool(anchor)
        and anchor not in {".", ".."}
        and "\x00" not in anchor
        and "/" not in anchor
        and "\\" not in anchor
        # The organization review export rejects these, so accepting them here
        # would make a loaded rule fail later at export.
        and ":" not in anchor
        and ".." not in anchor
    )


def _default_display_name(anchor: str) -> str:
    if anchor.upper() == anchor and any(character.isdigit() for character in anchor):
        return anchor
    if any(character.isdigit() for character in anchor) and any(character.isalpha() for character in anchor):
        return anchor.upper()
    return anchor.replace("_", " ").title().replace(" ", "_")
