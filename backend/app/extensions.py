"""Per-process Skill and MCP extension loading.

External MCP servers are opt-in through MCP_SERVERS_JSON.  Credentials stay in
the environment/configuration and are never included in tool descriptions or
logs.  Mutation-capable tools are intentionally rejected until they are wired
through the existing approval state machine.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .observability import CircuitBreaker

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(os.getenv("SKILLS_DIR", "/app/skills"))
MAX_SKILL_CHARS = 12_000
MAX_TOOL_OUTPUT_CHARS = 12_000
MCP_TIMEOUT_SECONDS = int(os.getenv("MCP_TIMEOUT_SECONDS", "30"))
MCP_STDIO_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
_breakers: dict[tuple[str, str], CircuitBreaker] = {}


def _breaker(owner_user_id: str | None, server_name: str) -> CircuitBreaker:
    return _breakers.setdefault((owner_user_id or "", server_name), CircuitBreaker())


def _stdio_env(config: dict) -> dict[str, str]:
    env = {k: os.environ[k] for k in MCP_STDIO_ENV_ALLOWLIST if k in os.environ}
    extra = config.get("env")
    if isinstance(extra, dict):
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _audit(owner_user_id: str | None, action: str, detail: dict) -> None:
    if not owner_user_id:
        return
    try:
        from .approval_store import get_approval_store_or_none

        store = get_approval_store_or_none()
        if store is not None:
            store.record_event(owner_user_id, kind="mcp", action=action, detail=detail)
    except Exception as exc:
        logger.error("MCP audit failed: %s", type(exc).__name__)


def load_skills() -> list[dict]:
    skills = []
    if not SKILLS_DIR.is_dir():
        return skills
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        try:
            content = path.read_text(encoding="utf-8")[:MAX_SKILL_CHARS]
        except OSError:
            continue
        skills.append({"name": path.parent.name, "content": content})
    return skills


def _server_config(owner_user_id: str | None = None) -> dict:
    if owner_user_id:
        from .mcp_store import get_mcp_store_or_none

        store = get_mcp_store_or_none()
        if store is not None:
            return store.configs(owner_user_id)
    raw = os.getenv("MCP_SERVERS_JSON", "{}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MCP_SERVERS_JSON 格式无效") from exc
    if not isinstance(value, dict):
        raise RuntimeError("MCP_SERVERS_JSON 必须是对象")
    return value


def _tool_schema(server_name: str, tool) -> dict:
    schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    return {
        "type": "function",
        "function": {
            "name": f"mcp__{server_name}__{tool.name}",
            "description": (tool.description or f"MCP 工具 {tool.name}")[:1000],
            "parameters": schema,
        },
    }


def _is_read_only(name: str, description: str) -> bool:
    text = f"{name} {description}".casefold()
    dangerous = (
        "delete", "remove", "write", "update", "create", "send", "publish",
        "execute", "run", "payment", "purchase", "提交", "删除", "修改",
        "发送", "执行", "创建", "支付",
    )
    return not any(word in text for word in dangerous)


class ExtensionManager:
    @asynccontextmanager
    async def _session(self, config: dict):
        transport = config.get("transport", "streamable_http")
        if transport == "streamable_http":
            url = config.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise RuntimeError("HTTP MCP 必须使用 HTTPS URL")
            async with streamablehttp_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif transport == "stdio":
            command = config.get("command")
            args = config.get("args", [])
            if not isinstance(command, str) or not isinstance(args, list):
                raise RuntimeError("stdio MCP 配置无效")
            params = StdioServerParameters(
                command=command,
                args=[str(arg) for arg in args],
                env=_stdio_env(config),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            raise RuntimeError("不支持的 MCP 传输类型")

    async def list_tools(self, owner_user_id: str | None = None) -> list[dict]:
        result = []
        for server_name, config in _server_config(owner_user_id).items():
            if not isinstance(config, dict) or config.get("enabled") is not True:
                continue
            breaker = _breaker(owner_user_id, server_name)
            if not breaker.allow():
                continue
            try:
                result.extend(await asyncio.wait_for(
                    self._discover(server_name, config), MCP_TIMEOUT_SECONDS
                ))
                breaker.success()
            except Exception as exc:
                breaker.failure()
                logger.error("MCP discovery failed server=%s: %s", server_name, type(exc).__name__)
        return result

    async def _discover(self, server_name: str, config: dict) -> list[dict]:
        found = []
        async with self._session(config) as session:
            tools = await session.list_tools()
            for tool in tools.tools:
                found.append({
                    "server": server_name,
                    "name": tool.name,
                    "description": tool.description or "",
                    "read_only": _is_read_only(tool.name, tool.description or ""),
                    "schema": _tool_schema(server_name, tool),
                })
        return found

    async def call(
        self,
        qualified_name: str,
        arguments: dict,
        owner_user_id: str | None = None,
        approved: bool = False,
    ) -> str:
        parts = qualified_name.split("__", 2)
        server_name = parts[1] if len(parts) == 3 else ""
        breaker = _breaker(owner_user_id, server_name)
        if not breaker.allow():
            raise RuntimeError("MCP 服务暂时熔断，请稍后重试")
        try:
            text = await asyncio.wait_for(
                self._call(qualified_name, arguments, owner_user_id, approved), MCP_TIMEOUT_SECONDS
            )
        except Exception as exc:
            breaker.failure()
            _audit(owner_user_id, "mcp_tool_failed", {"tool": qualified_name, "error": type(exc).__name__})
            raise
        breaker.success()
        _audit(owner_user_id, "mcp_tool_called", {"tool": qualified_name, "argument_keys": sorted(arguments)})
        return text

    async def _call(
        self,
        qualified_name: str,
        arguments: dict,
        owner_user_id: str | None = None,
        approved: bool = False,
    ) -> str:
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise RuntimeError("MCP 工具名称无效")
        _, server_name, tool_name = parts
        config = _server_config(owner_user_id).get(server_name)
        if not isinstance(config, dict) or config.get("enabled") is not True:
            raise RuntimeError("MCP 服务未启用")
        async with self._session(config) as session:
            tools = await session.list_tools()
            tool = next((item for item in tools.tools if item.name == tool_name), None)
            if tool is None:
                raise RuntimeError("MCP 工具不存在")
            if not approved and not _is_read_only(tool.name, tool.description or ""):
                raise RuntimeError("该 MCP 工具可能改变状态，当前版本需要审批")
            result = await session.call_tool(tool_name, arguments=arguments)
            text = "\n".join(
                item.text for item in result.content if hasattr(item, "text")
            )
            return text[:MAX_TOOL_OUTPUT_CHARS]


def extension_snapshot(owner_user_id: str | None = None) -> dict:
    servers = _server_config(owner_user_id)
    return {
        "skills": [{"name": item["name"]} for item in load_skills()],
        "mcp_servers": [
            {
                "name": name,
                "transport": config.get("transport", "streamable_http"),
                "enabled": config.get("enabled") is True,
            }
            for name, config in servers.items()
            if isinstance(config, dict)
        ],
    }
