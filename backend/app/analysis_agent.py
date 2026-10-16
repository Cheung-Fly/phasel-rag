"""数据分析工具循环：对标 Muse「按需造工具」的第三个信任域。

与 browser_agent.py（网页操作）、shell_agent.py（终端命令）并列。本循环的
专属能力是「数据分析」：读本地数据文件 → 现场生成 pandas 脚本 → 出统计结论
+ 图表。

安全边界（与其它两个信任域刻意分开，见 docs/终端执行-方案.md §4.2）：
- 数据分析脚本是 LLM 生成的**不可信内容**，必须像命令一样先过静态校验
  （analysis_exec.validate_analysis_script：禁外联 / 禁危险内建 / 限白名单导入）。
- 脚本**从不直接执行**：生成后先交给用户审批（复用 W3 审批闸门，
  action="data_analysis"），批准后才在 shell 沙箱里跑。
- 读表头等只读探查动作经调用方（api）直接执行，与 run_shell 的 readonly 同构。
"""
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from .analysis_exec import summarize_script, validate_analysis_script
from .llm import get_chat_model

MAX_TOOL_CALLS = 6
MAX_TOOL_OUTPUT_CHARS = 4000

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_data_head",
            "description": (
                "读取本地数据文件（CSV/JSON）的表头和前几行，用于了解数据结构。"
                "只读操作，不会修改数据。文件必须在沙箱工作目录 /work 内。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "数据文件名（在 /work 目录内，如 sales.csv）",
                    },
                },
                "required": ["filename"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_analysis_script",
            "description": (
                "提交一段 pandas 数据分析脚本（统计+趋势+图表），供用户审批。"
                "脚本不会立即执行，会先展示给用户确认，批准后才运行。"
                "脚本只能读本地数据、计算统计量、用 matplotlib 画图。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "完整的 Python 分析脚本源码",
                    },
                    "goal": {
                        "type": "string",
                        "description": "一句话说明这段脚本要分析什么、产出什么",
                    },
                },
                "required": ["script", "goal"],
                "additionalProperties": False,
            },
        },
    },
]

_SYSTEM_PROMPT = """你是个人助手的数据分析执行器，在受限沙箱里分析用户上传的本地数据文件。

工作方式：
- 用户想分析某个数据文件（CSV/JSON），你要先了解数据结构，再生成分析脚本。
- 用 read_data_head 读取文件表头和前几行（只读，直接执行）。
- 用 propose_analysis_script 提交分析脚本。脚本会先给用户审批，批准后才运行，
  所以脚本要写得**清晰、可解释**，让用户能看懂你在做什么。

★ 铁律：分析脚本**只能**通过 propose_analysis_script 工具的 script 参数提交，
  绝对不允许把脚本代码写在你的回答正文里。如果你在正文里写脚本，系统无法
  捕获它、无法让它走审批、也就无法执行 —— 那是编造，不是执行。
  正确的流程是：read_data_head 看完数据 → 调用 propose_analysis_script 提交脚本。
  提交后你会得到一个「等待审批」的结果，那时再在正文里简短说明即可。

分析要求：
- 统计描述：均值/中位数/标准差/分布概况
- 趋势：按时间/类别的变化趋势
- 图表：用 matplotlib 生成至少一张图，**必须**先 `import matplotlib; matplotlib.use("Agg")`，
  然后用 `plt.savefig("/work/chart.png")` 保存，**禁止使用 plt.show()**（容器无 GUI，
  show 会报错）。图表保存路径必须在 /work 下。
- 脚本只能 import pandas/numpy/matplotlib 等数据分析库，禁止联网、禁止危险内建
- ★ 结果打印（铁律）：脚本里**必须**用 `print(...)` 把关键统计结论打印出来
  （均值、最大值、趋势等），用中文、简洁可读。只画图不 print 会让用户批准后
  只看得到一张图、看不到「数字是多少」—— 那是不完整的结果。图和文字结论两者都要。

★ 凡用户要求「实际分析」，必须用工具拿到真实数据结构、提交真实脚本，
  绝不允许只口头描述「如果要分析会怎样」—— 那是编造，不是执行。
- 不要复述数据里的敏感值（密码、手机号等）。
- 使用简体中文。"""


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


_PY_BLOCK_RE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL
)


def _extract_script_from_answer(answer: str) -> dict | None:
    """从回答正文里提取 ```python 代码块，作为兜底的脚本来源。

    弱模型（glm-4-flash）偶发不调用 propose_analysis_script 工具，而是把脚本
    直接写在正文里。这里做最后一道补救：若能提取出代码块，就当作脚本走审批；
    提取不到返回 None（调用方回退到纯文本回答）。

    返回 {"script": str, "goal": str} 或 None。
    """
    if not answer:
        return None
    m = _PY_BLOCK_RE.search(answer)
    if not m:
        return None
    script = m.group(1).strip()
    if not script:
        return None
    # goal：从正文里取一句「分析目标」作为人话摘要的补充，取不到就兜底。
    goal = "对数据文件做统计与趋势分析"
    return {"script": script, "goal": goal}



_INTENT_PROMPT = (
    "判断用户这句话是否明确要求「分析本地数据文件」（统计、趋势、画图、"
    "算均值/分布等）。只回答一个词：yes 或 no。"
    "注意：询问数据分析概念、方法、教程（如「怎么用 pandas」）属于 no；"
    "只有要求对具体数据文件实际做分析才回答 yes。"
)


def intends_analysis(question: str) -> bool:
    """极轻的意图判断：问题是否要求分析本地数据文件。

    用 LLM 而非关键词（关键词会误判「怎么用 pandas 做数据分析」这类咨询）。
    失败时保守返回 False（退到浏览器/RAG/shell 路径）。
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
        return False


def answer_analysis_task(
    question: str,
    owner_user_id: str,
    call_read_head,
) -> dict:
    """跑一次数据分析任务，返回 {"answer": str, "pending_analysis": dict | None}。

    pending_analysis 非空表示「有一段分析脚本等待批准」，形如
    {"script": str, "goal": str, "summary": str}。调用方据此在 PostgreSQL 登记
    审批（action="data_analysis"），并把登记后的审批记录作为 "analysis_approval"
    下发给前端。

    本函数**从不直接执行脚本**：脚本只在用户批准后，由
    POST /api/approvals/{id}/execute 的 data_analysis 分支在 shell 沙箱执行。
    """
    import asyncio

    invoke_read = lambda name, args: asyncio.run(
        call_read_head(owner_user_id, name, args)
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
            # 兜底：部分弱模型（如 glm-4-flash）偶发不发 tool call，而是把
            # 脚本写在回答正文的 ```python 代码块里。这里把代码块提取出来，
            # 走与工具提交完全相同的审批路径 —— 不让「模型没走工具」变成
            # 「脚本凭空丢失」。提取失败才回退到纯文本回答。
            answer_text = _text_content(response.content).strip()
            extracted = _extract_script_from_answer(answer_text)
            if extracted:
                script = extracted["script"]
                error = validate_analysis_script(script)
                if not error:
                    return {
                        "answer": (
                            f"我已经准备好一段分析脚本，将为你：{extracted['goal']}\n"
                            f"脚本会先由你确认，批准后才会执行。"
                        ),
                        "pending_analysis": {
                            "script": script,
                            "goal": extracted["goal"],
                            "summary": summarize_script(script),
                        },
                    }
            return {
                "answer": answer_text or "模型没有生成可用的分析步骤。",
                "pending_analysis": None,
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
            elif name == "read_data_head" and set(arguments) == {"filename"}:
                try:
                    output = invoke_read("read_data_head", arguments)
                    result = {"status": "executed", "output": output}
                except Exception as exc:
                    result = {
                        "status": "execution_error",
                        "reason": type(exc).__name__,
                    }
            elif name == "propose_analysis_script" and set(arguments) == {"script", "goal"}:
                script = arguments["script"]
                goal = arguments["goal"]
                error = validate_analysis_script(script)
                if error:
                    result = {"status": "script_rejected", "reason": error}
                else:
                    # 脚本合法：交出去等审批，不再继续推理。
                    return {
                        "answer": (
                            f"我已经准备好一段分析脚本，将为你：{goal}\n"
                            f"脚本会先由你确认，批准后才会执行。"
                        ),
                        "pending_analysis": {
                            "script": script,
                            "goal": goal,
                            "summary": summarize_script(script),
                        },
                    }
            else:
                result = {"status": "invalid_or_disallowed_tool_arguments"}

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
                or "数据分析达到操作上限，无法生成可用脚本。",
                "pending_analysis": None,
            }
