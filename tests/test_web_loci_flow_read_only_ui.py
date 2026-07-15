from pathlib import Path
import tempfile
import unittest

import httpx

from organizer.web.app import create_app
from organizer.web.config import WebAppConfig


class WebLociFlowReadOnlyUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.token = "l" * 32
        self.app = create_app(
            WebAppConfig(
                self.root,
                session_secret="s" * 32,
                launch_token=self.token,
                testing=True,
            )
        )

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_shell_requires_authentication_and_is_no_store(self) -> None:
        async with _client(self.app) as client:
            unauthenticated = await client.get("/loci-flow")
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            response = await client.get("/loci-flow")

        self.assertEqual(unauthenticated.status_code, 403)
        self.assertEqual(unauthenticated.headers["cache-control"], "no-store")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn(self.token, response.text)
        self.assertIn("Prototype", response.text)
        self.assertIn("Sample files only. Your files are not changed.", response.text)
        self.assertNotIn("Backend", response.text)

    async def test_shell_rejects_all_query_parameters_and_non_get(self) -> None:
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            unknown = await client.get("/loci-flow?unknown=1")
            root = await client.get("/loci-flow?root=/tmp")
            style = await client.get("/loci-flow?style=cautious")
            post = await client.post("/loci-flow")

        for response in (unknown, root, style):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(str(self.root), response.text)
        self.assertEqual(post.status_code, 405)

    async def test_shell_and_all_integrated_assets_are_local(self) -> None:
        async with _client(self.app) as client:
            await client.get(f"/launch/{self.token}", follow_redirects=False)
            shell = await client.get("/loci-flow")
            paths = (
                "/static/loci_flow/styles.css",
                "/static/loci_flow/app.js",
                "/static/loci_flow/data_source.js",
                "/static/loci_flow/view_models.js",
                "/static/loci_flow/fixtures.json",
                "/static/loci_flow/store.js",
                "/static/loci_flow/tree.js",
                "/static/loci_flow/views.js",
                "/static/loci_flow/assets/loci-flow-mark.svg",
                "/static/loci_flow/assets/file-icons.svg",
            )
            responses = [await client.get(path) for path in paths]

        self.assertNotIn("https://", shell.text)
        self.assertNotIn("http://", shell.text)
        self.assertIn('/static/loci_flow/styles.css', shell.text)
        self.assertIn('/static/loci_flow/app.js', shell.text)
        self.assertTrue(all(response.status_code == 200 for response in responses))


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://127.0.0.1",
        follow_redirects=False,
    )


if __name__ == "__main__":
    unittest.main()
