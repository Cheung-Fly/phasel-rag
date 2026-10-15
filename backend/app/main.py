"""FastAPI 入口：阶段一对外接口。

包含：
  - 文档管理：上传 / 列表 / 删除
  - 问答：普通（一次性返回）+ 流式（SSE 逐字返回）
  - 记忆：查询 / 清空

存储拓扑（2026-10-07 起）：
    本进程只负责业务逻辑，三种数据各自独立成服务 ——
    向量 → 独立 Chroma 容器；会话存档 → PostgreSQL（真相源）；
    会话热缓存 → Redis。本进程挂了，数据一条都不会丢。

前端拓扑（2026-10-07 起）：
    ★ 本服务不再托管前端，一个 HTML/CSS/JS 都不发。
    前端是独立的 Vue3 + Vite 工程（./frontend），生产环境由 nginx 容器
    托管构建产物并把 /api、/health 反代回本服务。三点好处：
      1) 发布解耦：换前端不必重建/重启 API 镜像，反过来 API 升级也不
         影响静态页面能否打开（哪怕 API 挂了，页面仍能加载并显示错误）；
      2) 职责单一：本服务只处理接口，静态文件的缓存头、gzip、TLS
         交给擅长做这些的 nginx；
      3) 无同源妥协：浏览器访问的一直是 nginx 那一个源，
         不需要为了让前端调接口而放开 CORS。
    因此导入本模块时不会再去检查任何前端目录、也不会挂载 StaticFiles。
"""
import json
import asyncio
import base64
import ipaddress
import logging
import threading
import uuid
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from . import rag
from .browser_agent import answer_browser_task
from .approval_store import (
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    close_approval_store,
    get_approval_store,
    get_approval_store_or_none,
)
from .audit_timeline import (
    MAX_LIMIT as AUDIT_MAX_LIMIT,
    close_audit_timeline,
    get_audit_timeline,
    get_audit_timeline_or_none,
    make_cursor,
    parse_cursor,
)
from .auth_store import (
    AuthenticatedUser,
    close_auth_store,
    get_auth_store,
    login_attempt_key,
    rate_limit_retry_after,
    session_cookie_name,
    session_cookie_settings,
)
from .config import get_settings
from .memory import get_memory
from .observability import CircuitBreaker, RuntimeMetrics, SlidingWindowRateLimiter
from .site_store import (
    SiteAlreadyExists,
    SiteValidationError,
    close_site_store,
    get_site_store,
    get_site_store_or_none,
    match_site,
)
from .task_store import close_task_store, get_task_store, get_task_store_or_none

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Phase 1 RAG API", version="0.2.0")
_settings = get_settings()
metrics = RuntimeMetrics()
request_limiter = SlidingWindowRateLimiter(
    _settings.api_rate_limit, _settings.api_rate_window_seconds
)
browser_breaker = CircuitBreaker(
    _settings.browser_circuit_failure_threshold,
    _settings.browser_circuit_recovery_seconds,
)

# CORS 白名单来自环境变量 CORS_ORIGINS（逗号分隔），默认留空。
# 留空是常态且是正确配置：开发期由 Vite dev server 把 /api 代理过来，
# 生产期由 nginx 把 /api 反代过来 —— 两种情况下浏览器看到的都是同源，
# 压根不会发起跨域请求。只有「前端与后端之间没有任何代理」时
# （例如前端单独部署到另一个域名）才需要在这里填白名单。
# 安全前提：原先的 allow_origins=["*"] 会让任意网站借访客浏览器调本 API，
# 对外暴露前必须收紧到具体域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def prevent_sensitive_response_caching(request: Request, call_next):
    started = time.monotonic()
    client = request.client.host if request.client else "unknown"
    if request.url.path not in {"/health", "/metrics"}:
        allowed, retry_after = await request_limiter.allow(client)
        if not allowed:
            response = Response("请求过于频繁，请稍后重试", status_code=429)
            response.headers["Retry-After"] = str(retry_after)
            metrics.request_finished(request.method, request.url.path, 429, time.monotonic() - started)
            return response
    metrics.request_started()
    try:
        response = await call_next(request)
    except Exception:
        metrics.request_finished(request.method, request.url.path, 500, time.monotonic() - started)
        logger.exception("Unhandled request error method=%s path=%s", request.method, request.url.path)
        raise
    metrics.request_finished(request.method, request.url.path, response.status_code, time.monotonic() - started)
    if request.url.path.startswith(
        (
            "/api/auth/",
            "/api/credentials",
            "/api/browser-session",
            "/api/tasks",
            "/api/sites",
            "/api/approvals",
            "/api/audit",
        )
    ):
        # /api/tasks 也加进来：任务状态是「此刻的进度」，被浏览器或中间层缓存
        # 下来就会显示一个永远不动的进度条，比不显示更让人困惑。
        # /api/sites 同理：它是「此刻生效的配置」，刚新增的站点如果被缓存挡住，
        # 用户会以为「加了但没生效」。
        # /api/approvals 更要紧：审批有 90 秒时效且一次有效，一份被缓存的
        # 「待批准」列表会让用户对着一条早就过期的记录点下去。
        # /api/audit 是「助手做过什么」的证据页 —— 它存在的意义就是能被信任，
        # 一份被缓存的副本会让用户拿旧记录去核对刚发生的事。
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    return Response(metrics.prometheus(), media_type="text/plain; version=0.0.4")


# 上传上限：第一道粗筛，只拦「文件体积」。
# 真正决定 embedding 成本与容器负载的是「切片数」，由 rag.ingest 里的两道闸门负责
# （见 config.py 中 ingest_max_chars / ingest_max_chunks 的说明）。
MAX_UPLOAD_BYTES = _settings.max_upload_mb * 1024 * 1024


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="", max_length=64)
    assistant_mode: Literal["school", "student"] = "school"


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=1024)


class CredentialRequest(BaseModel):
    service_key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    site_url: str = Field(min_length=1, max_length=2048)
    full_name: str = Field(min_length=1, max_length=100)
    student_id: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("site_url")
    @classmethod
    def validate_site_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("校园网站地址格式不正确") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("校园网站地址必须是无凭据、无查询参数的 HTTPS 地址")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("校园网站地址不能指向非公网 IP")
        return value.strip()


class SiteRequest(BaseModel):
    """站点规格（阶段 C ①）。

    ★ 这里的长度上限刻意**放宽**到比 site_store 的规则更松：真正的校验只有
    `site_store.validate_site()` 一处。若 pydantic 先拦下常见错误，前端会收到
    另一种形状的错误体（列表而非字符串），同一种「填错了」要在两处分别处理。
    所以本模型只挡「畸形的大 payload」，语义错误一律交给 validate_site 用它
    自己那句中文报错。
    """

    site_key: str = Field(default="", max_length=200)
    name: str = Field(default="", max_length=400)
    aliases: list[str] = Field(default_factory=list, max_length=200)
    host: str = Field(default="", max_length=512)
    login_url: str = Field(default="", max_length=8192)
    username_selector: str = Field(default="", max_length=1000)
    password_selector: str = Field(default="", max_length=1000)
    blocked_path_parts: list[str] = Field(default_factory=list, max_length=400)
    requires_captcha: bool = True
    enabled: bool = True


class BrowserActionRequest(BaseModel):
    action: str = Field(pattern=r"^(move|down|up|click|press)$")
    x: float = Field(default=-1, ge=-1, le=1280)
    y: float = Field(default=-1, ge=-1, le=800)
    key: str = Field(default="", max_length=16)


def _get_auth_store():
    try:
        return get_auth_store()
    except Exception as exc:
        logger.error("Authentication store unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "身份认证或凭证存储暂不可用") from exc


def _auth_mode() -> str:
    """运行时可读的认证模式。

    为什么不用模块级的 _settings 常量：集成测试需要在同一个进程里把模式
    钉死成 session 来验证多用户隔离，而部署时又要靠 .env 切成 local。
    读 get_settings() 才可被 patch；它本身有 lru_cache，重复调用零成本。
    """
    return get_settings().auth_mode


# ★ 缓存变量不能叫 _local_owner：同名函数定义会在模块导入时把全局名重绑成
#   函数对象，于是 `if _local_owner is None` 永假、函数返回的是它自己。
#   这个错误 py_compile 查不出来，只会在启动时以 AttributeError 的形式炸出来。
_local_owner_cache: AuthenticatedUser | None = None
_local_owner_lock = threading.Lock()


def _local_owner() -> AuthenticatedUser:
    """单用户本地模式下的固定身份（进程内缓存，幂等）。"""
    global _local_owner_cache
    if _local_owner_cache is None:
        with _local_owner_lock:
            if _local_owner_cache is None:
                try:
                    _local_owner_cache = _get_auth_store().ensure_local_owner(
                        get_settings().local_owner_username
                    )
                except HTTPException:
                    raise
                except Exception as exc:
                    logger.error(
                        "Local owner initialization failed: %s", type(exc).__name__
                    )
                    raise HTTPException(503, "本地身份初始化失败") from exc
    return _local_owner_cache


def _current_user(request: Request) -> AuthenticatedUser:
    if _auth_mode() == "local":
        # 单用户本地模式：不存在「未登录」这个状态，身份由 .env 决定。
        # ★ 关键：这里仍然返回一个真实的 AuthenticatedUser，而不是把 user_id
        #   抽掉。下游所有 owner_user_id 作用域（文档 / 会话 / 凭证）一行都不用改，
        #   数据也就一条都不用迁 —— 这是本次「去登录」能做到零迁移的原因。
        return _local_owner()
    try:
        user = _get_auth_store().resolve_session(
            request.cookies.get(session_cookie_name())
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Session lookup failed: %s", type(exc).__name__)
        raise HTTPException(503, "身份认证服务暂不可用") from exc
    if user is None:
        raise HTTPException(401, "请先登录个人助手")
    return user


BROWSER_MCP_URL = "http://browser:8766/mcp"


def _match_login_target(question: str, owner_user_id: str) -> dict | None:
    """在用户问题里按别名匹配已登记的站点。

    ★ 为什么每次都要查库、不做进程内缓存：站点面板刚新增一个站点，
    用户下一句就会说「登录 XX」。带 TTL 的缓存会让刚建的站点在那几秒里
    「看起来没生效」，而这正是新用户最容易踩的体验坑 —— 排查成本远大于
    一次对 50 行小表的 SELECT。站点表由 (owner_user_id, created_at) 索引，
    查询代价可以忽略。
    """
    store = get_site_store_or_none()
    if store is None:
        return None
    try:
        return match_site(store.list_for_owner(owner_user_id), question)
    except Exception as exc:
        # 站点表不可用不该让整个问答挂掉：退化成「没有登录意图」，
        # 后续的浏览器任务会以「尚无会话，请先登录」如实回复。
        logger.error("Site registry lookup failed: %s", type(exc).__name__)
        return None


def _requested_login_target(question: str, user: AuthenticatedUser) -> dict | None:
    if "登录" not in question or any(
        phrase in question for phrase in ("不要登录", "别登录")
    ):
        return None
    return _match_login_target(question, user.user_id)


# ★ 这里曾经有 _requested_open_target / _requested_browser_task 两张关键词表，
#   用来猜「这句话是不是网页任务」。实测证明它不可用：真实对话里
#     「查看大学生就业指导」→ 不在"页面/平台/课表…"词表内 → 被判定为非网页任务
#     「去作答第三节课」    → 同上
#   于是请求根本没进浏览器智能体，用户只收到一句兜底话术，表现为"点了没反应"。
#   现在改为：个人助手模式下，凡不是登录请求的，一律交给浏览器智能体。
#   它的开场检查会自己给出准确引导（无会话 → 提示先登录；未验证 → 提示先验证），
#   既不会静默吞掉请求，也不需要维护一张永远追不上口语表达的词表。
#   "要不要登录"仍然保持确定性判断、不经过模型 —— 这条安全边界没有改变。


def _new_session_id(mode: str) -> str:
    return f"{mode}_{uuid.uuid4().hex[:16]}"


def _validate_session_mode(session_id: str, mode: str) -> None:
    if session_id.startswith("student_") != (mode == "student"):
        raise HTTPException(404, "未找到该会话")


async def _call_browser_mcp(browser_url: str, tool_name: str, arguments: dict) -> str:
    if not browser_breaker.allow():
        raise RuntimeError("Browser MCP circuit is open")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        async with streamablehttp_client(browser_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
        if result.isError:
            raise RuntimeError("Browser MCP tool returned an error")
        text_results = [item.text for item in result.content if hasattr(item, "text")]
        if len(text_results) != 1:
            raise RuntimeError("Browser MCP tool returned an invalid result")
        browser_breaker.success()
        return text_results[0]
    except Exception:
        browser_breaker.failure()
        raise


def _execute_login_tool(
    user: AuthenticatedUser,
    target: dict,
) -> tuple[str, bool]:
    """按登记好的站点规格启动一次自动登录（只填表，不点击）。"""
    site_name = str(target.get("name") or target.get("site_key") or "该站点")

    # 停用站点直接拒绝，连凭证都不必查 —— 停用的语义就是「助手不要碰它」。
    if not target.get("enabled", True):
        return f"站点「{site_name}」已停用，本次没有尝试登录。", False

    try:
        auth_store = _get_auth_store()
        matches = [
            item
            for item in auth_store.list_credentials(user.user_id)
            if item["service_key"] == target["site_key"]
        ]
        if not matches:
            return f"当前个人账号尚未登记{site_name}的登录凭证。", False
        if len(matches) != 1:
            return "该平台登记了多个账号，为避免选错，本次没有尝试登录。", False
        # ★ 全项目唯一需要口令明文的调用点：它要把口令填进隔离浏览器。
        #   因此必须显式 reveal —— 这个动作会被记入凭证审计（action="reveal"），
        #   审计时间线上能看出「某次自动登录把口令取了出来」。
        credential = auth_store.get_credential(
            user.user_id,
            matches[0]["credential_id"],
            reveal_password=True,
        )
    except HTTPException:
        return "个人凭证库暂不可用，本次没有尝试登录。", False
    except Exception as exc:
        logger.error("Browser login credential lookup failed: %s", type(exc).__name__)
        return "个人凭证库暂不可用，本次没有尝试登录。", False

    if credential is None or credential["site_url"] != target["login_url"]:
        # 凭证自己记的地址与站点规格必须一致。不一致说明用户改过站点配置
        # 却没更新凭证 —— 这时自动纠正（取其一）会让「白名单」变成一句空话。
        return "已登记的网站地址与站点配置不匹配，本次没有尝试登录。", False

    if not credential.get("password_revealed"):
        # 防御：万一将来有人改了上面的调用而漏传 reveal_password，
        # 这里必须安全失败 —— 绝不能把 "********" 当成口令提交到登录页，
        # 那会变成「用错误口令反复尝试登录」，既无意义又可能触发平台风控。
        logger.error("Credential lookup returned a masked password for browser login")
        return "个人凭证库读取异常，本次没有尝试登录。", False

    try:
        status = asyncio.run(
            _call_browser_mcp(
                BROWSER_MCP_URL,
                "start_login_session",
                {
                    "owner_user_id": user.user_id,
                    # 站点规格整份下发。browser 侧会**再校验一遍**自洽性
                    # （见 browser/mcp_server._validate_site_spec），两份实现相互独立。
                    "login_url": target["login_url"],
                    "host": target["host"],
                    "username_selector": target["username_selector"],
                    "password_selector": target["password_selector"],
                    "blocked_path_parts": target["blocked_path_parts"],
                    # site_key 是纯信息性的：browser 记下它、在控件清单里回传，
                    # 于是审批记录能说出「这一次点击属于哪个站点」，审计时间线要用。
                    # （漏传的后果不是功能坏掉，而是审计里所有点击都归到空站点 ——
                    #   这一条是被 .verify-w3-e2e.py 的端到端取证抓出来的。）
                    "site_key": target["site_key"],
                    "username": credential["account"],
                    "password": credential["password"],
                },
            )
        )
    except Exception as exc:
        logger.error("Browser MCP invocation failed: %s", type(exc).__name__)
        return "自动登录工具暂不可用，本次没有完成登录。", False

    messages = {
        "verification_pending": (
            f"已在助手内打开{site_name}的隔离浏览器，并确认账号和密码已自动填入"
            "（密码不会显示在聊天中）。请检查验证窗口，完成人工验证码后自行点击登录。"
        ),
        "login_confirmed": "登录页似乎已验证；请在助手内确认登录状态。",
        "credentials_rejected": "平台未接受已登记的账号凭证，请检查个人凭证库中的记录后重试。",
        "credentials_not_filled": "平台登录页字段未能确认自动填入，未开始登录。请在助手内检查登录页面，必要时手动填写后继续。",
        "target_blocked": "目标地址未通过安全检查，本次没有尝试登录。",
        "login_unconfirmed": "无法确认登录页面是否已正常打开或完成填写；未执行登录或其他操作。",
    }
    if status == "verification_pending":
        return messages[status], True
    return messages.get(status, "自动登录工具返回了无法识别的结果，本次未能确认登录状态。"), False


async def _browser_tool(user: AuthenticatedUser, name: str, arguments: dict) -> str:
    try:
        return await _call_browser_mcp(
            BROWSER_MCP_URL,
            name,
            {"owner_user_id": user.user_id, **arguments},
        )
    except Exception as exc:
        logger.error("Browser MCP session operation failed: %s", type(exc).__name__)
        raise HTTPException(503, "浏览器会话暂不可用") from exc


def _answer_browser_task(
    question: str,
    user: AuthenticatedUser,
    session_id: str = "",
) -> dict:
    """走一次浏览器任务，并把「待批准的点击」登记成审批记录。

    阶段 C ② 的关键一步：浏览器智能体选中一个控件时**不会点击**，只回一份
    pending_click。这里把它落成 PG 里的一条 pending 审批，返回给前端渲染
    「批准并执行」。没有这一步，那条 pending_click 就只是浏览器内存里一个
    90 秒后消失的状态，既批准不了也审计不了。
    """
    async def call_for_user(owner_user_id: str, name: str, arguments: dict) -> str:
        if owner_user_id != user.user_id:
            raise RuntimeError("Browser task attempted to cross user boundary")
        return await _browser_tool(user, name, arguments)

    task = answer_browser_task(question, user.user_id, call_for_user)
    task["approval"] = None

    pending = task.get("pending_click") or {}
    if not pending.get("target"):
        return task

    store = get_approval_store_or_none()
    if store is None:
        # 闸门不可用 ⇒ 不登记审批，也就没有一键批准按钮。
        # ★ 刻意**不**退化成「直接点」：闸门挂了不等于可以在没有批准的情况下点击。
        #   宁可让用户暂时用不了这个功能，也不要让「安全机制坏了」变成「安全机制没了」。
        logger.error("审批闸门不可用，本次待点击控件未被登记")
        task["answer"] += "\n\n（审批闸门暂时不可用，本次没有登记可批准的操作。）"
        return task

    try:
        task["approval"] = store.create(
            user.user_id,
            target=pending["target"],
            site_key=pending.get("site_key", ""),
            session_id=session_id,
            action="click_element",
            fingerprint=pending.get("fingerprint", ""),
            detail={"question": question[:200]},
        )
    except ValueError as exc:
        # 例如「待审批的操作过多」。这是可读的失败，不该变成 500 ——
        # 用户需要知道的是「先去处理掉几条旧的」。
        logger.error("审批登记被拒绝: %s", exc)
        task["answer"] += f"\n\n（{exc}）"
    except Exception as exc:
        logger.error("审批登记失败: %s", type(exc).__name__)
        task["answer"] += "\n\n（审批登记失败，本次没有可批准的操作。）"
    return task


# ---------------------------------------------------------------- 基础
def _probe_chroma(s) -> str:
    """实时探测独立的 Chroma 服务。

    为什么要主动探一次向量库：vectorstore 是懒加载的 —— 只有真的上传或检索时
    才会建立连接。也就是说，主机名写错这类配置问题在"第一次使用"之前完全静默，
    /health 却一直报 ok。这里用 2 秒超时探一次，把问题暴露在健康检查里。
    """
    import urllib.request

    url = f"http://{s.chroma_host}:{s.chroma_port}/api/v2/heartbeat"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return "ok" if resp.status == 200 else f"http {resp.status}"
    except Exception as exc:
        return f"unreachable ({type(exc).__name__})"


def _task_queue_health() -> dict:
    """任务队列与后台执行器的状态。

    为什么要暴露「在线 worker」：任务入队后由 worker 执行，worker 没起来时
    上传会成功（202）却永远停在排队中。这是异步设计最常见的故障态，而且
    从界面上看和「还在处理」几乎一模一样 —— 必须能从 /health 一眼区分开。
    """
    store = get_task_store_or_none()
    if store is None:
        return {"backend": "unavailable", "online_workers": [], "worker_online": False}
    try:
        workers = store.active_workers(get_settings().task_lease_seconds)
    except Exception as exc:
        logger.warning("worker 心跳读取失败: %s", type(exc).__name__)
        return {"backend": "postgres", "online_workers": [], "worker_online": False}
    return {
        "backend": "postgres",
        "online_workers": workers,
        "worker_online": len(workers) > 0,
    }


@app.get("/health", tags=["基础"], summary="健康检查")
def health() -> dict:
    s = get_settings()
    chroma_status = _probe_chroma(s)
    return {
        # 向量库不可达时降级为 degraded：API 进程本身还能响应，
        # 但知识库功能已经不可用，不该报 ok。
        "status": "ok" if chroma_status == "ok" else "degraded",
        "llm_provider": s.llm_provider,
        "llm_model": s.llm_model,
        "embedding_model": s.embedding_model,
        # 由 MemoryStore 自报【运行态】而非读配置：原先 health 直接返回
        # 配置里有没有填 REDIS_URL，结果是"填了但 redis 包没装"时照样报 redis，
        # 属于谎报。现在只有真的 ping 通才会出现在这里。
        "memory_backend": get_memory().backend_summary(),
        "vector_store": f"chroma://{s.chroma_host}:{s.chroma_port}",
        "chroma_status": chroma_status,
        # 前端不再写死「最大 20MB」这类数字：上限是会随机型调整的配置，
        # 写死在前端就会出现「界面说 20MB、后端其实收 50MB」的假信息。
        # 这里把真实生效值暴露出来，前端读它来显示与预校验。
        "max_upload_mb": s.max_upload_mb,
        "ingest_max_chunks": s.ingest_max_chunks,
        # 任务队列后端 + 执行器存活情况（2026-10-14 阶段 B）
        "task_queue": _task_queue_health(),
    }


@app.on_event("shutdown")
def close_persistence_pools() -> None:
    close_auth_store()
    close_task_store()
    close_site_store()
    close_approval_store()
    close_audit_timeline()


@app.on_event("startup")
def initialize_owner_scoping() -> None:
    get_auth_store()
    if _auth_mode() == "local":
        # 启动期就把本地身份解析并落库，而不是等第一个请求。
        # 理由：LOCAL_OWNER_USERNAME 写错、或库不可用，都属于「配置事故」，
        # 应该在启动日志里立刻可见；拖到用户第一次提问才报 503，
        # 现象会是「页面能打开但什么都查不到」，排查方向完全被带偏。
        owner = _local_owner()
        logger.info(
            "认证模式 local（免登录）：本机主人身份 = %s (user_id=%s)",
            owner.username,
            owner.user_id,
        )
    # 任务队列表结构由建表语句里的 IF NOT EXISTS 保证幂等，这里主动跑一次，
    # 让「表没建出来 / PG 不可达」这类问题在启动日志里就暴露，而不是等
    # 用户上传时才发现。
    if get_task_store_or_none() is None:
        logger.error(
            "后台任务队列初始化失败：上传将退回同步入库（功能可用，但请求会长时间挂住）"
        )
    else:
        logger.info("后台任务队列已就绪（PostgreSQL）；执行器见 rag-worker 服务")
    # 站点注册表（阶段 C ①）。同样主动跑一次，让建表 / 连接问题在启动日志暴露。
    site_store = get_site_store_or_none()
    if site_store is None:
        logger.error(
            "站点注册表初始化失败：站点面板不可用，且「登录 XX」不会再触发自动登录"
        )
    else:
        logger.info("站点注册表已就绪（PostgreSQL）")
        if _auth_mode() == "local":
            # 升级不改变行为：把改造前的硬编码站点播种给本机主人（幂等，不覆盖改动）。
            # ★ 只在 local 模式播种。多用户部署从空注册表起步 —— 内置站点是
            #   「本项目单用户形态」的行为快照，不该强加给别人的部署；那边由用户在
            #   站点面板自行新增（这本来就是本次改造要买到的东西）。
            try:
                count = site_store.ensure_builtin_sites(_local_owner().user_id)
            except Exception as exc:
                logger.error("内置站点播种失败: %s", type(exc).__name__)
            else:
                if count:
                    logger.info("已播种 %d 个内置站点", count)
    # 操作审批闸门（阶段 C ②）。同样主动跑一次：建表 / 连接问题必须在启动
    # 日志里现形，而不是等用户点了「批准并执行」才发现受理不了。
    if get_approval_store_or_none() is None:
        logger.error(
            "操作审批闸门初始化失败：需审批的页面点击将无法登记，"
            "因此也不会下发一键批准按钮（不会退化成直接点击）"
        )
    else:
        logger.info("操作审批闸门已就绪（PostgreSQL）")
    # 审计时间线（阶段 C ④）。只读模型，它挂了不影响别的功能，但「助手做过什么」
    # 这一页会空掉 —— 而一页空白的时间线比一页报错更危险：用户会以为「助手
    # 什么都没做」，实际上可能是查不出来。所以这里必须留下启动日志。
    if get_audit_timeline_or_none() is None:
        logger.error(
            "审计时间线初始化失败：时间线页面会显示为空，"
            "但这不是「助手什么都没做过」，不要据此判断"
        )
    else:
        logger.info("审计时间线读模型已就绪（PostgreSQL）")
    memory = get_memory()
    memory._require_pg()
    rag.migrate_legacy_documents_to_shared()


# ---------------------------------------------------------------- 个人助手登录与校园凭证库
@app.post("/api/auth/login", tags=["个人凭证"], summary="登录个人助手")
def login(req: LoginRequest, request: Request, response: Response) -> dict:
    # 本地模式直接关掉登录入口。不能只是「前端不再调用」就完事：
    # 一个仍能签发会话、但会话又被 _current_user 完全忽略的接口，
    # 只会让调用方（含未来的自己）误以为「登录成功了但界面没变」。
    if _auth_mode() == "local":
        raise HTTPException(404, "单用户本地模式未启用账号登录")
    store = _get_auth_store()
    client_host = request.client.host if request.client else "unknown"
    attempt_key = login_attempt_key(req.username, client_host)
    try:
        if store.is_rate_limited(attempt_key):
            raise HTTPException(
                429,
                "登录尝试次数过多，请稍后再试",
                headers={"Retry-After": str(rate_limit_retry_after())},
            )
        user_id = store.authenticate(req.username, req.password, attempt_key)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Login attempt failed: %s", type(exc).__name__)
        raise HTTPException(503, "身份认证服务暂不可用") from exc
    if user_id is None:
        raise HTTPException(401, "用户名或密码不正确")
    try:
        token = store.create_session(user_id)
    except Exception as exc:
        logger.error("Session creation failed: %s", type(exc).__name__)
        raise HTTPException(503, "无法建立登录会话") from exc
    response.set_cookie(value=token, **session_cookie_settings())
    return {"authenticated": True}


@app.post("/api/auth/logout", tags=["个人凭证"], summary="退出个人助手")
def logout(request: Request, response: Response) -> dict:
    # 本地模式下没有会话可退，「退出」这个动作本身就是无意义的。
    # 返回 404 而不是 200，是为了让「退出成功但下次请求照样是主人」这种
    # 自相矛盾的状态无法出现。
    if _auth_mode() == "local":
        raise HTTPException(404, "单用户本地模式没有登录会话")
    try:
        _get_auth_store().revoke_session(
            request.cookies.get(session_cookie_name())
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Session revocation failed: %s", type(exc).__name__)
        raise HTTPException(503, "无法安全结束登录会话") from exc
    response.delete_cookie(
        key=session_cookie_name(),
        path="/",
        httponly=True,
        secure=get_settings().auth_cookie_secure,
        samesite="strict",
    )
    return {"authenticated": False}


@app.get("/api/auth/me", tags=["个人凭证"], summary="查询个人助手登录状态")
def auth_me(user: AuthenticatedUser = Depends(_current_user)) -> dict:
    return {
        "authenticated": True,
        "username": user.username,
        "user_id": user.user_id,
    }


@app.get("/api/credentials", tags=["个人凭证"], summary="列出校园系统账号标签")
def credentials(user: AuthenticatedUser = Depends(_current_user)) -> dict:
    try:
        return {"credentials": _get_auth_store().list_credentials(user.user_id)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Credential listing failed: %s", type(exc).__name__)
        raise HTTPException(503, "凭证库暂不可用") from exc


@app.post("/api/credentials", tags=["个人凭证"], summary="保存校园系统账号")
def create_credential(
    req: CredentialRequest,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    if not req.service_key.strip() or not req.label.strip():
        raise HTTPException(422, "校园系统标识和自定义标签不能为空")
    try:
        credential_id = _get_auth_store().save_credential(
            owner_user_id=user.user_id,
            service_key=req.service_key.strip(),
            label=req.label.strip(),
            site_url=req.site_url,
            full_name=req.full_name,
            student_id=req.student_id,
            account=req.account,
            password=req.password,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, "未找到该校园账号") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Credential save failed: %s", type(exc).__name__)
        raise HTTPException(503, "凭证无法保存；请检查加密密钥和数据库配置") from exc
    return {"credential_id": credential_id}


@app.get(
    "/api/credentials/{credential_id}",
    tags=["个人凭证"],
    summary="精确读取单项校园账号（口令已脱敏）",
)
def get_credential(
    credential_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    """返回脱敏视图：口令字段是固定占位符，不含明文。

    本接口的路径与形状保持不变，只是不再回传口令 —— 前端从未使用它，
    真正需要明文的是服务端内部路径（见 _execute_login_tool）。
    需要明文时走 POST /api/credentials/{credential_id}/reveal。
    """
    try:
        credential = _get_auth_store().get_credential(user.user_id, credential_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Credential read failed: %s", type(exc).__name__)
        raise HTTPException(503, "凭证无法解密；请检查加密密钥") from exc
    if credential is None:
        raise HTTPException(404, "未找到该校园账号")
    return credential


@app.post(
    "/api/credentials/{credential_id}/reveal",
    tags=["个人凭证"],
    summary="显式读取口令明文（会记入凭证审计）",
)
def reveal_credential(
    credential_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    """显式取明文口令。刻意用 POST，而不是 GET 加 ?reveal=true：

    1. 我们给接口加了 Cache-Control: no-store，但管不住中间的访问日志 ——
       nginx 记的是 `$request`，包含 query string。用 GET 会让每次「取明文」
       在日志里留下一条带 reveal 标记的路径，等于亲手制造新的可观测泄露面。
    2. 「把口令取出来」是一次敏感操作，语义上属于动作而不是查询，
       这与本项目对浏览器会话操作的既有约定一致（/api/browser-session/action）。

    调用会被记入 credential_audit，动作值为 'reveal'，与脱敏读取的 'read' 区分。
    """
    try:
        credential = _get_auth_store().get_credential(
            user.user_id, credential_id, reveal_password=True
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Credential reveal failed: %s", type(exc).__name__)
        raise HTTPException(503, "凭证无法解密；请检查加密密钥") from exc
    if credential is None:
        raise HTTPException(404, "未找到该校园账号")
    return credential


@app.get("/api/browser-session", tags=["个人凭证"], summary="读取当前用户的浏览器验证画面")
async def browser_session_snapshot(
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    raw = await _browser_tool(user, "get_login_session", {})
    try:
        snapshot = json.loads(raw)
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("status") not in {
                "verification_pending",
                "login_confirmed",
                "session_closed",
                "session_expired",
            }
            or not isinstance(snapshot.get("image"), str)
            or len(snapshot["image"]) > 4_000_000
            or type(snapshot.get("expires_in", 0)) is not int
            or not 0 <= snapshot.get("expires_in", 0) <= 15 * 60
        ):
            raise ValueError("invalid browser snapshot")
        if snapshot["image"]:
            base64.b64decode(snapshot["image"], validate=True)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("Browser MCP returned an invalid session snapshot")
        raise HTTPException(502, "浏览器会话返回了无效画面") from exc
    return snapshot


@app.post("/api/browser-session/action", tags=["个人凭证"], summary="向当前用户的验证页发送输入")
async def browser_session_action(
    req: BrowserActionRequest,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    result = await _browser_tool(
        user,
        "interact_login_session",
        {
            "action": req.action,
            "x": req.x,
            "y": req.y,
            "key": req.key,
        },
    )
    if result in {"session_expired", "session_closed"}:
        raise HTTPException(410, "浏览器验证会话已结束")
    if result != "ok":
        raise HTTPException(422, "浏览器输入未被接受")
    return {"status": result}


@app.post("/api/browser-session/complete", tags=["个人凭证"], summary="确认校园网站登录状态")
async def complete_browser_verification(
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    status = await _browser_tool(user, "confirm_login_session", {})
    if status not in {
        "login_confirmed",
        "session_closed",
        "session_expired",
    }:
        logger.error("Browser MCP returned an invalid completion status")
        raise HTTPException(502, "无法读取浏览器登录状态")
    if status in {"session_expired", "session_closed"}:
        raise HTTPException(410, "浏览器验证会话已过期")
    return {"status": status}


@app.delete("/api/browser-session", tags=["个人凭证"], summary="结束当前用户的浏览器会话")
async def close_browser_session(
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    result = await _browser_tool(user, "close_login_session", {})
    return {"status": result}


@app.put(
    "/api/credentials/{credential_id}",
    tags=["个人凭证"],
    summary="更新校园系统账号",
)
def update_credential(
    credential_id: str,
    req: CredentialRequest,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    if not req.service_key.strip() or not req.label.strip():
        raise HTTPException(422, "校园系统标识和自定义标签不能为空")
    try:
        updated_id = _get_auth_store().save_credential(
            owner_user_id=user.user_id,
            service_key=req.service_key.strip(),
            label=req.label.strip(),
            site_url=req.site_url,
            full_name=req.full_name,
            student_id=req.student_id,
            account=req.account,
            password=req.password,
            credential_id=credential_id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, "未找到该校园账号") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Credential update failed: %s", type(exc).__name__)
        raise HTTPException(503, "凭证无法更新；请检查加密密钥和数据库配置") from exc
    return {"credential_id": updated_id}


@app.delete(
    "/api/credentials/{credential_id}",
    tags=["个人凭证"],
    summary="删除校园系统账号",
)
def delete_credential(
    credential_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    try:
        deleted = _get_auth_store().delete_credential(user.user_id, credential_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Credential deletion failed: %s", type(exc).__name__)
        raise HTTPException(503, "凭证库暂不可用") from exc
    if not deleted:
        raise HTTPException(404, "未找到该校园账号")
    return {"deleted": True}


# ---------------------------------------------------------------- 站点注册表
# 阶段 C ①：把「助手能操作哪些站点」从代码常量搬到数据库。
# 验收口径 —— **新增一个站点全程不改 Python 代码、不重建镜像**。
# 改造前这些信息硬编码在 app/main.py 与 browser/mcp_server.py 两处，
# 加一个站点要同时改两个容器并重建镜像。


def _get_site_store():
    try:
        return get_site_store()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Site registry unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "站点注册表暂不可用") from exc


@app.get("/api/sites", tags=["站点"], summary="列出已登记的站点")
def list_sites(user: AuthenticatedUser = Depends(_current_user)) -> dict:
    return {"sites": _get_site_store().list_for_owner(user.user_id)}


@app.post(
    "/api/sites",
    tags=["站点"],
    summary="登记一个新站点",
    status_code=201,
)
def create_site(
    req: SiteRequest,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    try:
        return _get_site_store().create(user.user_id, req.model_dump())
    except SiteAlreadyExists as exc:
        # 必须排在 SiteValidationError 之前：它是后者的子类，
        # 顺序反了会把「名字被占用」报成「你填错了」。
        raise HTTPException(409, str(exc)) from exc
    except SiteValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Site creation failed: %s", type(exc).__name__)
        raise HTTPException(503, "站点注册表暂不可用") from exc


@app.put("/api/sites/{site_key}", tags=["站点"], summary="更新站点配置")
def update_site(
    site_key: str,
    req: SiteRequest,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    try:
        updated = _get_site_store().update(user.user_id, site_key, req.model_dump())
    except SiteValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Site update failed: %s", type(exc).__name__)
        raise HTTPException(503, "站点注册表暂不可用") from exc
    if updated is None:
        raise HTTPException(404, "未找到该站点")
    return updated


@app.delete("/api/sites/{site_key}", tags=["站点"], summary="删除站点")
def delete_site(
    site_key: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    """删除站点。

    ★ 刻意不连带删除引用它的凭证。删站点后那些凭证会变成「孤儿」，
    登录工具会回一句「尚未登记该站点的登录凭证」—— 这是可读的失败，
    比悄悄删掉用户的账号记录安全得多。用户可在凭证面板自行清理。
    """
    try:
        deleted = _get_site_store().delete(user.user_id, site_key)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Site deletion failed: %s", type(exc).__name__)
        raise HTTPException(503, "站点注册表暂不可用") from exc
    if not deleted:
        raise HTTPException(404, "未找到该站点")
    return {"deleted": True, "site_key": site_key}


# ---------------------------------------------------------------- 操作审批闸门
# 2026-10-15 阶段 C ②。真相源在 PostgreSQL（app/approval_store.py）。
# 改造前，「确认点击」是一段进程内内存态：用户在聊天里手打「确认点击：X」
# 就真的点了。三个问题 —— 容器重启即丢、没有任何地方能回答「批准过什么」、
# 同一句话发两次就可能点两次。现在批准与执行都落库，且各只能成功一次。
_BROWSER_CLICK_TOOL = "confirm_pending_login_session_click"


def _get_approval_store():
    try:
        return get_approval_store()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Approval gate unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "操作审批闸门暂不可用") from exc


@app.get("/api/approvals", tags=["审批"], summary="列出待处理与最近的操作审批")
def list_approvals(
    limit: int = Query(default=20, ge=1, le=100),
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    store = _get_approval_store()
    # 惰性维护（与 expire_stale 同因：为「读一次就能顺带算出」的事常驻线程不划算）：
    #   1) 到期的 pending/approved 落 expired —— 由 list_for_owner 内部顺带做；
    #   2) 卡在 executing 超过 5 分钟的标为 unknown（结果未知）。
    # 第 2 条**不重放也不判失败**：重放等于重复点击，而网页点击的副作用不可逆。
    try:
        store.mark_stuck_executions()
    except Exception as exc:
        logger.error("Stuck approval sweep failed: %s", type(exc).__name__)
    return {"approvals": store.list_for_owner(user.user_id, limit)}


@app.post(
    "/api/approvals/{approval_id}/approve",
    tags=["审批"],
    summary="批准一次待执行的操作",
)
def approve_operation(
    approval_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    store = _get_approval_store()
    approval = store.approve(user.user_id, approval_id)
    if approval is None:
        # 「不存在」「已过期」「已被处理」必须分开报：合并成 409 会让用户以为
        # 是自己点重了，而真实原因可能是那条记录压根没写进去。
        if store.get(user.user_id, approval_id) is None:
            raise HTTPException(404, "未找到该审批")
        raise HTTPException(409, "该审批已过期或已被处理")
    return {"status": approval["status"], "approval": approval}


@app.post(
    "/api/approvals/{approval_id}/reject",
    tags=["审批"],
    summary="拒绝一次待执行的操作",
)
def reject_operation(
    approval_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    store = _get_approval_store()
    approval = store.reject(user.user_id, approval_id)
    if approval is None:
        if store.get(user.user_id, approval_id) is None:
            raise HTTPException(404, "未找到该审批")
        raise HTTPException(409, "该审批已过期或已被处理")
    return {"status": approval["status"], "approval": approval}


@app.post(
    "/api/approvals/{approval_id}/execute",
    tags=["审批"],
    summary="执行一次已批准的操作（唯一会真点击网页的入口）",
)
async def execute_operation(
    approval_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    """★ 全项目**唯一**会触发真实网页点击的入口。

    阶段 C ② 的验收口径「未批准时零外部副作用」就落在这里：没有一条
    approved 的 PG 记录，下面的点击工具不会被调用一次。
    """
    store = _get_approval_store()
    approval = store.get(user.user_id, approval_id)
    if approval is None:
        raise HTTPException(404, "未找到该审批")
    if approval["status"] == STATUS_EXPIRED:
        # 410 而不是 409：对用户而言「过期了，重新发起一次」和
        # 「这条已经被处理掉了」是两种不同的下一步动作。
        raise HTTPException(410, "该审批已过期，请重新发起这次操作")
    if approval["status"] != STATUS_APPROVED:
        # pending（还没批准）/ rejected / executing（并发中）/ executed / unknown
        # 全走同一个响应：它们都不是「可以执行」的状态。
        raise HTTPException(409, "该审批尚未批准或已被处理")

    if not browser_breaker.allow():
        # 电路已断开 ⇒ 请求根本没发出去，点击不可能发生 ⇒ 审批保持 approved，
        # 用户稍后重试即可。这是**唯一**允许「保持 approved」的失败路径。
        raise HTTPException(502, "浏览器会话暂不可用，请稍后重试")

    claimed = store.begin_execution(user.user_id, approval_id)
    if claimed is None:
        # 两次并发 execute 只有一次能拿到租约（原子 UPDATE + 影响行数）。
        raise HTTPException(409, "该审批正在执行或已过期")

    try:
        raw = await _call_browser_mcp(
            BROWSER_MCP_URL,
            _BROWSER_CLICK_TOOL,
            {"owner_user_id": user.user_id, "target": claimed["target"]},
        )
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("invalid browser result")
    except json.JSONDecodeError as exc:
        # 浏览器回了一段非 JSON。按「结果不确定」处理，见下面 failed 的注释。
        logger.error("Browser click returned invalid JSON")
        store.complete_execution(
            user.user_id, approval_id, outcome=STATUS_FAILED,
            detail={"reason": "invalid_browser_result"},
        )
        raise HTTPException(502, "无法确认点击结果，请到网站上核对") from exc
    except ValueError as exc:
        logger.error("Browser click returned an unexpected shape")
        store.complete_execution(
            user.user_id, approval_id, outcome=STATUS_FAILED,
            detail={"reason": "invalid_browser_result"},
        )
        raise HTTPException(502, "无法确认点击结果，请到网站上核对") from exc
    except Exception as exc:
        # ★ 这里**不**把审批退回 approved。传输层异常无法区分「请求没送达」与
        #   「送达了但没等到回复」，而后者可能已经点过了 —— 允许重试就是允许点两次。
        #   宁可要一次人工介入（用户自己去网站上核对），也不要静默做两次。
        #   这与阶段 B「普通异常即终态、不自动重试」同源。
        logger.error("Browser click execution failed: %s", type(exc).__name__)
        store.complete_execution(
            user.user_id, approval_id, outcome=STATUS_FAILED,
            detail={"reason": "browser_unreachable"},
        )
        raise HTTPException(502, "浏览器点击未能确认，请到网站上核对实际结果") from exc

    status = result.get("status")
    if status == "clicked":
        page = result.get("page") or {}
        store.complete_execution(
            user.user_id, approval_id, outcome=STATUS_EXECUTED,
            detail={"page_title": page.get("title", "")},
        )
        return {"status": STATUS_EXECUTED, "page": page}
    if status in {"no_pending_click", "pending_click_expired"}:
        # 放行+标记：请求本身被正确处理了，只是浏览器侧那个待点击项已经没了
        # （90 秒 TTL 到期，或页面已被重新读过）。**什么都没点成**，
        # 所以落 expired 而不是 executed —— 但也不能报错，这不是故障。
        store.complete_execution(
            user.user_id, approval_id, outcome=STATUS_EXPIRED,
            detail={"reason": status},
        )
        return {"status": STATUS_EXPIRED, "reason": status}
    # target_changed / confirmation_mismatch / session_not_verified 等：
    # 发生了某种变化，但不确定是不是我们要的那个 ⇒ failed，且不自动重试。
    store.complete_execution(
        user.user_id, approval_id, outcome=STATUS_FAILED,
        detail={"reason": status or "unknown_status"},
    )
    return {"status": STATUS_FAILED, "reason": status or "unknown_status"}


# ---------------------------------------------------------------- 审计时间线（阶段 C ④）
# 「助手做过什么」的唯一查看入口：把凭证审计与操作审计合成一条按时间倒序的流水。
# 只读，且**只能看自己的** —— 时间线会暴露「取过明文口令」这类事实，
# 越权读到的信息量不比凭证本身小。
def _get_audit_timeline():
    try:
        return get_audit_timeline()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Audit timeline unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "审计时间线暂不可用") from exc


# 一页最多取多少条。上限刻意比模块的 MAX_LIMIT 小 1：下面要多取一条来判断
# 「还有没有下一页」，若正好取到 MAX_LIMIT，多取的那条会被模块内部截断，
# has_more 就永远算不对（表现为「加载更多」点一次就消失，或永远显示还有更多）。
_AUDIT_PAGE_MAX = max(1, min(100, AUDIT_MAX_LIMIT - 1))


@app.get(
    "/api/audit/timeline",
    tags=["审计"],
    summary="助手做过什么（凭证与操作事件合并成一条流水）",
)
def list_audit_timeline(
    limit: int = Query(default=30, ge=1, le=_AUDIT_PAGE_MAX),
    cursor: str = Query(default=""),
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    """游标翻页，不用 offset。

    理由与模块头部一致：时间线持续增长且新事件插在最前，用 offset 翻页时
    「第 1 页的最后一条」会滑到第 2 页开头，用户看到重复；反之清理旧审计后
    会整条跳过。游标里带的是排序键本身，不受新增/删除影响。
    """
    try:
        position = parse_cursor(cursor)
    except ValueError as exc:
        # 手改 query string 不该变成 500 —— 那会让人以为服务坏了。
        raise HTTPException(422, str(exc)) from exc
    rows = _get_audit_timeline().for_owner(
        user.user_id, limit=limit + 1, cursor=position
    )
    has_more = len(rows) > limit
    events = rows[:limit]
    return {
        "events": events,
        "has_more": has_more,
        # 只在确实还有下一页时才给游标，否则前端会挂着一条点了没反应的「加载更多」。
        "next_cursor": make_cursor(events[-1]) if has_more and events else "",
    }


# ---------------------------------------------------------------- 后台任务
# 2026-10-14 阶段 B：长任务不再占用 HTTP 请求，改为「入队 → worker 执行 → 查状态」。
# 本进程只做前两步与查询，不执行任何长任务（执行体在 app/worker.py）。

# 回传给前端时要剥掉的字段。剥的是「只有服务端才关心的实现细节」：
#   stored_path —— 源文件在容器里的绝对路径，对外没有意义
#   locked_by   —— worker 身份，前端不该知道也不该依赖
_INTERNAL_TASK_FIELDS = {"stored_path", "locked_by", "lease_expires_at"}


def _get_task_store():
    try:
        return get_task_store()
    except Exception as exc:
        logger.error("任务队列不可用: %s", type(exc).__name__)
        raise HTTPException(503, "任务队列暂不可用") from exc


def _public_task(task: dict) -> dict:
    payload = {
        key: value
        for key, value in (task.get("payload") or {}).items()
        if key not in _INTERNAL_TASK_FIELDS
    }
    return {
        **{k: v for k, v in task.items() if k in {
            "task_id", "task_type", "status", "stage",
            "progress_current", "progress_total",
            "created_at", "started_at", "finished_at", "updated_at",
            "attempts", "cancel_requested", "error",
        }},
        "payload": payload,
        "result": task.get("result"),
    }


@app.get("/api/tasks", tags=["后台任务"], summary="列出本机主人的后台任务")
def list_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    store = _get_task_store()
    try:
        tasks = store.list_for_owner(user.user_id, limit)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("任务列表读取失败: %s", type(exc).__name__)
        raise HTTPException(503, "任务队列暂不可用") from exc
    return {"tasks": [_public_task(task) for task in tasks]}


@app.get("/api/tasks/{task_id}", tags=["后台任务"], summary="查询单个任务状态")
def get_task(
    task_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    store = _get_task_store()
    try:
        task = store.get(task_id, user.user_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("任务读取失败: %s", type(exc).__name__)
        raise HTTPException(503, "任务队列暂不可用") from exc
    if task is None:
        # 越权与不存在返回同一个 404：不告诉调用方「这个 id 是存在的，
        # 只是不属于你」。凭证接口用的是同一条约定。
        raise HTTPException(404, "未找到该任务")
    return _public_task(task)


@app.post("/api/tasks/{task_id}/cancel", tags=["后台任务"], summary="取消任务")
def cancel_task(
    task_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    store = _get_task_store()
    try:
        task = store.cancel(task_id, user.user_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("任务取消失败: %s", type(exc).__name__)
        raise HTTPException(503, "任务队列暂不可用") from exc
    if task is None:
        raise HTTPException(404, "未找到该任务")
    # 注意「取消」有两种结局，靠 status + cancel_requested 区分：
    #   排队中 → status 立刻变成 cancelled
    #   运行中 → status 仍是 running，cancel_requested=true，等 worker 在检查点停下
    # 前端据此显示「取消中…」，而不是假装已经停好了。
    #
    # ★ 排队中取消必须在这里删源文件（2026-10-14 验收实测的泄漏）。
    #   worker 的清理只覆盖「领取并执行」的路径；排队中就终结的任务永远不会
    #   被 claim_next 领到，于是那份落盘的源文件既不会被入库、也不会被删除。
    #   更糟的是它没有向量片段，因此不出现在文档列表里 —— 用户连手动删的入口
    #   都没有。判据用「返回的行是否已终结为 cancelled」而不是「取消前是不是
    #   queued」：万一在读取与更新之间被 worker 领走，返回的就是 running，
    #   此时不该删（worker 会负责）。
    if task["status"] == "cancelled":
        rag.discard_stored_file((task.get("payload") or {}).get("stored_path"))
    logger.info("任务取消请求：%s（当前状态 %s）", task_id, task["status"])
    return _public_task(task)


# ---------------------------------------------------------------- 文档
@app.post(
    "/api/documents/upload",
    tags=["文档"],
    summary="上传文档并入库（后台任务）",
    status_code=202,
)
async def upload(
    response: Response,
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 限制")
    filename = file.filename or "unnamed.txt"
    # 先验文件类型再落盘：让用户在点击上传后【立刻】看到「不支持的类型」，
    # 而不是等任务跑起来才失败。
    try:
        rag.validate_upload(filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    store = get_task_store_or_none()
    if store is not None:
        doc_id = uuid.uuid4().hex
        try:
            stored = await run_in_threadpool(rag.store_upload, data, filename, doc_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            logger.error("上传文件落盘失败: %s", type(exc).__name__)
            raise HTTPException(500, "无法保存上传文件") from exc

        try:
            task = await run_in_threadpool(
                store.enqueue,
                "document_ingest",
                user.user_id,
                {"doc_id": doc_id, "filename": filename, "stored_path": str(stored)},
            )
        except Exception as exc:
            # 入队失败就把刚落盘的文件删掉：否则每失败一次就在 uploads 里
            # 多一个永远不会被引用的孤儿（这个坑此前踩过一次）。
            logger.error("任务入队失败: %s", type(exc).__name__)
            await run_in_threadpool(rag.discard_stored_file, str(stored))
            raise HTTPException(503, "任务队列暂不可用，请稍后重试") from exc

        # 202 = 已接受、尚未处理完。刻意不用 200：这个响应不代表「入库完成」，
        # 而入库结果稍后通过 /api/tasks/{id} 取。前端据 mode 区分两种语义。
        return {"mode": "queued", "filename": filename, **_public_task(task)}

    # ---- 降级路径：队列不可用（PG 连不上）时退回同步入库 ----
    # 队列是「更好的方式」，不是「唯一的方式」：PG 抖一下不该让整个入库能力消失。
    # 代价是这次请求会挂着 1~3 分钟，所以用 mode=sync 明确告诉前端。
    # ★ 状态码要跟着语义走：本路由的默认状态码是 202（已受理、未完成），
    #   但这条路是同步做完的，必须改成 200。否则同一条路径会出现
    #   「202 但结果已经拿到了」这种自相矛盾的响应，让调用方无从判断。
    response.status_code = 200
    logger.warning("任务队列不可用，本次上传退回同步入库")
    try:
        return {
            "mode": "sync",
            **await run_in_threadpool(rag.ingest, data, filename, user.user_id),
        }
    except ValueError as exc:
        # 业务校验失败（类型不支持 / 超出字符或切片上限）→ 400，原因原样给用户
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("ingest failed")
        raise HTTPException(500, f"入库失败：{exc}") from exc


@app.get("/api/documents", tags=["文档"], summary="列出已入库文档")
def documents(user: AuthenticatedUser = Depends(_current_user)) -> dict:
    return {"documents": rag.list_documents(user.user_id)}


@app.delete("/api/documents/{doc_id}", tags=["文档"], summary="删除文档")
def delete_document(
    doc_id: str,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    deleted = rag.delete_document(
        doc_id,
        user.user_id,
        # 本机单人模式下 "shared" 就是这台机器主人自己上传的资料，必须可删
        # （否则公共知识库的文档在 UI 上永远删不掉）。
        # 多用户（session）模式保持原语义：shared 文档不可由普通用户删除。
        allow_shared=_auth_mode() == "local",
    )
    if deleted == 0:
        raise HTTPException(404, "未找到该文档")
    return {"deleted_chunks": deleted}


# ---------------------------------------------------------------- 问答
@app.post("/api/chat", tags=["问答"], summary="问答（一次性返回全部结果）")
def chat(
    req: AskRequest,
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    session_id = req.session_id or _new_session_id(req.assistant_mode)
    _validate_session_mode(session_id, req.assistant_mode)
    if req.session_id and not get_memory().session_exists_for_user(user.user_id, session_id):
        raise HTTPException(404, "未找到该会话")
    if req.assistant_mode == "student":
        target = _requested_login_target(req.question, user)
        if target:
            answer, browser_session_active = _execute_login_tool(
                user, target
            )
            get_memory().append_for_user(user.user_id, session_id, req.question, answer)
            return {
                "answer": answer,
                "sources": [],
                "session_id": session_id,
                "browser_session_active": browser_session_active,
            }
        task = _answer_browser_task(req.question, user, session_id)
        get_memory().append_for_user(user.user_id, session_id, req.question, task["answer"])
        return {
            "answer": task["answer"],
            "sources": [],
            "session_id": session_id,
            # 非空表示有一个控件等待用户在界面上批准（阶段 C ②）。
            # 前端据此渲染「批准并执行 / 拒绝」，不再走「手打确认文本」那条路。
            "click_approval": task["approval"],
        }
    try:
        return rag.answer(req.question, session_id, user.user_id)
    except PermissionError as exc:
        raise HTTPException(404, "未找到该会话") from exc
    except Exception as exc:
        logger.exception("chat failed")
        raise HTTPException(500, f"生成失败：{exc}") from exc


@app.post("/api/chat/stream", tags=["问答"], summary="问答（SSE 流式逐字返回）")
def chat_stream(
    req: AskRequest,
    request: Request,
    user: AuthenticatedUser = Depends(_current_user),
) -> StreamingResponse:
    """Server-Sent Events 流式问答。

    事件协议（每条 data 都是一个 JSON 对象，带 type 字段）：
        {"type":"meta",  "session_id":"..."}          会话建立
        {"type":"sources","sources":[...]}            命中的知识库片段
        {"type":"token", "text":"..."}                一个回答片段（可能多次）
        {"type":"done",  "session_id":"..."}          正常结束
        {"type":"error", "message":"..."}             出错

    个人助手（assistant_mode=student）额外事件：
        {"type":"browser_session","active":true}      隔离浏览器验证窗口已开启
        {"type":"click_approval","approval":{...}}
            有一个页面控件被选中、等待用户在界面上批准这一次点击；
            approval 里带 approval_id / target / expires_at（90 秒）。
            前端据此渲染「批准并执行 / 拒绝」卡片 —— 批准与执行分别走
            POST /api/approvals/{id}/approve 与 /execute。
            手打「确认点击：X」已不再生效（那会让审批闸门形同虚设）。

    为什么用 POST + fetch 而不是浏览器原生 EventSource：
        EventSource 只支持 GET，且不能带请求体。
        我们需要 POST 一个 JSON（含问题和 session_id），所以改用
        fetch + ReadableStream 手动解析 SSE。协议本身仍是标准的 SSE。
    """
    session_id = req.session_id or _new_session_id(req.assistant_mode)
    _validate_session_mode(session_id, req.assistant_mode)
    if req.session_id and not get_memory().session_exists_for_user(user.user_id, session_id):
        raise HTTPException(404, "未找到该会话")

    def sse(payload: dict) -> str:
        # ensure_ascii=False 让中文直接输出，不必转成 \uXXXX
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def event_stream():
        try:
            yield sse({"type": "meta", "session_id": session_id})

            if req.assistant_mode == "student":
                yield sse({"type": "sources", "sources": []})
                target = _requested_login_target(req.question, user)
                if target:
                    answer, browser_session_active = _execute_login_tool(
                        user, target
                    )
                    if browser_session_active:
                        yield sse({"type": "browser_session", "active": True})
                    get_memory().append_for_user(user.user_id, session_id, req.question, answer)
                    yield sse({"type": "token", "text": answer})
                    yield sse({"type": "done", "session_id": session_id})
                    return
                task = _answer_browser_task(req.question, user, session_id)
                if task["approval"]:
                    # 结构化下发待批准的操作（含 approval_id 与过期时刻），
                    # 前端据此渲染「批准并执行 / 拒绝」卡片与倒计时。
                    yield sse({"type": "click_approval", "approval": task["approval"]})
                get_memory().append_for_user(
                    user.user_id, session_id, req.question, task["answer"]
                )
                yield sse({"type": "token", "text": task["answer"]})
                yield sse({"type": "done", "session_id": session_id})
                return

            for event in rag.answer_stream(req.question, session_id, user.user_id):
                if event["type"] == "sources":
                    yield sse({"type": "sources", "sources": event["sources"]})
                else:
                    yield sse({"type": "token", "text": event["text"]})

            yield sse({"type": "done", "session_id": session_id})
        except Exception as exc:
            logger.exception("stream failed")
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # 关掉各级缓冲：否则代理/浏览器会攒够一批才吐出来，失去"流式"意义
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 给 nginx 看的：不要缓冲
        },
    )


# ---------------------------------------------------------------- 记忆 / 会话
@app.get("/api/sessions", tags=["记忆"], summary="列出所有会话")
def sessions(
    assistant_mode: Literal["school", "student"] = Query(default="school"),
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    """给前端做会话列表用。

    数据源是 PostgreSQL（真相源），所以容器重启、Redis 缓存过期都不会丢。
    只有 PostgreSQL 不可用时才会退回 Redis / 进程内，那时这个列表才是临时的。
    """
    return {
        "sessions": get_memory().list_sessions_for_user(
            user.user_id,
            assistant_mode,
        )
    }


@app.delete("/api/sessions", tags=["记忆"], summary="清空所有会话")
def clear_all_sessions(
    assistant_mode: Literal["school", "student"] = Query(default="school"),
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    return {
        "cleared": get_memory().clear_assistant_for_user(
            user.user_id,
            assistant_mode,
        )
    }


@app.get("/api/chat/{session_id}/history", tags=["记忆"], summary="查询会话历史")
def history(
    session_id: str,
    assistant_mode: Literal["school", "student"] = Query(default="school"),
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    _validate_session_mode(session_id, assistant_mode)
    memory = get_memory()
    if not memory.session_exists_for_user(user.user_id, session_id):
        raise HTTPException(404, "未找到该会话")
    return {
        "session_id": session_id,
        "history": memory.get_history_for_user(user.user_id, session_id),
    }


@app.delete("/api/chat/{session_id}/history", tags=["记忆"], summary="清空会话历史")
def clear_history(
    session_id: str,
    assistant_mode: Literal["school", "student"] = Query(default="school"),
    user: AuthenticatedUser = Depends(_current_user),
) -> dict:
    _validate_session_mode(session_id, assistant_mode)
    if not get_memory().clear_for_user(user.user_id, session_id):
        raise HTTPException(404, "未找到该会话")
    return {"session_id": session_id, "cleared": True}


# ---------------------------------------------------------------- 根路径
# 这里曾经挂载过静态前端（StaticFiles 挂在 /ui，根路径 307 跳到 /ui/）。
# 前端迁到 Vue3 + Vite、由独立 nginx 容器托管后，本服务不再发任何静态文件。
#
# 为什么根路径还要保留一个响应，而不是干脆让它 404：
#   直接访问 127.0.0.1:8000 的人（多半是在排查问题）需要立刻知道
#   「这是 API，不是网站 —— 页面在 nginx 那个端口上」。
#   一个 404 会让人以为服务没起来，然后去查一堆无关的东西。
#   返回一份自描述的 JSON 是最省事的指路牌。
@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "Phase 1 RAG API",
        "notice": "本服务只提供接口，不托管前端页面。",
        "frontend": "页面由独立的 nginx 容器提供（生产 8080，开发 Vite 5173）",
        "docs": "/docs",
        "health": "/health",
    }
