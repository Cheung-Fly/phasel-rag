"""操作审批闸门测试（阶段 C ②）。

这组用例盯的是「一旦写错就会真的做错事」的地方，而不是「函数有没有被调用」：

  · 未批准就执行            → 必须拿不到执行租约（这是闸门的全部意义）
  · 同一次批准执行两次      → 第二次必须失败（重放 = 重复点击，副作用不可逆）
  · 批准后拖到超时才执行    → 必须失败（页面早变了，那个控件索引已不是原来那个）
  · approve 双击            → 只有一次生效
  · 别人的审批                → 不可见、不可批准

超时相关的用例全部用**直接改数据库 expires_at** 制造，不靠 sleep ——
靠 sleep 的测试会又慢又不稳定，而后者的危害更大（会被人为忽略）。
"""
import os
import unittest
import uuid

from app import auth_store as auth_module
from app.approval_store import (
    ACTION_EXPIRED,
    ACTION_GRANTED,
    ACTION_REQUESTED,
    ApprovalStore,
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_EXECUTING,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
)


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class ApprovalStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ★ app_users 必须先于审批表存在（外键依赖），见 test_site_registry 的同名注释。
        cls.auth = auth_module.AuthStore()
        cls.store = ApprovalStore()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.auth.close()

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.owner = self.auth.ensure_local_owner(f"stagec-appr-{self.suffix}").user_id
        self.other = self.auth.ensure_local_owner(f"stagec-appr2-{self.suffix}").user_id

    def tearDown(self):
        for owner in (self.owner, self.other):
            with self.store._pool.connection() as connection:
                connection.execute(
                    "DELETE FROM operation_approvals WHERE owner_user_id = %s", (owner,)
                )
                connection.execute(
                    "DELETE FROM operation_audit WHERE owner_user_id = %s", (owner,)
                )

    # ------------------------------------------------------------ 辅助
    def _new(self, target="button「开始学习」", **kwargs):
        return self.store.create(self.owner, target=target, **kwargs)

    def _force_expired(self, approval_id):
        with self.store._pool.connection() as connection:
            connection.execute(
                """
                UPDATE operation_approvals
                SET expires_at = now() - interval '1 second'
                WHERE approval_id = %s
                """,
                (approval_id,),
            )

    def _events(self, owner=None):
        with self.store._pool.connection() as connection:
            return connection.execute(
                """
                SELECT action, detail FROM operation_audit
                WHERE owner_user_id = %s ORDER BY audit_id
                """,
                (owner or self.owner,),
            ).fetchall()

    # ------------------------------------------------------------ 创建
    def test_new_approval_starts_pending(self):
        approval = self._new()

        self.assertEqual(approval["status"], STATUS_PENDING)
        self.assertTrue(approval["approval_id"])
        self.assertEqual(approval["target"], "button「开始学习」")

    def test_target_whitespace_is_collapsed(self):
        approval = self._new(target="  button 「  开始   学习 」  ")

        self.assertEqual(approval["target"], "button 「 开始 学习 」")

    def test_blank_target_is_rejected(self):
        with self.assertRaises(ValueError):
            self._new(target="   ")

    def test_oversized_target_is_rejected(self):
        with self.assertRaises(ValueError):
            self._new(target="x" * 201)

    def test_ttl_is_clamped_to_a_sane_range(self):
        short = self._new(ttl_seconds=1)
        long = self._new(ttl_seconds=100000)

        self.assertLess(short["expires_at"], long["expires_at"])

    def test_pending_backlog_is_capped(self):
        """待审批堆积要有上限，否则界面被刷爆、真需要批准的那条被淹没。"""
        for index in range(20):
            self._new(target=f"控件 {index}")

        with self.assertRaises(ValueError) as ctx:
            self._new(target="第 21 个")

        self.assertIn("过多", str(ctx.exception))

    def test_request_is_recorded_in_audit(self):
        approval = self._new()

        actions = [event["action"] for event in self._events()]
        self.assertIn(ACTION_REQUESTED, actions)
        detail = self._events()[-1]["detail"]
        self.assertEqual(detail["approval_id"], approval["approval_id"])

    # ------------------------------------------------------------ 批准 / 拒绝
    def test_approve_moves_pending_to_approved(self):
        approval = self._new()

        approved = self.store.approve(self.owner, approval["approval_id"])

        self.assertEqual(approved["status"], STATUS_APPROVED)
        self.assertIsNotNone(approved["approved_at"])
        self.assertIn(ACTION_GRANTED, [event["action"] for event in self._events()])

    def test_approve_twice_only_applies_once(self):
        """双击批准按钮：第二次必须拿不到行（接口回 409），而不是静默成功。"""
        approval = self._new()
        self.assertIsNotNone(self.store.approve(self.owner, approval["approval_id"]))

        self.assertIsNone(self.store.approve(self.owner, approval["approval_id"]))

    def test_reject_then_approve_is_refused(self):
        approval = self._new()
        rejected = self.store.reject(self.owner, approval["approval_id"])

        self.assertEqual(rejected["status"], STATUS_REJECTED)
        self.assertIsNone(self.store.approve(self.owner, approval["approval_id"]))

    def test_approve_after_expiry_is_refused(self):
        approval = self._new()
        self._force_expired(approval["approval_id"])

        self.assertIsNone(
            self.store.approve(self.owner, approval["approval_id"]),
            "过期后批准必须失败 —— 否则界面上的按钮等于永久有效",
        )

    def test_reject_after_expiry_is_refused(self):
        """过期只能由 expire_stale 落成 expired。

        若这里放行，同一条记录会因为「用户有没有先打开列表页」而变成 rejected 或 expired，
        而 rejected 那种说法是假的：没有人拒绝过它，它只是超时了。
        """
        approval = self._new()
        self._force_expired(approval["approval_id"])

        self.assertIsNone(self.store.reject(self.owner, approval["approval_id"]))

        # 状态未被污染；经惰性清扫后落到唯一正确的终态
        self.assertEqual(
            self.store.get(self.owner, approval["approval_id"])["status"], STATUS_PENDING
        )
        self.store.expire_stale(self.owner)
        self.assertEqual(
            self.store.get(self.owner, approval["approval_id"])["status"], STATUS_EXPIRED
        )

    def test_other_owner_cannot_approve(self):
        approval = self._new()

        self.assertIsNone(self.store.approve(self.other, approval["approval_id"]))
        self.assertEqual(
            self.store.get(self.owner, approval["approval_id"])["status"],
            STATUS_PENDING,
        )

    def test_get_does_not_cross_owner_boundary(self):
        approval = self._new()

        self.assertIsNotNone(self.store.get(self.owner, approval["approval_id"]))
        self.assertIsNone(self.store.get(self.other, approval["approval_id"]))

    # ------------------------------------------------------------ 执行租约（核心）
    def test_execution_is_refused_without_approval(self):
        """★ 闸门的全部意义：没批准就拿不到执行租约 ⇒ 不产生任何外部副作用。"""
        approval = self._new()

        self.assertIsNone(self.store.begin_execution(self.owner, approval["approval_id"]))
        self.assertEqual(
            self.store.get(self.owner, approval["approval_id"])["status"],
            STATUS_PENDING,
        )

    def test_execution_is_refused_after_rejection(self):
        approval = self._new()
        self.store.reject(self.owner, approval["approval_id"])

        self.assertIsNone(self.store.begin_execution(self.owner, approval["approval_id"]))

    def test_execution_takes_the_lease_once(self):
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])

        first = self.store.begin_execution(self.owner, approval["approval_id"])
        second = self.store.begin_execution(self.owner, approval["approval_id"])

        self.assertEqual(first["status"], STATUS_EXECUTING)
        self.assertIsNone(second, "★ 重放必须失败：重放等于重复点击")

    def test_execution_is_refused_after_expiry_even_if_approved(self):
        """批准之后拖到超时才点执行 —— 页面早已变化，必须拒绝。"""
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])
        self._force_expired(approval["approval_id"])

        self.assertIsNone(self.store.begin_execution(self.owner, approval["approval_id"]))

    def test_other_owner_cannot_take_the_lease(self):
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])

        self.assertIsNone(self.store.begin_execution(self.other, approval["approval_id"]))

    # ------------------------------------------------------------ 终结
    def test_executed_outcome_is_recorded(self):
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])
        self.store.begin_execution(self.owner, approval["approval_id"])

        done = self.store.complete_execution(
            self.owner, approval["approval_id"], outcome=STATUS_EXECUTED
        )

        self.assertEqual(done["status"], STATUS_EXECUTED)
        self.assertIsNotNone(done["executed_at"])

    def test_failure_outcome_keeps_the_reason(self):
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])
        self.store.begin_execution(self.owner, approval["approval_id"])

        done = self.store.complete_execution(
            self.owner,
            approval["approval_id"],
            outcome=STATUS_FAILED,
            detail={"browser_status": "target_changed"},
        )

        self.assertEqual(done["status"], STATUS_FAILED)
        self.assertIsNone(done["executed_at"], "未执行成功就不该有 executed_at")

    def test_expired_outcome_keeps_no_executed_at(self):
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])
        self.store.begin_execution(self.owner, approval["approval_id"])

        done = self.store.complete_execution(
            self.owner,
            approval["approval_id"],
            outcome=STATUS_EXPIRED,
            detail={"browser_status": "pending_click_expired"},
        )

        self.assertEqual(done["status"], STATUS_EXPIRED)
        self.assertIsNone(done["executed_at"])

    def test_unsupported_outcome_is_rejected(self):
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])
        self.store.begin_execution(self.owner, approval["approval_id"])

        with self.assertRaises(ValueError):
            self.store.complete_execution(
                self.owner, approval["approval_id"], outcome="pending"
            )

    def test_complete_is_refused_from_pending(self):
        approval = self._new()

        self.assertIsNone(
            self.store.complete_execution(
                self.owner, approval["approval_id"], outcome=STATUS_EXECUTED
            )
        )

    def test_execution_cannot_be_repeated_after_completion(self):
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])
        self.store.begin_execution(self.owner, approval["approval_id"])
        self.store.complete_execution(
            self.owner, approval["approval_id"], outcome=STATUS_EXECUTED
        )

        self.assertIsNone(self.store.begin_execution(self.owner, approval["approval_id"]))

    # ------------------------------------------------------------ 超时与卡死
    def test_expire_stale_marks_pending_and_approved(self):
        pending = self._new(target="控件 A")
        approved_approval = self._new(target="控件 B")
        self.store.approve(self.owner, approved_approval["approval_id"])
        self._force_expired(pending["approval_id"])
        self._force_expired(approved_approval["approval_id"])

        affected = self.store.expire_stale(self.owner)

        self.assertGreaterEqual(affected, 2)
        self.assertEqual(
            self.store.get(self.owner, pending["approval_id"])["status"], STATUS_EXPIRED
        )
        self.assertEqual(
            self.store.get(self.owner, approved_approval["approval_id"])["status"],
            STATUS_EXPIRED,
        )
        self.assertIn(ACTION_EXPIRED, [event["action"] for event in self._events()])

    def test_expire_stale_leaves_healthy_rows_alone(self):
        approval = self._new()

        self.store.expire_stale(self.owner)

        self.assertEqual(
            self.store.get(self.owner, approval["approval_id"])["status"], STATUS_PENDING
        )

    def test_list_lazily_expires_before_returning(self):
        """列表必须先落过期再返回 —— 否则界面会显示一堆「点了才知道过期」的条目。"""
        approval = self._new(target="过期控件")
        self._force_expired(approval["approval_id"])

        listed = {item["approval_id"]: item for item in self.store.list_for_owner(self.owner)}

        self.assertEqual(listed[approval["approval_id"]]["status"], STATUS_EXPIRED)

    def test_stuck_execution_becomes_unknown(self):
        """execute 中途崩溃会停在 executing。不重放、不判失败，而是标成「结果未知」。"""
        approval = self._new()
        self.store.approve(self.owner, approval["approval_id"])
        self.store.begin_execution(self.owner, approval["approval_id"])
        with self.store._pool.connection() as connection:
            connection.execute(
                """
                UPDATE operation_approvals
                SET updated_at = now() - interval '1 hour'
                WHERE approval_id = %s
                """,
                (approval["approval_id"],),
            )

        marked = self.store.mark_stuck_executions(timeout_seconds=300)

        self.assertGreaterEqual(marked, 1)
        self.assertEqual(
            self.store.get(self.owner, approval["approval_id"])["status"], "unknown"
        )


if __name__ == "__main__":
    unittest.main()
