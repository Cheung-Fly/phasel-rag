import asyncio
import time
import unittest
from unittest.mock import patch

import mcp_server


class AgentBrowserCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_is_used_as_named_session_and_output_is_returned(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "ok\n"})()
        with patch("mcp_server.subprocess.run", return_value=completed) as run:
            output = await mcp_server.AgentBrowser("owner/one").run("open", "https://study.nsu.edu.cn/login")

        self.assertEqual(output, "ok")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["agent-browser", "--session", "campus-owner-one"])

    async def test_command_failure_does_not_expose_arguments(self):
        completed = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "password=secret"})()
        with patch("mcp_server.subprocess.run", return_value=completed):
            with self.assertRaises(mcp_server.AgentBrowserError) as error:
                await mcp_server.AgentBrowser("owner").run("fill", "#password", "secret")
        self.assertNotIn("secret", str(error.exception))

    async def test_mouse_button_actions_move_to_coordinates_first(self):
        calls = []

        class FakeBrowser:
            async def run(self, *args, **kwargs):
                calls.append(args)
                return ""

        mcp_server._sessions["owner"] = {
            "browser": FakeBrowser(),
            "operation_lock": asyncio.Lock(),
            "expires_at": time.monotonic() + 60,
        }
        try:
            self.assertEqual(
                await mcp_server.interact_login_session("owner", "down", 321, 123),
                "ok",
            )
            self.assertEqual(
                calls,
                [
                    ("mouse", "move", "321", "123"),
                    ("mouse", "down"),
                ],
            )
        finally:
            mcp_server._sessions.pop("owner", None)

    async def test_mouse_coordinates_are_rounded_for_cli(self):
        calls = []

        class FakeBrowser:
            async def run(self, *args, **kwargs):
                calls.append(args)
                return ""

        mcp_server._sessions["owner"] = {
            "browser": FakeBrowser(),
            "operation_lock": asyncio.Lock(),
            "expires_at": time.monotonic() + 60,
        }
        try:
            self.assertEqual(
                await mcp_server.interact_login_session("owner", "move", 321.4, 123.6),
                "ok",
            )
            self.assertEqual(calls, [("mouse", "move", "321", "124")])
        finally:
            mcp_server._sessions.pop("owner", None)

    async def test_login_typing_uses_real_keyboard_events(self):
        calls = []

        class FakeBrowser:
            async def run(self, *args, **kwargs):
                calls.append(args)
                return ""

        await mcp_server._type_into_login_field(
            FakeBrowser(), 'input[placeholder="请输入用户名"]', "synthetic-user"
        )
        self.assertEqual(
            calls,
            [
                ("click", 'input[placeholder="请输入用户名"]'),
                ("press", "Control+a"),
                ("keyboard", "type", "synthetic-user"),
            ],
        )

    async def test_interactive_elements_include_dynamic_dom_controls(self):
        class FakeBrowser:
            async def json(self, *args):
                if args[:2] == ("snapshot", "-i"):
                    return {"data": {"refs": {"e1": {"role": "generic", "name": "课程卡片"}}}}
                return {"data": [{
                    "selector": "div.course-card > div:nth-of-type(2)",
                    "tag": "div",
                    "role": "",
                    "text": "开始学习",
                }]}

        session = {"browser": FakeBrowser()}
        elements = await mcp_server._interactive_elements(session)
        self.assertTrue(any(item["text"] == "开始学习" for item in elements))
        self.assertEqual(
            next(item for item in elements if item["text"] == "开始学习")["ref"],
            "div.course-card > div:nth-of-type(2)",
        )


if __name__ == "__main__":
    unittest.main()
