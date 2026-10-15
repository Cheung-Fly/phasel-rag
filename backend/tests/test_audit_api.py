"""审计时间线的接口契约（阶段 C ④ / W4）。

【这一组真正要证明的事】
阶段 C 的验收口径是「时间线能读出 W2/W3 产生的全部事件」。
所以这里的核心不是「接口返回 200」，而是：
  1. W3 写下的操作事件，能从 W4 的接口**读回来**（用 ApprovalStore 造真实事件，
     再走 HTTP 读 —— 一条断言就覆盖了「写」与「读」之间的那道缝）；
  2. 翻页走的是游标：跨页不重复、不漏；
  3. 只能看见自己的事件（时间线会暴露「取过明文口令」这类事实，
     越权读到它的信息量不比凭证本身小）；
  4. 游标填错是 422 而不是 500。

【为什么不断言返回码就完事】
「返回 200」在这类接口上没有信息量 —— 空列表也是 200。
断言落在**事件集合本身**上，才是对「能核对」这句话的检验。
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from app import auth_store as auth_module
from app import main
from app.approval_store import (
    STATUS_EXECUTED,
    ApprovalStore,
    close_approval_store,
)
from app.audit_timeline import close_audit_timeline
from app.auth_store import AuthenticatedUser

_PATH = "/api/audit/timeline"


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class AuditApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # app_users 必须先于 operation_audit / credential_audit 存在（外键依赖）。
        cls.auth = auth_module.AuthStore()
        cls.store = ApprovalStore()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.auth.close()
        # 请求路径用的是模块单例，顺手回收它们的连接池。
        close_approval_store()
        close_audit_timeline()

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.owner = self.auth.ensure_local_owner(f"stagec-audit-{self.suffix}").user_id
        self.other = self.auth.ensure_local_owner(f"stagec-audit2-{self.suffix}").user_id
        self.user = AuthenticatedUser(
            user_id=self.owner, username=f"stagec-audit-{self.suffix}"
        )
        main.app.dependency_overrides[main._current_user] = lambda: self.user
        self.client = TestClient(main.app)

    def tearDown(self):
        main.app.dependency_overrides.pop(main._current_user, None)
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
    def _requested(self, target):
        self.store.record_event(
            self.owner, kind="approval", action="approval_requested",
            detail={"target": target},
        )

    def _actions(self, cursor=""):
        query = f"{_PATH}?limit=50"
        if cursor:
            query += f"&cursor={cursor}"
        response = self.client.get(query)
        self.assertEqual(response.status_code, 200, response.text)
        return [event["action"] for event in response.json()["events"]]

    # ------------------------------------------------------------ 验收
    def test_timeline_reads_back_the_w3_approval_lifecycle(self):
        """★ 验收：一次走完整生命周期的审批，产生的每一条事件都能在时间线上读到。"""
        approval = self.store.create(
            self.owner,
            target="button「开始学习」",
            site_key="demo-site",
            session_id="session-1",
        )
        self.store.approve(self.owner, approval["approval_id"])
        self.store.begin_execution(self.owner, approval["approval_id"])
        self.store.complete_execution(
            self.owner,
            approval["approval_id"],
            outcome=STATUS_EXECUTED,
            detail={"page_title": "课程页"},
        )

        actions = self._actions()

        # 三条各出现一次
        self.assertEqual(
            sorted(a for a in actions if a.startswith("approval_")),
            ["approval_executed", "approval_granted", "approval_requested"],
        )
        # 而且是倒序：最新的一条在最前。同微秒时由 (source, seq) 兜底，
        # 所以这个顺序是确定的，不是「碰巧」。
        self.assertLess(
            actions.index("approval_executed"), actions.index("approval_requested")
        )

    def test_timeline_is_scoped_to_the_owner(self):
        self.store.record_event(
            self.other, kind="approval", action="approval_requested",
            detail={"target": "别人的操作"},
        )

        response = self.client.get(_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["events"], [])

    # ------------------------------------------------------------ 翻页
    def test_cursor_pagination_walks_pages_without_overlap(self):
        for index in range(5):
            self._requested(f"事件{index}")

        collected = []
        flags = []
        cursor = ""
        for _ in range(3):
            query = f"{_PATH}?limit=2" + (f"&cursor={cursor}" if cursor else "")
            payload = self.client.get(query).json()
            page = payload["events"]
            self.assertTrue(page)
            collected.extend(event["id"] for event in page)
            flags.append(payload["has_more"])
            # 还有下一页就必须给出游标；没有了就必须为空 ——
            # 否则前端会挂一个点了没反应的「加载更多」。
            self.assertEqual(bool(payload["next_cursor"]), payload["has_more"])
            cursor = payload["next_cursor"]

        self.assertEqual(len(collected), 5)
        self.assertEqual(len(set(collected)), 5, "跨页出现重复")
        self.assertEqual(flags, [True, True, False])

    def test_last_page_has_no_cursor(self):
        self._requested("唯一一条")

        payload = self.client.get(f"{_PATH}?limit=2").json()

        self.assertFalse(payload["has_more"])
        self.assertEqual(payload["next_cursor"], "")

    def test_malformed_cursor_is_422_not_500(self):
        """手改过的 query string 必须是 422 —— 500 会让人以为服务坏了。"""
        response = self.client.get(f"{_PATH}?cursor=not-a-cursor")

        self.assertEqual(response.status_code, 422)

    def test_limit_out_of_range_is_rejected(self):
        """上限由接口自己声明，越界是 422 而不是悄悄改成最大值。"""
        self.assertEqual(self.client.get(f"{_PATH}?limit=0").status_code, 422)
        self.assertEqual(self.client.get(f"{_PATH}?limit=100000").status_code, 422)

    # ------------------------------------------------------------ 缓存
    def test_timeline_is_never_cached(self):
        """时间线是「助手做过什么」的证据页，被缓存会让用户拿旧记录核对新发生的事。"""
        headers = self.client.get(_PATH).headers

        self.assertEqual(headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
