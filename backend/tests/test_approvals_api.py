"""操作审批闸门的接口契约与状态机（阶段 C ② 的 W3）。

三组：
  ApprovalGateAcceptanceTests  ★ 验收口径：未批准时**零外部副作用**
  ApprovalStateMachineTests    重放 / 过期 / 放行+标记 / 结果未知
  TypedConfirmationRemovalTests 「手打确认文本」这条路已封死

【为什么核心断言是「零调用」而不是「返回码」】
路线图对阶段 C 的验收口径是「一次需审批的操作在无批准时不产生任何外部副作用」。
对这个项目来说，「外部副作用」有一个具体的所指：`confirm_pending_login_session_click`
被调用 —— 它是全项目唯一会真点网页的工具。所以这里不对返回码下断言，
而是把浏览器工具 mock 掉并断言**调用次数为 0**：返回码可能由别的原因造成，
调用次数不会。

【为什么「放行+标记」也要测】
只测「拒绝」会漏掉一半：browser 回 `no_pending_click` 时请求本身是被正确处理
的（HTTP 200），但**什么都没点成**。把它当成功写进 `executed`，
审计时间线上就会出现一次「点过了」而实际没有 —— 这比报错更危险。
"""
import json
import os
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import auth_store as auth_module
from app import browser_agent
from app import main
from app.approval_store import ApprovalStore, close_approval_store
from app.auth_store import AuthenticatedUser

_CLICKED = json.dumps({"status": "clicked", "page": {"title": "课程页"}}, ensure_ascii=False)

_CLICK_TOOL = "confirm_pending_login_session_click"


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class ApprovalGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # app_users 必须先于 operation_approvals 存在（外键依赖）。
        cls.auth = auth_module.AuthStore()
        cls.store = ApprovalStore()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.auth.close()
        # 请求路径用的是模块单例（main._get_approval_store），顺手回收它的连接池。
        close_approval_store()

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.owner = self.auth.ensure_local_owner(f"stagec-appr-{self.suffix}").user_id
        self.other = self.auth.ensure_local_owner(f"stagec-appr2-{self.suffix}").user_id
        self.user = AuthenticatedUser(
            user_id=self.owner, username=f"stagec-appr-{self.suffix}"
        )
        main.app.dependency_overrides[main._current_user] = lambda: self.user
        self.client = TestClient(main.app)
        # 熔断器是 main 模块级单例，会被别的用例训练成「已断开」。
        # 不重置的话，本文件会在别人留下的状态上跑，出现难以复现的 502。
        self._reset_breaker()

    def tearDown(self):
        main.app.dependency_overrides.pop(main._current_user, None)
        self._reset_breaker()
        with self.store._pool.connection() as connection:
            connection.execute(
                "DELETE FROM operation_audit WHERE owner_user_id = ANY(%s)",
                ([self.owner, self.other],),
            )
            connection.execute(
                "DELETE FROM operation_approvals WHERE owner_user_id = ANY(%s)",
                ([self.owner, self.other],),
            )

    # ------------------------------------------------------------ 夹具
    @staticmethod
    def _reset_breaker():
        main.browser_breaker.opened_at = None
        main.browser_breaker.failures = 0

    def _create(self, **overrides) -> dict:
        payload = {
            "target": "div「开始学习」",
            "site_key": "demo-site",
            "session_id": f"student_{self.suffix}",
            "fingerprint": "fp-1",
        }
        payload.update(overrides)
        return self.store.create(self.owner, **payload)

    def _status(self, approval_id: str) -> str:
        return self.store.get(self.owner, approval_id)["status"]

    def _force_expired(self, approval_id: str) -> None:
        with self.store._pool.connection() as connection:
            connection.execute(
                """
                UPDATE operation_approvals
                SET expires_at = now() - interval '1 second'
                WHERE approval_id = %s
                """,
                (approval_id,),
            )

    def _approve(self, approval_id: str):
        return self.client.post(f"/api/approvals/{approval_id}/approve")

    def _execute(self, approval_id: str):
        return self.client.post(f"/api/approvals/{approval_id}/execute")

    # ============================================================ ★ 验收口径
    def test_execute_without_approval_is_refused_and_clicks_nothing(self):
        """★ W3 的验收口径：未批准时零外部副作用。

        pending 状态直接点执行必须被拒，且浏览器工具**一次都不能被调用**。
        这是整个审批闸门存在的理由 —— 任何一次「顺手放行 pending」的改动
        都会让这条失效，但只有这条测试会发现。
        """
        approval = self._create()

        with patch.object(main, "_call_browser_mcp", new=AsyncMock()) as browser:
            response = self._execute(approval["approval_id"])

        self.assertEqual(response.status_code, 409)
        browser.assert_not_called()
        self.assertEqual(self._status(approval["approval_id"]), "pending")

    def test_rejected_approval_also_clicks_nothing(self):
        """拒绝之后再执行同样必须零调用 —— 否则「拒绝」只是个装饰。"""
        approval = self._create()
        self.assertEqual(
            self.client.post(
                f"/api/approvals/{approval['approval_id']}/reject"
            ).status_code,
            200,
        )

        with patch.object(main, "_call_browser_mcp", new=AsyncMock()) as browser:
            response = self._execute(approval["approval_id"])

        self.assertEqual(response.status_code, 409)
        browser.assert_not_called()

    def test_typed_confirmation_never_reaches_the_click_tool(self):
        """★ 手打「确认点击：X」这条路必须封死。

        改造前它就是真点击的入口 —— 也就是说审批闸门可以被一句话绕过，
        等于不存在。现在这个输入只得到一句指路话术，浏览器工具零调用。
        """
        calls = []

        async def call_browser_tool(owner_user_id, name, arguments):
            calls.append((name, arguments))
            return json.dumps({"status": "session_not_verified"})

        result = browser_agent.answer_browser_task(
            "确认点击：div「开始学习」", "synthetic-user", call_browser_tool
        )

        self.assertEqual(calls, [], "手打确认文本不得触达浏览器工具")
        self.assertIsNone(result["pending_click"])
        self.assertIn("批准", result["answer"])

    # ============================================================ 正向路径
    def test_approve_then_execute_clicks_once_with_the_approved_target(self):
        approval = self._create()
        self.assertEqual(self._approve(approval["approval_id"]).status_code, 200)

        with patch.object(
            main, "_call_browser_mcp", new=AsyncMock(return_value=_CLICKED)
        ) as browser:
            response = self._execute(approval["approval_id"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "executed")
        browser.assert_called_once()
        # 传下去的必须是**审批记录里那个** target，不是调用方现编的字符串。
        # 否则「批准的」和「执行的」可以是两个不同的控件。
        _, tool_name, arguments = browser.call_args.args
        self.assertEqual(tool_name, _CLICK_TOOL)
        self.assertEqual(arguments["owner_user_id"], self.owner)
        self.assertEqual(arguments["target"], "div「开始学习」")
        self.assertEqual(self._status(approval["approval_id"]), "executed")

    def test_clicked_from_browser_is_the_only_path_to_executed(self):
        """api 侧的状态自认不算数：只有 browser 明确回 clicked 才落 executed。

        守护的不变量：把「我们以为点了」写成「点了」是这里最危险的错误，
        因为它会让审计时间线撒谎。
        """
        approval = self._create()
        self._approve(approval["approval_id"])

        with patch.object(
            main,
            "_call_browser_mcp",
            new=AsyncMock(return_value=json.dumps({"status": "target_changed"})),
        ):
            response = self._execute(approval["approval_id"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(self._status(approval["approval_id"]), "failed")

    # ============================================================ 放行 + 标记
    def test_browser_side_timeout_marks_expired_not_executed(self):
        """browser 说「没有待点击项了」⇒ 200，但落 expired。

        这是「放行+标记」那一档：请求被正确处理（不是故障，不该报 5xx），
        但确实什么都没点成（不能记 executed）。两件事必须同时成立。
        """
        approval = self._create()
        self._approve(approval["approval_id"])

        with patch.object(
            main,
            "_call_browser_mcp",
            new=AsyncMock(return_value=json.dumps({"status": "pending_click_expired"})),
        ):
            response = self._execute(approval["approval_id"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "expired")
        self.assertEqual(response.json()["reason"], "pending_click_expired")
        self.assertEqual(self._status(approval["approval_id"]), "expired")

    # ============================================================ 反向路径
    def test_execute_after_expiry_returns_410(self):
        """过期与「已被处理」要分开报：对用户是两种不同的下一步动作。"""
        approval = self._create()
        self._force_expired(approval["approval_id"])
        # 先读一次列表，让惰性过期把状态落库（这正是界面上的时序）
        self.client.get("/api/approvals")

        with patch.object(main, "_call_browser_mcp", new=AsyncMock()) as browser:
            response = self._execute(approval["approval_id"])

        self.assertEqual(response.status_code, 410)
        browser.assert_not_called()
        self.assertEqual(self._status(approval["approval_id"]), "expired")

    def test_approve_after_expiry_returns_409_and_stays_expired(self):
        """过期后批准必须失败，且终态只能是 expired（不能是 approved）。

        approved 意味着「我确认过眼前这个东西」。过期就意味着眼前的东西
        已经变了 —— 控件索引可能被新内容顶掉，点下去就是点错地方。
        """
        approval = self._create()
        self._force_expired(approval["approval_id"])
        self.client.get("/api/approvals")

        response = self._approve(approval["approval_id"])

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self._status(approval["approval_id"]), "expired")

    def test_approve_twice_returns_409_on_the_second_attempt(self):
        approval = self._create()

        self.assertEqual(self._approve(approval["approval_id"]).status_code, 200)
        second = self._approve(approval["approval_id"])

        self.assertEqual(second.status_code, 409)
        self.assertIn("已被处理", second.json()["detail"])

    def test_execute_twice_returns_409_on_the_second_attempt(self):
        """一次性消费：重放不得点第二次。

        靠的是 `UPDATE ... WHERE status='approved'` 的影响行数，
        而不是「先查后改」—— 后者在双击时两边都能查到 approved。
        """
        approval = self._create()
        self._approve(approval["approval_id"])

        with patch.object(
            main, "_call_browser_mcp", new=AsyncMock(return_value=_CLICKED)
        ) as browser:
            self.assertEqual(self._execute(approval["approval_id"]).status_code, 200)
            replay = self._execute(approval["approval_id"])

        self.assertEqual(replay.status_code, 409)
        self.assertEqual(browser.call_count, 1, "重放不得再点击一次")

    def test_unknown_approval_returns_404(self):
        self.assertEqual(self._approve(uuid.uuid4().hex).status_code, 404)
        self.assertEqual(self._execute(uuid.uuid4().hex).status_code, 404)

    def test_browser_circuit_open_keeps_the_approval_retryable(self):
        """浏览器整体不可用 ⇒ 502，但审批保持 approved（可以重试）。

        这是**唯一**允许「保持 approved」的失败路径，判据是「请求压根没发出去」
        （熔断器已断开）。与下面的传输层异常必须分开处理，理由见那条测试。
        """
        approval = self._create()
        self._approve(approval["approval_id"])
        main.browser_breaker.opened_at = time.monotonic()

        with patch.object(main, "_call_browser_mcp", new=AsyncMock()) as browser:
            response = self._execute(approval["approval_id"])

        self.assertEqual(response.status_code, 502)
        browser.assert_not_called()
        self.assertEqual(self._status(approval["approval_id"]), "approved")

    def test_transport_failure_is_terminal_and_not_retryable(self):
        """传输层异常无法区分「没送达」与「送达了但没等到回复」。

        后者可能已经点过了 —— 所以允许重试就是允许点两次。
        这里刻意落 `failed` 而不是退回 `approved`：宁可要一次人工介入，
        也不要静默做两次。与阶段 B「普通异常即终态、不自动重试」同源。
        """
        approval = self._create()
        self._approve(approval["approval_id"])

        with patch.object(
            main, "_call_browser_mcp", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            response = self._execute(approval["approval_id"])

        self.assertEqual(response.status_code, 502)
        self.assertEqual(self._status(approval["approval_id"]), "failed")
        # 而且确实不可重试
        self.assertEqual(self._execute(approval["approval_id"]).status_code, 409)

    def test_stuck_execution_is_reported_as_unknown_not_replayed(self):
        """卡在 executing 超过 5 分钟 ⇒ unknown（结果未知），**不自动重放**。

        重放等于重复点击，而网页点击的副作用不可逆。unknown 是一个诚实的
        表达：我们确实不知道那次点击有没有生效，用户应当自行去网站上核对。
        """
        approval = self._create()
        self._approve(approval["approval_id"])
        self.assertIsNotNone(self.store.begin_execution(self.owner, approval["approval_id"]))
        with self.store._pool.connection() as connection:
            connection.execute(
                """
                UPDATE operation_approvals
                SET updated_at = now() - interval '10 minutes'
                WHERE approval_id = %s
                """,
                (approval["approval_id"],),
            )

        listing = self.client.get("/api/approvals")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(self._status(approval["approval_id"]), "unknown")
        # 而且 unknown 同样是终态：不得因为它「不确定」就允许再执行一次
        self.assertEqual(self._execute(approval["approval_id"]).status_code, 409)

    # ============================================================ 隔离
    def test_other_owner_cannot_see_or_touch_the_approval(self):
        approval = self._create()
        main.app.dependency_overrides[main._current_user] = lambda: AuthenticatedUser(
            user_id=self.other, username="other"
        )
        try:
            self.assertEqual(self.client.get("/api/approvals").json()["approvals"], [])
            self.assertEqual(self._approve(approval["approval_id"]).status_code, 404)
            self.assertEqual(self._execute(approval["approval_id"]).status_code, 404)
        finally:
            main.app.dependency_overrides[main._current_user] = lambda: self.user

        self.assertEqual(self._status(approval["approval_id"]), "pending")

    def test_listing_is_scoped_to_the_owner(self):
        mine = self._create()
        self.store.create(self.other, target="div「别人的控件」")

        listing = self.client.get("/api/approvals").json()["approvals"]

        self.assertEqual([item["approval_id"] for item in listing], [mine["approval_id"]])

    # ============================================================ 登记（chat 侧）
    def test_pending_click_from_browser_is_registered_as_an_approval(self):
        """浏览器只回「选中了哪个控件」，登记审批是 api 的责任。

        没有这一步，那条 pending_click 就只是浏览器内存里一个 90 秒后消失的
        状态：批准不了，也审计不了。
        """
        pending = {
            "target": "button「提交作业」",
            "fingerprint": "fp-9",
            "site_key": "demo-site",
        }
        with patch.object(
            main,
            "answer_browser_task",
            return_value={"answer": "已找到该控件。", "pending_click": pending},
        ):
            task = main._answer_browser_task("提交作业", self.user, f"student_{self.suffix}")

        self.assertIsNotNone(task["approval"])
        self.assertEqual(task["approval"]["status"], "pending")
        self.assertEqual(task["approval"]["target"], "button「提交作业」")
        self.assertEqual(task["approval"]["site_key"], "demo-site")
        self.assertEqual(task["approval"]["session_id"], f"student_{self.suffix}")
        # 真落库了，不只是返回了一个对象
        self.assertEqual(self._status(task["approval"]["approval_id"]), "pending")

    def test_no_pending_click_means_no_approval(self):
        with patch.object(
            main,
            "answer_browser_task",
            return_value={"answer": "这是课表。", "pending_click": None},
        ):
            task = main._answer_browser_task("查课表", self.user, f"student_{self.suffix}")

        self.assertIsNone(task["approval"])
        self.assertEqual(self.client.get("/api/approvals").json()["approvals"], [])

    def test_gate_unavailable_does_not_degrade_into_clicking(self):
        """闸门挂了 ⇒ 不下发批准按钮，但**绝不**退化成直接点击。

        「安全机制坏了」和「安全机制没了」必须区分开：宁可让用户暂时用不了
        这个功能，也不能让它在无人批准的情况下自己点下去。
        """
        pending = {"target": "button「提交」", "fingerprint": "fp", "site_key": "demo-site"}
        with (
            patch.object(main, "get_approval_store_or_none", return_value=None),
            patch.object(
                main,
                "answer_browser_task",
                return_value={"answer": "已找到该控件。", "pending_click": pending},
            ),
        ):
            task = main._answer_browser_task("提交", self.user, f"student_{self.suffix}")

        self.assertIsNone(task["approval"])
        self.assertIn("审批闸门", task["answer"])

    # ============================================================ 审计
    def test_each_transition_leaves_exactly_one_audit_event(self):
        """审计是「批准过什么」的唯一答案，事件条数与动作必须一一对应。"""
        approval = self._create()
        self._approve(approval["approval_id"])
        with patch.object(
            main, "_call_browser_mcp", new=AsyncMock(return_value=_CLICKED)
        ):
            self._execute(approval["approval_id"])

        with self.store._pool.connection() as connection:
            actions = [
                row["action"]
                for row in connection.execute(
                    """
                    SELECT action FROM operation_audit
                    WHERE owner_user_id = %s
                    ORDER BY audit_id
                    """,
                    (self.owner,),
                ).fetchall()
            ]

        self.assertEqual(
            actions,
            ["approval_requested", "approval_granted", "approval_executed"],
        )


if __name__ == "__main__":
    unittest.main()
