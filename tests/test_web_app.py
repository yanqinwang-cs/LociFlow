import asyncio
from pathlib import Path
import tempfile
import unittest

import httpx

from organizer.web.app import create_app
from organizer.web.config import WebAppConfig
from organizer.web.security import CONTENT_SECURITY_POLICY, SECURITY_HEADERS
from organizer.application.runtime_lifecycle import RuntimeOperationKind


class WebAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.token = "t" * 32
        self.config = WebAppConfig(
            self.root,
            session_secret="s" * 32,
            launch_token=self.token,
            testing=True,
        )
        self.app = create_app(self.config)

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_launch_authenticates_once_and_redirects_to_loci_flow(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.get("/")
            wrong = await client.get("/launch/wrong", follow_redirects=False)
            launched = await client.get(
                f"/launch/{self.token}",
                follow_redirects=False,
            )
            home = await client.get(launched.headers["location"])
            replay = await client.get(
                f"/launch/{self.token}",
                follow_redirects=False,
            )

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(launched.status_code, 303)
        self.assertEqual(launched.headers["location"], "/loci-flow")
        self.assertEqual(home.status_code, 200)
        self.assertEqual(replay.status_code, 403)
        self.assertIn("Home · Loci Flow", home.text)
        self.assertNotIn(str(self.root), home.text)
        self.assertNotIn(self.token, home.text)

    async def test_app_factory_cannot_be_reached_with_a_symlink_root(self) -> None:
        real_root = self.root / "real-root"
        real_root.mkdir()
        symlink_root = self.root / "symlink-root"
        symlink_root.symlink_to(real_root, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            create_app(
                WebAppConfig(
                    symlink_root,
                    session_secret="s" * 32,
                    launch_token="u" * 32,
                    testing=True,
                )
            )

    async def test_concurrent_launch_requests_authenticate_only_one_client(self) -> None:
        async def launch_once() -> httpx.Response:
            async with _client(self.app) as client:
                return await client.get(
                    f"/launch/{self.token}",
                    follow_redirects=False,
                )

        responses = await asyncio.gather(*(launch_once() for _ in range(12)))
        self.assertEqual(
            [response.status_code for response in responses].count(303),
            1,
        )

    async def test_apps_have_independent_launch_gates_and_sessions(self) -> None:
        other_token = "u" * 32
        other_app = create_app(
            WebAppConfig(
                self.root,
                session_secret="v" * 32,
                launch_token=other_token,
                testing=True,
            )
        )
        async with _client(self.app) as first, _client(other_app) as second:
            self.assertEqual(
                (await second.get(f"/launch/{self.token}")).status_code,
                403,
            )
            self.assertEqual(
                (await first.get(f"/launch/{self.token}")).status_code,
                303,
            )
            self.assertEqual(
                (await second.get(f"/launch/{other_token}")).status_code,
                303,
            )

        self.assertIs(
            self.app.state.organization_execution.runtime_lifecycle,
            self.app.state.runtime_lifecycle,
        )
        self.assertIs(
            self.app.state.organization_decisions._runtime_lifecycle,
            self.app.state.runtime_lifecycle,
        )
        self.assertIs(
            self.app.state.review_explorer._runtime_lifecycle,
            self.app.state.runtime_lifecycle,
        )
        first_lease = self.app.state.runtime_lifecycle.begin_operation(
            RuntimeOperationKind.SCAN
        )
        self.assertEqual(self.app.state.runtime_lifecycle.snapshot().active_count, 1)
        self.assertEqual(other_app.state.runtime_lifecycle.snapshot().active_count, 0)
        first_lease.finish()

    async def test_altered_signed_cookie_does_not_authenticate(self) -> None:
        async with _client(self.app) as client:
            launched = await client.get(
                f"/launch/{self.token}",
                follow_redirects=False,
            )
            cookie_value = client.cookies["bootai_session"]
            signed_value, separator, signature = cookie_value.rpartition(".")
            self.assertEqual(separator, ".")
            client.cookies.set(
                "bootai_session",
                signed_value
                + separator
                + ("A" if signature[0] != "A" else "B")
                + signature[1:],
            )
            response = await client.get("/")

        self.assertEqual(launched.status_code, 303)
        self.assertEqual(response.status_code, 403)

    async def test_session_cookie_has_locked_attributes_and_minimal_data(self) -> None:
        async with _client(self.app) as client:
            response = await client.get(
                f"/launch/{self.token}",
                follow_redirects=False,
            )

        cookie = response.headers["set-cookie"]
        lower = cookie.lower()
        attributes = {part.strip() for part in lower.split(";")[1:]}
        self.assertIn("bootai_session=", lower)
        self.assertIn("httponly", lower)
        self.assertIn("samesite=strict", lower)
        self.assertIn("path=/", lower)
        self.assertNotIn("domain=", lower)
        self.assertNotIn("max-age", lower)
        self.assertNotIn("expires=", lower)
        self.assertNotIn("secure", attributes)
        self.assertNotIn(str(self.root), cookie)
        self.assertNotIn(self.token, cookie)

    async def test_hosts_are_strict_and_testserver_is_testing_only(self) -> None:
        production = create_app(
            WebAppConfig(
                self.root,
                session_secret="p" * 32,
                launch_token="q" * 32,
            )
        )
        async with _client(production, base_url="http://testserver") as client:
            rejected = await client.get("/healthz")
        async with _client(production, base_url="http://localhost") as client:
            localhost = await client.get("/healthz")
        async with _client(self.app, base_url="http://testserver") as client:
            testing = await client.get("/healthz")

        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn(str(self.root), rejected.text)
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(rejected.headers[name], value)
        self.assertEqual(localhost.status_code, 200)
        self.assertEqual(testing.status_code, 200)

    async def test_security_headers_cover_pages_redirects_errors_and_health(self) -> None:
        async with _client(self.app) as client:
            responses = [
                await client.get("/healthz"),
                await client.get("/missing"),
                await client.get(f"/launch/{self.token}", follow_redirects=False),
                await client.get("/"),
            ]

        for response in responses:
            with self.subTest(status=response.status_code):
                for name, value in SECURITY_HEADERS.items():
                    self.assertEqual(response.headers[name], value)
                self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertEqual(
            responses[0].headers["content-security-policy"],
            CONTENT_SECURITY_POLICY,
        )
        self.assertNotIn("unsafe-eval", CONTENT_SECURITY_POLICY)
        self.assertNotIn("http:", CONTENT_SECURITY_POLICY)
        self.assertNotIn("https:", CONTENT_SECURITY_POLICY)
        self.assertNotIn("*", CONTENT_SECURITY_POLICY)

    async def test_route_methods_and_documentation_are_locked(self) -> None:
        methods_by_path = {
            route.path: set(route.methods or set())
            for route in _application_routes(self.app.routes)
            if hasattr(route, "path") and hasattr(route, "methods")
        }
        self.assertEqual(methods_by_path["/healthz"], {"GET"})
        self.assertEqual(methods_by_path["/launch/{token}"], {"GET"})
        self.assertEqual(methods_by_path["/"], {"GET"})
        self.assertEqual(methods_by_path["/loci-flow"], {"GET"})
        self.assertEqual(methods_by_path["/scan"], {"POST"})
        self.assertEqual(methods_by_path["/scan/status"], {"GET"})
        self.assertEqual(
            methods_by_path["/api/organization/current"],
            {"GET"},
        )
        self.assertEqual(
            methods_by_path["/api/organization/decisions"],
            {"POST"},
        )
        self.assertEqual(
            methods_by_path["/api/organization/approval"],
            {"POST"},
        )
        self.assertEqual(
            methods_by_path["/api/organization/move/preview"],
            {"POST"},
        )
        self.assertEqual(methods_by_path["/api/organization/move"], {"POST"})
        self.assertEqual(methods_by_path["/api/activity"], {"GET"})
        self.assertEqual(
            methods_by_path["/api/activity/{operation_id}/undo/preview"],
            {"POST"},
        )
        self.assertEqual(
            methods_by_path["/api/activity/{operation_id}/undo"],
            {"POST"},
        )
        for path in (
            "/duplicates",
            "/organize",
            "/attention",
            "/scans",
            "/settings",
            "/review",
            "/review/advanced",
            "/review/items/{item_id}",
            "/review/conflicts",
        ):
            self.assertEqual(methods_by_path[path], {"GET"})
        self.assertEqual(
            methods_by_path["/review/items/{item_id}/decision"],
            {"POST"},
        )
        self.assertEqual(
            methods_by_path["/review/page-decision/preview"],
            {"POST"},
        )
        self.assertEqual(
            methods_by_path["/review/page-decision/confirm"],
            {"POST"},
        )
        self.assertEqual(methods_by_path["/review/save"], {"POST"})
        for path in (
            "/duplicates/save",
            "/organize/save",
            "/attention/save",
            "/duplicates/revisit-skipped",
            "/organize/revisit-skipped",
            "/attention/revisit-skipped",
        ):
            self.assertEqual(methods_by_path[path], {"POST"})

        async with _client(self.app) as client:
            for path in ("/docs", "/redoc", "/openapi.json"):
                self.assertEqual((await client.get(path)).status_code, 404)
            self.assertEqual((await client.post("/healthz")).status_code, 405)

    async def test_authenticated_root_redirects_without_legacy_home(self) -> None:
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            response = await client.get("/", follow_redirects=False)
            shell = await client.get(response.headers["location"])

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/loci-flow")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(shell.status_code, 200)
        self.assertIn("<title>Home · Loci Flow</title>", shell.text)
        self.assertIn("No files can move in this stage.", shell.text)
        self.assertNotIn("Sample files only", shell.text)
        self.assertNotIn("<title>Home · bootAI</title>", shell.text)
        self.assertNotIn(str(self.root), shell.text)

    async def test_static_assets_and_generic_missing_page_are_local(self) -> None:
        async with _client(self.app) as client:
            static = await client.get("/static/vendor/htmx.min.js")
            missing = await client.get("/not-here")

        self.assertEqual(static.status_code, 200)
        self.assertIn("javascript", static.headers["content-type"])
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(static.headers[name], value)
        self.assertEqual(missing.status_code, 404)
        self.assertIn("This bootAI page is not available.", missing.text)
        self.assertNotIn(str(self.root), missing.text)

    async def test_health_reveals_no_root_or_secrets(self) -> None:
        async with _client(self.app) as client:
            response = await client.get("/healthz")

        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn(self.token, response.text)
        self.assertNotIn(self.config.session_secret, response.text)

    async def test_production_error_is_generic_and_has_security_headers(self) -> None:
        async def fail() -> None:
            raise RuntimeError("sensitive internal detail")

        self.app.add_api_route("/test-error", fail, methods=["GET"])
        async with _client(self.app, raise_app_exceptions=False) as client:
            response = await client.get("/test-error")

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("sensitive internal detail", response.text)
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn(str(self.root), response.text)
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(response.headers[name], value)


def _client(
    app: object,
    *,
    base_url: str = "http://127.0.0.1",
    raise_app_exceptions: bool = True,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=app,  # type: ignore[arg-type]
            raise_app_exceptions=raise_app_exceptions,
        ),
        base_url=base_url,
    )


def _application_routes(routes: list[object]) -> list[object]:
    flattened: list[object] = []
    for route in routes:
        included_router = getattr(route, "original_router", None)
        if included_router is not None:
            flattened.extend(_application_routes(included_router.routes))
        else:
            flattened.append(route)
    return flattened


if __name__ == "__main__":
    unittest.main()
