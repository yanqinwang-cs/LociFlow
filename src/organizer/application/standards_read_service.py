from __future__ import annotations

import re
import uuid

from organizer.application.standards_read_models import (
    StandardsReadItem,
    StandardsReadProjection,
)
from organizer.grouping import ANCHOR_DECISION_IGNORED, AnchorDecision
from organizer.organization_rules import (
    DEFAULT_IGNORED_TERMS,
    ORGANIZATION_RULES_STATUS_LOADED,
    ORGANIZATION_RULES_UNAVAILABLE_CODE,
    ORGANIZATION_RULES_UNAVAILABLE_MESSAGE,
    OrganizationRules,
    OrganizationRulesLoadResult,
    canonical_anchor,
    display_anchor,
    normalize_anchor,
)
from organizer.scope import (
    PROJECT_PROTECTION_STANDARD_ID,
    SHARED_PROTECTION_STANDARD_ID,
    SUBMISSION_PROTECTION_STANDARD_ID,
)


EXISTING_FOLDER_POLICY_STANDARD_ID = "builtin-use-existing-folders-only"
_READ_ID_NAMESPACE = uuid.UUID("66378439-bc92-4ea5-a710-dbb8b7ad3103")


def build_standards_read_projection(
    load_result: OrganizationRulesLoadResult,
    anchor_decisions: tuple[AnchorDecision, ...] = (),
) -> StandardsReadProjection:
    items = list(_built_in_items())
    if not load_result.available:
        return StandardsReadProjection(
            status="unavailable",
            items=tuple(items),
            error_code=load_result.error_code or ORGANIZATION_RULES_UNAVAILABLE_CODE,
            attention_message=ORGANIZATION_RULES_UNAVAILABLE_MESSAGE,
        )

    rules = load_result.require_rules()
    if load_result.status == ORGANIZATION_RULES_STATUS_LOADED:
        items.extend(_legacy_rule_items(rules, anchor_decisions))
    return StandardsReadProjection(
        status="available",
        items=tuple(sorted(items, key=lambda item: (item.preset_type, item.name, item.id))),
    )


def read_item_by_id(
    projection: StandardsReadProjection,
    standard_id: str | None,
) -> StandardsReadItem | None:
    return next((item for item in projection.items if item.id == standard_id), None)


def _built_in_items() -> tuple[StandardsReadItem, ...]:
    return (
        StandardsReadItem(
            id=PROJECT_PROTECTION_STANDARD_ID,
            name="Protect project folders",
            description="Project files stay together so their references are not broken.",
            active=True,
            mutable=False,
            scope_label="Protected project folders",
            effect_summary="Project files are excluded from organization suggestions.",
            preset_type="built_in_protection",
        ),
        StandardsReadItem(
            id=SUBMISSION_PROTECTION_STANDARD_ID,
            name="Protect submission folders",
            description="Submission files stay in their current folders.",
            active=True,
            mutable=False,
            scope_label="Submission folders",
            effect_summary="Submission files are excluded from organization suggestions.",
            preset_type="built_in_protection",
        ),
        StandardsReadItem(
            id=SHARED_PROTECTION_STANDARD_ID,
            name="Protect shared folders",
            description="Shared files stay in their current folders.",
            active=True,
            mutable=False,
            scope_label="Shared folders",
            effect_summary="Shared files are excluded from organization suggestions.",
            preset_type="built_in_protection",
        ),
        StandardsReadItem(
            id=EXISTING_FOLDER_POLICY_STANDARD_ID,
            name="Use existing folders only",
            description="Organization suggestions use folders that already exist.",
            active=True,
            mutable=False,
            scope_label="Selected folder",
            effect_summary="Suggestions that require a new destination folder are not shown.",
            preset_type="existing_folder_policy",
        ),
    )


def _legacy_rule_items(
    rules: OrganizationRules,
    anchor_decisions: tuple[AnchorDecision, ...],
) -> list[StandardsReadItem]:
    items: list[StandardsReadItem] = []
    explicit_ignored = sorted(rules.ignored_terms - DEFAULT_IGNORED_TERMS)
    for anchor in sorted(rules.locked_anchors):
        label = display_anchor(anchor, rules, anchor)
        items.append(
            _legacy_item(
                "legacy_locked_anchor",
                anchor,
                f"Keep matching {label} files together",
                f"Matching files may be grouped using {label}.",
                "Matching files shape deterministic grouping before suggestions are created.",
            )
        )
    for anchor in explicit_ignored:
        label = display_anchor(anchor, rules, anchor)
        matched_paths = tuple(
            sorted(
                {
                    file.relative_path.as_posix()
                    for decision in anchor_decisions
                    if decision.decision == ANCHOR_DECISION_IGNORED
                    and canonical_anchor(decision.anchor, rules) == anchor
                    for file in decision.files
                }
            )
        )
        items.append(
            _legacy_item(
                "legacy_ignored_term",
                anchor,
                f"Do not organize by {label}",
                f"The label {label} is not used to group files.",
                "Matching grouping suggestions are suppressed before a plan is created.",
                matched_relative_paths=matched_paths,
            )
        )
    for alias, target in sorted(rules.anchor_aliases.items()):
        alias_label = _display_token(alias)
        target_label = display_anchor(target, rules, target)
        items.append(
            _legacy_item(
                "legacy_alias",
                f"{alias}\0{target}",
                f"Treat {alias_label} as {target_label}",
                f"Matching {alias_label} labels are grouped as {target_label}.",
                "The alias is resolved before deterministic grouping.",
            )
        )
    return items


def _legacy_item(
    preset_type: str,
    identity: str,
    name: str,
    description: str,
    effect_summary: str,
    *,
    matched_relative_paths: tuple[str, ...] = (),
) -> StandardsReadItem:
    standard_id = uuid.uuid5(
        _READ_ID_NAMESPACE,
        f"{preset_type}\0{normalize_anchor(identity)}",
    )
    return StandardsReadItem(
        id=f"standard-{standard_id}",
        name=name,
        description=description,
        active=True,
        mutable=False,
        scope_label="Selected folder",
        effect_summary=effect_summary,
        preset_type=preset_type,
        matched_relative_paths=matched_relative_paths,
    )


def _display_token(value: str) -> str:
    pieces = [piece for piece in re.split(r"[_\s]+", value.strip()) if piece]
    return " ".join(piece.upper() if any(char.isdigit() for char in piece) else piece.title() for piece in pieces)
