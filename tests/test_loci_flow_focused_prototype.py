from __future__ import annotations

from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE_ROOT = REPOSITORY_ROOT / "prototype" / "loci-flow-ux-1-1-focused"
PASS_ONE_ROOT = REPOSITORY_ROOT / "prototype" / "loci-flow-ux-1-1-pass-1"

EXPECTED_PROTOTYPE_FILES = {
    "README.md",
    "app.js",
    "fixtures.json",
    "index.html",
    "store.js",
    "styles.css",
    "views.js",
}
PRIMARY_ROUTES = {
    "#/home": "Home",
    "#/organize": "Organize",
    "#/duplicates": "Duplicates",
    "#/activity": "Activity",
    "#/settings": "Settings",
}
PASS_ONE_SHA256 = {
    "prototype/loci-flow-ux-1-1-pass-1/index.html": (
        "20900650ab677fac197c26519829738560509490ba578268647f7651cd73bac4"
    ),
    "prototype/loci-flow-ux-1-1-pass-1/styles.css": (
        "61b933ff267271ba3cfa25f9a42b602f824e72c270a338b330edd96f9ae2cf66"
    ),
    "prototype/loci-flow-ux-1-1-pass-1/fixtures.json": (
        "0e037d43e41f22eedc84b9d54a53e7aff5762072c6516af3f4aace0296338b08"
    ),
    "prototype/loci-flow-ux-1-1-pass-1/store.js": (
        "52db477e857efc84b2be4aa6ec135066ee515991b3faac692914bd83c5900866"
    ),
    "prototype/loci-flow-ux-1-1-pass-1/views.js": (
        "a8f3c144631831e8210e4f36da4adcfb9b9bad59fd258d4e608808f0c9951504"
    ),
    "prototype/loci-flow-ux-1-1-pass-1/app.js": (
        "03809dd10b1dff3979d1aceee2054f0498c7e45b7e2a046b81cdd4a7d17c7287"
    ),
    "prototype/loci-flow-ux-1-1-pass-1/README.md": (
        "f097b184be9584016679b6bfca07ab4212df3eb2f0e613fb947cdb4bd9cc4b7c"
    ),
    "tests/test_loci_flow_prototype.py": (
        "dc261d7bec60f35cc209f2ca76b12ade5f852cf67810b3ccd495761c446c59e3"
    ),
}
PROTOTYPE_NOTICE = (
    "Prototype using sample files. "
    "No files on your computer are accessed or changed."
)


class _SemanticHTMLParser(HTMLParser):
    """Collect semantic and asset hooks without assuming page geometry."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.primary_hrefs: list[str] = []
        self.asset_references: list[tuple[str, str]] = []
        self._primary_nav_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))

        if tag == "nav":
            label = (attributes.get("aria-label") or "").lower()
            if "primary" in label:
                self._primary_nav_depth = 1
            elif self._primary_nav_depth:
                self._primary_nav_depth += 1
        elif self._primary_nav_depth:
            self._primary_nav_depth += 1

        href = attributes.get("href")
        if self._primary_nav_depth and tag == "a" and href:
            self.primary_hrefs.append(href)

        for attribute in ("href", "src"):
            value = attributes.get(attribute)
            if value:
                self.asset_references.append((attribute, value))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if self._primary_nav_depth:
            self._primary_nav_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if self._primary_nav_depth:
            self._primary_nav_depth -= 1


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LociFlowFocusedPrototypeContractTests(unittest.TestCase):
    """Protect fixture, safety, and semantic contracts without freezing layout."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_path = PROTOTYPE_ROOT / "fixtures.json"
        cls.fixture_text = cls.fixture_path.read_text(encoding="utf-8")
        cls.fixture = json.loads(cls.fixture_text)
        cls.index = (PROTOTYPE_ROOT / "index.html").read_text(encoding="utf-8")
        cls.styles = (PROTOTYPE_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.javascript = {
            name: (PROTOTYPE_ROOT / name).read_text(encoding="utf-8")
            for name in ("store.js", "views.js", "app.js")
        }
        cls.runtime_sources = {
            "index.html": cls.index,
            "styles.css": cls.styles,
            "fixtures.json": cls.fixture_text,
            **cls.javascript,
        }
        cls.runtime_text = "\n".join(cls.runtime_sources.values())
        cls.primary_ui_text = "\n".join(
            (cls.index, cls.javascript["views.js"])
        ).lower()
        cls.parser = _SemanticHTMLParser()
        cls.parser.feed(cls.index)
        cls.folders_by_id = {
            item["id"]: item for item in cls.fixture["folders"]
        }
        cls.files_by_id = {item["id"]: item for item in cls.fixture["files"]}

    def test_only_the_focused_prototype_file_set_was_added(self) -> None:
        self.assertTrue(PROTOTYPE_ROOT.is_dir())
        actual_files = {
            path.name
            for path in PROTOTYPE_ROOT.iterdir()
            if path.is_file() and not path.name.startswith(".")
        }
        self.assertEqual(actual_files, EXPECTED_PROTOTYPE_FILES)
        self.assertFalse(
            any(
                path.is_dir()
                for path in PROTOTYPE_ROOT.iterdir()
                if not path.name.startswith(".")
            )
        )
        for manifest in (
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "deno.json",
        ):
            self.assertFalse((PROTOTYPE_ROOT / manifest).exists())

    def test_pass_one_prototype_and_contract_test_are_byte_unchanged(self) -> None:
        self.assertTrue(PASS_ONE_ROOT.is_dir())
        for relative_path, expected_hash in PASS_ONE_SHA256.items():
            path = REPOSITORY_ROOT / relative_path
            with self.subTest(path=relative_path):
                self.assertTrue(path.is_file())
                self.assertEqual(_sha256(path), expected_hash)

    def test_fixture_version_and_five_primary_routes_are_exact(self) -> None:
        self.assertEqual(self.fixture["fixtureVersion"], 1)
        destinations = self.fixture["meta"]["primaryRoutes"]
        route_ids = [item["id"] for item in destinations]
        route_values = [item["route"] for item in destinations]
        self.assertEqual(len(destinations), 5)
        self.assertEqual(len(route_ids), len(set(route_ids)))
        self.assertEqual(len(route_values), len(set(route_values)))
        self.assertEqual(
            set(route_ids),
            {"home", "organize", "duplicates", "activity", "settings"},
        )
        self.assertEqual(
            {item["route"]: item["label"] for item in destinations},
            PRIMARY_ROUTES,
        )
        self.assertEqual(len(self.parser.primary_hrefs), 5)
        self.assertEqual(
            len(self.parser.primary_hrefs), len(set(self.parser.primary_hrefs))
        )
        self.assertEqual(set(self.parser.primary_hrefs), set(PRIMARY_ROUTES))
        self.assertNotIn("#/search", self.runtime_text.lower())
        self.assertNotIn("#/files", self.runtime_text.lower())
        self.assertNotIn("#/advanced", self.runtime_text.lower())
        self.assertIn("Advanced", self.runtime_text)

    def test_canonical_folder_file_and_plan_references_are_consistent(self) -> None:
        folder_ids = [item["id"] for item in self.fixture["folders"]]
        file_ids = [item["id"] for item in self.fixture["files"]]
        self.assertEqual(len(folder_ids), len(set(folder_ids)))
        self.assertEqual(len(file_ids), len(set(file_ids)))
        self.assertTrue(all(item.startswith("folder-") for item in folder_ids))
        self.assertTrue(all(item.startswith("file-") for item in file_ids))

        for folder in self.fixture["folders"]:
            parent_id = folder.get("parentId")
            if parent_id is not None:
                self.assertIn(parent_id, self.folders_by_id)
        for file_record in self.fixture["files"]:
            self.assertIn(file_record["folderId"], self.folders_by_id)
        aggregate_ids = [item["id"] for item in self.fixture["aggregateRows"]]
        self.assertEqual(len(aggregate_ids), len(set(aggregate_ids)))
        for aggregate in self.fixture["aggregateRows"]:
            self.assertIn(aggregate["parentFolderId"], self.folders_by_id)
            self.assertGreater(aggregate["count"], 0)

        plan = self.fixture["initialState"]["plan"]
        changes_by_id = {item["id"]: item for item in plan["changes"]}
        self.assertEqual(len(changes_by_id), len(plan["changes"]))
        for change in plan["changes"]:
            self.assertIn(change["fileId"], self.files_by_id)
            self.assertIn(change["sourceFolderId"], self.folders_by_id)
            self.assertIn(change["destinationFolderId"], self.folders_by_id)
            self.assertTrue(
                {"fileName", "sourcePath", "destinationPath"}.isdisjoint(change)
            )

        grouped_change_ids = [
            change_id
            for group in plan["groups"]
            for change_id in group["changeIds"]
        ]
        self.assertCountEqual(grouped_change_ids, changes_by_id)
        self.assertEqual(len(grouped_change_ids), len(set(grouped_change_ids)))
        for group in plan["groups"]:
            self.assertIn(group["destinationFolderId"], self.folders_by_id)

        for review in plan["reviewItems"]:
            self.assertIn(review["changeId"], changes_by_id)
            self.assertIn(review["fileId"], self.files_by_id)
            self.assertEqual(
                changes_by_id[review["changeId"]]["fileId"], review["fileId"]
            )
            self.assertEqual(changes_by_id[review["changeId"]]["status"], "Review")
        for conflict in plan["conflicts"]:
            self.assertIn(conflict["changeId"], changes_by_id)
            self.assertIn(conflict["fileId"], self.files_by_id)
            self.assertIn(conflict["collidingFileId"], self.files_by_id)
            self.assertEqual(
                changes_by_id[conflict["changeId"]]["fileId"], conflict["fileId"]
            )
            self.assertEqual(
                changes_by_id[conflict["changeId"]]["status"], "Conflict"
            )

    def test_plan_has_exact_counts_destinations_and_issue_examples(self) -> None:
        plan = self.fixture["initialState"]["plan"]
        counts = self.fixture["meta"]["counts"]
        statuses = Counter(item["status"] for item in plan["changes"])
        destination_ids = {
            group["destinationFolderId"] for group in plan["groups"]
        }

        self.assertEqual(len(plan["changes"]), 38)
        self.assertEqual(len({item["fileId"] for item in plan["changes"]}), 38)
        self.assertEqual(len(plan["groups"]), 5)
        self.assertEqual(len(destination_ids), 5)
        self.assertEqual(statuses, {"Ready": 35, "Review": 2, "Conflict": 1})
        self.assertEqual(len(plan["reviewItems"]), 2)
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(
            plan["summary"],
            {
                "fileChanges": 38,
                "destinations": 5,
                "reviewRequired": 2,
                "blockingConflicts": 1,
            },
        )
        self.assertEqual(
            counts,
            {
                "plannedFileChanges": 38,
                "plannedDestinations": 5,
                "reviewRequired": 2,
                "blockingConflicts": 1,
                "activeDuplicateGroups": 2,
            },
        )
        self.assertEqual(
            {group["label"] for group in plan["groups"]},
            {
                "Elarian",
                "Northstar",
                "CS1010S study material",
                "Client Forms",
                "Receipts",
            },
        )

        review_names = {
            self.files_by_id[item["fileId"]]["name"]
            for item in plan["reviewItems"]
        }
        conflict_names = {
            self.files_by_id[item["fileId"]]["name"]
            for item in plan["conflicts"]
        }
        self.assertEqual(review_names, {"Elarian logo.svg", "final.pdf"})
        self.assertEqual(conflict_names, {"Notes.docx"})
        self.assertTrue(
            all(item["status"] == "unresolved" for item in plan["reviewItems"])
        )
        self.assertTrue(
            all(item["status"] == "unresolved" for item in plan["conflicts"])
        )

    def test_required_plan_and_rule_filenames_exist(self) -> None:
        filenames = {item["name"] for item in self.fixture["files"]}
        self.assertTrue(
            {
                "Architecture.pdf",
                "Elarian logo.svg",
                "final.pdf",
                "Notes.docx",
                "CS1010X 2016 Finals.pdf",
                "CS1010S 2016 Finals.pdf",
                "Client Intake Form.pdf",
                "INV-2048.pdf",
            }.issubset(filenames)
        )

    def test_duplicate_groups_and_recheck_scenarios_are_exact(self) -> None:
        duplicates = self.fixture["initialState"]["duplicates"]
        groups = duplicates["groups"]
        groups_by_id = {item["id"]: item for item in groups}
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups_by_id), 2)
        self.assertEqual(
            len(duplicates["activeGroupIds"]),
            len(set(duplicates["activeGroupIds"])),
        )
        self.assertEqual(set(duplicates["activeGroupIds"]), set(groups_by_id))
        self.assertEqual(
            {item["recommendation"] for item in groups}, {"clear", "ambiguous"}
        )
        self.assertEqual(
            self.fixture["meta"]["counts"]["activeDuplicateGroups"], 2
        )

        active_member_ids: list[str] = []
        for group in groups:
            self.assertEqual(group["exactCopyCount"], len(group["memberFileIds"]))
            self.assertGreaterEqual(group["exactCopyCount"], 2)
            self.assertEqual(
                len(group["memberFileIds"]), len(set(group["memberFileIds"]))
            )
            active_member_ids.extend(group["memberFileIds"])
            for file_id in group["memberFileIds"]:
                self.assertIn(file_id, self.files_by_id)
            working_copy = group["workingCopyFileId"]
            if working_copy is not None:
                self.assertIn(working_copy, group["memberFileIds"])
            self.assertTrue(
                set(group["selectedExtraFileIds"]).issubset(group["memberFileIds"])
            )
            self.assertNotIn(working_copy, group["selectedExtraFileIds"])
            self.assertTrue(group["spaceUsedByExtraCopies"])
        self.assertEqual(len(active_member_ids), len(set(active_member_ids)))

        clear_group = next(
            item for item in groups if item["recommendation"] == "clear"
        )
        ambiguous_group = next(
            item for item in groups if item["recommendation"] == "ambiguous"
        )
        self.assertIsNotNone(clear_group["workingCopyFileId"])
        self.assertIsNone(ambiguous_group["workingCopyFileId"])

        rechecks = {item["scenario"]: item for item in duplicates["rechecks"]}
        self.assertEqual(
            set(rechecks), {"preferred-copy-edited", "extra-copy-changed"}
        )
        preferred = rechecks["preferred-copy-edited"]
        self.assertIn(preferred["preferredFileId"], self.files_by_id)
        self.assertTrue(preferred["requiresReconfirmation"])
        for file_id in preferred["olderCopyFileIds"]:
            self.assertIn(file_id, self.files_by_id)
        changed_extra = rechecks["extra-copy-changed"]
        self.assertIn(changed_extra["referenceFileId"], self.files_by_id)
        self.assertIn(changed_extra["excludedFileId"], self.files_by_id)
        self.assertFalse(changed_extra["eligibleForExactCopyAction"])
        recheck_file_ids = {
            preferred["preferredFileId"],
            *preferred["olderCopyFileIds"],
            changed_extra["referenceFileId"],
            changed_extra["excludedFileId"],
        }
        self.assertTrue(
            recheck_file_ids.isdisjoint(active_member_ids),
            "Recheck scenarios must stay outside the active exact-copy groups",
        )

    def test_activity_has_required_truth_and_undo_states(self) -> None:
        activity = self.fixture["initialState"]["activity"]
        plan_file_ids = {
            item["fileId"]
            for item in self.fixture["initialState"]["plan"]["changes"]
        }
        active_duplicate_file_ids = {
            file_id
            for group in self.fixture["initialState"]["duplicates"]["groups"]
            for file_id in group["memberFileIds"]
        }
        holding_area_id = self.fixture["initialState"]["duplicates"][
            "holdingAreaFolderId"
        ]
        statuses = {item["status"] for item in activity}
        self.assertTrue(
            {
                "Applied and verified",
                "Partially completed",
                "Verification needed",
                "Failed with no changes",
                "Undone and verified",
                "Undo unavailable",
            }.issubset(statuses)
        )
        for item in activity:
            for file_id in item["fileIds"]:
                self.assertIn(file_id, self.files_by_id)
            for change in item.get("reversibleChanges", []):
                self.assertIn(change["fileId"], self.files_by_id)
                if change.get("beforeFolderId"):
                    self.assertIn(change["beforeFolderId"], self.folders_by_id)
            self.assertTrue(item["verification"].strip())

        available_undo = [
            item for item in activity if item["undo"]["status"] == "available"
        ]
        self.assertEqual(len(available_undo), 2)
        available_file_ids = {
            file_id for item in available_undo for file_id in item["fileIds"]
        }
        self.assertTrue(available_file_ids.isdisjoint(plan_file_ids))
        self.assertTrue(available_file_ids.isdisjoint(active_duplicate_file_ids))
        for item in available_undo:
            reversible = item.get("reversibleChanges", [])
            self.assertTrue(reversible)
            self.assertTrue(
                {change["fileId"] for change in reversible}.issubset(item["fileIds"])
            )
            for change in reversible:
                current = self.files_by_id[change["fileId"]]
                folder_changed = current["folderId"] != change.get("beforeFolderId")
                name_changed = (
                    "beforeName" in change and current["name"] != change["beforeName"]
                )
                self.assertTrue(
                    folder_changed or name_changed,
                    f"{item['id']} must describe a real reversible fixture change",
                )
            if item["undo"]["scope"] == "full-operation":
                self.assertEqual(
                    {change["fileId"] for change in reversible}, set(item["fileIds"])
                )
            if item["undo"]["scope"] == "return-copy":
                self.assertEqual(len(reversible), 1)
                held_file = self.files_by_id[reversible[0]["fileId"]]
                self.assertEqual(held_file["folderId"], holding_area_id)
                self.assertTrue(held_file.get("inHoldingArea"))

        self.assertTrue(
            any(
                item["workflow"] == "organization"
                and item["status"] == "Applied and verified"
                for item in activity
            )
        )
        self.assertTrue(
            any(
                item["workflow"] == "duplicates"
                and item["undo"]["status"] == "available"
                and item["undo"]["scope"] == "return-copy"
                for item in activity
            )
        )
        self.assertTrue(
            any(
                item["status"] == "Undo unavailable"
                and item["undo"]["status"] == "unavailable"
                and "Prepare a new organization plan" in item["undo"]["detail"]
                for item in activity
            )
        )

    def test_one_workflow_state_and_stage_definitions_are_complete(self) -> None:
        state = self.fixture["initialState"]
        workflow = state["workflow"]
        self.assertEqual(workflow["active"], "none")
        self.assertEqual(
            set(workflow["allowed"]),
            {"none", "organization", "duplicates", "applying", "verifying"},
        )
        self.assertEqual(workflow["switchPolicy"], "confirm-discard")
        self.assertIn("Continue plan", workflow["switchDialog"]["continueLabel"])

        stages = self.fixture["stageDefinitions"]
        self.assertEqual(
            [item["id"] for item in stages["planPreparation"]],
            [
                "checking-files",
                "finding-groups",
                "checking-conflicts",
                "preparing-plan",
            ],
        )
        self.assertEqual(
            [item["id"] for item in stages["organizationApply"]],
            ["rechecking", "applying", "verifying", "complete"],
        )
        self.assertEqual(
            [item["id"] for item in stages["duplicateConsolidation"]],
            ["rechecking-copies", "consolidating", "verifying", "complete"],
        )
        self.assertNotIn("%", json.dumps(stages))
        self.assertEqual(state["stagedOperation"]["status"], "idle")
        self.assertIn("selection", state)
        self.assertEqual(state["planUndo"]["snapshots"], [])
        self.assertGreater(state["planUndo"]["maxSnapshots"], 0)

    def test_settings_defaults_and_locked_rule_match_the_contract(self) -> None:
        defaults = self.fixture["meta"]["defaults"]
        settings = self.fixture["initialState"]["settings"]
        self.assertEqual(defaults["planPreparation"], "manual")
        self.assertTrue(defaults["filesystemActionsRequireConfirmation"])
        self.assertTrue(defaults["planReadyNotifications"])
        self.assertTrue(defaults["noRealFileAccess"])
        self.assertEqual(settings["planPreparation"]["mode"], "manual")
        self.assertEqual(
            settings["planPreparation"]["application"], "always-ask"
        )
        self.assertEqual(
            set(settings["categories"]),
            {
                "Location",
                "Plan preparation",
                "Rules",
                "Duplicates",
                "Notifications",
                "Local AI",
                "Privacy",
                "Advanced",
            },
        )
        self.assertNotIn("Search", settings["categories"])
        self.assertNotIn("Files", settings["categories"])

        rules = settings["rules"]["items"]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertTrue(rule["locked"])
        self.assertTrue(rule["active"])
        self.assertEqual(rule["matchPrefixes"], ["CS1010X", "CS1010S"])
        self.assertEqual(rule["destinationFolderId"], "folder-cs1010s")
        self.assertEqual(
            set(rule["controls"]), {"inspect", "pause", "edit", "remove"}
        )
        self.assertTrue(settings["notifications"]["organizationPlanReady"])
        self.assertFalse(settings["localAI"]["enabled"])
        self.assertTrue(settings["localAI"]["advisoryOnly"])
        self.assertFalse(settings["localAI"]["runtimeConnection"])
        self.assertTrue(settings["privacy"]["localOnly"])
        self.assertFalse(settings["privacy"]["uploads"])
        self.assertFalse(settings["privacy"]["telemetryImplemented"])
        self.assertFalse(settings["privacy"]["fileContentsTransmitted"])

    def test_assets_and_runtime_integrations_are_local_and_bounded(self) -> None:
        for attribute, reference in self.parser.asset_references:
            with self.subTest(attribute=attribute, reference=reference):
                lowered = reference.lower()
                self.assertFalse(lowered.startswith(("http:", "https:", "//")))
                self.assertFalse(lowered.startswith("data:text/html"))

        self.assertIsNone(
            re.search(r"https?://(?!127\.0\.0\.1|localhost)", self.runtime_text)
        )
        self.assertIsNone(re.search(r"@import\s+url", self.styles, re.I))

        javascript_text = "\n".join(self.javascript.values())
        forbidden_runtime_patterns = {
            "backend API": r"(?:['\"]|`)\/api(?:\/|['\"`])",
            "XMLHttpRequest": r"\bXMLHttpRequest\b",
            "WebSocket": r"\bWebSocket\b",
            "EventSource": r"\bEventSource\b",
            "telemetry beacon": r"\bsendBeacon\b",
            "analytics integration": r"\b(?:gtag|googleAnalytics|mixpanel|segment)\s*\(",
            "directory picker": r"\bshowDirectoryPicker\b",
            "file picker": r"\bshow(?:Open|Save)FilePicker\b",
            "filesystem handle": r"\bFileSystem(?:File|Directory)Handle\b",
            "Node filesystem": r"(?:node:fs|require\s*\(\s*['\"]fs['\"]|from\s+['\"]fs['\"])",
            "Deno filesystem": r"\bDeno\.(?:read|write|open|remove|rename)",
            "production package": r"(?:\.\./)+src/organizer|organizer\.web|executor\.py",
        }
        for label, pattern in forbidden_runtime_patterns.items():
            with self.subTest(integration=label):
                self.assertIsNone(re.search(pattern, javascript_text, re.I))
        self.assertIsNone(re.search(r"type\s*=\s*['\"]file['\"]", self.index, re.I))

        fetch_calls = re.findall(
            r"\bfetch\s*\(\s*(['\"])(.*?)\1", javascript_text, re.S
        )
        for _, target in fetch_calls:
            with self.subTest(fetch_target=target):
                self.assertIn(target, {"fixtures.json", "./fixtures.json"})

        module_specifiers = re.findall(
            r"(?:\bfrom\s+|\bimport\s*\()\s*['\"]([^'\"]+)['\"]",
            javascript_text,
        )
        self.assertTrue(module_specifiers)
        for specifier in module_specifiers:
            with self.subTest(module=specifier):
                self.assertTrue(specifier.startswith("./"))
                self.assertNotIn("../", specifier)

    def test_accessibility_hooks_exist_without_layout_assumptions(self) -> None:
        elements = self.parser.elements
        ids = {
            attrs["id"]
            for _, attrs in elements
            if attrs.get("id") is not None
        }
        skip_targets = {
            attrs["href"][1:]
            for tag, attrs in elements
            if tag == "a"
            and (attrs.get("href") or "").startswith("#")
            and "main" in (attrs.get("href") or "").lower()
        }
        self.assertTrue(any(tag == "main" for tag, _ in elements))
        self.assertTrue(skip_targets)
        self.assertTrue(skip_targets.issubset(ids))
        self.assertTrue(
            any("primary" in (attrs.get("aria-label") or "").lower()
                for tag, attrs in elements if tag == "nav")
        )
        self.assertTrue(
            any(attrs.get("aria-live") in {"polite", "assertive"}
                for _, attrs in elements)
        )
        self.assertIn("aria-current", self.runtime_text)
        self.assertTrue(
            any(tag == "dialog" for tag, _ in elements)
            or 'role="dialog"' in self.runtime_text
            or "role='dialog'" in self.runtime_text
        )
        self.assertRegex(self.javascript["app.js"], r"\bEscape\b")
        self.assertRegex(self.javascript["app.js"], r"\.focus\s*\(")
        self.assertIn(":focus-visible", self.styles)
        self.assertIn("prefers-reduced-motion", self.styles)
        self.assertRegex(self.styles, r"@media\s*\(")
        self.assertNotRegex(self.index, r"draggable\s*=\s*['\"]true['\"]")

    def test_persistent_notice_and_primary_language_are_safe(self) -> None:
        self.assertIn(PROTOTYPE_NOTICE, self.index)
        self.assertIn("Preview — no files have moved.", self.fixture_text)
        for phrase in (
            "reversible holding area",
            "Files are not deleted.",
            "Storage is not recovered.",
            "Copies are rechecked before applying.",
        ):
            self.assertIn(phrase, self.fixture_text)

        forbidden_primary_phrases = (
            "safe to delete",
            "useless",
            "automatic cleanup",
            "permanent cleanup",
            "remove junk",
            "junk files",
            "recover storage",
            "space saved",
            "health score",
            "cleanup score",
            "executor.py",
            "moveplanitem",
            "csrf",
            "sha-256",
            "json log",
        )
        for phrase in forbidden_primary_phrases:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.primary_ui_text)


if __name__ == "__main__":
    unittest.main()
