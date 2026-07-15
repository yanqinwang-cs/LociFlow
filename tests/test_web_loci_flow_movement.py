from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "organizer" / "web"
LOCI = WEB / "static" / "loci_flow"


class WebLociFlowMovementBoundaryTests(unittest.TestCase):
    def test_backend_routes_exist_while_frontend_movement_remains_disconnected(self) -> None:
        assets = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(LOCI.glob("*.js"))
        )
        route = (WEB / "routes" / "organization_api.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("Move files — not connected yet", assets)
        self.assertNotIn("/api/organization/move", assets)
        self.assertNotIn("MOVE APPROVED FILES", assets)
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
