import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import extensions


class ExtensionLoadingTests(unittest.TestCase):
    def test_only_builtin_searxng_endpoint_can_use_plain_http(self):
        self.assertTrue(extensions.is_allowed_mcp_url("https://search.example/mcp"))
        self.assertTrue(extensions.is_allowed_mcp_url(extensions.INTERNAL_SEARXNG_MCP_URL))
        self.assertFalse(extensions.is_allowed_mcp_url("http://search.example/mcp"))
        self.assertFalse(extensions.is_allowed_mcp_url("http://mcp-searxng:3000/other"))

    def test_load_skills_reads_only_skill_files(self):
        with tempfile.TemporaryDirectory() as root:
            skill_dir = Path(root) / "demo"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# Demo\n只读检查", encoding="utf-8")
            (skill_dir / "notes.txt").write_text("ignored", encoding="utf-8")
            with patch.object(extensions, "SKILLS_DIR", Path(root)):
                skills = extensions.load_skills()
        self.assertEqual([item["name"] for item in skills], ["demo"])
        self.assertIn("只读检查", skills[0]["content"])

    def test_snapshot_does_not_expose_mcp_credentials(self):
        config = {
            "private": {
                "enabled": True,
                "transport": "streamable_http",
                "url": "https://example.test/mcp?token=secret",
            }
        }
        with patch.dict(os.environ, {"MCP_SERVERS_JSON": json.dumps(config)}, clear=False):
            snapshot = extensions.extension_snapshot()
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertIn("private", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("example.test", serialized)

    def test_mutating_tool_is_not_read_only(self):
        self.assertFalse(extensions._is_read_only("delete_file", "Delete a file"))
        self.assertTrue(extensions._is_read_only("list_files", "List files"))

    def test_stdio_env_only_passes_allowlist_and_explicit_env(self):
        with patch.dict(os.environ, {"PATH": "/bin", "SECRET_KEY": "x"}, clear=True):
            env = extensions._stdio_env({"env": {"TOKEN": "t"}})
        self.assertEqual(env, {"PATH": "/bin", "TOKEN": "t"})

    def test_mcp_tool_error_is_not_returned_as_successful_content(self):
        import asyncio

        class FailedToolSession:
            async def list_tools(self):
                return SimpleNamespace(
                    tools=[SimpleNamespace(name="web_url_read", description="Read a webpage")]
                )

            async def call_tool(self, name, arguments):
                return SimpleNamespace(
                    content=[SimpleNamespace(text="URL blocked")],
                    isError=True,
                )

        @asynccontextmanager
        async def session(_config):
            yield FailedToolSession()

        manager = extensions.ExtensionManager()
        config = {"Search": {"enabled": True, "transport": "streamable_http", "url": "https://search.test/mcp"}}
        with (
            patch("app.extensions._server_config", return_value=config),
            patch.object(manager, "_session", session),
            self.assertRaisesRegex(RuntimeError, "MCP 工具调用失败"),
        ):
            asyncio.run(manager._call("mcp__Search__web_url_read", {"url": "https://example.test"}))

    def test_open_breaker_blocks_call(self):
        import asyncio

        breaker = extensions._breaker("u", "srv")
        for _ in range(3):
            breaker.failure()
        with self.assertRaises(RuntimeError):
            asyncio.run(extensions.ExtensionManager().call("mcp__srv__tool", {}, "u"))


class MutatingToolApprovalTests(unittest.TestCase):
    def test_mutating_call_returns_approval_without_executing(self):
        from types import SimpleNamespace
        from app import extension_agent

        tool = {
            "read_only": False,
            "schema": {"function": {"name": "mcp__srv__delete"}},
        }
        response = SimpleNamespace(
            content="",
            tool_calls=[{"id": "1", "name": "mcp__srv__delete", "args": {"id": 1}}],
        )
        model = SimpleNamespace(bind_tools=lambda _s: SimpleNamespace(invoke=lambda _m: response))
        store = SimpleNamespace(create=lambda *a, **k: {"approval_id": "a1", "action": k["action"]})
        manager = SimpleNamespace(list_tools=lambda _o: _async_value([tool]), call=None)
        with patch.object(extension_agent, "ExtensionManager", return_value=manager), \
             patch.object(extension_agent, "get_chat_model", return_value=model), \
             patch.object(extension_agent, "get_approval_store_or_none", return_value=store), \
             patch.object(extension_agent, "load_skills", return_value=[]):
            result = extension_agent.answer_extension_task("删除 1", "u")
        self.assertEqual(result["approval"]["action"], "mcp_tool")
        self.assertEqual(result["tools"], [])


async def _async_value(value):
    return value


class ExtensionRoutingTests(unittest.TestCase):
    def test_named_skill_or_server_routes_to_extension(self):
        from app import main

        snapshot = {
            "skills": [{"name": "system-check"}],
            "mcp_servers": [
                {"name": "github", "enabled": True},
                {"name": "offline", "enabled": False},
            ],
        }
        with patch.object(main, "extension_snapshot", return_value=snapshot):
            self.assertTrue(main._requests_extension("用 system-check 看看磁盘", "u"))
            self.assertTrue(main._requests_extension("帮我查 GitHub 的 issue", "u"))
            self.assertFalse(main._requests_extension("offline 是什么", "u"))
            self.assertFalse(main._requests_extension("在终端执行 npx skills add a/b@system-check -y", "u"))
            self.assertTrue(main._requests_extension("/skill system-check 看看磁盘", "u"))
            self.assertFalse(main._requests_extension("今天食堂几点开门", "u"))
            self.assertFalse(main._requests_extension("github", None))
        only_skill = {"skills": [{"name": "find-skills"}], "mcp_servers": []}
        with patch.object(main, "extension_snapshot", return_value=only_skill):
            self.assertFalse(main._requests_extension("用 find-skills 找 agent-browser", "u"))
        with patch.object(main, "load_skills", return_value=[{"name": "find-skills", "content": "# x"}]):
            self.assertIn("find-skills", main._named_skills("用 find-skills 找 agent-browser"))
            self.assertEqual(main._named_skills("今天天气"), "")


class RealStdioServerTests(unittest.TestCase):
    SERVER = (
        "from mcp.server.fastmcp import FastMCP\n"
        "m = FastMCP('demo')\n"
        "@m.tool()\n"
        "def delete_item(item_id: int) -> str:\n"
        "    'Delete an item'\n"
        "    return f'deleted {item_id}'\n"
        "m.run()\n"
    )

    def test_mutating_tool_runs_only_when_approved(self):
        import asyncio
        import sys

        with tempfile.TemporaryDirectory() as root:
            script = Path(root) / "srv.py"
            script.write_text(self.SERVER, encoding="utf-8")
            config = {"demo": {
                "enabled": True, "transport": "stdio",
                "command": sys.executable, "args": [str(script)],
            }}
            with patch.dict(os.environ, {"MCP_SERVERS_JSON": json.dumps(config)}):
                manager = extensions.ExtensionManager()
                with self.assertRaisesRegex(RuntimeError, "需要审批"):
                    asyncio.run(manager.call("mcp__demo__delete_item", {"item_id": 1}, None))
                result = asyncio.run(
                    manager.call("mcp__demo__delete_item", {"item_id": 1}, None, approved=True)
                )
        self.assertEqual(result, "deleted 1")


class ServerConfigMergeTests(unittest.TestCase):
    def test_env_servers_are_kept_and_user_config_overrides_same_name(self):
        from types import SimpleNamespace

        env = {"a": {"enabled": True, "command": "env-a"}, "b": {"enabled": True, "command": "env-b"}}
        store = SimpleNamespace(configs=lambda _o: {"b": {"enabled": True, "command": "user-b"}})
        with patch.dict(os.environ, {"MCP_SERVERS_JSON": json.dumps(env)}), \
             patch("app.mcp_store.get_mcp_store_or_none", return_value=store):
            merged = extensions._server_config("u")
        self.assertEqual(merged["a"]["command"], "env-a")
        self.assertEqual(merged["b"]["command"], "user-b")

    def test_bad_env_json_does_not_hide_user_servers(self):
        from types import SimpleNamespace

        store = SimpleNamespace(configs=lambda _o: {"mine": {"enabled": True}})
        with patch.dict(os.environ, {"MCP_SERVERS_JSON": "{oops"}), \
             patch("app.mcp_store.get_mcp_store_or_none", return_value=store):
            self.assertEqual(list(extensions._server_config("u")), ["mine"])


if __name__ == "__main__":
    unittest.main()


class SkillWriteTests(unittest.TestCase):
    def test_save_and_delete_skill(self):
        with tempfile.TemporaryDirectory() as root, patch.object(extensions, "SKILLS_DIR", Path(root)):
            extensions.save_skill("my-skill", "# 说明")
            self.assertEqual([s["name"] for s in extensions.load_skills()], ["my-skill"])
            self.assertTrue(extensions.delete_skill("my-skill"))
            self.assertEqual(extensions.load_skills(), [])
            self.assertFalse(extensions.delete_skill("my-skill"))

    def test_rejects_path_traversal_and_empty(self):
        with tempfile.TemporaryDirectory() as root, patch.object(extensions, "SKILLS_DIR", Path(root)):
            for bad in ("../x", "a/b", "", ".hidden"):
                with self.assertRaises(ValueError):
                    extensions.save_skill(bad, "x")
            with self.assertRaises(ValueError):
                extensions.save_skill("ok", "  ")
            self.assertFalse(extensions.delete_skill("../etc"))


class SkillSyncTests(unittest.TestCase):
    def test_syncs_real_files_and_skips_symlinks_and_oversize(self):
        with tempfile.TemporaryDirectory() as skills, tempfile.TemporaryDirectory() as work:
            base = Path(work) / ".agents" / "skills"
            (base / "good").mkdir(parents=True)
            (base / "good" / "SKILL.md").write_text("# good", encoding="utf-8")
            (base / "big").mkdir()
            (base / "big" / "SKILL.md").write_text("x" * 13000, encoding="utf-8")
            (base / "linked").symlink_to(Path(work))
            with patch.object(extensions, "SKILLS_DIR", Path(skills)):
                result = extensions.sync_installed_skills(Path(work))
                self.assertEqual(result["synced"], ["good"])
                self.assertEqual(result["skipped"], ["big"])
                self.assertEqual([s["name"] for s in extensions.load_skills()], ["good"])

    def test_no_install_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as work:
            self.assertEqual(extensions.sync_installed_skills(Path(work)), {"synced": [], "skipped": []})
