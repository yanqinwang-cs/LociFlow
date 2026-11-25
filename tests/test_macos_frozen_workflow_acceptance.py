from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organizer.macos_launcher.application_paths import MacOSApplicationPaths
from organizer.macos_launcher.frozen_acceptance import (
    format_frozen_workflow_report,
    run_frozen_workflow_acceptance,
)
from organizer.macos_launcher.packaged_diagnostics import (
    PackagedDiagnosticInput,
    PackagedDiagnosticSink,
    PackagedReleaseIdentity,
)


class MacOSFrozenWorkflowAcceptanceTests(unittest.TestCase):
    def test_real_server_authenticated_check_conflicts_and_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory).resolve()
            selected = temporary / "selected"
            different = temporary / "different"
            (selected / "Inbox").mkdir(parents=True)
            different.mkdir()
            (selected / "Inbox" / "alpha.txt").write_text(
                "alpha\n",
                encoding="utf-8",
            )
            paths = MacOSApplicationPaths.for_home(temporary / "home")
            sink = PackagedDiagnosticSink(
                paths,
                identity=PackagedReleaseIdentity("0.1.0-beta.1", 1),
            )
            sink(PackagedDiagnosticInput("startup", "launcher_started"))

            report = run_frozen_workflow_acceptance(
                selected_root=selected,
                different_root=different,
                application_paths=paths,
                diagnostic_sink=sink,
            )

            self.assertEqual(report.authenticated_home, "passed")
            self.assertEqual(report.check_request, "passed")
            self.assertEqual(report.required_assets, "passed")
            self.assertEqual(report.browser_url_count, 1)
            self.assertEqual(report.global_conflict, "passed")
            self.assertEqual(report.same_root_conflict, "passed")
            self.assertEqual(report.different_root_coexistence, "passed")
            self.assertEqual(report.coordinated_shutdown, "passed")
            self.assertEqual(report.post_shutdown_reacquisition, "passed")
            self.assertEqual(report.packaged_diagnostics, "passed")

            safe = format_frozen_workflow_report(report)
            self.assertNotIn(str(temporary), safe)
            self.assertNotIn("http://", safe)
            self.assertNotIn("/launch/", safe)
            self.assertEqual(
                json.loads(safe)["post_shutdown_reacquisition"],
                "passed",
            )
        self.assertFalse(temporary.exists())


if __name__ == "__main__":
    unittest.main()
