import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import main
from app.auth_store import AuthenticatedUser, MASKED_PASSWORD
from starlette.requests import Request

# 一份「已登记站点」的形状。阶段 C 起站点规格来自数据库，这里直接喂一份
# 内存里的等价物 —— 本文件只关心**登录工具的行为**，不关心规格怎么存。
# 真正的「从库里读出来并命中」由 tests/test_sites_api.py 覆盖。
_LOGIN_SITE = {
    "site_key": "nsu-smart-education",
    "name": "东软智慧教育平台",
    "aliases": ["东软智慧教育平台", "智慧教育平台"],
    "host": "study.nsu.edu.cn",
    "login_url": "https://study.nsu.edu.cn/login",
    "username_selector": 'input[placeholder="请输入用户名"]',
    "password_selector": 'input[placeholder="请输入密码"]',
    "blocked_path_parts": ["logout", "submit", "提交"],
    "requires_captcha": True,
    "enabled": True,
}


class BrowserLoginToolTests(unittest.TestCase):
    def test_browser_mode_field_was_removed(self):
        self.assertNotIn("browser_mode", main.AskRequest.model_fields)

    def test_login_intent_gate_is_explicit_and_deterministic(self):
        """「要不要登录」这一步是确定性判断，不经过模型，也不查库。

        阶段 C 把「命中的是哪个站点」交给了站点注册表（需要数据库），
        但「这句话算不算登录请求」这条安全边界仍然是纯字符串判断，
        因此把匹配层 patch 掉之后可以独立验证。
        """
        with patch.object(main, "_match_login_target", return_value=_LOGIN_SITE) as match:
            self.assertEqual(
                main._requested_login_target(
                    "帮我登录东软智慧教育平台",
                    AuthenticatedUser(user_id="u", username="u"),
                ),
                _LOGIN_SITE,
            )
            self.assertEqual(
                main._requested_login_target(
                    "帮我登录智慧教育平台",
                    AuthenticatedUser(user_id="u", username="u"),
                ),
                _LOGIN_SITE,
            )

        # 没有「登录」二字 → 连匹配层都不该被调用（省一次数据库查询，
        # 也保证「介绍一下 XX 平台」这类提问不会意外触发浏览器）
        with patch.object(main, "_match_login_target") as match:
            self.assertIsNone(
                main._requested_login_target(
                    "介绍一下东软智慧教育平台",
                    AuthenticatedUser(user_id="u", username="u"),
                )
            )
            match.assert_not_called()

        # 显式否定：即使带了「登录」也不能触发
        with patch.object(main, "_match_login_target") as match:
            self.assertIsNone(
                main._requested_login_target(
                    "不要登录智慧教育平台",
                    AuthenticatedUser(user_id="u", username="u"),
                )
            )
            match.assert_not_called()

        # 有登录意图但没命中任何已登记站点 → 交给浏览器智能体，不当成登录
        with patch.object(main, "_match_login_target", return_value=None):
            self.assertIsNone(
                main._requested_login_target(
                    "帮我登录其它网站",
                    AuthenticatedUser(user_id="u", username="u"),
                )
            )

    def test_conversation_ids_are_scoped_to_assistant_mode(self):
        school_id = main._new_session_id("school")
        student_id = main._new_session_id("student")
        self.assertTrue(school_id.startswith("school_"))
        self.assertTrue(student_id.startswith("student_"))
        main._validate_session_mode(school_id, "school")
        main._validate_session_mode("legacy-session-id", "school")
        main._validate_session_mode(student_id, "student")
        with self.assertRaises(main.HTTPException):
            main._validate_session_mode(student_id, "school")
        with self.assertRaises(main.HTTPException):
            main._validate_session_mode(school_id, "student")

    def test_school_chat_uses_rag_without_invoking_student_tools(self):
        user = AuthenticatedUser(user_id="synthetic-user", username="synthetic")
        memory = SimpleNamespace(session_exists_for_user=lambda *_: False)
        def answer_from_rag(question, session_id, owner_user_id):
            return {"answer": "RAG answer", "session_id": session_id}

        with (
            patch.object(main, "get_memory", return_value=memory),
            patch.object(main.rag, "answer", side_effect=answer_from_rag) as rag_answer,
            patch.object(main, "_execute_login_tool") as login_tool,
            patch.object(main, "_answer_browser_task") as browser_task,
        ):
            response = main.chat(
                main.AskRequest(question="帮我登录东软智慧教育平台"),
                user,
            )

        self.assertEqual(response["answer"], "RAG answer")
        self.assertTrue(response["session_id"].startswith("school_"))
        rag_answer.assert_called_once()
        login_tool.assert_not_called()
        browser_task.assert_not_called()

    def test_student_chat_uses_login_tool_without_rag(self):
        user = AuthenticatedUser(user_id="synthetic-user", username="synthetic")
        appended = []
        memory = SimpleNamespace(
            session_exists_for_user=lambda *_: False,
            append_for_user=lambda *args: appended.append(args),
        )
        with (
            patch.object(main, "get_memory", return_value=memory),
            patch.object(main, "_match_login_target", return_value=_LOGIN_SITE),
            patch.object(main, "_execute_login_tool", return_value=("login started", True)) as login_tool,
            patch.object(main.rag, "answer") as rag_answer,
        ):
            response = main.chat(
                main.AskRequest(
                    question="帮我登录东软智慧教育平台",
                    assistant_mode="student",
                ),
                user,
            )

        self.assertEqual(response["answer"], "login started")
        self.assertTrue(response["session_id"].startswith("student_"))
        self.assertTrue(response["browser_session_active"])
        login_tool.assert_called_once()
        rag_answer.assert_not_called()
        self.assertEqual(len(appended), 1)

    def test_student_chat_routes_non_login_request_to_browser_agent(self):
        """个人助手里，凡不是登录请求的一律交给浏览器智能体。

        旧实现用关键词表猜意图：「查看大学生就业指导」「去作答第三节课」都
        不在词表内，请求被一句兜底话术吞掉，用户看到的就是「点了没反应」。
        这条测试是那次回归的保护。
        """
        user = AuthenticatedUser(user_id="synthetic-user", username="synthetic")
        appended = []
        memory = SimpleNamespace(
            session_exists_for_user=lambda *_: False,
            append_for_user=lambda *args: appended.append(args),
        )
        with (
            patch.object(main, "get_memory", return_value=memory),
            patch.object(
                main,
                "_answer_browser_task",
                return_value={
                    "answer": "browser answered",
                    "pending_click": None,
                    "approval": None,
                },
            ) as browser_task,
            patch.object(main.rag, "answer") as rag_answer,
        ):
            response = main.chat(
                main.AskRequest(
                    question="查看大学生就业指导",
                    assistant_mode="student",
                ),
                user,
            )

        self.assertEqual(response["answer"], "browser answered")
        self.assertEqual(response["sources"], [])
        self.assertIsNone(response["click_approval"])
        browser_task.assert_called_once()
        rag_answer.assert_not_called()
        self.assertEqual(len(appended), 1)

    def test_student_stream_emits_structured_click_approval(self):
        """待批准的点击要以结构化事件下发，前端才能渲染「批准并执行」卡片。

        阶段 C ② 起这个事件带的是 approval_id 与过期时刻（而不是一段确认文本）：
        「批准」必须对应 PG 里一条能验证、能审计、且只能消费一次的记录。
        """
        user = AuthenticatedUser(user_id="synthetic-user", username="synthetic")
        request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""})
        memory = SimpleNamespace(append_for_user=lambda *_: None)
        approval = {
            "approval_id": "a" * 32,
            "target": "div「开始学习」",
            "status": "pending",
            "expires_at": "2026-10-15T07:00:00+00:00",
        }

        async def collect(response):
            return [chunk async for chunk in response.body_iterator]

        with (
            patch.object(main, "get_memory", return_value=memory),
            patch.object(
                main,
                "_answer_browser_task",
                return_value={
                    "answer": "这个控件可能会改变网站状态，我还没有点它。",
                    "pending_click": {"target": approval["target"]},
                    "approval": approval,
                },
            ) as browser_task,
            patch.object(main, "rag") as rag,
        ):
            response = main.chat_stream(
                main.AskRequest(question="点击开始学习", assistant_mode="student"),
                request,
                user,
            )
            events = asyncio.run(collect(response))

        joined = "".join(events)
        self.assertIn('"type": "click_approval"', joined)
        self.assertIn(approval["approval_id"], joined)
        self.assertIn(approval["target"], joined)
        self.assertIn('"session_id": "student_', joined)
        browser_task.assert_called_once()
        rag.answer_stream.assert_not_called()

    def test_school_stream_uses_rag_without_invoking_student_tools(self):
        user = AuthenticatedUser(user_id="synthetic-user", username="synthetic")
        request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""})

        def answer_stream(question, session_id, owner_user_id):
            yield {"type": "text", "text": "RAG answer"}

        async def collect(response):
            return [chunk async for chunk in response.body_iterator]

        with (
            patch.object(main, "rag") as rag,
            patch.object(rag, "answer_stream", side_effect=answer_stream) as rag_stream,
            patch.object(main, "_execute_login_tool") as login_tool,
            patch.object(main, "_answer_browser_task") as browser_task,
        ):
            response = main.chat_stream(
                main.AskRequest(question="帮我登录东软智慧教育平台"),
                request,
                user,
            )
            events = asyncio.run(collect(response))

        joined = "".join(events)
        self.assertIn('"text": "RAG answer"', joined)
        self.assertIn('"session_id": "school_', joined)
        rag_stream.assert_called_once()
        login_tool.assert_not_called()
        browser_task.assert_not_called()

    def test_student_stream_routes_login_tool_without_rag(self):
        user = AuthenticatedUser(user_id="synthetic-user", username="synthetic")
        request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""})
        memory = SimpleNamespace(append_for_user=lambda *_: None)

        async def collect(response):
            return [chunk async for chunk in response.body_iterator]

        with (
            patch.object(main, "get_memory", return_value=memory),
            patch.object(main, "_match_login_target", return_value=_LOGIN_SITE),
            patch.object(main, "_execute_login_tool", return_value=("login started", True)) as login_tool,
            patch.object(main, "rag") as rag,
        ):
            response = main.chat_stream(
                main.AskRequest(
                    question="帮我登录东软智慧教育平台",
                    assistant_mode="student",
                ),
                request,
                user,
            )
            events = asyncio.run(collect(response))

        joined = "".join(events)
        self.assertIn('"session_id": "student_', joined)
        self.assertIn('"active": true', joined)
        self.assertIn('"text": "login started"', joined)
        login_tool.assert_called_once()
        rag.answer_stream.assert_not_called()

    def test_dispatch_sends_only_account_password_and_returns_no_secret(self):
        credential = {
            "credential_id": "synthetic-id",
            "service_key": "nsu-smart-education",
            "label": "synthetic",
            "site_url": "https://study.nsu.edu.cn/login",
            "full_name": "Synthetic Person",
            "student_id": "TEST123",
            "account": "synthetic-account",
            "password": "synthetic-password",
            # 浏览器登录是全项目唯一需要明文口令的路径，所以它必须显式 reveal。
            "password_revealed": True,
        }
        reveal_flags = []

        def fake_get_credential(user_id, credential_id, **kwargs):
            reveal_flags.append(kwargs.get("reveal_password"))
            return credential

        store = SimpleNamespace(
            list_credentials=lambda user_id: [
                {"credential_id": "synthetic-id", "service_key": "nsu-smart-education"}
            ],
            get_credential=fake_get_credential,
        )

        captured = {}

        async def call_tool(browser_url, tool_name, arguments):
            captured["url"] = browser_url
            captured["tool_name"] = tool_name
            captured["arguments"] = arguments
            return "verification_pending"

        with (
            patch.object(main, "_get_auth_store", return_value=store),
            patch.object(main, "BROWSER_MCP_URL", "http://browser:8766/mcp"),
            patch.object(main, "_call_browser_mcp", new=AsyncMock(side_effect=call_tool)),
        ):
            answer = main._execute_login_tool(
                AuthenticatedUser(user_id="synthetic-user", username="synthetic"),
                _LOGIN_SITE,
            )

        self.assertEqual(captured["url"], "http://browser:8766/mcp")
        self.assertEqual(captured["tool_name"], "start_login_session")
        self.assertEqual(
            captured["arguments"],
            {
                "owner_user_id": "synthetic-user",
                # 阶段 C：站点规格整份下发，browser 侧不再持有任何站点常量。
                "login_url": "https://study.nsu.edu.cn/login",
                "host": "study.nsu.edu.cn",
                "username_selector": 'input[placeholder="请输入用户名"]',
                "password_selector": 'input[placeholder="请输入密码"]',
                "blocked_path_parts": ["logout", "submit", "提交"],
                # site_key 只作标签用（审计里要说得出「在哪个站点上」），
                # 但它必须真的传下去 —— 漏传不会坏功能，只会让所有点击
                # 在审计里归到空站点，是那种「跑起来没问题、事后才发现」的错。
                "site_key": "nsu-smart-education",
                "username": "synthetic-account",
                "password": "synthetic-password",
            },
        )
        self.assertNotIn("full_name", captured["arguments"])
        self.assertNotIn("student_id", captured["arguments"])
        self.assertTrue(answer[1])
        self.assertIn("自动填入", answer[0])
        self.assertNotIn("synthetic-password", answer[0])
        # 钉住不变量：这条路径必须显式索取明文，而不是依赖存储层的默认值。
        # 存储层默认脱敏之后，这里的 [True] 就是「自动登录仍然可用」的唯一保证。
        self.assertEqual(reveal_flags, [True])

    def test_masked_credential_is_refused_instead_of_submitted(self):
        """漏传 reveal_password 时必须安全失败，而不是把掩码当成口令提交。

        守护的不变量：脱敏占位符永远不能被送进登录页。否则表现会是
        「用 ******** 反复尝试登录」—— 既不可能成功，又可能触发平台风控。
        """
        credential = {
            "credential_id": "synthetic-id",
            "service_key": "nsu-smart-education",
            "label": "synthetic",
            "site_url": "https://study.nsu.edu.cn/login",
            "full_name": "Synthetic Person",
            "student_id": "TEST123",
            "account": "synthetic-account",
            "password": MASKED_PASSWORD,
            "password_revealed": False,
        }
        store = SimpleNamespace(
            list_credentials=lambda user_id: [
                {"credential_id": "synthetic-id", "service_key": "nsu-smart-education"}
            ],
            get_credential=lambda user_id, credential_id, **kwargs: credential,
        )

        with (
            patch.object(main, "_get_auth_store", return_value=store),
            patch.object(main, "BROWSER_MCP_URL", "http://browser:8766/mcp"),
            patch.object(main, "_call_browser_mcp", new=AsyncMock()) as mcp,
        ):
            answer = main._execute_login_tool(
                AuthenticatedUser(user_id="synthetic-user", username="synthetic"),
                _LOGIN_SITE,
            )

        mcp.assert_not_called()
        self.assertFalse(answer[1])
        self.assertNotIn(MASKED_PASSWORD, answer[0])

    def test_disabled_site_is_refused_before_reading_credentials(self):
        """停用站点的语义是「助手不要碰它」—— 连凭证都不该读。

        钉住的是顺序：如果先读凭证再判 enabled，一次「不该发生」的登录
        仍会在凭证审计里留下一条 reveal 记录（口令被取出解密过），
        而且平台侧可能已经收到请求。
        """
        with patch.object(main, "_get_auth_store") as auth_store:
            answer = main._execute_login_tool(
                AuthenticatedUser(user_id="synthetic-user", username="synthetic"),
                {**_LOGIN_SITE, "enabled": False},
            )

        auth_store.assert_not_called()
        self.assertFalse(answer[1])
        self.assertIn("停用", answer[0])

    def test_credential_site_mismatch_is_refused_not_corrected(self):
        """凭证自己记的地址与站点规格不一致时拒绝，而不是自动取其一。

        守护的不变量：站点 host 就是浏览器侧的白名单。若凭证里的地址与规格
        不符还照样放行，白名单就退化成「随便哪个地址都能开」。
        """
        credential = {
            "credential_id": "synthetic-id",
            "service_key": "nsu-smart-education",
            "label": "synthetic",
            "site_url": "https://somewhere-else.example.cn/login",
            "account": "synthetic-account",
            "password": "synthetic-password",
            "password_revealed": True,
        }
        store = SimpleNamespace(
            list_credentials=lambda user_id: [
                {"credential_id": "synthetic-id", "service_key": "nsu-smart-education"}
            ],
            get_credential=lambda user_id, credential_id, **kwargs: credential,
        )

        with (
            patch.object(main, "_get_auth_store", return_value=store),
            patch.object(main, "_call_browser_mcp", new=AsyncMock()) as mcp,
        ):
            answer = main._execute_login_tool(
                AuthenticatedUser(user_id="synthetic-user", username="synthetic"),
                _LOGIN_SITE,
            )

        mcp.assert_not_called()
        self.assertFalse(answer[1])
        self.assertIn("不匹配", answer[0])


if __name__ == "__main__":
    unittest.main()
