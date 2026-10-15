"""审计时间线测试（阶段 C ④ / W4）。

四组：
  SummarizeTests          可读文案（纯函数）
  CursorTests             游标编解码（纯函数）
  TimelineMergeTests      跨表合并与**游标翻页**（需要 PostgreSQL）
  SiteAuditTests          站点变更也留痕（W2 → W4 的接缝）

【为什么不认识的 action 不许猜】
时间线是用户用来核对「助手到底做了什么」的界面，一句编出来的中文
比空白更有害 —— 用户会以为系统理解了这个动作。

【为什么翻页单独一组】
「跨表合并」与「翻页」是同一个问题的两半：选 UNION 是因为应用层合并会让
分页错，那么分页本身就必须被真的验一次 —— 尤其「取两页之间插进一条新事件」
这个场景，它正是 OFFSET 会出错、游标不会的地方。
"""
import os
import unittest
import uuid
from unittest import mock

from app import auth_store as auth_module
from app import site_store
from app.approval_store import (
    ACTION_EXECUTED,
    ACTION_REQUESTED,
    ApprovalStore,
)
from app.audit_timeline import (
    AuditTimeline,
    make_cursor,
    parse_cursor,
    summarize,
)


class SummarizeTests(unittest.TestCase):
    """纯函数：任何环境都应全跑。"""

    def test_credential_actions_use_the_label(self):
        event = {
            "source": "credential",
            "action": "reveal",
            "detail": {"label": "我的校园账号", "credential_id": "abc12345"},
        }

        self.assertEqual(
            summarize(event), "取出了凭证「我的校园账号」的明文口令（用于自动登录）"
        )

    def test_missing_label_falls_back_to_a_short_id(self):
        """凭证被删后 join 不到 label —— 不能显示成「删除了凭证『』」。"""
        event = {
            "source": "credential",
            "action": "delete",
            "detail": {"label": "", "credential_id": "abcdef0123456789"},
        }

        self.assertEqual(summarize(event), "删除了凭证「凭证 abcdef01」")

    def test_approval_actions_include_the_target(self):
        event = {
            "source": "operation",
            "action": ACTION_REQUESTED,
            "kind": "approval",
            "detail": {"target": "button「开始学习」"},
        }

        self.assertEqual(summarize(event), "请求你批准：button「开始学习」")

    def test_approval_without_target_does_not_render_empty_quotes(self):
        event = {
            "source": "operation",
            "action": ACTION_EXECUTED,
            "kind": "approval",
            "detail": {},
        }

        self.assertEqual(summarize(event), "已执行：（未记录目标）")

    def test_unknown_action_is_passed_through_not_guessed(self):
        """★ 不认识的动作原样返回。编一句中文会让用户以为系统理解了这个动作。"""
        event = {
            "source": "operation",
            "action": "some_future_action",
            "kind": "session",
            "detail": {},
        }

        self.assertIn("some_future_action", summarize(event))

    def test_unknown_credential_action_is_passed_through(self):
        event = {
            "source": "credential",
            "action": "archive",
            "detail": {"label": "x"},
        }

        self.assertIn("archive", summarize(event))

    def test_site_actions_read_as_sentences(self):
        """站点变更（W4 补写）走的是与审批事件同一套 target 取值。"""
        for action, expected in (
            ("site_registered", "登记了可操作站点「东软智慧教育平台」"),
            ("site_updated", "修改了可操作站点「东软智慧教育平台」"),
            ("site_removed", "移除了可操作站点「东软智慧教育平台」"),
        ):
            with self.subTest(action=action):
                event = {
                    "source": "operation",
                    "action": action,
                    "kind": "site",
                    "detail": {"target": "东软智慧教育平台", "site_key": "nsu"},
                }

                self.assertEqual(summarize(event), expected)


class CursorTests(unittest.TestCase):
    """游标编解码（纯函数，任何环境都跑）。"""

    def test_cursor_round_trips_the_sort_key(self):
        event = {
            "created_at": "2026-10-15T07:46:47.583487+00:00",
            "source": "credential",
            "seq": 176,
        }

        parsed = parse_cursor(make_cursor(event))

        self.assertEqual(parsed[1], "credential")
        self.assertEqual(parsed[2], 176)
        self.assertEqual(parsed[0].isoformat(), event["created_at"])

    def test_empty_cursor_means_first_page(self):
        self.assertIsNone(parse_cursor(""))
        self.assertIsNone(parse_cursor("   "))
        self.assertIsNone(parse_cursor(None))

    def test_cursor_survives_a_raw_query_string(self):
        """★ 游标必须能**原样**放进 query string 传回来。

        这不是洁癖，是踩出来的：`+` 在 query string 里会被解码成**空格**，
        于是服务端刚打印出来的游标、原样回传就变成「格式不正确」——
        而且错误信息看起来像用户自己填错了。修法是产出时就写成 URL 安全的
        `…Z`（见 audit_timeline._cursor_time），这条测试把那个不变量钉住。
        """
        cursor = make_cursor(
            {
                "created_at": "2026-10-15T07:46:47.583487+00:00",
                "source": "credential",
                "seq": 176,
            }
        )

        for trap in ("+", "&", "#", "%", "?", " ", "="):
            self.assertNotIn(trap, cursor, f"游标里不该出现 {trap!r}")
        self.assertEqual(parse_cursor(cursor)[2], 176)

    def test_malformed_cursor_raises_instead_of_crashing(self):
        """★ 手改过的 query string 必须得到 422，而不是 500。

        一个 500 会让人以为服务坏了；而 422 说的是「这个游标不对」。
        两种信息的排查方向完全不同。
        """
        for bad in (
            "abc",
            "a|b|c|d",
            "2026-10-15T00:00:00+00:00|nope|1",
            "2026-10-15T00:00:00+00:00|credential|x",
            "||",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_cursor(bad)


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class TimelineMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = auth_module.AuthStore()
        cls.approvals = ApprovalStore()
        cls.model = AuditTimeline()

    @classmethod
    def tearDownClass(cls):
        cls.model.close()
        cls.auth.close()
        cls.approvals.close()

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.owner = self.auth.ensure_local_owner(f"stagec-tl-{self.suffix}").user_id
        self.other = self.auth.ensure_local_owner(f"stagec-tl2-{self.suffix}").user_id

    def tearDown(self):
        for owner in (self.owner, self.other):
            with self.model._pool.connection() as connection:
                connection.execute(
                    "DELETE FROM credential_audit WHERE owner_user_id = %s", (owner,)
                )
                connection.execute(
                    "DELETE FROM operation_audit WHERE owner_user_id = %s", (owner,)
                )
                connection.execute(
                    "DELETE FROM operation_approvals WHERE owner_user_id = %s", (owner,)
                )

    def _credential_audit(self, owner, credential_id, action):
        with self.model._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO credential_audit(owner_user_id, credential_id, action)
                VALUES (%s, %s, %s)
                """,
                (owner, credential_id, action),
            )

    def test_timeline_merges_both_sources(self):
        self._credential_audit(self.owner, f"cred-{self.suffix}", "reveal")
        self.approvals.record_event(
            self.owner, kind="approval", action="approval_granted",
            detail={"approval_id": "x", "target": "button「提交」"},
        )

        events = self.model.for_owner(self.owner)

        sources = {event["source"] for event in events}
        self.assertEqual(sources, {"credential", "operation"})

    def test_timeline_is_globally_ordered_newest_first(self):
        """★ 跨表顺序必须由 SQL 决定。应用层合并会让分页错乱。"""
        self._credential_audit(self.owner, f"cred-{self.suffix}", "create")
        self.approvals.record_event(
            self.owner, kind="approval", action="approval_requested",
            detail={"target": "较新的一条"},
        )

        events = self.model.for_owner(self.owner)

        self.assertEqual(events[0]["action"], "approval_requested", "最新的事件应排在最前")
        timestamps = [event["created_at"] for event in events]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_timeline_is_scoped_to_owner(self):
        self.approvals.record_event(
            self.other, kind="approval", action="approval_requested",
            detail={"target": "别人的操作"},
        )

        events = self.model.for_owner(self.owner)

        self.assertNotIn("别人的操作", [event["summary"] for event in events])

    def test_deleted_credential_still_renders_readably(self):
        """凭证已删除时 join 不到 label，但事件仍须可读（不能是空引号）。"""
        self._credential_audit(self.owner, "cred-gone-1234", "delete")

        events = self.model.for_owner(self.owner)

        self.assertEqual(len(events), 1)
        self.assertIn("凭证 cred-gon", events[0]["summary"])

    def test_event_ids_are_unique_across_sources(self):
        """两张表的 audit_id 会撞号，所以对外 id 必须带来源前缀。"""
        self._credential_audit(self.owner, f"cred-{self.suffix}", "read")
        self.approvals.record_event(
            self.owner, kind="approval", action="approval_requested", detail={}
        )

        events = self.model.for_owner(self.owner)

        ids = [event["id"] for event in events]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(event["id"].startswith(("credential-", "operation-")) for event in events))

    def test_limit_is_clamped(self):
        events = self.model.for_owner(self.owner, limit=100000)

        self.assertIsInstance(events, list)

    # ------------------------------------------------------------ 翻页
    def _requested(self, target):
        self.approvals.record_event(
            self.owner, kind="approval", action=ACTION_REQUESTED,
            detail={"target": target},
        )

    def test_pagination_covers_everything_without_repeats(self):
        for index in range(5):
            self._requested(f"事件{index}")

        collected = []
        cursor = None
        for _ in range(3):
            page = self.model.for_owner(self.owner, limit=2, cursor=cursor)
            self.assertTrue(page, "每一页都应有内容（共 5 条，每页 2 条）")
            collected.extend(page)
            cursor = parse_cursor(make_cursor(page[-1]))

        ids = [event["id"] for event in collected]
        self.assertEqual(len(ids), 5, "翻页总共应取到 5 条，既不能漏也不能多")
        self.assertEqual(len(set(ids)), 5, "翻页出现了重复")
        # 跨页拼起来之后，整体仍须是全局倒序 —— 这正是由 SQL 排序保证的那件事。
        stamps = [event["created_at"] for event in collected]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_a_new_event_between_pages_shifts_nothing(self):
        """★ 这条是「游标而不是 OFFSET」的全部理由。

        用 OFFSET 时，两页之间新增一条会把整体往后推一位：
        第 1 页的最后一条随即出现在第 2 页开头（用户看到重复），
        而本该出现在第 2 页的那条被挤到了第 3 页。
        """
        for index in range(4):
            self._requested(f"旧事件{index}")
        page1 = self.model.for_owner(self.owner, limit=2)

        # 取第 1 页之后、取第 2 页之前，插一条最新的。
        self._requested("插进来的新事件")

        page2 = self.model.for_owner(
            self.owner, limit=2, cursor=parse_cursor(make_cursor(page1[-1]))
        )

        self.assertEqual(
            {event["id"] for event in page1} & {event["id"] for event in page2},
            set(),
            "两页出现重复 —— 说明翻页用的是 OFFSET 语义，不是游标",
        )
        # ★ 用子串匹配，不用全等：摘要带「请求你批准：」前缀，
        #   拿裸 target 去 assertNotIn 会**永远成立**（假通过）。
        summaries = [event["summary"] for event in page2]
        self.assertFalse(
            any("插进来的新事件" in text for text in summaries),
            "比游标新的事件不该出现在后面",
        )

        # 但它必须出现在「重新取的第一页」里 —— 否则就是被整条吞掉了。
        fresh = [event["summary"] for event in self.model.for_owner(self.owner, limit=2)]
        self.assertTrue(
            any("插进来的新事件" in text for text in fresh),
            f"新事件没出现在重新取的第一页里：{fresh}",
        )

    def test_cursor_excludes_the_row_it_points_at(self):
        """游标是**排他**的。若写成 <=，同一条会在两页里各出现一次。"""
        for index in range(3):
            self._requested(f"事件{index}")
        page1 = self.model.for_owner(self.owner, limit=1)
        page2 = self.model.for_owner(
            self.owner, limit=1, cursor=parse_cursor(make_cursor(page1[-1]))
        )

        self.assertNotEqual(page1[0]["id"], page2[0]["id"])


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class SiteAuditTests(unittest.TestCase):
    """阶段 C 的验收口径写的是「时间线能读出 **W2**/W3 产生的全部事件」。

    W3 的操作事件天然会进 operation_audit；W2 的站点变更在 W4 之前**一条都不写**，
    所以这条口径当时只有一半成立。这里验的就是补上的那一半：
    登记/修改/删除站点必须留下可读的痕迹，且与站点写入同生共死。
    """

    @classmethod
    def setUpClass(cls):
        cls.auth = auth_module.AuthStore()
        cls.sites = site_store.SiteStore()
        cls.model = AuditTimeline()

    @classmethod
    def tearDownClass(cls):
        cls.model.close()
        cls.sites.close()
        cls.auth.close()

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.owner = self.auth.ensure_local_owner(f"stagec-site-{self.suffix}").user_id
        self.key = f"audit-{self.suffix}"

    def tearDown(self):
        with self.model._pool.connection() as connection:
            connection.execute(
                "DELETE FROM sites WHERE owner_user_id = %s", (self.owner,)
            )
            connection.execute(
                "DELETE FROM operation_audit WHERE owner_user_id = %s", (self.owner,)
            )

    def _payload(self, **overrides):
        payload = {
            "site_key": self.key,
            "name": "审计用示例站点",
            "aliases": ["审计用示例站点"],
            "host": "example.edu.cn",
            "login_url": "https://example.edu.cn/login",
            "username_selector": 'input[name="u"]',
            "password_selector": 'input[name="p"]',
            "blocked_path_parts": [],
            "requires_captcha": False,
        }
        payload.update(overrides)
        return payload

    def _summaries(self):
        return [event["summary"] for event in self.model.for_owner(self.owner)]

    def test_registering_a_site_shows_up_on_the_timeline(self):
        self.sites.create(self.owner, self._payload())

        self.assertIn("登记了可操作站点「审计用示例站点」", self._summaries())

    def test_updating_and_removing_a_site_show_up_too(self):
        self.sites.create(self.owner, self._payload())
        self.sites.update(self.owner, self.key, self._payload(name="改过名的站点"))
        self.sites.delete(self.owner, self.key)

        summaries = self._summaries()

        self.assertIn("修改了可操作站点「改过名的站点」", summaries)
        self.assertIn("移除了可操作站点「改过名的站点」", summaries)

    def test_a_failed_update_writes_nothing(self):
        """改一个不存在的站点 → 时间线上不该出现「改了」但实际没改的记录。

        注意 payload 的 site_key 必须与被改的键一致：update() 先做校验，
        site_key 不一致会被判成「想改主键」而 422，那是另一条分支
        （它同样不写审计，但走的是另一条路，别把两条混成一条测）。
        """
        self.assertIsNone(
            self.sites.update(
                self.owner, "no-such-site", self._payload(site_key="no-such-site")
            )
        )

        self.assertEqual(self._summaries(), [])

    def test_renaming_a_site_is_rejected_and_writes_nothing(self):
        """site_key 是凭证的锚点，不允许改 —— 被拒的操作同样不留痕。"""
        self.sites.create(self.owner, self._payload())

        with self.assertRaises(site_store.SiteValidationError):
            self.sites.update(self.owner, self.key, self._payload(site_key="another-key"))

        summaries = self._summaries()
        self.assertEqual(len(summaries), 1, "只该有最初那条「登记」")
        self.assertIn("登记了可操作站点", summaries[0])

    def test_audit_and_site_write_succeed_or_fail_together(self):
        """★ 审计写不进去时，站点也不该留下来。

        反过来（站点写成功、审计失败）会造出「配置改了却没有记录」的静默盲区 ——
        而阶段 C 整条信任链（能操作谁 → 要不要批准 → 做过什么）正建立在
        「每一步都留痕」上。这也是本文件里唯一一条用 mock 制造故障的用例：
        正常路径下这条 insert 没有任何会失败的约束，故障只能人工注入。
        """
        with mock.patch.object(site_store, "_record", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.sites.create(self.owner, self._payload())

        self.assertIsNone(self.sites.get(self.owner, self.key), "站点不应残留")


if __name__ == "__main__":
    unittest.main()
