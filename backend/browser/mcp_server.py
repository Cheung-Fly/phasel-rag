"""MCP browser tools backed by Vercel's agent-browser CLI.

The CLI owns Chromium and keeps one named browser session per owner.  This
module deliberately never logs command arguments: login passwords are passed
only to the child process and are not included in MCP responses or logs.

【站点规格从哪来】本模块曾经把站点信息写成常量（ALLOWED_HOST /
ALLOWED_LOGIN_URL / 两个选择器 / _BLOCKED_PATH_PARTS），加一个站点要改
两个容器的 Python 并重建镜像。阶段 C 起改为由 api 在每次开会话时**下发**整份
规格（见 `_validate_site_spec`），本模块只负责执行，不读数据库。

★ 代价必须说清楚：白名单的来源从「本模块自持」变成「api 下发」，强度少了一级。
补回来的两条是：
  1) `_validate_site_spec` 的自一致性检查 —— host 决定允许访问哪些域，
     login_url 决定打开哪个页面，两者不同域时白名单形同虚设；
  2) `_public_target()` —— DNS 解析后必须落在公网 IP，这才是真正挡 SSRF 的那道，
     与白名单的来源无关。
第 1 条刻意放在本侧而不是只放 api 侧：api 被攻破时它才有意义。
"""
import asyncio
import base64
import json
import os
import re
import subprocess
import ipaddress
import socket
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------- 站点规格
# 域名（不是 IP）：至少两段、末段是字母 TLD。与 app/site_store.py 的 _HOST_RE 同源。
# IP 字面量另行拒绝 —— 否则 169.254.169.254（云元数据）能靠「看起来像域名」蒙进来。
_HOST_RE = re.compile(
    r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)
_MAX_SELECTOR_CHARS = 300
_MAX_BLOCKED_PART_CHARS = 50
_MAX_BLOCKED_PARTS = 100


def _validate_site_spec(
    login_url: str,
    host: str,
    username_selector: str,
    password_selector: str,
    blocked_path_parts,
) -> dict | None:
    """校验 api 下发的站点规格，不合法返回 None（调用方回 target_blocked）。

    与 app/site_store.validate_site 是**两份独立实现**，故意不共用代码：
    跨容器共用等于把「第二道防线」变成「同一道防线调用两次」。
    两份都通过，才说明这份规格在两个信任域里都自洽。
    """
    host = str(host or "").strip().casefold()
    if not _HOST_RE.fullmatch(host):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None

    login_url = str(login_url or "").strip()
    if len(login_url) > 2048 or "\r" in login_url or "\n" in login_url:
        return None
    parsed = urlparse(login_url)
    if parsed.scheme != "https" or parsed.hostname != host:
        return None
    try:
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        # urlparse 对 "https://a.cn:abc/login" 会延迟到这里才抛，属于非法输入
        return None
    if parsed.query or parsed.fragment:
        return None

    selectors = []
    for value in (username_selector, password_selector):
        text = str(value or "").strip()
        if not text or len(text) > _MAX_SELECTOR_CHARS:
            return None
        # 选择器会成为 argv 里的一项。不经 shell（无注入面），但换行/空字节
        # 会让子进程参数不可读，直接拒绝。
        if any(ch in text for ch in "\r\n\x00"):
            return None
        selectors.append(text)

    if blocked_path_parts is None:
        parts: list[str] = []
    elif isinstance(blocked_path_parts, (list, tuple)):
        parts = []
        for item in blocked_path_parts:
            text = str(item).strip().casefold()
            if not text:
                continue
            if len(text) > _MAX_BLOCKED_PART_CHARS or len(parts) >= _MAX_BLOCKED_PARTS:
                return None
            if text not in parts:
                parts.append(text)
    else:
        return None

    return {
        "host": host,
        "login_url": login_url,
        "username_selector": selectors[0],
        "password_selector": selectors[1],
        "blocked_path_parts": parts,
    }


def _public_target(site_url: str, host: str) -> str | None:
    parsed = urlparse(site_url)
    if not _safe_url(site_url, host):
        return None
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    except OSError:
        return None
    public = []
    for address in addresses:
        try:
            value = ipaddress.ip_address(address)
        except ValueError:
            continue
        if value.version == 4 and value.is_global:
            public.append(address)
    return sorted(public)[0] if public else None

SESSION_TTL_SECONDS = 15 * 60
PENDING_CLICK_TTL_SECONDS = 90
LOGIN_FORM_TIMEOUT_MS = 20_000
LOGIN_OVERLAY_CLEAR_EXPRESSION = "!document.querySelector('.el-loading-mask, .el-loading-spinner')"
AGENT_BROWSER = os.getenv("AGENT_BROWSER_BIN", "agent-browser")
SCREENSHOT_DIR = Path(os.getenv("AGENT_BROWSER_SCREENSHOT_DIR", "/var/cache/agent-browser"))

_sessions = {}
_sessions_lock = asyncio.Lock()
_reaper_started = False
mcp = FastMCP("campus-browser", host="0.0.0.0", port=8766, streamable_http_path="/mcp", stateless_http=True, json_response=True)


def _session_name(owner: str) -> str:
    # agent-browser session names are shell/process identifiers, not user data.
    return "campus-" + re.sub(r"[^A-Za-z0-9_.-]", "-", owner)[:100]


class AgentBrowserError(RuntimeError):
    pass


class AgentBrowser:
    def __init__(self, owner: str):
        self.owner = owner
        self.name = _session_name(owner)

    async def run(self, *args: str, timeout: float = 30) -> str:
        command = [AGENT_BROWSER, "--session", self.name, *map(str, args)]
        try:
            result = await asyncio.to_thread(
                subprocess.run, command, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentBrowserError("agent-browser unavailable") from exc
        if result.returncode:
            raise AgentBrowserError("agent-browser command failed")
        return result.stdout.strip()

    async def json(self, *args: str) -> object:
        output = await self.run(*args, "--json")
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise AgentBrowserError("agent-browser returned invalid JSON") from exc


async def _type_into_login_field(browser: AgentBrowser, selector: str, value: str) -> None:
    await browser.run("click", selector)
    await browser.run("press", "Control+a")
    await browser.run("keyboard", "type", value)


async def _close_session(owner: str) -> None:
    async with _sessions_lock:
        session = _sessions.pop(owner, None)
    if session:
        async with session["operation_lock"]:
            try:
                await session["browser"].run("close")
            except AgentBrowserError:
                pass


async def _get_session(owner: str):
    async with _sessions_lock:
        session = _sessions.get(owner)
        if session and session["expires_at"] > time.monotonic():
            session["expires_at"] = time.monotonic() + SESSION_TTL_SECONDS
            return session
        if session:
            _sessions.pop(owner, None)
    if session:
        try:
            await session["browser"].run("close")
        except AgentBrowserError:
            pass
    return None


async def _reap_sessions():
    while True:
        await asyncio.sleep(30)
        async with _sessions_lock:
            expired = [owner for owner, session in _sessions.items() if session["expires_at"] <= time.monotonic()]
        for owner in expired:
            await _close_session(owner)


def _safe_url(url: str, host: str) -> bool:
    """允许访问的地址：https + 域名必须等于当前会话的站点 host + 端口 443。

    host 从模块常量改成了**参数** —— 每个会话带自己那份站点规格，
    不同会话可以指向不同站点，而白名单的判定逻辑只有这一处。
    """
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == host and parsed.port in (None, 443)


def _safe_path(path: str, blocked_path_parts) -> bool:
    parsed = urlparse(path)
    value = unquote(parsed.path or "/").casefold()
    return (
        not parsed.query
        and not parsed.fragment
        and not any(part in value for part in blocked_path_parts)
    )


def _site_of(session) -> dict:
    """会话里的站点规格。规格在开会话时校验并固定下来，之后不再变。"""
    return session["site"]


def _snapshot_elements(snapshot: object) -> list[dict]:
    if isinstance(snapshot, dict):
        raw_data = snapshot.get("data")
        if isinstance(raw_data, list):
            items = raw_data
            data = {}
        else:
            data = raw_data if isinstance(raw_data, dict) else snapshot
            items = None
        refs = data.get("refs") or {}
        if items is not None:
            pass
        elif isinstance(refs, dict):
            items = [
                dict(item, ref=f"@{ref}")
                for ref, item in refs.items()
                if isinstance(item, dict)
            ]
        else:
            items = data.get("elements") or data.get("interactive") or data.get("nodes") or []
    else:
        items = snapshot if isinstance(snapshot, list) else []
    result = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        ref = item.get("ref") or item.get("selector") or f"legacy-{index}"
        if not ref:
            continue
        role = item.get("role", "")
        name = item.get("name") or item.get("text") or item.get("value") or ""
        result.append({"index": len(result), "ref": ref, "tag": item.get("tag", ""), "role": role, "type": item.get("type", ""), "text": str(name)[:120], "aria_label": item.get("aria_label", ""), "title": item.get("title", ""), "href": item.get("href", ""), "target": item.get("target", "")})
    return result


def _click_target(element: dict) -> str:
    label = element.get("aria_label") or element.get("text") or element.get("title") or element.get("type") or "未命名控件"
    return f"{element.get('role') or element.get('tag') or '控件'}「{' '.join(str(label).split())[:100]}」"


def _fingerprint(element: dict) -> tuple:
    return tuple(element.get(key, "") for key in ("tag", "role", "type", "text", "aria_label", "title", "href", "target"))


_DOM_INTERACTIVE_SCRIPT = """(() => {
  const visible = (el) => {
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && style.pointerEvents !== 'none' && box.width > 0 && box.height > 0;
  };
  const selector = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.body && parts.length < 8) {
      let part = el.tagName.toLowerCase();
      if (el.id) {
        part += '#' + CSS.escape(el.id);
        parts.unshift(part);
        break;
      }
      const parent = el.parentElement;
      if (parent) {
        const siblings = [...parent.children].filter((node) => node.tagName === el.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(el) + 1})`;
      }
      parts.unshift(part);
      el = parent;
    }
    return parts.join(' > ');
  };
  const candidates = [...document.querySelectorAll(
    'a,button,[role="button"],[role="link"],[onclick],[tabindex],*'
  )];
  const result = [];
  const seen = new Set();
  for (const el of candidates) {
    if (!visible(el)) continue;
    const text = (el.innerText || el.getAttribute('aria-label')
      || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
    const style = getComputedStyle(el);
    const semantic = /^(A|BUTTON)$/.test(el.tagName)
      || el.getAttribute('role') === 'button'
      || el.getAttribute('role') === 'link'
      || el.hasAttribute('onclick')
      || el.hasAttribute('tabindex')
      || style.cursor === 'pointer';
    if (!semantic || !text) continue;
    const path = selector(el);
    if (!path || seen.has(path)) continue;
    seen.add(path);
    result.push({
      selector: path,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '',
      text: text.slice(0, 120),
      aria_label: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      href: el.href || '',
      target: el.target || ''
    });
    if (result.length >= 100) break;
  }
  return result;
})()"""


async def _interactive_elements(session):
    snapshot = await session["browser"].json("snapshot", "-i")
    elements = _snapshot_elements(snapshot)
    try:
        dom_elements = await session["browser"].json("eval", _DOM_INTERACTIVE_SCRIPT)
    except AgentBrowserError:
        dom_elements = []
    known = {(item.get("ref"), item.get("text"), item.get("href")) for item in elements}
    for item in _snapshot_elements(dom_elements):
        key = (item.get("ref"), item.get("text"), item.get("href"))
        if key not in known:
            elements.append(item)
            known.add(key)
    return [
        dict(item, index=index)
        for index, item in enumerate(elements[:100])
    ]


async def _page_summary(session):
    browser = session["browser"]
    site = _site_of(session)
    elements = await _interactive_elements(session)
    text = await browser.run("get", "text", "body")
    url = await browser.run("get", "url")
    title = await browser.run("get", "title")
    links = []
    for element in elements:
        href = element.get("href", "")
        if not href and element.get("role") == "link":
            try:
                href = await browser.run("get", "attr", element["ref"], "href")
            except AgentBrowserError:
                href = ""
        if href and _safe_url(href, site["host"]) and _safe_path(href, site["blocked_path_parts"]):
            element["href"] = href
            links.append({"index": len(links), "text": element.get("text", ""), "path": urlparse(href).path or "/"})
        if len(links) >= 40:
            break
    return {"status": "login_confirmed", "path": urlparse(url).path, "title": title[:200], "text": text[:6000], "links": links, "interactive_elements": elements, "interactive_truncated": len(elements) >= 60}


async def _snapshot(session):
    path = SCREENSHOT_DIR / f"{session['browser'].name}.jpg"
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await session["browser"].run("screenshot", str(path))
        image = base64.b64encode(path.read_bytes()).decode("ascii")
    except (AgentBrowserError, OSError):
        return {"status": "session_closed", "image": ""}
    return {"status": "login_confirmed" if session.get("verified") else "verification_pending", "image": image, "expires_in": max(0, int(session["expires_at"] - time.monotonic()))}


@mcp.tool()
async def start_login_session(
    owner_user_id: str,
    login_url: str,
    host: str,
    username: str,
    password: str,
    username_selector: str,
    password_selector: str,
    blocked_path_parts: list[str],
    site_key: str = "",
) -> str:
    """开一个隔离浏览器会话并自动填好登录表单。**不点击任何按钮**。

    站点规格由 api 下发（见模块头）。这里先校验规格自身是否自洽，
    再做 SSRF 检查，最后才开浏览器 —— 顺序不能颠倒：先开浏览器再校验，
    等于已经把一个未验证的地址交给了 Chromium。

    `site_key` 是**纯信息性**的：它会被记住并在控件清单里回传，供 api 在登记
    审批时标注「这一次点击属于哪个站点」（审计时间线要用）。因此它刻意**不参与**
    安全判定 —— 传了非法值最多是标签难看，不会放宽任何一条访问限制；
    默认空串也让旧调用方继续可用（阶段 C 的 W3 之前没有这个参数）。
    """
    global _reaper_started
    if not owner_user_id or len(owner_user_id) > 128:
        return "invalid_owner"
    spec = _validate_site_spec(
        login_url, host, username_selector, password_selector, blocked_path_parts
    )
    if spec is None:
        return "target_blocked"
    if _public_target(spec["login_url"], spec["host"]) is None:
        return "target_blocked"
    await _close_session(owner_user_id)
    browser = AgentBrowser(owner_user_id)
    try:
        await browser.run("open", "--allowed-domains", spec["host"], spec["login_url"])
        await browser.run("wait", spec["username_selector"], "--timeout", str(LOGIN_FORM_TIMEOUT_MS))
        await browser.run(
            "wait", "--fn", LOGIN_OVERLAY_CLEAR_EXPRESSION,
            "--timeout", str(LOGIN_FORM_TIMEOUT_MS),
        )
        await _type_into_login_field(browser, spec["username_selector"], username)
        await browser.run("wait", spec["password_selector"], "--timeout", str(LOGIN_FORM_TIMEOUT_MS))
        await browser.run(
            "wait", "--fn", LOGIN_OVERLAY_CLEAR_EXPRESSION,
            "--timeout", str(LOGIN_FORM_TIMEOUT_MS),
        )
        await _type_into_login_field(browser, spec["password_selector"], password)
        filled_username = await browser.run("get", "value", spec["username_selector"])
        filled_password = await browser.run("get", "value", spec["password_selector"])
        if filled_username != username or filled_password != password:
            raise AgentBrowserError("login fields were not filled")
    except AgentBrowserError:
        return "login_unconfirmed"
    session = {
        "browser": browser,
        # site_key 只作标签用，截断而不是拒绝：它是 api 自己生成的值，
        # 这里多一道校验只会多一种「配置看起来对但会话开不起来」的失败。
        "site": {**spec, "site_key": str(site_key or "").strip()[:64]},
        "operation_lock": asyncio.Lock(),
        "expires_at": time.monotonic() + SESSION_TTL_SECONDS,
    }
    async with _sessions_lock:
        _sessions[owner_user_id] = session
    if not _reaper_started:
        asyncio.create_task(_reap_sessions())
        _reaper_started = True
    return "verification_pending"


@mcp.tool()
async def confirm_login_session(owner_user_id: str) -> str:
    session = await _get_session(owner_user_id)
    if not session:
        return "session_expired"
    session["verified"] = True
    return "login_confirmed"


@mcp.tool()
async def get_login_session(owner_user_id: str) -> str:
    session = await _get_session(owner_user_id)
    return json.dumps(await _snapshot(session), ensure_ascii=False) if session else json.dumps({"status": "session_expired", "image": ""})


@mcp.tool()
async def inspect_login_session(owner_user_id: str) -> str:
    session = await _get_session(owner_user_id)
    if not session:
        return json.dumps({"status": "session_expired"})
    if not session.get("verified"):
        return json.dumps({"status": "session_not_verified"})
    async with session["operation_lock"]:
        try:
            return json.dumps(await _page_summary(session), ensure_ascii=False)
        except AgentBrowserError:
            return json.dumps({"status": "session_closed"})


@mcp.tool()
async def open_login_session_link(owner_user_id: str, link_index: int) -> str:
    session = await _get_session(owner_user_id)
    if not session or not session.get("verified"):
        return json.dumps({"status": "session_not_verified" if session else "session_expired"})
    async with session["operation_lock"]:
        summary = await _page_summary(session)
        links = summary["links"]
        if link_index < 0 or link_index >= len(links):
            return json.dumps({"status": "invalid_link"})
        # urljoin 的基准是**本会话站点的登录地址**，不是某个全局常量 ——
        # links 里的 path 已经在 _page_summary 里过了同域校验。
        await session["browser"].run(
            "open", urljoin(_site_of(session)["login_url"], links[link_index]["path"])
        )
        return json.dumps(await _page_summary(session), ensure_ascii=False)


@mcp.tool()
async def go_back_login_session(owner_user_id: str) -> str:
    session = await _get_session(owner_user_id)
    if not session or not session.get("verified"):
        return json.dumps({"status": "session_not_verified" if session else "session_expired"})
    async with session["operation_lock"]:
        try:
            await session["browser"].run("back")
            current_url = await session["browser"].run("get", "url")
            if not _safe_url(current_url, _site_of(session)["host"]):
                return json.dumps({"status": "target_blocked"})
            return json.dumps(await _page_summary(session), ensure_ascii=False)
        except AgentBrowserError:
            return json.dumps({"status": "navigation_failed"})


@mcp.tool()
async def click_login_session_element(owner_user_id: str, element_index: int) -> str:
    """登记一次「待点击」并返回确认信息。**本工具本身绝不点击任何东西。**

    ★ 这是「未批准时零外部副作用」的第一半：选择控件的动作只产生一条
    `pending_click`（进程内、90 秒 TTL），不碰页面。真正的点击在
    `confirm_pending_login_session_click` 里，而那个工具在阶段 C 起
    **只允许由 api 的 `POST /api/approvals/{id}/execute` 在 PG 校验通过后调用**。
    """
    session = await _get_session(owner_user_id)
    if not session or not session.get("verified"):
        return json.dumps({"status": "session_not_verified" if session else "session_expired"})
    async with session["operation_lock"]:
        elements = await _interactive_elements(session)
        if element_index < 0 or element_index >= len(elements):
            return json.dumps({"status": "invalid_element"})
        element = elements[element_index]
        target = _click_target(element)
        fingerprint = _fingerprint(element)
        session["pending_click"] = {"target": target, "fingerprint": fingerprint, "expires_at": time.monotonic() + PENDING_CLICK_TTL_SECONDS, "ref": element["ref"]}
        return json.dumps(
            {
                "status": "confirmation_required",
                "target": target,
                # fingerprint 与 site_key 都交给 api 记进审批记录：
                # 前者让「批准时看到的东西」与「执行时点的东西」可对账，
                # 后者让审计时间线能说出「在哪个站点上」。
                "fingerprint": fingerprint,
                "site_key": _site_of(session).get("site_key", ""),
                "confirmation_text": f"确认点击：{target}",
            },
            ensure_ascii=False,
        )


@mcp.tool()
async def confirm_pending_login_session_click(owner_user_id: str, target: str) -> str:
    """★ 全项目**唯一**会真的点击网页的工具，也是审批闸门的落点。

    两道关：
      1. api 侧（第一道）—— `POST /api/approvals/{id}/execute` 用 PG 里的
         原子状态迁移（approved → executing）确认「这条操作被批准过、且没被消费过」。
         未批准、已消费、已过期都会在那一步被挡掉，本工具根本不会被调用。
      2. 本工具自己（第二道）—— `pending_click` 的 90 秒 TTL + 确认文本精确匹配。
         它不依赖 api 的判断，所以即使 api 侧逻辑出错，这里仍要求一次真正的确认。

    两道刻意独立：纵深防御的意义在于两处不会同时错。
    """
    session = await _get_session(owner_user_id)
    if not session or not session.get("verified"):
        return json.dumps({"status": "session_not_verified" if session else "session_expired"})
    async with session["operation_lock"]:
        pending = session.get("pending_click")
        if not pending:
            return json.dumps({"status": "no_pending_click"})
        if pending["expires_at"] <= time.monotonic():
            session.pop("pending_click", None)
            return json.dumps({"status": "pending_click_expired"})
        if " ".join(target.split()) != " ".join(pending["target"].split()):
            return json.dumps({"status": "confirmation_mismatch"})
        session.pop("pending_click", None)
        try:
            # The ref belongs to the snapshot that produced the confirmation.
            # Taking another snapshot here can renumber dynamic-page refs and
            # incorrectly reject the user's exact confirmation.
            await session["browser"].run("click", pending["ref"])
            return json.dumps({"status": "clicked", "page": await _page_summary(session)}, ensure_ascii=False)
        except AgentBrowserError:
            return json.dumps({"status": "target_changed"})


@mcp.tool()
async def interact_login_session(owner_user_id: str, action: str, x: float = -1, y: float = -1, key: str = "") -> str:
    session = await _get_session(owner_user_id)
    if not session:
        return "session_expired"
    if action in {"move", "down", "up", "click"}:
        if x < 0 or y < 0 or x >= 1280 or y >= 800:
            return "invalid_coordinates"
        coordinate_x = str(round(x))
        coordinate_y = str(round(y))
        if action == "move":
            args = ["mouse", "move", coordinate_x, coordinate_y]
        elif action == "click":
            args = ["mouse", "move", coordinate_x, coordinate_y]
            await session["browser"].run(*args)
            args = ["mouse", "down"]
            await session["browser"].run(*args)
            args = ["mouse", "up"]
        else:
            await session["browser"].run("mouse", "move", coordinate_x, coordinate_y)
            args = ["mouse", action]
    elif action == "press":
        allowed = (len(key) == 1 and key.isascii() and key.isprintable()) or key in {"Enter", "Tab", "Backspace", "Delete", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"}
        if not allowed:
            return "key_not_allowed"
        args = ["press", key]
    else:
        return "action_not_allowed"
    try:
        await session["browser"].run(*args)
        return "ok"
    except AgentBrowserError:
        return "session_closed"


@mcp.tool()
async def close_login_session(owner_user_id: str) -> str:
    await _close_session(owner_user_id)
    return "closed"
