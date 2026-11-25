from __future__ import annotations

import json
import stat
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from unittest import mock

from organizer.macos_launcher.application_paths import MacOSApplicationPaths
from organizer.macos_launcher.packaged_diagnostics import (
    ACTIVE_LOG_NAME,
    LOCK_NAME,
    MAX_ACTIVE_BYTES,
    ROTATED_LOG_NAMES,
    PackagedDiagnosticInput,
    PackagedDiagnosticSink,
    PackagedReleaseIdentity,
    create_packaged_diagnostic_sink,
    read_runtime_identity,
)

_IDENTITY = PackagedReleaseIdentity("0.1.0-beta.1", 7)
_NOW = datetime(2026, 7, 27, 12, 30, tzinfo=UTC)


class MacOSPackagedDiagnosticsTests(unittest.TestCase):
    def test_construction_has_no_filesystem_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "missing-home"
            paths = MacOSApplicationPaths.for_home(home)

            PackagedDiagnosticSink(paths, identity=_IDENTITY)

            self.assertFalse(home.exists())

    def test_exact_location_permissions_and_bounded_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            sink = PackagedDiagnosticSink(
                paths,
                identity=_IDENTITY,
                now=lambda: _NOW,
            )

            sink(
                PackagedDiagnosticInput(
                    phase="waiting_for_ready",
                    code="readiness_failed",
                    exception_class="TimeoutError",
                )
            )

            expected = (
                Path(directory).resolve()
                / "home"
                / "Library"
                / "Logs"
                / "Loci Flow"
                / "launcher.log"
            )
            self.assertEqual(paths.logs_directory / ACTIVE_LOG_NAME, expected)
            self.assertEqual(
                stat.S_IMODE(paths.logs_directory.stat().st_mode),
                0o700,
            )
            self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE((paths.logs_directory / LOCK_NAME).stat().st_mode),
                0o600,
            )
            payload = json.loads(expected.read_text(encoding="ascii"))
            self.assertEqual(
                payload,
                {
                    "application_version": "0.1.0-beta.1",
                    "build_number": 7,
                    "code": "readiness_failed",
                    "exception_class": "TimeoutError",
                    "phase": "waiting_for_ready",
                    "timestamp_utc": "2026-07-27T12:30:00+00:00",
                },
            )

    def test_untrusted_fields_messages_and_tracebacks_are_not_persisted(
        self,
    ) -> None:
        forbidden = (
            "/private/tmp/selected/Secret.txt",
            "relative/Secret.txt",
            "http://127.0.0.1:1234/launch/token-value",
            "csrf-secret-value",
            "session-secret-value",
            "a" * 64,
            "123e4567-e89b-12d3-a456-426614174000",
            "Traceback (most recent call last)",
            "arbitrary private exception message",
        )

        @dataclass(frozen=True)
        class HostileDiagnostic:
            phase: str
            code: str
            exception_class: str
            traceback: str
            message: str

        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            sink = PackagedDiagnosticSink(
                paths,
                identity=_IDENTITY,
                now=lambda: _NOW,
            )
            sink(
                HostileDiagnostic(
                    phase=forbidden[0],
                    code=forbidden[2],
                    exception_class="PrivateFilesystemException",
                    traceback="\n".join(forbidden),
                    message=forbidden[-1],
                )
            )

            content = (paths.logs_directory / ACTIVE_LOG_NAME).read_text(
                encoding="ascii"
            )
            payload = json.loads(content)
            self.assertEqual(payload["phase"], "startup")
            self.assertEqual(payload["code"], "unknown_launcher_failure")
            self.assertEqual(payload["exception_class"], "Exception")
            for value in forbidden:
                self.assertNotIn(value, content)
            self.assertEqual(
                set(payload),
                {
                    "timestamp_utc",
                    "application_version",
                    "build_number",
                    "phase",
                    "code",
                    "exception_class",
                },
            )

    def test_existing_symlink_or_nonregular_target_is_rejected_as_noop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            paths.create_logs_directory()
            outside = Path(directory) / "outside.log"
            outside.write_text("untouched", encoding="utf-8")
            (paths.logs_directory / ACTIVE_LOG_NAME).symlink_to(outside)

            _sink(paths)(PackagedDiagnosticInput("startup", "launcher_started"))

            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")
            self.assertTrue((paths.logs_directory / ACTIVE_LOG_NAME).is_symlink())

        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            paths.create_logs_directory()
            (paths.logs_directory / ACTIVE_LOG_NAME).mkdir()

            _sink(paths)(PackagedDiagnosticInput("startup", "launcher_started"))

            self.assertTrue((paths.logs_directory / ACTIVE_LOG_NAME).is_dir())

    def test_rotation_rejects_symlinked_rotated_target_without_deletion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            paths.create_logs_directory()
            active = paths.logs_directory / ACTIVE_LOG_NAME
            active.write_bytes(b"x" * MAX_ACTIVE_BYTES)
            active.chmod(0o600)
            outside = Path(directory) / "outside.log"
            outside.write_text("untouched", encoding="utf-8")
            (paths.logs_directory / ROTATED_LOG_NAMES[0]).symlink_to(outside)

            _sink(paths)(PackagedDiagnosticInput("startup", "launcher_started"))

            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")
            self.assertEqual(active.stat().st_size, MAX_ACTIVE_BYTES)

    def test_rotation_keeps_active_plus_two_rotated_files_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            paths.create_logs_directory()
            active = paths.logs_directory / ACTIVE_LOG_NAME
            unrelated = paths.logs_directory / "tester-note.txt"
            unrelated.write_text("preserve", encoding="utf-8")
            sink = _sink(paths)
            event = PackagedDiagnosticInput("startup", "launcher_started")

            for _ in range(3):
                active.write_bytes(b"x" * MAX_ACTIVE_BYTES)
                active.chmod(0o600)
                sink(event)

            retained = [
                paths.logs_directory / ACTIVE_LOG_NAME,
                *(paths.logs_directory / name for name in ROTATED_LOG_NAMES),
            ]
            self.assertTrue(all(path.is_file() for path in retained))
            self.assertTrue(
                all(path.stat().st_size <= MAX_ACTIVE_BYTES for path in retained)
            )
            self.assertLessEqual(
                sum(path.stat().st_size for path in retained),
                MAX_ACTIVE_BYTES * 3,
            )
            self.assertFalse((paths.logs_directory / "launcher.log.3").exists())
            self.assertEqual(
                unrelated.read_text(encoding="utf-8"),
                "preserve",
            )

    def test_concurrent_writes_remain_complete_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            sink = _sink(paths)
            threads = [
                Thread(
                    target=lambda: [
                        sink(
                            PackagedDiagnosticInput(
                                "startup",
                                "launcher_started",
                            )
                        )
                        for _ in range(20)
                    ]
                )
                for _ in range(8)
            ]

            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            lines = (
                (paths.logs_directory / ACTIVE_LOG_NAME)
                .read_text(encoding="ascii")
                .splitlines()
            )
            self.assertEqual(len(lines), 160)
            self.assertTrue(all(json.loads(line) for line in lines))

    def test_storage_failure_is_always_nonfatal(self) -> None:
        paths = mock.Mock(spec=MacOSApplicationPaths)
        paths.create_logs_directory.side_effect = OSError("private path")
        sink = PackagedDiagnosticSink(paths, identity=_IDENTITY)

        sink(PackagedDiagnosticInput("startup", "launcher_started"))

        paths.create_logs_directory.assert_called_once_with()

    def test_runtime_identity_is_strict_and_factory_has_safe_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            identity_path = Path(directory) / "identity.json"
            identity_path.write_text(
                '{"application_version":"1.2.3","build_number":42}\n',
                encoding="ascii",
            )
            self.assertEqual(
                read_runtime_identity(identity_path),
                PackagedReleaseIdentity("1.2.3", 42),
            )
            for invalid in (
                "{}",
                '{"application_version":"1.2.3","build_number":true}',
                '{"application_version":"1.2.3","build_number":1,"path":"/x"}',
            ):
                identity_path.write_text(invalid, encoding="ascii")
                self.assertIsNone(read_runtime_identity(identity_path))

            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            with mock.patch(
                "organizer.macos_launcher.packaged_diagnostics.read_runtime_identity",
                return_value=None,
            ):
                sink = create_packaged_diagnostic_sink(paths, now=lambda: _NOW)
            sink(PackagedDiagnosticInput("startup", "launcher_started"))
            payload = json.loads(
                (paths.logs_directory / ACTIVE_LOG_NAME).read_text(encoding="ascii")
            )
            self.assertEqual(payload["application_version"], "unknown")
            self.assertEqual(payload["build_number"], 0)

    def test_untrusted_release_metadata_is_replaced_not_persisted(self) -> None:
        forbidden = "http://example.test/private-token"
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            sink = PackagedDiagnosticSink(
                paths,
                identity=PackagedReleaseIdentity(forbidden, -4),
                now=lambda: _NOW,
            )

            sink(PackagedDiagnosticInput("startup", "launcher_started"))

            content = (paths.logs_directory / ACTIVE_LOG_NAME).read_text(
                encoding="ascii"
            )
            self.assertNotIn(forbidden, content)
            payload = json.loads(content)
            self.assertEqual(payload["application_version"], "unknown")
            self.assertEqual(payload["build_number"], 0)

    def test_existing_coordinator_failures_map_to_bounded_categories(self) -> None:
        cases = (
            (
                "application_lease",
                "application_already_open",
                "global_lease_already_held",
            ),
            ("application_lease", "lease_unavailable", "global_lease_unavailable"),
            ("root_lease", "root_already_open", "root_lease_already_held"),
            ("root_lease", "lease_unavailable", "root_lease_unavailable"),
            ("failed", "app_creation_failed", "application_construction_failed"),
            ("failed", "bind_failed", "socket_bind_failed"),
            ("running", "server_failed", "server_start_failed"),
            ("waiting_for_ready", "readiness_failed", "readiness_failed"),
            ("browser_bootstrapped", "browser_open_failed", "browser_launch_failed"),
            ("native_shutdown", "worker_failed", "coordinated_shutdown_failed"),
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = MacOSApplicationPaths.for_home(Path(directory) / "home")
            sink = _sink(paths)
            for phase, code, _ in cases:
                sink(PackagedDiagnosticInput(phase, code, "RuntimeError"))

            payloads = [
                json.loads(line)
                for line in (
                    (paths.logs_directory / ACTIVE_LOG_NAME)
                    .read_text(encoding="ascii")
                    .splitlines()
                )
            ]
            self.assertEqual(
                [payload["code"] for payload in payloads],
                [expected for _, _, expected in cases],
            )

    def test_module_has_no_network_or_upload_dependency(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "organizer"
            / "macos_launcher"
            / "packaged_diagnostics.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "import socket",
            "import urllib",
            "import http",
            "import requests",
            "telemetry",
            "upload",
        ):
            self.assertNotIn(forbidden, source.lower())


def _sink(paths: MacOSApplicationPaths) -> PackagedDiagnosticSink:
    return PackagedDiagnosticSink(
        paths,
        identity=_IDENTITY,
        now=lambda: _NOW,
    )


if __name__ == "__main__":
    unittest.main()
