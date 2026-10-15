"""站点注册表的接口层与 browser 侧校验（阶段 C ① 的 W2）。

三组：
  BrowserSiteSpecTests        browser 侧 `_validate_site_spec` / `_safe_url` /
                              `_safe_path`（纯函数，不需要数据库、不需要浏览器）
  SiteApiTests                站点接口的对外契约（201 / 409 / 422 / 404）+ owner 隔离
                              +「新建站点立刻可被命中」这条验收口径
                              后两组同属 SiteApiTests，因为它们共用同一份 PG 前置。

【为什么「不改代码」这条要单独测】它是整个阶段 C ① 存在的理由。改造前站点信息
硬编码在两个容器的 Python 里，加一个站点要改码 + 重建两个镜像。任何一次「顺手
把内置站点写回常量」的改动都会让这条失效，但只有这条测试会发现。

【为什么 site_key 不可改要测】它是凭证 service_key 的锚点。允许改会让既有凭证
变成孤儿 —— 而孤儿凭证的表现是「登录工具说没有凭证」，用户会去凭证面板反复
重建，永远修不好。
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app import auth_store as auth_module
from app import main
from app.auth_store import AuthenticatedUser
from app.site_store import SiteStore, close_site_store

# browser 容器与 api 容器共用同一份源码树，但 api 镜像里只 COPY 了 app/ 与 scripts/。
# 跑测试时把 backend/browser 挂进来即可；没挂就跳过这一组，而不是让整个模块
# 因为一句 import 失败而收不了（那会连带把接口契约测试也藏起来）。
_BROWSER_DIR = Path(__file__).resolve().parent.parent / "browser"
if _BROWSER_DIR.is_dir() and str(_BROWSER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROWSER_DIR))
try:
    import mcp_server
except Exception:  # pragma: no cover - 依赖未挂载时的降级
    mcp_server = None


def _site_payload(**overrides) -> dict:
    payload = {
        "site_key": "demo-site",
        "name": "示例站点",
        "aliases": ["示例站点", "示例平台"],
        "host": "example.edu.cn",
        "login_url": "https://example.edu.cn/login",
        "username_selector": 'input[placeholder="用户名"]',
        "password_selector": 'input[placeholder="密码"]',
        "blocked_path_parts": ["logout", "退出"],
        "requires_captcha": False,
    }
    payload.update(overrides)
    return payload


@unittest.skipIf(mcp_server is None, "browser 侧源码未挂载")
class BrowserSiteSpecTests(unittest.TestCase):
    """browser 侧的规格校验。

    ★ 这是把「白名单强度」补回来的第一道：规格来源从 browser 自持的常量
    变成了 api 下发的数据，所以 browser 必须自己再确认这份数据**自身自洽**。
    api 侧已经校验过一遍，但两份实现刻意独立 —— 跨容器共用同一份代码，
    等于把「第二道防线」变成「同一道防线调用了两次」。
    """

    def test_valid_spec_is_normalized(self):
        spec = mcp_server._validate_site_spec(
            "https://Example.EDU.cn/login",
            "Example.EDU.cn",
            'input[placeholder="用户名"]',
            'input[placeholder="密码"]',
            ["Logout", "退出"],
        )

        self.assertIsNotNone(spec)
        self.assertEqual(spec["host"], "example.edu.cn")
        self.assertEqual(spec["blocked_path_parts"], ["logout", "退出"])

    def test_host_and_login_url_must_agree(self):
        """★ 这条就是自一致性检查本身。

        host 决定允许访问哪些域，login_url 决定打开哪个页面。两者不同域时，
        允许列表就形同虚设 —— 浏览器会去打开一个不在白名单里的地址。
        """
        spec = mcp_server._validate_site_spec(
            "https://evil.example.cn/login",
            "example.edu.cn",
            'input[placeholder="用户名"]',
            'input[placeholder="密码"]',
            [],
        )

        self.assertIsNone(spec)

    def test_non_https_or_ip_literal_host_is_refused(self):
        base = ("example.edu.cn", 'input[a]', 'input[b]', [])
        for login_url, host in (
            ("http://example.edu.cn/login", "example.edu.cn"),  # 明文 http
            ("https://example.edu.cn:8443/login", "example.edu.cn"),  # 非 443
            ("https://example.edu.cn/login?next=1", "example.edu.cn"),  # 带查询参数
            ("https://169.254.169.254/login", "169.254.169.254"),  # 云元数据地址
            ("https://example.edu.cn/login", "not a domain"),
        ):
            with self.subTest(login_url=login_url, host=host):
                self.assertIsNone(
                    mcp_server._validate_site_spec(
                        login_url, host, base[1], base[2], base[3]
                    )
                )

    def test_blank_selector_is_refused(self):
        """选择器为空时不要「放行但填不进去」，要直接拒绝。

        放行的后果是浏览器开了、等一个永远不存在的元素直到超时，
        用户看到的是「卡住」，而不是「配置错了」。
        """
        self.assertIsNone(
            mcp_server._validate_site_spec(
                "https://example.edu.cn/login", "example.edu.cn", "  ", "input[b]", []
            )
        )

    def test_selector_with_control_chars_is_refused(self):
        # 用**内部**换行：两端空白会被 strip 掉（那是善意的容错，粘贴选择器时很常见），
        # 真正畸形的是选择器里夹着换行/空字节。
        self.assertIsNone(
            mcp_server._validate_site_spec(
                "https://example.edu.cn/login",
                "example.edu.cn",
                'input[a\r\nx]',
                "input[b]",
                [],
            )
        )

    def test_blocked_path_parts_must_be_a_list(self):
        self.assertIsNone(
            mcp_server._validate_site_spec(
                "https://example.edu.cn/login",
                "example.edu.cn",
                "input[a]",
                "input[b]",
                "logout",  # 字符串不是列表
            )
        )

    def test_safe_url_needs_the_sessions_own_host(self):
        self.assertTrue(
            mcp_server._safe_url("https://example.edu.cn/portal", "example.edu.cn")
        )
        # 同一份代码、不同会话：host 是参数而不是模块常量，这正是本次改造的关键
        self.assertFalse(
            mcp_server._safe_url("https://example.edu.cn/portal", "other.edu.cn")
        )
        self.assertFalse(
            mcp_server._safe_url("http://example.edu.cn/portal", "example.edu.cn")
        )
        self.assertFalse(
            mcp_server._safe_url("https://example.edu.cn.evil.cn/x", "example.edu.cn")
        )

    def test_safe_path_uses_the_sessions_blocked_parts(self):
        self.assertTrue(mcp_server._safe_path("/course/1", ["logout"]))
        self.assertFalse(mcp_server._safe_path("/logout", ["logout"]))
        self.assertFalse(mcp_server._safe_path("/course/1?next=logout", ["logout"]))
        # 阻断项是每站点自己的配置，不是全局常量
        self.assertTrue(mcp_server._safe_path("/logout", ["unrelated"]))
        # 百分号编码要还原后再比
        self.assertFalse(mcp_server._safe_path("/%E9%80%80%E5%87%BA", ["退出"]))


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class SiteApiTests(unittest.TestCase):
    """站点接口的对外契约。用 TestClient 走真实 HTTP，因此能验到状态码。"""

    @classmethod
    def setUpClass(cls):
        # app_users 必须先于 sites 存在（外键依赖），见 test_site_registry 的同名注释。
        cls.auth = auth_module.AuthStore()
        cls.store = SiteStore()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.auth.close()
        # 请求路径会用模块单例（main.get_site_store），顺手回收它的连接池。
        close_site_store()

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.owner = self.auth.ensure_local_owner(f"stagec-api-{self.suffix}").user_id
        self.other = self.auth.ensure_local_owner(f"stagec-api2-{self.suffix}").user_id
        self.user = AuthenticatedUser(
            user_id=self.owner, username=f"stagec-api-{self.suffix}"
        )
        # 覆盖身份依赖，而不是把 local 模式跑起来 —— 后者会去动真实的 sites 表。
        main.app.dependency_overrides[main._current_user] = lambda: self.user
        self.client = TestClient(main.app)

    def tearDown(self):
        main.app.dependency_overrides.pop(main._current_user, None)
        with self.store._pool.connection() as connection:
            connection.execute(
                "DELETE FROM sites WHERE owner_user_id = ANY(%s)",
                ([self.owner, self.other],),
            )

    def _key(self, name="demo"):
        return f"{name}-{self.suffix}"

    # ------------------------------------------------------------ 契约
    def test_create_then_list_roundtrips(self):
        created = self.client.post(
            "/api/sites", json=_site_payload(site_key=self._key())
        )

        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertEqual(body["site_key"], self._key())
        self.assertEqual(body["aliases"], ["示例站点", "示例平台"])
        self.assertTrue(body["enabled"], "默认应当是启用的")
        self.assertTrue(body["created_at"], "创建时间要回传给前端")

        listing = self.client.get("/api/sites")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([item["site_key"] for item in listing.json()["sites"]], [self._key()])

    def test_duplicate_site_key_returns_409(self):
        payload = _site_payload(site_key=self._key())
        self.client.post("/api/sites", json=payload)

        again = self.client.post("/api/sites", json=payload)

        # 409 而不是 422：两者对用户是不同的信息
        self.assertEqual(again.status_code, 409)
        self.assertIn("已存在", again.json()["detail"])

    def test_inconsistent_host_and_login_url_returns_422(self):
        response = self.client.post(
            "/api/sites",
            json=_site_payload(
                site_key=self._key(),
                host="example.edu.cn",
                login_url="https://other.example.cn/login",
            ),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("必须与 host", response.json()["detail"])

    def test_unknown_site_returns_404(self):
        self.assertEqual(
            self.client.put(
                f"/api/sites/{self._key('nope')}",
                json=_site_payload(site_key=self._key("nope")),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"/api/sites/{self._key('nope')}").status_code, 404
        )

    def test_update_keeps_key_and_applies_changes(self):
        key = self._key()
        self.client.post("/api/sites", json=_site_payload(site_key=key))

        updated = self.client.put(
            f"/api/sites/{key}",
            json=_site_payload(site_key=key, name="改名后", enabled=False),
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["name"], "改名后")
        self.assertFalse(updated.json()["enabled"])

    def test_update_refuses_site_key_change(self):
        """site_key 是凭证 service_key 的锚点，改了会让既有凭证失联。"""
        key = self._key()
        self.client.post("/api/sites", json=_site_payload(site_key=key))

        response = self.client.put(
            f"/api/sites/{key}",
            json=_site_payload(site_key=self._key("renamed")),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("不可修改", response.json()["detail"])

    def test_delete_removes_the_site(self):
        key = self._key()
        self.client.post("/api/sites", json=_site_payload(site_key=key))

        deleted = self.client.delete(f"/api/sites/{key}")

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"deleted": True, "site_key": key})
        self.assertEqual(self.client.get("/api/sites").json()["sites"], [])

    # ------------------------------------------------------------ 隔离
    def test_other_owner_cannot_see_or_touch_the_site(self):
        key = self._key()
        self.client.post("/api/sites", json=_site_payload(site_key=key))

        main.app.dependency_overrides[main._current_user] = lambda: AuthenticatedUser(
            user_id=self.other, username="other"
        )
        try:
            self.assertEqual(self.client.get("/api/sites").json()["sites"], [])
            self.assertEqual(
                self.client.put(
                    f"/api/sites/{key}", json=_site_payload(site_key=key)
                ).status_code,
                404,
            )
            self.assertEqual(
                self.client.delete(f"/api/sites/{key}").status_code, 404
            )
        finally:
            main.app.dependency_overrides[main._current_user] = lambda: self.user

        # 别人的删除动作不得影响原主人
        self.assertEqual(len(self.client.get("/api/sites").json()["sites"]), 1)

    # ------------------------------------------------------------ ★ 验收口径
    def test_new_site_is_immediately_matchable_without_code_changes(self):
        """★ W2 的验收口径：新增一个站点全程不改 Python 代码。

        必须走**真实路径**才作数：用 HTTP 接口建站（不是直接调 store），
        再通过上线时的那个入口 `_requested_login_target` 看它能否被一句
        自然语言命中 —— 而且中间不做任何缓存刷新动作。
        """
        alias = f"示例平台{self.suffix}"
        created = self.client.post(
            "/api/sites", json=_site_payload(site_key=self._key(), aliases=[alias])
        )
        self.assertEqual(created.status_code, 201)

        target = main._requested_login_target(f"帮我登录{alias}", self.user)

        self.assertIsNotNone(target, "新建的站点必须立刻能被登录意图命中")
        self.assertEqual(target["site_key"], self._key())
        self.assertEqual(target["host"], "example.edu.cn")
        self.assertEqual(target["login_url"], "https://example.edu.cn/login")
        self.assertEqual(target["blocked_path_parts"], ["logout", "退出"])

    def test_disabled_site_still_matches_so_the_user_gets_a_clear_message(self):
        """停用站点**仍然参与匹配**，只是执行时被拒。

        若在匹配阶段就把停用站点过滤掉，用户说「登录 X」会掉进浏览器智能体，
        收到「尚无会话，请先登录」这种驴唇不对马嘴的回复；而真正的原因
        （那个站点被停用了）永远不会被说出来。
        """
        alias = f"停用平台{self.suffix}"
        self.client.post(
            "/api/sites",
            json=_site_payload(site_key=self._key(), aliases=[alias], enabled=False),
        )

        target = main._requested_login_target(f"登录{alias}", self.user)

        self.assertIsNotNone(target)
        self.assertFalse(target["enabled"])

    def test_matching_is_scoped_to_the_owner(self):
        alias = f"别家平台{self.suffix}"
        self.client.post(
            "/api/sites", json=_site_payload(site_key=self._key(), aliases=[alias])
        )

        self.assertIsNone(
            main._requested_login_target(
                f"登录{alias}",
                AuthenticatedUser(user_id=self.other, username="other"),
            ),
            "别人的站点不该被自己的登录意图命中",
        )


if __name__ == "__main__":
    unittest.main()
