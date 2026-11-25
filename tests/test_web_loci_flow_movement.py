from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "organizer" / "web"
LOCI = WEB / "static" / "loci_flow"


class WebLociFlowMovementBoundaryTests(unittest.TestCase):
    def test_frontend_connects_only_to_the_fixed_backend_movement_routes(self) -> None:
        assets = "\n".join(
            (LOCI / name).read_text(encoding="utf-8")
            for name in ("api.js", "data_source.js", "view_models.js", "app.js", "views.js")
        )
        route = (WEB / "routes" / "organization_api.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("Move files — not connected yet", assets)
        self.assertIn("/api/organization/move/preview", assets)
        self.assertIn("/api/organization/move", assets)
        self.assertIn("MOVE APPROVED FILES", assets)
        self.assertIn('action === "preview-move"', assets)
        self.assertIn('"/api/organization/move/preview"', route)
        self.assertIn('"/api/organization/move"', route)
        self.assertNotIn("organizer.executor", route)
        self.assertNotIn("apply_move_plan", route)
        self.assertNotIn("undo_operation_log", route)

    def test_duplicate_bin_execution_is_not_part_of_movement_boundary(self) -> None:
        movement = (
            ROOT
            / "src"
            / "organizer"
            / "application"
            / "organization_movement_service.py"
        ).read_text(encoding="utf-8")
        route = (WEB / "routes" / "organization_api.py").read_text(
            encoding="utf-8"
        )
        combined = movement + route

        self.assertIn("duplicate_copies_deferred", combined)
        self.assertNotIn("Duplicate Bin", combined)
        self.assertNotIn("hash_file", combined)
        self.assertNotIn("group_sha256", combined)
        self.assertNotIn("selected_paths", combined)


if __name__ == "__main__":
    unittest.main()
