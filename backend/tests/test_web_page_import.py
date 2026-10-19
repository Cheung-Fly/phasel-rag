import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Response
from pydantic import ValidationError

from app import main
from app.auth_store import AuthenticatedUser


class WebPageImportTests(unittest.IsolatedAsyncioTestCase):
    def test_url_requires_https_without_embedded_credentials(self):
        request = main.WebPageImportRequest(url="https://example.com/article#section")
        self.assertEqual(request.url, "https://example.com/article")
        for url in ("http://example.com", "https://user:pass@example.com/article"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                main.WebPageImportRequest(url=url)

    async def test_import_uses_read_only_searxng_tool_and_queues_markdown(self):
        tool = {
            "server": "SearXNG Web Search",
            "name": "web_url_read",
            "read_only": True,
            "schema": {"function": {"name": "mcp__SearXNG Web Search__web_url_read"}},
        }
        manager = type("Manager", (), {
            "list_tools": AsyncMock(return_value=[tool]),
            "call": AsyncMock(return_value="# Article\n\nReadable article text."),
        })()
        queued = {"mode": "queued", "filename": "web-example.com-12345678.md"}
        user = AuthenticatedUser(user_id="owner", username="owner")
        request = main.WebPageImportRequest(
            url="https://example.com/article?token=secret#section"
        )

        with (
            patch.object(main, "ExtensionManager", return_value=manager),
            patch.object(main, "_accept_document_bytes", new=AsyncMock(return_value=queued)) as accept,
        ):
            result = await main.import_web_page(request, Response(), user)

        self.assertEqual(result, queued)
        manager.call.assert_awaited_once_with(
            "mcp__SearXNG Web Search__web_url_read",
            {"url": "https://example.com/article?token=secret", "maxLength": 10_000},
            "owner",
        )
        data, filename, *_ = accept.await_args.args
        self.assertTrue(filename.startswith("web-example.com-"))
        self.assertIn(b"Source: https://example.com/article", data)
        self.assertNotIn(b"token=secret", data)

    async def test_import_rejects_missing_or_ambiguous_readers(self):
        user = AuthenticatedUser(user_id="owner", username="owner")
        request = main.WebPageImportRequest(url="https://example.com")
        reader = {
            "server": "SearXNG Web Search",
            "name": "web_url_read",
            "read_only": True,
        }
        for tools, status in (([], 503), ([reader, reader], 409)):
            manager = type("Manager", (), {"list_tools": AsyncMock(return_value=tools)})()
            with (
                patch.object(main, "ExtensionManager", return_value=manager),
                self.assertRaises(HTTPException) as caught,
            ):
                await main.import_web_page(request, Response(), user)
            self.assertEqual(caught.exception.status_code, status)


if __name__ == "__main__":
    unittest.main()
