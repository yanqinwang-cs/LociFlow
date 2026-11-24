from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pickle
import stat
import subprocess
import sys
import tempfile
from threading import Barrier, Event, Lock, Thread
import unittest
from unittest import mock

from organizer.macos_launcher.application_paths import MacOSApplicationPaths
from organizer.macos_launcher.process_lease import (
    ProcessLeaseError,
    ProcessLeaseKind,
    acquire_application_instance_lease,
    acquire_root_process_lease,
    canonical_root_lock_key,
)
from organizer.macos_launcher import process_lease as process_lease_module


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class MacOSProcessLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.paths = MacOSApplicationPaths.for_home(self.base / "home")
        self.root = self.base / "selected-root"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_root_key_is_deterministic_alias_stable_and_path_free(self) -> None:
        second = self.base / "second-root"
        second.mkdir()
        alias = self.base / "root-alias"
        alias.symlink_to(self.root, target_is_directory=True)

        first_key = canonical_root_lock_key(self.root)

        self.assertEqual(first_key, canonical_root_lock_key(self.root))
        self.assertEqual(first_key, canonical_root_lock_key(alias))
        self.assertNotEqual(first_key, canonical_root_lock_key(second))
        self.assertRegex(first_key, r"^[0-9a-f]{64}$")
        self.assertNotIn(self.root.name, first_key)
        self.assertEqual(
            self.paths.root_lock_path(first_key).name,
            f"{first_key}.lock",
        )

    def test_first_process_holds_lease_second_fails_then_succeeds(self) -> None:
        lease = acquire_root_process_lease(self.paths, self.root)
        try:
            self.assertEqual(self._child_attempt("root"), "already_held")
            self.assertFalse(lease.released)
        finally:
            lease.release()

        self.assertEqual(self._child_attempt("root"), "acquired")

    def test_application_and_root_leases_are_independent(self) -> None:
        with acquire_application_instance_lease(self.paths) as application:
            with acquire_root_process_lease(self.paths, self.root) as root:
                self.assertEqual(
                    application.kind,
                    ProcessLeaseKind.APPLICATION_INSTANCE,
                )
                self.assertEqual(root.kind, ProcessLeaseKind.ROOT)
                self.assertEqual(self._child_attempt("application"), "already_held")
                self.assertEqual(self._child_attempt("root"), "already_held")

    def test_different_root_leases_can_coexist(self) -> None:
        other = self.base / "other-root"
        other.mkdir()

        with acquire_root_process_lease(self.paths, self.root):
            with acquire_root_process_lease(self.paths, other):
                self.assertEqual(
                    self._child_attempt("root", root=other),
                    "already_held",
                )

    def test_same_canonical_root_aliases_conflict(self) -> None:
        alias = self.base / "root-alias"
        alias.symlink_to(self.root, target_is_directory=True)

        with acquire_root_process_lease(self.paths, self.root):
            with self.assertRaises(ProcessLeaseError) as raised:
                acquire_root_process_lease(self.paths, alias)

        self.assertEqual(raised.exception.code, "already_held")

    def test_context_manager_and_thread_safe_idempotent_release(self) -> None:
        lease = acquire_application_instance_lease(self.paths)
        barrier = Barrier(6)
        results: list[bool] = []
        result_lock = Lock()

        def release() -> None:
            barrier.wait()
            result = lease.release()
            with result_lock:
                results.append(result)

        threads = [Thread(target=release) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 5)
        self.assertTrue(lease.released)
        self.assertFalse(lease.release())

        with acquire_application_instance_lease(self.paths) as replacement:
            self.assertFalse(replacement.released)
        self.assertTrue(replacement.released)

    def test_concurrent_threads_have_exactly_one_winner(self) -> None:
        barrier = Barrier(8)
        winner_acquired = Event()
        all_attempted = Event()
        release_winner = Event()
        results: list[str] = []
        result_lock = Lock()
        attempted = 0

        def contend() -> None:
            nonlocal attempted
            barrier.wait()
            try:
                lease = acquire_application_instance_lease(self.paths)
            except ProcessLeaseError as error:
                result = error.code
            else:
                result = "acquired"
                winner_acquired.set()
            with result_lock:
                attempted += 1
                if attempted == 8:
                    all_attempted.set()
            if result == "acquired":
                release_winner.wait(timeout=5)
                lease.release()
            with result_lock:
                results.append(result)

        threads = [Thread(target=contend) for _ in range(8)]
        for thread in threads:
            thread.start()
        self.assertTrue(winner_acquired.wait(timeout=5))
        self.assertTrue(all_attempted.wait(timeout=5))
        release_winner.set()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(results.count("acquired"), 1)
        self.assertEqual(results.count("already_held"), 7)

    def test_stale_malformed_or_wrong_pid_metadata_is_not_authority(self) -> None:
        self.paths.create_lock_directories()
        lock_path = self.paths.application_instance_lock
        lock_path.write_text("{not-json", encoding="utf-8")
        lock_path.chmod(0o600)

        with acquire_application_instance_lease(self.paths):
            metadata = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["lease_kind"], "application_instance")

        lock_path.write_text(
            json.dumps({"process_id": 999999, "lease_kind": "wrong"}),
            encoding="utf-8",
        )
        lock_path.chmod(0o600)
        with acquire_application_instance_lease(self.paths):
            metadata = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["process_id"], os.getpid())

    def test_failed_contender_does_not_rewrite_held_metadata(self) -> None:
        with acquire_root_process_lease(
            self.paths,
            self.root,
            now=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
            nonce_factory=lambda: "a" * 32,
        ):
            lock_path = self.paths.root_lock_path(
                canonical_root_lock_key(self.root)
            )
            before = lock_path.read_bytes()
            self.assertEqual(self._child_attempt("root"), "already_held")
            self.assertEqual(lock_path.read_bytes(), before)

    def test_metadata_is_bounded_restrictive_and_contains_no_root_or_secret(
        self,
    ) -> None:
        secret = "private-browser-token"
        with acquire_root_process_lease(
            self.paths,
            self.root,
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            nonce_factory=lambda: "b" * 32,
        ):
            lock_path = self.paths.root_lock_path(
                canonical_root_lock_key(self.root)
            )
            raw = lock_path.read_text(encoding="utf-8")
            metadata = json.loads(raw)

            self.assertEqual(
                set(metadata),
                {
                    "created_at",
                    "instance_nonce",
                    "lease_kind",
                    "process_id",
                    "root_key",
                },
            )
            self.assertEqual(metadata["lease_kind"], "root")
            self.assertLessEqual(len(raw.encode("utf-8")), 512)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertNotIn(str(self.root), raw)
            self.assertNotIn(secret, raw)
            for forbidden in (
                "browser",
                "session",
                "csrf",
                "operation_id",
                "file_list",
            ):
                self.assertNotIn(forbidden, raw.lower())

    def test_symlink_and_nonregular_lock_paths_are_rejected(self) -> None:
        self.paths.create_lock_directories()
        external = self.base / "external-lock-target"
        external.write_text("unchanged", encoding="utf-8")
        external.chmod(0o600)
        self.paths.application_instance_lock.symlink_to(external)

        with self.assertRaises(ProcessLeaseError) as symlink:
            acquire_application_instance_lease(self.paths)
        self.assertEqual(symlink.exception.code, "unsafe_lock_path")
        self.assertEqual(external.read_text(encoding="utf-8"), "unchanged")

        self.paths.application_instance_lock.unlink()
        self.paths.application_instance_lock.mkdir()
        with self.assertRaises(ProcessLeaseError) as nonregular:
            acquire_application_instance_lease(self.paths)
        self.assertEqual(nonregular.exception.code, "unsafe_lock_path")

    def test_hard_linked_lock_file_is_rejected_without_rewriting_target(self) -> None:
        self.paths.create_lock_directories()
        external = self.base / "external-lock-target"
        external.write_text("unchanged", encoding="utf-8")
        external.chmod(0o600)
        os.link(external, self.paths.application_instance_lock)

        with self.assertRaises(ProcessLeaseError) as raised:
            acquire_application_instance_lease(self.paths)

        self.assertEqual(raised.exception.code, "unsafe_lock_path")
        self.assertEqual(external.read_text(encoding="utf-8"), "unchanged")

    def test_safe_errors_expose_no_paths_pid_descriptor_or_os_detail(self) -> None:
        with acquire_root_process_lease(self.paths, self.root):
            with self.assertRaises(ProcessLeaseError) as raised:
                acquire_root_process_lease(self.paths, self.root)

        text = str(raised.exception)
        self.assertEqual(raised.exception.code, "already_held")
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(str(self.paths.locks), text)
        self.assertNotIn(str(os.getpid()), text)
        self.assertNotIn("Errno", text)

    def test_process_death_releases_os_ownership(self) -> None:
        result = self._child_attempt("root", exit_without_release=True)
        self.assertEqual(result, "acquired")

        with acquire_root_process_lease(self.paths, self.root):
            self.assertEqual(self._child_attempt("root"), "already_held")

    @unittest.skipUnless(hasattr(os, "fork"), "fork is unavailable")
    def test_forked_child_drops_inherited_descriptor_without_claiming(self) -> None:
        lease = acquire_application_instance_lease(self.paths)
        read_descriptor, write_descriptor = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_descriptor)
            try:
                inherited = "released" if lease.released else "held"
                try:
                    acquire_application_instance_lease(self.paths)
                except ProcessLeaseError as error:
                    result = f"{inherited}:{error.code}"
                else:
                    result = f"{inherited}:acquired"
                os.write(write_descriptor, result.encode("ascii"))
            finally:
                os._exit(0)

        os.close(write_descriptor)
        try:
            result = os.read(read_descriptor, 128).decode("ascii")
            _, status = os.waitpid(child, 0)
            self.assertEqual(status, 0)
            self.assertEqual(result, "released:already_held")
            self.assertFalse(lease.released)
        finally:
            os.close(read_descriptor)
            lease.release()

    def test_lease_object_is_not_serializable_authority(self) -> None:
        with acquire_application_instance_lease(self.paths) as lease:
            with self.assertRaisesRegex(TypeError, "cannot be serialized"):
                pickle.dumps(lease)
            self.assertNotIn(str(self.paths.locks), repr(lease))

    def test_unsupported_platform_fails_before_creating_storage(self) -> None:
        with mock.patch.object(process_lease_module, "_fcntl", None):
            with self.assertRaises(ProcessLeaseError) as raised:
                acquire_application_instance_lease(self.paths)

        self.assertEqual(raised.exception.code, "unsupported_platform")
        self.assertFalse(self.paths.application_support.exists())

    def test_invalid_injected_metadata_is_rejected_without_path_disclosure(
        self,
    ) -> None:
        with self.assertRaises(ProcessLeaseError) as raised:
            acquire_root_process_lease(
                self.paths,
                self.root,
                nonce_factory=lambda: str(self.root),
            )

        self.assertEqual(raised.exception.code, "unavailable")
        self.assertNotIn(str(self.root), raised.exception.safe_message)

    def test_launch_composition_owns_leases_but_app_and_routes_do_not(self) -> None:
        launch_sources = (
            REPOSITORY_ROOT / "src" / "organizer" / "macos_launcher" / "__main__.py",
            REPOSITORY_ROOT / "src" / "organizer" / "macos_launcher" / "coordinator.py",
            REPOSITORY_ROOT / "src" / "organizer" / "web" / "launcher.py",
            REPOSITORY_ROOT / "src" / "organizer" / "web" / "server.py",
        )
        launch_combined = "\n".join(
            path.read_text(encoding="utf-8") for path in launch_sources
        )
        protected_sources = [
            REPOSITORY_ROOT / "src" / "organizer" / "web" / "app.py",
            *(
                REPOSITORY_ROOT / "src" / "organizer" / "web" / "routes"
            ).glob("*.py"),
        ]
        protected_combined = "\n".join(
            path.read_text(encoding="utf-8") for path in protected_sources
        )

        self.assertIn("acquire_application_instance_lease", launch_combined)
        self.assertIn("acquire_root_process_lease", launch_combined)
        self.assertNotIn("process_lease", protected_combined)
        self.assertNotIn("application_paths", protected_combined)
        self.assertNotIn(
            "acquire_application_instance_lease",
            protected_combined,
        )
        self.assertNotIn("acquire_root_process_lease", protected_combined)

    def _child_attempt(
        self,
        kind: str,
        *,
        root: Path | None = None,
        exit_without_release: bool = False,
    ) -> str:
        script = "\n".join(
            (
                "import os",
                "from pathlib import Path",
                "from organizer.macos_launcher.application_paths import MacOSApplicationPaths",
                "from organizer.macos_launcher.process_lease import ProcessLeaseError, acquire_application_instance_lease, acquire_root_process_lease",
                "paths = MacOSApplicationPaths.for_home(Path(os.environ['LEASE_HOME']))",
                "try:",
                "    lease = (acquire_application_instance_lease(paths) if os.environ['LEASE_KIND'] == 'application' else acquire_root_process_lease(paths, Path(os.environ['LEASE_ROOT'])))",
                "except ProcessLeaseError as error:",
                "    print(error.code, flush=True)",
                "else:",
                "    print('acquired', flush=True)",
                (
                    "    os._exit(0)"
                    if exit_without_release
                    else "    lease.release()"
                ),
            )
        )
        environment = dict(os.environ)
        environment.update(
            {
                "LEASE_HOME": str(self.paths.home),
                "LEASE_KIND": kind,
                "LEASE_ROOT": str(root or self.root),
                "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
