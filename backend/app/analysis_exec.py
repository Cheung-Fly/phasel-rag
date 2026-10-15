"""数据分析脚本的安全校验与执行约束（纯函数集合，双镜像共用）。

这是「数据分析」能力的专用安全层，与 shell_exec.py 并列：
- shell_exec.py 管「终端命令」—— 白名单命令 + 默认拒绝，`python` 不在白名单。
- 本模块管「数据分析脚本」—— 唯一允许跑 `python` 的通道，但脚本必须
  先过静态校验（禁外联 / 禁危险内建 / 限路径），再经审批闸门后才能执行。

设计要点（对齐 docs/终端执行-方案.md 的安全思路）：
- 分析脚本是「按需造工具」的产物（Muse 范式）：任务需要一个不存在的工具时，
  agent 现场写一个。因此脚本内容来自 LLM，**不可信**，必须像命令一样先验。
- 禁外联：数据必须已在本地 /work，分析脚本不得访问网络（requests/urllib/
  socket/httpx 等）。这同时防了「数据外传」与「把沙箱当跳板」两类风险。
- 禁危险内建：os.system / subprocess / eval / exec / __import__ / open 写模式
  等一律拒绝 —— 分析脚本只该「读数据、算、画图」，不该碰系统。
- 输出落地受控：图表/结果只允许写到 /work 内，路径经 posixpath.normpath 校验。
"""
from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

WORK_DIR = "/work"

# 外联能力的关键词（命中即拒）。覆盖常见的网络访问路径：
# requests/urllib/httpx/aiohttp（HTTP 客户端）、socket（裸套接字）、
# urllib.request、curl/wget（子进程）已由 subprocess 禁用兜底。
_NETWORK_KEYWORDS = (
    "requests", "urllib", "httpx", "aiohttp", "socket",
    "urlopen", "http.client", "ftp", "smtplib",
)

# 危险内建 / 逃逸能力（命中即拒）。
_DANGEROUS_BUILTINS = (
    "__import__", "eval", "exec", "compile", "globals", "locals",
    "getattr", "setattr", "delattr", "open(", "os.system", "subprocess",
    "os.popen", "os.spawn", "os.fork", "os.exec", "pickle", "marshal",
)

# 允许的安全导入白名单：只放行数据分析真正需要的库。
# 不在白名单的 import 一律拒绝（默认拒绝原则）。
_ALLOWED_IMPORTS = (
    "pandas", "numpy", "matplotlib", "csv", "json", "datetime",
    "math", "statistics", "collections", "itertools", "functools",
    "typing", "pathlib", "os.path", "re", "decimal", "fractions",
    "seaborn", "scipy",
)


class _Rejected(Exception):
    """脚本被安全校验拒绝（含原因）。"""


def _has_network_access(code: str) -> bool:
    """脚本是否尝试外联。"""
    lowered = code.lower()
    for kw in _NETWORK_KEYWORDS:
        if kw in lowered:
            return True
    return False


def _has_dangerous_builtin(code: str) -> bool:
    """脚本是否使用危险内建 / 逃逸能力。"""
    for kw in _DANGEROUS_BUILTINS:
        if kw in code:
            return True
    return False


def _has_disallowed_import(code: str) -> bool:
    """脚本是否 import 了白名单之外的库。

    用正则抓 `import xxx` / `from xxx import yyy`，逐条比对白名单。
    白名单里的库名若作为更长名字的前缀（如 `os` 匹配 `os.path`）允许；
    但 `os` 单独 import 是被拒的（`os` 不在 _ALLOWED_IMPORTS）。
    """
    import_pat = re.compile(
        r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))",
        re.MULTILINE,
    )
    for m in import_pat.finditer(code):
        module = m.group(1) or m.group(2)
        top = module.split(".")[0]
        if top not in _ALLOWED_IMPORTS:
            return True
    return False


def validate_analysis_script(code: str) -> str:
    """校验一段数据分析脚本，返回错误原因；通过则返回空串。

    纯函数：只做静态扫描，不执行任何代码。
    三道检查任一中标即拒，且按「外联 > 危险内建 > 越权导入」顺序返回原因。
    """
    text = str(code or "").strip()
    if not text:
        return "脚本为空"
    if _has_network_access(text):
        return "脚本尝试访问网络（requests/socket/urllib 等），数据分析只允许处理本地文件"
    if _has_dangerous_builtin(text):
        return "脚本使用了危险内建（eval/exec/subprocess/os.system 等），分析脚本只该读数据、算、画图"
    if _has_disallowed_import(text):
        return "脚本 import 了白名单之外的库，仅允许 pandas/numpy/matplotlib 等数据分析库"
    return ""


def validate_output_path(token: str) -> str:
    """校验图表/结果输出路径必须落在 /work 内，返回错误原因；通过则返回空串。"""
    try:
        base = PurePosixPath(WORK_DIR)
        candidate = PurePosixPath(token)
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = PurePosixPath(posixpath.normpath(str(candidate)))
        work = PurePosixPath(WORK_DIR)
        if not (resolved == work or str(resolved).startswith(str(work) + "/")):
            return f"输出路径越界：{token}"
    except Exception as exc:
        return f"输出路径解析失败：{token}（{exc}）"
    return ""


def summarize_script(code: str) -> str:
    """为审批卡片生成「脚本在做什么」的人话摘要（模板，不信任 LLM）。

    不做复杂分析，只提取几个高信号特征：用了哪些库、是否画图、是否写文件。
    """
    lowered = code.lower()
    parts = []
    if "matplotlib" in lowered or "plot" in lowered or "savefig" in lowered:
        parts.append("生成图表")
    if "groupby" in lowered:
        parts.append("分组聚合")
    if "mean" in lowered or "median" in lowered or "std" in lowered:
        parts.append("计算统计量")
    if "corr" in lowered:
        parts.append("计算相关性")
    if "read_csv" in lowered or "read_excel" in lowered:
        parts.append("读取数据文件")
    if not parts:
        parts.append("执行数据分析")
    return "，".join(parts)
