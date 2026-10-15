"""站点注册表（阶段 C ①）：把「助手能操作哪些站点」从代码搬到数据库。

【为什么必须做这件事】
改造前，同一个站点的信息硬编码在两个容器的四处：

    backend/app/main.py              _LOGIN_TARGETS / _LOGIN_TARGET_ALIASES
    backend/browser/mcp_server.py    ALLOWED_HOST / ALLOWED_LOGIN_URL
    backend/browser/mcp_server.py    USERNAME_SELECTOR / PASSWORD_SELECTOR
    backend/browser/mcp_server.py    _BLOCKED_PATH_PARTS

加一个站点要同时改两个容器的 Python 并重建镜像。Muse 的形态是
「接入哪些 App 由用户决定」，所以真相源必须是数据而非代码 ——
路线图阶段 C 的验收口径正是：**新增一个站点全程不改 Python 代码**。

【为什么真相源在 api 侧，而不是让 browser 容器直连数据库】
两个候选：
  A. browser 直连 PG 读 sites      —— browser 自持白名单，最强；但要给
     browser 引入数据库依赖，且让它同时承担「读库」与「开浏览器」两种角色
  B. api 持有真相源，把站点规格下发给 browser（当前实现）
选 B。理由：A 换来的强度只在「api 已被攻破」时才有意义，而那种威胁模型下
攻击者本来就能读 sites 表；代价却是一个容器新增依赖 + 角色混淆。
更进一步，阶段 E v2 计划把容器压成单进程，A 的改动那时会被推翻。

白名单强度用两条补回来（见 browser/mcp_server 的 `_validate_site_spec`）：
  1) login_url 的 host 必须等于 host —— 自一致性，挡「host 写 A、url 写 B」
  2) 保留 _public_target()（DNS 解析 + 必须公网 IP）—— 它才是真正挡 SSRF 的
     那道防线，与白名单的来源无关

【为什么按 owner 隔离】
凭证表 `encrypted_credentials` 已经是 owner 作用域的。站点与凭证通过
service_key / site_key 关联，若站点做成全局表，多用户模式下会出现
「A 用户新增的站点改了 B 用户的登录行为」。所以从一开始就按 owner 隔离。
既有硬编码站点由 `ensure_builtin_sites()` 在启动时播种给本机主人，
保证升级后行为不变。
"""
import ipaddress
import logging
import re
import threading
from datetime import datetime
from urllib.parse import urlparse

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import get_settings

logger = logging.getLogger(__name__)

MAX_SITES_PER_OWNER = 50
MAX_ALIASES = 20
MAX_BLOCKED_PATH_PARTS = 100
_MAX_FIELD_CHARS = 300
_MAX_URL_CHARS = 2048

_SITE_KEY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# 域名（不是 IP）：至少两段，末段是字母 TLD。IP 字面量另行拒绝 ——
# 否则 169.254.169.254（云元数据服务）这类地址能靠「看起来像域名」蒙进来。
_HOST_RE = re.compile(
    r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)

# 内置站点：把改造前的硬编码值原样搬到数据层，保证升级后行为不变。
# 选择器与危险路径取自 backend/browser/mcp_server.py 改造前的常量。
BUILTIN_SITES = (
    {
        "site_key": "nsu-smart-education",
        "name": "东软智慧教育平台",
        "aliases": ["东软智慧教育平台", "智慧教育平台"],
        "host": "study.nsu.edu.cn",
        "login_url": "https://study.nsu.edu.cn/login",
        "username_selector": 'input[placeholder="请输入用户名"]',
        "password_selector": 'input[placeholder="请输入密码"]',
        "blocked_path_parts": [
            "logout", "signout", "delete", "remove", "withdraw", "submit",
            "save", "enroll", "register", "payment", "checkout", "cancel",
            "退出", "删除", "提交", "保存", "缴费", "退选", "取消",
        ],
        "requires_captcha": True,
    },
)


class SiteValidationError(ValueError):
    """站点规格不合法。继承 ValueError，调用方可以统一按 422 处理。"""


class SiteAlreadyExists(SiteValidationError):
    """site_key 已存在。

    单独立一个类型是为了让接口能回 409 而不是 422 —— 两者对用户是不同的
    信息（「你填错了」vs「这个名字已被占用」）。刻意不做「先查再插」，
    那会留下 TOCTOU 窗口；直接依赖主键冲突再把异常翻译过来。
    """


def _clean_list(value, *, field: str, limit: int, casefold: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SiteValidationError(f"{field} 必须是列表")
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if casefold:
            text = text.casefold()
        if not text:
            continue
        if len(text) > 50:
            raise SiteValidationError(f"{field} 的单项不能超过 50 字符")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > limit:
        raise SiteValidationError(f"{field} 最多 {limit} 项")
    return cleaned


def _clean_text(value, *, field: str, max_chars: int = _MAX_FIELD_CHARS) -> str:
    text = str(value or "").strip()
    if not text:
        raise SiteValidationError(f"{field} 不能为空")
    if len(text) > max_chars:
        raise SiteValidationError(f"{field} 不能超过 {max_chars} 字符")
    # 选择器会成为 argv 里的一项传给 agent-browser。虽说不经 shell（无注入面），
    # 但换行/空字节会让子进程参数变得不可读，属于应当直接拒绝的输入。
    if any(ch in text for ch in "\r\n\x00"):
        raise SiteValidationError(f"{field} 不能包含换行或空字节")
    return text


def validate_site(payload: dict) -> dict:
    """校验并规范化一个站点规格。不合法时抛 SiteValidationError。

    刻意「拒绝」而不是「自动纠正」：host 与 login_url 不同域时，
    自动取其一都可能让用户以为配对了其实没配对。安全相关的配置宁可报错。
    """
    if not isinstance(payload, dict):
        raise SiteValidationError("站点规格必须是对象")

    site_key = str(payload.get("site_key") or "").strip().casefold()
    if not _SITE_KEY_RE.fullmatch(site_key):
        raise SiteValidationError(
            "site_key 只能由小写字母、数字和连字符组成，且必须以字母或数字开头"
        )

    name = _clean_text(payload.get("name"), field="name", max_chars=100)

    host = str(payload.get("host") or "").strip().casefold()
    if not _HOST_RE.fullmatch(host):
        raise SiteValidationError("host 必须是合法域名（不能是 IP 地址）")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SiteValidationError("host 必须是域名，不能是 IP 地址")

    login_url = _clean_text(
        payload.get("login_url"), field="login_url", max_chars=_MAX_URL_CHARS
    )
    parsed = _parse_login_url(login_url)
    if parsed.hostname != host:
        # 这条是「自一致性」检查：api 下发给 browser 的规格里，
        # host 决定允许访问的域，login_url 决定打开哪个页面。
        # 两者不一致时，白名单就形同虚设。
        raise SiteValidationError(
            f"login_url 的域名（{parsed.hostname}）必须与 host（{host}）一致"
        )

    return {
        "site_key": site_key,
        "name": name,
        "aliases": _clean_list(
            payload.get("aliases"), field="aliases", limit=MAX_ALIASES
        ),
        "host": host,
        "login_url": login_url,
        "username_selector": _clean_text(
            payload.get("username_selector"), field="username_selector"
        ),
        "password_selector": _clean_text(
            payload.get("password_selector"), field="password_selector"
        ),
        "blocked_path_parts": _clean_list(
            payload.get("blocked_path_parts"),
            field="blocked_path_parts",
            limit=MAX_BLOCKED_PATH_PARTS,
            casefold=True,
        ),
        "requires_captcha": bool(payload.get("requires_captcha", True)),
        "enabled": bool(payload.get("enabled", True)),
    }


def _parse_login_url(login_url: str):
    parsed = urlparse(login_url)
    if parsed.scheme != "https":
        raise SiteValidationError("login_url 必须使用 https")
    try:
        port = parsed.port
    except ValueError as exc:
        # urlparse 对 "https://a.cn:abc/login" 这类端口会延迟到取 .port 时才抛
        # ValueError。不翻译的话会一路冒到接口层变成 500，而它其实是用户填错了。
        raise SiteValidationError("login_url 的端口号不合法") from exc
    if port not in (None, 443):
        raise SiteValidationError("login_url 只能使用 443 端口")
    if parsed.query or parsed.fragment:
        raise SiteValidationError("login_url 不能带查询参数或锚点")
    if not parsed.path or parsed.path == "/":
        raise SiteValidationError("login_url 必须包含具体路径")
    return parsed


def match_site(sites: list[dict], question: str) -> dict | None:
    """按别名在用户问题里做子串匹配，返回第一个命中的站点。

    保持改造前 `_match_login_target` 的语义（任一别名是问题的子串即命中），
    否则会出现「升级后同一个说法不再触发登录」这种静默行为变化。
    """
    for site in sites:
        for alias in site.get("aliases") or []:
            if alias and alias in question:
                return site
    return None


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sites (
        site_key           TEXT NOT NULL,
        owner_user_id      TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        name               TEXT NOT NULL,
        aliases            JSONB NOT NULL DEFAULT '[]'::jsonb,
        host               TEXT NOT NULL,
        login_url          TEXT NOT NULL,
        username_selector  TEXT NOT NULL,
        password_selector  TEXT NOT NULL,
        blocked_path_parts JSONB NOT NULL DEFAULT '[]'::jsonb,
        requires_captcha   BOOLEAN NOT NULL DEFAULT TRUE,
        enabled            BOOLEAN NOT NULL DEFAULT TRUE,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (owner_user_id, site_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sites_owner
    ON sites(owner_user_id, created_at)
    """,
)


def _serialize(row: dict) -> dict:
    return {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in row.items()
    }


class SiteStore:
    """站点注册表的读写入口。所有方法同步，FastAPI 线程池与脚本均适用。"""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.postgres_dsn.strip():
            raise RuntimeError("POSTGRES_DSN is required for the site registry")
        self._pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=1,
            max_size=max(2, min(settings.pg_pool_max, 8)),
            open=True,
            timeout=5,
            # 与 TaskStore 同因：row_factory 是【连接】属性，必须建在池这一层，
            # 否则会泄漏给下一个借到该连接的调用者（见 task_store.py 的注释）。
            kwargs={"row_factory": dict_row},
        )
        try:
            with self._pool.connection() as connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
        except Exception:
            self._pool.close()
            raise
        logger.info("站点注册表结构就绪")

    def close(self) -> None:
        self._pool.close()

    def list_for_owner(self, owner_user_id: str) -> list[dict]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sites
                WHERE owner_user_id = %s
                ORDER BY created_at
                """,
                (owner_user_id,),
            ).fetchall()
        return [_serialize(row) for row in rows]

    def get(self, owner_user_id: str, site_key: str) -> dict | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sites WHERE owner_user_id = %s AND site_key = %s",
                (owner_user_id, site_key),
            ).fetchone()
        return _serialize(row) if row else None

    def create(self, owner_user_id: str, payload: dict) -> dict:
        site = validate_site(payload)
        with self._pool.connection() as connection:
            count = connection.execute(
                "SELECT count(*) AS n FROM sites WHERE owner_user_id = %s",
                (owner_user_id,),
            ).fetchone()["n"]
            if count >= MAX_SITES_PER_OWNER:
                raise SiteValidationError(f"每个用户最多登记 {MAX_SITES_PER_OWNER} 个站点")
            try:
                row = connection.execute(
                    """
                    INSERT INTO sites(
                        owner_user_id, site_key, name, aliases, host, login_url,
                        username_selector, password_selector, blocked_path_parts,
                        requires_captcha, enabled
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        owner_user_id,
                        site["site_key"],
                        site["name"],
                        Jsonb(site["aliases"]),
                        site["host"],
                        site["login_url"],
                        site["username_selector"],
                        site["password_selector"],
                        Jsonb(site["blocked_path_parts"]),
                        site["requires_captcha"],
                        site["enabled"],
                    ),
                ).fetchone()
            except UniqueViolation as exc:
                raise SiteAlreadyExists(f"站点 {site['site_key']} 已存在") from exc
            # 与站点写入同事务（阶段 C ④）：要么都成，要么都不成。
            _record(connection, owner_user_id, ACTION_SITE_REGISTERED, _site_detail(row))
        logger.info("已登记站点 %s（owner=%s）", site["site_key"], owner_user_id)
        return _serialize(row)

    def update(self, owner_user_id: str, site_key: str, payload: dict) -> dict | None:
        """更新站点。**不允许改 site_key** —— 它是凭证 service_key 的锚点，
        改了会让既有凭证失联。需要换 key 时的正确做法是新增 + 删除。"""
        if str(payload.get("site_key") or site_key).casefold() != site_key:
            raise SiteValidationError("site_key 不可修改，请新增站点后再删除旧站点")
        merged = dict(payload)
        merged["site_key"] = site_key
        site = validate_site(merged)
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE sites SET
                    name = %s, aliases = %s, host = %s, login_url = %s,
                    username_selector = %s, password_selector = %s,
                    blocked_path_parts = %s, requires_captcha = %s, enabled = %s,
                    updated_at = now()
                WHERE owner_user_id = %s AND site_key = %s
                RETURNING *
                """,
                (
                    site["name"],
                    Jsonb(site["aliases"]),
                    site["host"],
                    site["login_url"],
                    site["username_selector"],
                    site["password_selector"],
                    Jsonb(site["blocked_path_parts"]),
                    site["requires_captcha"],
                    site["enabled"],
                    owner_user_id,
                    site_key,
                ),
            ).fetchone()
            # row 为 None = 没这个站点（或不属于你），那就什么都不记 ——
            # 时间线上不该出现一条「改了」但实际没改的记录。
            if row is not None:
                _record(connection, owner_user_id, ACTION_SITE_UPDATED, _site_detail(row))
        return _serialize(row) if row else None

    def delete(self, owner_user_id: str, site_key: str) -> bool:
        """删除站点。

        ★ 不级联删除凭证。删除站点后，引用它的凭证会变成「孤儿」，
        登录工具会返回「该站点未登记或已停用」——这是一个可读的失败，
        比悄悄删掉用户的账号记录安全得多。用户可以在凭证面板自行清理。
        """
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                DELETE FROM sites
                WHERE owner_user_id = %s AND site_key = %s
                RETURNING site_key, name, host
                """,
                (owner_user_id, site_key),
            ).fetchone()
            # 删除后 sites 行已经没了，所以明细只能取自 RETURNING ——
            # 这也正是摘要里必须能自己带出站点名的原因（不能靠 join 回 sites）。
            if row is not None:
                _record(
                    connection,
                    owner_user_id,
                    ACTION_SITE_REMOVED,
                    _site_detail({**row, "enabled": False}),
                )
        return row is not None

    def ensure_builtin_sites(self, owner_user_id: str) -> int:
        """播种内置站点（幂等：已存在则跳过，不覆盖用户改过的配置）。

        为什么需要：改造前 `_LOGIN_TARGETS` 是代码里的常量，升级后如果
        不播种，本机主人会突然发现「登录智慧教育平台」这句话不生效了 ——
        一个静默的行为回退。播种保证升级后行为不变。

        ★ 播种**不写审计**（create/update/delete 都写）。播种是升级带来的
        自动行为，不是用户或助手的操作；把它记进时间线，只会在每次升级时
        制造一条用户从没做过的记录 —— 那会让这页从「能核对」变成「要怀疑」。
        """
        seeded = 0
        with self._pool.connection() as connection:
            for site in BUILTIN_SITES:
                row = connection.execute(
                    """
                    INSERT INTO sites(
                        owner_user_id, site_key, name, aliases, host, login_url,
                        username_selector, password_selector, blocked_path_parts,
                        requires_captcha, enabled
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (owner_user_id, site_key) DO NOTHING
                    RETURNING site_key
                    """,
                    (
                        owner_user_id,
                        site["site_key"],
                        site["name"],
                        Jsonb(site["aliases"]),
                        site["host"],
                        site["login_url"],
                        site["username_selector"],
                        site["password_selector"],
                        Jsonb(site["blocked_path_parts"]),
                        site["requires_captcha"],
                    ),
                ).fetchone()
                if row is not None:
                    seeded += 1
        if seeded:
            logger.info("已播种 %d 个内置站点（owner=%s）", seeded, owner_user_id)
        return seeded


# 站点变更的动作取值。`operation_audit.action` **刻意没有 CHECK 约束**
# （见 approval_store 建表处的说明：取值会增长），所以这里新增取值不需要
# 任何迁移 —— 这正是当初不写 CHECK 想买到的东西。事件会出现在
# 「助手做过什么」时间线上（阶段 C ④）。
ACTION_SITE_REGISTERED = "site_registered"
ACTION_SITE_UPDATED = "site_updated"
ACTION_SITE_REMOVED = "site_removed"


def _record(connection, owner_user_id: str, action: str, detail: dict) -> None:
    """在**同一个事务里**写一条站点变更审计。

    ★ 与 approval_store.record_event() 的取舍刻意相反，两边都是对的：
      那边审计跟在一次**不可撤销的网页点击**之后，回滚已无意义，所以它
      吞掉异常、只记日志；这边审计与站点写入在同一个库、同一个事务里，
      一起回滚才是更安全的 —— 「改了什么」与「记下了改了什么」若能各自
      成功，时间线上就有了静默盲区，而阶段 C 的整条信任链正建立在
      「能核对」上。实践代价为零：这条 insert 没有任何会失败的约束
      （action 无 CHECK、detail 是 jsonb、owner 是已存在的外键）。

    detail 里固定带一个 `target` 键：时间线的摘要生成器（audit_timeline
    的 summarize）用它对操作事件统一取文案，站点事件走同一条路，
    前端与摘要都不需要为站点特判。
    """
    connection.execute(
        """
        INSERT INTO operation_audit(owner_user_id, kind, action, detail)
        VALUES (%s, 'site', %s, %s)
        """,
        (owner_user_id, action, Jsonb(detail)),
    )


def _site_detail(site: dict) -> dict:
    """站点规格 → 审计明细。只留可读且无敏感信息的字段。"""
    return {
        # target 是摘要文案里「操作了谁」的取值键，用显示名，
        # 显示名为空时退回 site_key（不会为空，它必填）。
        "target": site.get("name") or site.get("site_key") or "",
        "site_key": site.get("site_key") or "",
        "host": site.get("host") or "",
        "enabled": bool(site.get("enabled", True)),
    }


_store: SiteStore | None = None
_store_lock = threading.Lock()


def get_site_store() -> SiteStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SiteStore()
    return _store


def get_site_store_or_none() -> SiteStore | None:
    """拿不到就返回 None，让调用方降级 —— 站点表不可用不该让整个服务起不来。"""
    try:
        return get_site_store()
    except Exception as exc:
        logger.error("站点注册表不可用: %s", type(exc).__name__)
        return None


def close_site_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None
