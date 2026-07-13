from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE_ROOT = REPOSITORY_ROOT / "prototype" / "loci-flow-ux-1-1-pass-1"
EXPECTED_FILES = {
    "README.md",
    "app.js",
    "fixtures.json",
    "index.html",
    "store.js",
    "styles.css",
    "views.js",
}
PRIMARY_DESTINATIONS = {
    "#/home": "Home",
    "#/search": "Search",
    "#/files": "Files",
    "#/organize": "Organize",
    "#/duplicates": "Duplicates",
    "#/activity": "Activity",
    "#/settings": "Settings",
}
SAFETY_NOTICE = (
    "Prototype using sample files. "
    "No files on your computer are accessed or changed."
)


class _PrototypeHTMLParser(HTMLParser):
    """Collect semantic hooks without making assumptions about page geometry."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.primary_navigation_hrefs: list[str] = []
        self.asset_references: list[tuple[str, str]] = []
        self._inside_primary_navigation = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))

        if tag == "nav" and "primary" in (attributes.get("aria-label") or "").lower():
            self._inside_primary_navigation = True

        href = attributes.get("href")
        if self._inside_primary_navigation and href:
            self.primary_navigation_hrefs.append(href)

        for attribute in ("href", "src"):
            value = attributes.get(attribute)
            if value:
                self.asset_references.append((attribute, value))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag == "nav":
            self._inside_primary_navigation = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav":
            self._inside_primary_navigation = False


class LociFlowPrototypeContractTests(unittest.TestCase):
    """Protect product/safety contracts without freezing the visual design."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_path = PROTOTYPE_ROOT / "fixtures.json"
        cls.fixture = json.loads(cls.fixture_path.read_text(encoding="utf-8"))
        cls.index = (PROTOTYPE_ROOT / "index.html").read_text(encoding="utf-8")
        cls.styles = (PROTOTYPE_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.javascript = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(PROTOTYPE_ROOT.glob("*.js"))
        }
        cls.runtime_sources = {
            "index.html": cls.index,
            "styles.css": cls.styles,
            "fixtures.json": cls.fixture_path.read_text(encoding="utf-8"),
            **cls.javascript,
        }
        cls.runtime_text = "\n".join(cls.runtime_sources.values())
        cls.parser = _PrototypeHTMLParser()
        cls.parser.feed(cls.index)
        cls.files_by_id = {item["id"]: item for item in cls.fixture["files"]}
        cls.folders_by_id = {
            item["id"]: item for item in cls.fixture["folders"]
        }

    def test_prototype_has_the_intended_isolated_file_set(self) -> None:
        self.assertTrue(PROTOTYPE_ROOT.is_dir())
        actual = {
            path.name
            for path in PROTOTYPE_ROOT.iterdir()
            if path.is_file() and not path.name.startswith(".")
        }
        self.assertEqual(actual, EXPECTED_FILES)
        self.assertFalse(any(path.is_dir() for path in PROTOTYPE_ROOT.iterdir()))
        for manifest in (
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "deno.json",
        ):
            self.assertFalse((PROTOTYPE_ROOT / manifest).exists())

    def test_primary_destinations_are_semantic_and_advanced_is_subordinate(self) -> None:
        self.assertEqual(
            set(self.parser.primary_navigation_hrefs), set(PRIMARY_DESTINATIONS)
        )
        for route, label in PRIMARY_DESTINATIONS.items():
            with self.subTest(route=route):
                self.assertIn(route, self.parser.primary_navigation_hrefs)
                self.assertIn(label, self.runtime_text)

        self.assertIn("Advanced", self.runtime_text)
        self.assertNotIn("#/advanced", self.runtime_text.lower())
        self.assertNotIn("#/settings/advanced", self.parser.primary_navigation_hrefs)

    def test_fixture_root_version_counts_and_references_are_consistent(self) -> None:
        self.assertEqual(
            set(self.fixture),
            {
                "fixtureVersion",
                "meta",
                "folders",
                "files",
                "searchScenarios",
                "initialState",
            },
        )
        self.assertEqual(self.fixture["fixtureVersion"], 1)

        fixture_destinations = self.fixture["meta"]["primaryDestinations"]
        self.assertEqual(
            {item["route"]: item["label"] for item in fixture_destinations},
            PRIMARY_DESTINATIONS,
        )
        self.assertNotIn(
            "advanced",
            {item["id"].lower() for item in fixture_destinations},
        )

        counts = self.fixture["meta"]["counts"]
        self.assertEqual(
            counts,
            {
                "looseFiles": 84,
                "plannedFileChanges": 38,
                "plannedDestinations": 5,
                "reviewRequired": 2,
                "blockingConflicts": 1,
                "activeDuplicateGroups": 2,
            },
        )

        folder_ids = [item["id"] for item in self.fixture["folders"]]
        file_ids = [item["id"] for item in self.fixture["files"]]
        self.assertEqual(len(folder_ids), len(set(folder_ids)))
        self.assertEqual(len(file_ids), len(set(file_ids)))
        self.assertTrue(all(folder_ids))
        self.assertTrue(all(file_ids))

        for folder in self.fixture["folders"]:
            parent_id = folder.get("parentId")
            if parent_id is not None:
                self.assertIn(parent_id, self.folders_by_id)
        for file_record in self.fixture["files"]:
            self.assertIn(file_record["folderId"], self.folders_by_id)

    def test_required_hierarchy_files_and_identifiers_exist(self) -> None:
        folder_names = {item["name"] for item in self.fixture["folders"]}
        self.assertTrue(
            {
                "Downloads",
                "Projects",
                "Elarian",
                "Northstar",
                "Website Redesign",
                "Study",
                "CS1010X",
                "Mathematics",
                "Work",
                "Client Forms",
                "Reports",
                "Receipts",
                "Images",
                "Travel",
            }.issubset(folder_names)
        )

        filenames = {item["name"] for item in self.fixture["files"]}
        self.assertTrue(
            {
                "CS1010X 2016 Finals.pdf",
                "CS1010X 2016-2017 Finals.pdf",
                "CS1010X 2016 Final Solutions.pdf",
                "CS1010S 2016 Finals.pdf",
                "CS1010X 2019 Finals.pdf",
                "document (12).pdf",
                "53538976.jpg",
                "final_final_v2.docx",
                "download.pdf",
                "IMG_4832.png",
                "Elarian logo.svg",
                "final.pdf",
                "Notes.docx",
                "Architecture.pdf",
            }.issubset(filenames)
        )

        identifiers = {
            identifier
            for item in self.fixture["files"]
            for identifier in item.get("keywords", [])
        }
        self.assertTrue(
            {"CS1010X", "CS1010S", "Elarian", "V1_5", "Northstar", "INV-2048"}
            .issubset(identifiers)
        )

    def test_required_search_scenarios_and_ranked_result_are_present(self) -> None:
        scenarios = self.fixture["searchScenarios"]
        scenario_ids = [scenario["id"] for scenario in scenarios]
        self.assertEqual(len(scenario_ids), len(set(scenario_ids)))
        self.assertTrue(
            {
                "search-cs1010x-finals",
                "search-exact-filename",
                "search-partial-filename",
                "search-distinctive-identifier",
                "search-weak-only",
                "search-no-result",
                "search-refined-receipts",
            }.issubset(scenario_ids)
        )

        required = next(
            scenario
            for scenario in scenarios
            if scenario["id"] == "search-cs1010x-finals"
        )
        self.assertEqual(required["query"], "CS1010X 2016 Finals")
        ranked_names = [
            self.files_by_id[result["fileId"]]["name"]
            for result in required["results"]
        ]
        self.assertEqual(
            ranked_names,
            [
                "CS1010X 2016 Finals.pdf",
                "CS1010X 2016-2017 Finals.pdf",
                "CS1010X 2016 Final Solutions.pdf",
                "CS1010S 2016 Finals.pdf",
                "CS1010X 2019 Finals.pdf",
            ],
        )
        self.assertEqual(required["results"][0]["band"], "Strong match")
        self.assertEqual(required["results"][-1]["band"], "Weak match")
        for result in required["results"]:
            self.assertIn(result["fileId"], self.files_by_id)
            self.assertIn(
                result["band"], {"Strong match", "Possible match", "Weak match"}
            )
            self.assertTrue(result["explanation"].strip())
            self.assertNotIn("%", result["explanation"])
        for scenario in scenarios:
            for result in scenario["results"]:
                self.assertTrue(
                    {"score", "confidence", "percentage"}.isdisjoint(result)
                )
        by_id = {scenario["id"]: scenario for scenario in scenarios}
        self.assertTrue(by_id["search-exact-filename"]["results"])
        self.assertTrue(by_id["search-partial-filename"]["results"])
        self.assertTrue(by_id["search-distinctive-identifier"]["results"])
        self.assertGreaterEqual(len(by_id["search-cs1010x-finals"]["results"]), 3)
        self.assertTrue(by_id["search-weak-only"]["results"])
        self.assertTrue(
            all(
                result["band"] == "Weak match"
                for result in by_id["search-weak-only"]["results"]
            )
        )
        self.assertEqual(by_id["search-no-result"]["results"], [])
        self.assertTrue(by_id["search-refined-receipts"].get("refinements"))

    def test_relationships_use_valid_stable_ids_and_explain_the_connection(self) -> None:
        relationships = [
            (file_record["id"], relationship)
            for file_record in self.fixture["files"]
            for relationship in file_record.get("related", [])
        ]
        self.assertTrue(relationships)
        for source_id, relationship in relationships:
            with self.subTest(source=source_id, relationship=relationship):
                self.assertIn(relationship["fileId"], self.files_by_id)
                self.assertNotEqual(relationship["fileId"], source_id)
                self.assertTrue(relationship["reason"].strip())

    def test_initial_plan_has_five_groups_and_thirty_eight_valid_changes(self) -> None:
        plan = self.fixture["initialState"]["plan"]
        self.assertEqual(
            {
                key: plan["summary"][key]
                for key in (
                    "fileChanges",
                    "destinations",
                    "reviewRequired",
                    "blockingConflicts",
                )
            },
            {
                "fileChanges": 38,
                "destinations": 5,
                "reviewRequired": 2,
                "blockingConflicts": 1,
            },
        )
        self.assertEqual(len(plan["groups"]), 5)
        self.assertEqual(len(plan["changes"]), 38)
        self.assertEqual(len({item["fileId"] for item in plan["changes"]}), 38)
        self.assertEqual(len(plan["exceptions"]), 2)
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertTrue(all(item["tier"] == 2 for item in plan["exceptions"]))
        self.assertTrue(all(item["tier"] == 1 for item in plan["conflicts"]))
        self.assertTrue(
            all(
                item["status"] == "unresolved"
                for item in [*plan["exceptions"], *plan["conflicts"]]
            )
        )
        self.assertEqual(
            {
                self.files_by_id[item["fileId"]]["name"]
                for item in plan["exceptions"]
            },
            {"Elarian logo.svg", "final.pdf"},
        )
        self.assertIn(
            "notes",
            json.dumps(plan["conflicts"], ensure_ascii=False).lower(),
        )

        destination_ids = {
            group["destinationFolderId"] for group in plan["groups"]
        }
        self.assertEqual(len(destination_ids), 5)
        self.assertTrue(destination_ids.issubset(self.folders_by_id))
        self.assertEqual(
            {change["destinationFolderId"] for change in plan["changes"]},
            destination_ids,
        )
        for change in plan["changes"]:
            self.assertIn(change["fileId"], self.files_by_id)

        planned_text = json.dumps(plan, ensure_ascii=False).lower()
        for required_group in (
            "elarian",
            "northstar",
            "cs1010s",
            "client forms",
            "receipts",
        ):
            self.assertIn(required_group, planned_text)

    def test_duplicate_groups_and_recheck_scenarios_are_bounded(self) -> None:
        duplicates = self.fixture["initialState"]["duplicates"]
        active_groups = [
            group
            for group in duplicates["groups"]
            if group.get("status", "active") == "active"
        ]
        self.assertEqual(len(active_groups), 2)
        self.assertEqual(len(duplicates["groups"]), 2)

        active_copy_ids: set[str] = set()
        for group in active_groups:
            copy_ids = group["fileIds"]
            self.assertGreaterEqual(len(copy_ids), 2)
            self.assertTrue(set(copy_ids).issubset(self.files_by_id))
            self.assertTrue(active_copy_ids.isdisjoint(copy_ids))
            active_copy_ids.update(copy_ids)
        self.assertEqual(
            sum(group.get("preferredFileId") is not None for group in active_groups),
            1,
        )

        self.assertEqual(len(duplicates["rechecks"]), 2)
        self.assertEqual(
            {scenario["kind"] for scenario in duplicates["rechecks"]},
            {"preferred-copy-edited", "extra-copy-changed"},
        )
        for scenario in duplicates["rechecks"]:
            self.assertTrue(set(scenario["fileIds"]).issubset(self.files_by_id))
            self.assertTrue(set(scenario["fileIds"]).isdisjoint(active_copy_ids))
            self.assertTrue(scenario["message"].strip())
        changed_extra = next(
            scenario
            for scenario in duplicates["rechecks"]
            if scenario["kind"] == "extra-copy-changed"
        )
        self.assertTrue(changed_extra["excludedFromExactCopyActions"])
        self.assertNotIn(changed_extra["changedFileId"], active_copy_ids)

    def test_activity_contains_all_truthful_outcome_states(self) -> None:
        activity = self.fixture["initialState"]["activity"]
        statuses = {operation["status"] for operation in activity}
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
        undo_states = {item["undoAvailability"] for item in activity}
        self.assertTrue({"available", "limited", "unavailable"}.issubset(undo_states))

    def test_initial_settings_use_safe_defaults(self) -> None:
        state = self.fixture["initialState"]
        onboarding = state["onboarding"]
        self.assertFalse(onboarding["completed"])
        self.assertEqual(len(onboarding["requiredElements"]), 4)
        onboarding_text = " ".join(onboarding["requiredElements"]).lower()
        for required in ("downloads", "local", "confirmation", "home"):
            self.assertIn(required, onboarding_text)

        settings = state["settings"]
        self.assertTrue(
            {
                "locations",
                "organization",
                "automation",
                "lockedRules",
                "duplicates",
                "searchAndIndexing",
                "notifications",
                "privacyAndTelemetry",
                "advanced",
            }.issubset(settings)
        )
        self.assertEqual(state["location"]["status"], "Up to date")
        self.assertEqual(
            self.folders_by_id[state["location"]["folderId"]]["name"],
            "Downloads",
        )
        self.assertEqual(
            settings["locations"]["approvedFolderIds"],
            [state["location"]["folderId"]],
        )
        self.assertIs(settings["locations"]["usesRealFolderChooser"], False)
        self.assertEqual(settings["automation"]["planPreparation"], "manual")
        self.assertEqual(settings["automation"]["application"], "always-confirm")

        notifications = settings["notifications"]
        enabled = {
            name for name, value in notifications.items() if value is True
        }
        self.assertEqual(enabled, {"organizationPlanReady"})
        self.assertIs(
            settings["privacyAndTelemetry"]["telemetryEnabled"], False
        )
        self.assertEqual(state["demoOutcome"], "verified")
        self.assertEqual(
            set(settings["advanced"]["demoOutcomes"]),
            {"verified", "partial", "failed", "verification_incomplete"},
        )

        locked_rules = settings["lockedRules"]["items"]
        active_rules = [
            rule
            for rule in locked_rules
            if rule.get("active") is True or rule.get("status") == "active"
        ]
        self.assertEqual(len(locked_rules), 1)
        self.assertEqual(len(active_rules), 1)
        rule_text = json.dumps(active_rules[0], ensure_ascii=False)
        for required in ("CS1010X", "CS1010S"):
            self.assertIn(required, rule_text)
        rule_destination = self.folders_by_id[active_rules[0]["destinationFolderId"]]
        self.assertEqual(rule_destination["name"], "CS1010S")
        self.assertEqual(
            self.folders_by_id[rule_destination["parentId"]]["name"],
            "Study",
        )

    def test_assets_and_module_imports_are_local_only(self) -> None:
        for attribute, reference in self.parser.asset_references:
            with self.subTest(attribute=attribute, reference=reference):
                self.assertFalse(reference.startswith(("http://", "https://", "//")))
                if not reference.startswith("#"):
                    asset_path = (PROTOTYPE_ROOT / reference.split("?", 1)[0]).resolve()
                    self.assertTrue(asset_path.is_relative_to(PROTOTYPE_ROOT))
                    self.assertTrue(asset_path.is_file())

        for filename, source in self.runtime_sources.items():
            lowered = source.lower()
            with self.subTest(filename=filename):
                for forbidden in (
                    "http://",
                    "https://",
                    "//cdn.",
                    "fonts.googleapis",
                    "fonts.gstatic",
                    "@import",
                ):
                    self.assertNotIn(forbidden, lowered)

        import_pattern = re.compile(
            r"^\s*import(?:[\s\S]*?\sfrom\s*)?[\"']([^\"']+)[\"']",
            re.MULTILINE,
        )
        for filename, source in self.javascript.items():
            for module in import_pattern.findall(source):
                with self.subTest(filename=filename, module=module):
                    self.assertTrue(module.startswith("./"))
                    module_path = (PROTOTYPE_ROOT / module).resolve()
                    self.assertTrue(module_path.is_relative_to(PROTOTYPE_ROOT))
                    self.assertTrue(module_path.is_file())

    def test_runtime_has_no_backend_filesystem_network_or_collection_integration(self) -> None:
        lowered = self.runtime_text.lower()
        for forbidden in (
            "organizer.executor",
            "organizer.application",
            "src/organizer",
            "executor.py",
            "showdirectorypicker",
            "showopenfilepicker",
            "showsavefilepicker",
            "requestfilesystem",
            "webkitrequestfilesystem",
            "node:fs",
            "require(\"fs\")",
            "require('fs')",
            "xmlhttprequest",
            "websocket(",
            "eventsource(",
            "navigator.sendbeacon",
            "notification.requestpermission",
            "new notification(",
            "serviceworker.register",
            "google-analytics",
            "googletagmanager",
            "gtag(",
            "mixpanel",
            "amplitude",
            "posthog",
            "segment.io",
            "sentry.init",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

        self.assertIs(
            self.fixture["initialState"]["settings"]["privacyAndTelemetry"]
            ["telemetryEnabled"],
            False,
        )
        self.assertNotRegex(lowered, r"[\"']/api(?:/|[\"'])")
        fetch_calls = re.findall(
            r"\bfetch\s*\(\s*[\"']([^\"']+)[\"']", self.runtime_text
        )
        self.assertEqual(
            len(fetch_calls),
            len(re.findall(r"\bfetch\s*\(", self.runtime_text)),
            "Every fetch target must be a reviewable string literal.",
        )
        self.assertTrue(
            all(target in {"fixtures.json", "./fixtures.json"} for target in fetch_calls)
        )
        self.assertFalse(
            any(
                tag == "input" and attributes.get("type") == "file"
                for tag, attributes in self.parser.elements
            )
        )

    def test_accessibility_and_safety_hooks_are_present(self) -> None:
        elements = self.parser.elements
        self.assertEqual(self.index.count(SAFETY_NOTICE), 1)
        main_ids = {
            attrs["id"]
            for tag, attrs in elements
            if tag == "main" and attrs.get("id")
        }
        skip_targets = {
            attrs["href"].removeprefix("#")
            for tag, attrs in elements
            if tag == "a"
            and (attrs.get("href") or "").startswith("#")
            and not (attrs.get("href") or "").startswith("#/")
        }
        self.assertTrue(main_ids)
        self.assertTrue(main_ids & skip_targets)
        self.assertTrue(any(attrs.get("aria-live") for _, attrs in elements))
        self.assertTrue(
            any(
                tag == "dialog" or attrs.get("role") == "dialog"
                for tag, attrs in elements
            )
        )
        self.assertTrue(any(tag == "button" for tag, _ in elements))
        labelled_field = re.search(r"<label\b", self.runtime_text) or re.search(
            r"<(?:input|select|textarea)\b[^>]*\baria-(?:label|labelledby)=",
            self.runtime_text,
            re.IGNORECASE,
        )
        self.assertIsNotNone(labelled_field)
        self.assertIn(":focus-visible", self.styles)
        self.assertIn("prefers-reduced-motion", self.styles)

        lowered = self.runtime_text.lower()
        for forbidden in (
            'draggable="true"',
            "draggable='true'",
            "dragstart",
            "ondrop=",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_critical_consumer_terms_are_available_without_raw_scores(self) -> None:
        for term in (
            "Organization plan",
            "Leave unchanged",
            "Exact copy",
            "Activity",
            "Verified",
            "Review required",
            "Conflict",
            "Planned",
            "Applying",
            "Verifying",
        ):
            with self.subTest(term=term):
                self.assertIn(term.lower(), self.runtime_text.lower())
        self.assertRegex(
            self.runtime_text.lower(), r"\bincluded in (?:this )?plan\b"
        )
        self.assertNotRegex(
            self.runtime_text, r"\b(?:confidence|relevance)\s*[:=]?\s*\d+%"
        )


if __name__ == "__main__":
    unittest.main()
