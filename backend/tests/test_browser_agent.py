import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from app.browser_agent import MAX_TOOL_OUTPUT_CHARS, _bounded_json, answer_browser_task


class BrowserAgentTests(unittest.TestCase):
    def test_requires_explicit_user_confirmation_before_reading_page(self):
        calls = []

        async def call_browser(owner_user_id, name, arguments):
            calls.append((owner_user_id, name, arguments))
            return json.dumps({"status": "session_not_verified"})

        with patch("app.browser_agent.get_chat_model") as get_model:
            result = answer_browser_task(
                "帮我看看当前登录平台的课表",
                "synthetic-user",
                call_browser,
            )

        self.assertIn("点击", result["answer"])
        self.assertIsNone(result["pending_click"])
        self.assertEqual(get_model.call_count, 0)
        self.assertEqual(calls, [("synthetic-user", "inspect_login_session", {})])

    def test_agent_reads_page_and_uses_only_same_site_link_tool(self):
        calls = []
        response_messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "open_page_link",
                        "args": {"link_index": 2},
                        "id": "open-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="我已打开课表页面。"),
        ]

        class FakeModel:
            def bind_tools(self, tools):
                self.tools = tools
                return self

            def invoke(self, messages):
                return response_messages.pop(0)

        async def call_browser(owner_user_id, name, arguments):
            calls.append((owner_user_id, name, arguments))
            if name == "inspect_login_session":
                return json.dumps({
                    "status": "login_confirmed",
                    "text": "校园首页",
                    "links": [{"index": 2, "text": "课表", "path": "/course"}],
                })
            return json.dumps({
                "status": "login_confirmed",
                "text": "本周课程安排",
                "links": [],
            })

        with patch("app.browser_agent.get_chat_model", return_value=FakeModel()):
            result = answer_browser_task(
                "帮我看看当前登录平台的课表",
                "synthetic-user",
                call_browser,
            )

        self.assertEqual(result["answer"], "我已打开课表页面。")
        self.assertIsNone(result["pending_click"])
        self.assertEqual(
            calls,
            [
                ("synthetic-user", "inspect_login_session", {}),
                (
                    "synthetic-user",
                    "open_login_session_link",
                    {"link_index": 2},
                ),
            ],
        )

    def test_agent_can_go_back_without_confirmation(self):
        calls = []
        response_messages = [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "go_back",
                    "args": {},
                    "id": "back-1",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="已返回上一页。"),
        ]

        class FakeModel:
            def bind_tools(self, tools):
                self.tools = tools
                return self

            def invoke(self, messages):
                return response_messages.pop(0)

        async def call_browser(owner_user_id, name, arguments):
            calls.append((owner_user_id, name, arguments))
            return json.dumps({
                "status": "login_confirmed",
                "title": "上一页",
                "text": "课程列表",
                "links": [],
                "interactive_elements": [],
            })

        with patch("app.browser_agent.get_chat_model", return_value=FakeModel()):
            result = answer_browser_task("浏览器后退", "synthetic-user", call_browser)

        self.assertEqual(result["answer"], "已返回上一页。")
        self.assertIsNone(result["pending_click"])
        self.assertIn(("synthetic-user", "go_back_login_session", {}), calls)

    def test_control_click_stops_and_requests_approval_instead_of_clicking(self):
        """选控件 ≠ 点控件。

        阶段 C ② 起浏览器智能体只回一份 `pending_click`（等 api 落库成审批），
        自己**绝不点击**。真点击只有一个入口：
        `POST /api/approvals/{id}/execute`。
        """
        calls = []

        class FakeModel:
            def bind_tools(self, tools):
                self.tools = tools
                return self

            def invoke(self, messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "click_page_element",
                            "args": {"element_index": 3},
                            "id": "click-1",
                            "type": "tool_call",
                        }
                    ],
                )

        async def call_browser(owner_user_id, name, arguments):
            calls.append((owner_user_id, name, arguments))
            if name == "inspect_login_session":
                return json.dumps({
                    "status": "login_confirmed",
                    "text": "课程列表",
                    "interactive_elements": [{"index": 3, "text": "进入课程"}],
                })
            return json.dumps({
                "status": "confirmation_required",
                "target": "button「进入课程」(第 4 项)",
                "fingerprint": "fp-4",
                "site_key": "demo-site",
                "confirmation_text": "确认点击：button「进入课程」(第 4 项)",
            })

        with patch("app.browser_agent.get_chat_model", return_value=FakeModel()):
            result = answer_browser_task(
                "进入大学生就业指导",
                "synthetic-user",
                call_browser,
            )

        self.assertIn("还没有点", result["answer"])
        self.assertIn("进入课程", result["answer"])
        self.assertEqual(
            result["pending_click"],
            {
                "target": "button「进入课程」(第 4 项)",
                "fingerprint": "fp-4",
                "site_key": "demo-site",
            },
        )
        self.assertEqual(
            calls,
            [
                ("synthetic-user", "inspect_login_session", {}),
                (
                    "synthetic-user",
                    "click_login_session_element",
                    {"element_index": 3},
                ),
            ],
        )

    def test_typed_confirmation_no_longer_clicks_anything(self):
        """★ 手打「确认点击：X」不得触达点击工具。

        改造前它就是真点击的入口 —— 也就是说审批闸门可以被一句话绕过，
        等于不存在。现在这条路只得到一句指路话术，浏览器工具零调用。
        """
        calls = []

        async def call_browser(owner_user_id, name, arguments):
            calls.append((owner_user_id, name, arguments))
            return json.dumps({"status": "clicked", "page": {"title": "课程详情"}})

        with patch("app.browser_agent.get_chat_model") as get_model:
            result = answer_browser_task(
                "确认点击：button「进入课程」(第 4 项)",
                "synthetic-user",
                call_browser,
            )

        self.assertEqual(calls, [], "手打确认文本不得触达浏览器工具")
        self.assertIsNone(result["pending_click"])
        self.assertIn("批准", result["answer"])
        get_model.assert_not_called()


class BoundedJsonTests(unittest.TestCase):
    """工具结果序列化的上限策略。

    控件清单是模型调用点击工具的唯一依据，任何情况下都不能被截掉；
    正文和链接都是可牺牲的次要信息。
    """

    def test_element_list_survives_even_when_text_and_links_are_huge(self):
        payload = {
            "status": "login_confirmed",
            "text": "正文很长" * 3000,
            "links": [
                {"index": i, "text": "链接文字" * 200, "path": "/p"}
                for i in range(40)
            ],
            "interactive_elements": [
                {"index": i, "tag": "div", "text": f"控件{i}"} for i in range(60)
            ],
        }

        serialized = _bounded_json(payload)
        self.assertLessEqual(len(serialized), MAX_TOOL_OUTPUT_CHARS)
        parsed = json.loads(serialized)
        # 控件清单必须完整
        self.assertEqual(len(parsed["interactive_elements"]), 60)
        self.assertEqual(parsed["interactive_elements"][-1]["text"], "控件59")
        # 链接是次要信息，超限时被牺牲
        self.assertNotIn("links", parsed)

    def test_plain_payload_is_still_bounded(self):
        payload = {"status": "ok", "text": "x" * (MAX_TOOL_OUTPUT_CHARS + 1000)}

        self.assertLessEqual(len(_bounded_json(payload)), MAX_TOOL_OUTPUT_CHARS)


if __name__ == "__main__":
    unittest.main()
