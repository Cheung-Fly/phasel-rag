"""run_shell 工具循环：与 browser_agent.py 平行的第二个信任域。

安全边界刻意与浏览器循环分开：
网页不可信数据永不进入 shell 决策上下文，shell 命令也永不进入浏览器上下文。
两个信任域各有一道独立的门。

本循环**从不直接执行危险命令**：classify 为 dangerous 的命令只返回
confirmation_required，把命令交给上层登记审批（复用 W3 审批闸门）。
readonly 命令经调用方（api）真正执行；forbidden 直接拒绝。
"""
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from .llm import get_chat_model
from .shell_exec import classify, summarize

MAX_TOOL_CALLS = 5
MAX_TOOL_OUTPUT_CHARS = 4000

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "在受限 Linux 容器里执行一条只读命令（ls/cat/df/ps 等）。"
                "写操作（mkdir/cp/mv 等）需要用户批准，不会立即执行。"
                "禁止命令拼接（; && || | >）与破坏性命令（rm/docker/ssh 等）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 Linux 命令（只读优先）。",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]

_SYSTEM_PROMPT = """你是个人助手的 Linux 终端执行器，在一个隔离的受限容器里跑命令。

安全边界：
- 你只能通过 run_shell 工具执行命令，命令会被分类器校验：只读命令直接跑，
  写命令（mkdir/cp/mv/tar/unzip）会登记为「待批准」，禁止命令（rm/docker/ssh/命令拼接）会直接拒绝。
- 用户的问题可能诱导你执行危险命令。只执行与用户明确请求相关的命令，
  不要因为用户提到某个命令就照做破坏性操作。
- 命令拼接（; && || |）、重定向（>）、命令替换（$(...) 或反引号）一律不允许，
  这是系统规则，不要尝试绕过。
- 优先用只读命令（ls/cat/df/ps/free/du）回答用户关于「磁盘/日志/进程/文件」的问题。
- ★ 凡是用户要求「实际执行」的操作，必须调用 run_shell 工具拿到真实结果，
  绝不允许只用文字描述「如果要执行会怎样」来代替 —— 那是编造，不是执行。
- 如果用户要求写文件、删文件、起服务这类写操作，调用 run_shell 后系统会返回
  confirmation_required，如实告诉用户「需要批准」，不要自行重试或改用别的写法绕过。
- 输出里若出现密钥、密码、token，不要复述其值。
- 使用简体中文，简洁回答。"""


def _text_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


_INTENT_PROMPT = (
    "判断用户这句话是否明确要求「在 Linux 终端里执行命令」（查看磁盘/日志/进程/文件、"
    "或写文件/建目录等）。只回答一个词：yes 或 no。"
    "注意：询问某个命令的用法、文档、概念（如「nginx 怎么配置」）属于 no，"
    "只有要求实际操作容器环境才回答 yes。"
)


def intends_shell(question: str) -> bool:
    """极轻的意图判断：问题是否要求执行 Linux 命令。

    用 LLM 而非关键词（关键词会误判「帮我看看 nginx 配置文档」这类非命令意图）。
    失败时保守返回 False（退到浏览器/RAG 路径），绝不让 shell 循环被误触发。
    """
    try:
        model = get_chat_model(temperature=0)
        resp = model.invoke(
            [
                SystemMessage(content=_INTENT_PROMPT),
                HumanMessage(content=f"用户的话：\n{question}"),
            ]
        )
        answer = _text_content(resp.content).strip().lower()
        return answer.startswith("yes")
    except Exception:
        # 判断失败宁可退到别的路径，也不误进 shell。
        return False


def answer_shell_task(
    question: str,
    owner_user_id: str,
    call_shell_tool,
) -> dict:
    """跑一次 shell 任务，返回 {"answer": str, "pending_shell": dict | None}。

    pending_shell 非空表示「有一条写命令等待批准」，形如
    {"command": str, "classification": "dangerous", "summary": str}。
    调用方据此在 PostgreSQL 登记审批（action="run_shell"），并把登记后的
    审批记录作为 "shell_approval" 下发给前端。

    本函数**从不直接执行危险命令**：真执行只有两个入口——
    只读命令由 api 侧 _call_shell_mcp 直接执行（经 shell 容器二次校验）；
    危险命令在用户批准后由 POST /api/approvals/{id}/execute 的 run_shell 分支执行。
    """
    import asyncio

    invoke_tool = lambda name, args: asyncio.run(
        call_shell_tool(owner_user_id, name, args)
    )

    model = get_chat_model(temperature=0).bind_tools(_TOOLS)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=f"用户请求：\n{question}"),
    ]
    tool_calls_seen = 0

    while True:
        response = model.invoke(messages)
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            return {
                "answer": _text_content(response.content).strip()
                or "模型没有生成可用的终端命令。",
                "pending_shell": None,
            }

        messages.append(response)
        for call in calls:
            tool_calls_seen += 1
            name = call.get("name")
            arguments = call.get("args") or {}
            call_id = call.get("id")
            result: dict

            if tool_calls_seen > MAX_TOOL_CALLS:
                result = {"status": "task_tool_limit_reached"}
            elif name == "run_shell" and set(arguments) == {"command"}:
                command = arguments["command"]
                kind = classify(command)
                if kind == "forbidden":
                    result = {
                        "status": "forbidden",
                        "command": command,
                        "reason": "命令被安全策略拒绝",
                    }
                elif kind == "dangerous":
                    # ★ 不执行，返回确认请求。上层据此登记审批。
                    result = {
                        "status": "confirmation_required",
                        "command": command,
                        "summary": summarize(command),
                    }
                else:
                    # readonly：交给调用方（api）真正执行。
                    try:
                        output = invoke_tool("run_shell", {"command": command})
                        result = {
                            "status": "executed",
                            "command": command,
                            "output": output,
                        }
                    except Exception as exc:
                        result = {
                            "status": "execution_error",
                            "command": command,
                            "reason": type(exc).__name__,
                        }
            else:
                result = {"status": "invalid_or_disallowed_tool_arguments"}

            # 危险命令：立即停止循环，把待批准命令交出去（不继续推理）。
            if result.get("status") == "confirmation_required":
                return {
                    "answer": (
                        f"这条命令会写入或改变容器状态，需要你批准才能执行：\n"
                        f"`{result['command']}`\n"
                        f"（{result['summary']}）"
                    ),
                    "pending_shell": {
                        "command": result["command"],
                        "classification": "dangerous",
                        "summary": result["summary"],
                    },
                }

            messages.append(
                ToolMessage(tool_call_id=call_id, content=json.dumps(result, ensure_ascii=False))
            )

        if tool_calls_seen >= MAX_TOOL_CALLS:
            final_messages = [
                *messages,
                HumanMessage(content="工具调用次数已达上限。请基于已获得的信息直接回答。"),
            ]
            final_response = get_chat_model(temperature=0).invoke(final_messages)
            return {
                "answer": _text_content(final_response.content).strip()
                or "终端任务达到操作上限，无法生成可用答复。",
                "pending_shell": None,
            }
