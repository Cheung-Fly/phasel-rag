"""站点注册表测试（阶段 C ①）。

覆盖两类东西：
  1. **规格校验**（纯函数，不需数据库）—— 重点是那些「配错了却看起来配对了」
     的输入：host 写一个域名、login_url 写另一个域名。这条不一致会让
     browser 侧的白名单形同虚设，所以必须拒绝而不是自动取其一。
  2. **多用户隔离与幂等播种**（需要 PostgreSQL）—— 站点按 owner 隔离，
     内置站点重复播种不得覆盖用户改过的配置。

「新增一个站点全程不改 Python 代码」这条验收口径不在本文件里验证 ——
它属于接口层（W2），需要用真实 HTTP 请求证明。
"""
import os
import re
import unittest
import uuid

from app import auth_store as auth_module
from app.site_store import (
    BUILTIN_SITES,
    SiteAlreadyExists,
    SiteStore,
    SiteValidationError,
    match_site,
    validate_site,
)

_VALID = {
    "site_key": "demo-site",
    "name": "示例站点",
    "aliases": ["示例站点", "示例平台"],
    "host": "example.edu.cn",
    "login_url": "https://example.edu.cn/login",
    "username_selector": 'input[placeholder="用户名"]',
    "password_selector": 'input[placeholder="密码"]',
    "blocked_path_parts": ["logout", "退出"],
    "requires_captcha": True,
}


def _payload(**overrides):
    return {**_VALID, **overrides}


class SiteValidationTests(unittest.TestCase):
    """纯校验：不需要数据库，任何环境都应全跑。"""

    def test_valid_spec_is_normalized(self):
        site = validate_site(_payload(site_key="Demo-Site", host="Example.EDU.cn"))

        self.assertEqual(site["site_key"], "demo-site", "site_key 应转小写")
        self.assertEqual(site["host"], "example.edu.cn", "host 应转小写")
        self.assertEqual(site["aliases"], ["示例站点", "示例平台"])
        self.assertTrue(site["requires_captcha"])

    def test_aliases_are_deduped_and_blank_entries_dropped(self):
        site = validate_site(_payload(aliases=["  示例站点  ", "示例站点", "", "  "]))

        self.assertEqual(site["aliases"], ["示例站点"])

    def test_blocked_path_parts_are_casefolded(self):
        site = validate_site(_payload(blocked_path_parts=["LogOut", "退出"]))

        self.assertEqual(site["blocked_path_parts"], ["logout", "退出"])

    def test_host_and_login_url_must_share_a_domain(self):
        """★ 核心安全用例：host 写 A、login_url 写 B 时必须拒绝。

        browser 侧用 host 决定「允许访问哪个域」，用 login_url 决定
        「打开哪个页面」。两者不一致，白名单就只管住了导航、管不住表单提交。
        """
        with self.assertRaises(SiteValidationError) as ctx:
            validate_site(_payload(host="example.edu.cn", login_url="https://evil.cn/login"))

        self.assertIn("必须与 host", str(ctx.exception))

    def test_ip_literal_host_is_rejected(self):
        """IP 字面量必须拒绝 —— 否则 169.254.169.254 这类云元数据地址能蒙进来。"""
        for host in ("169.254.169.254", "127.0.0.1", "10.0.0.1"):
            with self.subTest(host=host):
                with self.assertRaises(SiteValidationError):
                    validate_site(_payload(host=host, login_url=f"https://{host}/login"))

    def test_plain_http_is_rejected(self):
        with self.assertRaises(SiteValidationError) as ctx:
            validate_site(_payload(login_url="http://example.edu.cn/login"))

        self.assertIn("https", str(ctx.exception))

    def test_login_url_with_query_or_fragment_is_rejected(self):
        for url in (
            "https://example.edu.cn/login?next=/x",
            "https://example.edu.cn/login#top",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SiteValidationError):
                    validate_site(_payload(login_url=url))

    def test_login_url_must_have_a_path(self):
        with self.assertRaises(SiteValidationError):
            validate_site(_payload(login_url="https://example.edu.cn/"))

    def test_non_default_port_is_rejected(self):
        with self.assertRaises(SiteValidationError):
            validate_site(_payload(login_url="https://example.edu.cn:8443/login"))

    def test_non_numeric_port_is_rejected_as_a_validation_error(self):
        """urlparse 对 "https://a.cn:abc/login" 会延迟到取 .port 时才抛 ValueError。

        不翻译的话它会一路冒到接口层变成 500 —— 而它其实只是用户填错了端口，
        应当是一句可读的 422。
        """
        with self.assertRaises(SiteValidationError):
            validate_site(_payload(login_url="https://example.edu.cn:abc/login"))

    def test_selector_with_newline_is_rejected(self):
        """选择器会成为 argv 里的一项。换行会让子进程参数变得不可读 —— 直接拒绝。"""
        for field in ("username_selector", "password_selector"):
            with self.subTest(field=field):
                with self.assertRaises(SiteValidationError):
                    validate_site(_payload(**{field: 'input\n[placeholder="x"]'}))

    def test_blank_selector_is_rejected(self):
        with self.assertRaises(SiteValidationError):
            validate_site(_payload(username_selector="   "))

    def test_site_key_pattern_is_enforced(self):
        for key in ("Demo_Site", "-leading", "有中文", "a" * 65, ""):
            with self.subTest(key=key):
                with self.assertRaises(SiteValidationError):
                    validate_site(_payload(site_key=key))

    def test_too_many_aliases_are_rejected(self):
        with self.assertRaises(SiteValidationError):
            validate_site(_payload(aliases=[f"别名{index}" for index in range(21)]))

    def test_oversized_selector_is_rejected(self):
        with self.assertRaises(SiteValidationError):
            validate_site(_payload(username_selector="a" * 301))


class MatchSiteTests(unittest.TestCase):
    """关键词匹配必须与改造前的语义一致，否则会出现「升级后同一句话不再触发登录」。"""

    def setUp(self):
        self.sites = [
            {
                "site_key": "a",
                "aliases": ["东软智慧教育平台", "智慧教育平台"],
            },
            {"site_key": "b", "aliases": ["图书馆"]},
        ]

    def test_alias_substring_matches(self):
        self.assertEqual(
            match_site(self.sites, "帮我登录东软智慧教育平台")["site_key"], "a"
        )

    def test_shorter_alias_still_matches(self):
        self.assertEqual(match_site(self.sites, "打开智慧教育平台")["site_key"], "a")

    def test_no_match_returns_none(self):
        self.assertIsNone(match_site(self.sites, "今天天气怎么样"))

    def test_empty_alias_list_never_matches(self):
        self.assertIsNone(match_site([{"site_key": "c", "aliases": []}], "任何问题"))


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class SiteStoreTests(unittest.TestCase):
    """需要 PostgreSQL：验证多用户隔离、唯一约束、以及播种的幂等性。"""

    @classmethod
    def setUpClass(cls):
        # ★ 顺序不能反：sites 表有指向 app_users 的外键，
        #   在全新库上若先建 sites 会直接失败（relation app_users does not exist）。
        #   main.py 的启动流程同样必须保持这个顺序。
        cls.auth = auth_module.AuthStore()
        cls.store = SiteStore()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.auth.close()

    def setUp(self):
        suffix = uuid.uuid4().hex[:10]
        # site_key 有 64 字符上限，用固定前缀 + 10 位随机后缀留足余量
        self.suffix = suffix
        self.owner = self.auth.ensure_local_owner(f"stagec-site-{suffix}").user_id
        self.other = self.auth.ensure_local_owner(f"stagec-other-{suffix}").user_id

    def tearDown(self):
        for owner in (self.owner, self.other):
            with self.store._pool.connection() as connection:
                connection.execute("DELETE FROM sites WHERE owner_user_id = %s", (owner,))

    def _key(self, name="demo"):
        return f"{name}-{self.suffix}"

    def test_create_then_get_roundtrips_all_fields(self):
        key = self._key()
        created = self.store.create(self.owner, _payload(site_key=key))

        fetched = self.store.get(self.owner, key)

        self.assertEqual(fetched["site_key"], key)
        self.assertEqual(fetched["host"], "example.edu.cn")
        self.assertEqual(fetched["aliases"], ["示例站点", "示例平台"])
        self.assertEqual(fetched["blocked_path_parts"], ["logout", "退出"])
        self.assertTrue(fetched["enabled"])
        self.assertEqual(created["site_key"], key)

    def test_duplicate_site_key_raises_already_exists(self):
        key = self._key()
        self.store.create(self.owner, _payload(site_key=key))

        with self.assertRaises(SiteAlreadyExists):
            self.store.create(self.owner, _payload(site_key=key))

    def test_same_key_is_allowed_for_different_owners(self):
        """站点按 owner 隔离：不同用户的同名 site_key 互不冲突。"""
        key = self._key()
        self.store.create(self.owner, _payload(site_key=key))
        self.store.create(self.other, _payload(site_key=key))

        self.assertIsNotNone(self.store.get(self.owner, key))
        self.assertIsNotNone(self.store.get(self.other, key))

    def test_get_does_not_leak_across_owners(self):
        key = self._key()
        self.store.create(self.owner, _payload(site_key=key))

        self.assertIsNone(
            self.store.get(self.other, key), "别的用户不应看到该站点"
        )

    def test_update_keeps_site_key_and_changes_other_fields(self):
        key = self._key()
        self.store.create(self.owner, _payload(site_key=key))

        updated = self.store.update(
            self.owner, key, _payload(site_key=key, name="改名后", enabled=False)
        )

        self.assertEqual(updated["site_key"], key)
        self.assertEqual(updated["name"], "改名后")
        self.assertFalse(updated["enabled"])

    def test_update_rejects_site_key_change(self):
        """site_key 是凭证 service_key 的锚点，改了会让既有凭证失联。"""
        key = self._key()
        self.store.create(self.owner, _payload(site_key=key))

        with self.assertRaises(SiteValidationError) as ctx:
            self.store.update(self.owner, key, _payload(site_key=self._key("renamed")))

        self.assertIn("不可修改", str(ctx.exception))

    def test_update_missing_site_returns_none(self):
        # ★ payload 的 site_key 必须与路径参数一致，否则会先撞上
        #   「site_key 不可修改」的守卫，测不到「不存在 → None」这条路径。
        self.assertIsNone(
            self.store.update(self.other, self._key(), _payload(site_key=self._key()))
        )

    def test_delete_removes_only_that_owner_row(self):
        key = self._key()
        self.store.create(self.owner, _payload(site_key=key))
        self.store.create(self.other, _payload(site_key=key))

        self.assertTrue(self.store.delete(self.owner, key))

        self.assertIsNone(self.store.get(self.owner, key))
        self.assertIsNotNone(self.store.get(self.other, key), "不应影响其他用户")
        self.assertFalse(self.store.delete(self.owner, key), "重复删除应为 False")

    def test_list_is_scoped_to_owner(self):
        self.store.create(self.owner, _payload(site_key=self._key("one")))
        self.store.create(self.other, _payload(site_key=self._key("two")))

        keys = {site["site_key"] for site in self.store.list_for_owner(self.owner)}

        self.assertIn(self._key("one"), keys)
        self.assertNotIn(self._key("two"), keys)

    def test_ensure_builtin_sites_is_idempotent_and_does_not_overwrite(self):
        """★ 播种只补缺、不覆盖 —— 否则用户改过的选择器会在每次启动被抹掉。"""
        first = self.store.ensure_builtin_sites(self.owner)
        self.assertEqual(first, len(BUILTIN_SITES))

        builtin_key = BUILTIN_SITES[0]["site_key"]
        self.store.update(
            self.owner, builtin_key, _payload(
                site_key=builtin_key,
                name="我改过的名字",
                host=BUILTIN_SITES[0]["host"],
                login_url=BUILTIN_SITES[0]["login_url"],
            )
        )

        second = self.store.ensure_builtin_sites(self.owner)

        self.assertEqual(second, 0, "已存在时不应再插入")
        self.assertEqual(
            self.store.get(self.owner, builtin_key)["name"], "我改过的名字",
            "播种不得覆盖用户配置",
        )

    def test_builtin_site_spec_passes_validation(self):
        """内置站点的硬编码值必须本身合法 —— 否则升级后第一次启动就炸。"""
        for site in BUILTIN_SITES:
            with self.subTest(site=site["site_key"]):
                validated = validate_site(site)
                self.assertEqual(validated["site_key"], site["site_key"])


if __name__ == "__main__":
    unittest.main()
