"""Shell 容器的 MCP 工具：真正执行受限命令（第二道防线）。

与 api 侧的 app/shell_exec.py 共用同一份分类器源码（构建时 COPY 进本镜像，
见 shell/Dockerfile），但**运行时物理独立**：即使 api 被攻破、网络层能直接调
本容器的 MCP 工具，本侧仍会**再分类校验一遍**才执行。

威胁模型（为什么有两个工具，见 docs/终端执行-方案.md §3/§6.6）：
- `run_shell` —— 只执行 readonly。这是**第二道防线**：即使 api 侧分类器有 bug
  把 dangerous 误判成 readonly 直接下发，本侧也会拦下（返回 approval_required）。
- `run_approved_shell` —— 执行 dangerous（但仍拒绝 forbidden）。dangerous 命令的
  授权**唯一来源是 api 侧的审批闸门（PG 里 approved 记录）**，本容器读不到 PG，
  无法（也无需）验证审批状态。这个工具的存在不降低安全性，因为它的调用入口
  `POST /api/approvals/{id}/execute` 在 api 侧已被审批状态机保护。本工具对
  dangerous 放行，但仍对 forbidden（rm/docker/命令拼接）做**最后一道兜底拒绝**——
  永久禁止的命令，连审批通过后也不放行。

执行约束（见 docs/终端执行-方案.md §6.7 / §2）：
- 永远 shell=False，shlex.split 拆 argv。
- 无状态：每条命令独立 subprocess.run(cwd=/work)，不保留跨命令状态。
- subprocess 执行超时 10 秒，强杀整棵进程树（start_new_session + killpg）。
- 输出脱敏 + 截断。
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess

from mcp.server.fastmcp import FastMCP

import shell_exec  # 与本模块同目录，构建时 COPY 进来
import analysis_exec  # 与本模块同目录，构建时 COPY 进来（数据分析专用校验）

WORK_DIR = shell_exec.WORK_DIR
EXEC_TIMEOUT_SECONDS = 10
ANALYSIS_TIMEOUT_SECONDS = 60  # 数据分析脚本可能跑较久（大表/绘图），放宽到 60s

mcp = FastMCP(
    "shell-executor",
    host="0.0.0.0",
    port=8767,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


def _run(command: str, *, allow_dangerous: bool) -> dict:
    """真正执行一条命令。

    allow_dangerous=False 时只执行 readonly（第二道防线）；
    allow_dangerous=True 时额外放行 dangerous（但仍拒绝 forbidden）。

    返回 {"status": str, "stdout": str, "exit_code": int}。
    """
    text = str(command or "").strip()
    kind = shell_exec.classify_or_forbidden(text)
    if kind == "forbidden":
        return {"status": "forbidden", "stdout": "", "exit_code": -1}
    if kind == "dangerous" and not allow_dangerous:
        return {"status": "approval_required", "stdout": "", "exit_code": -1}

    try:
        argv = shlex.split(text)
    except ValueError as exc:
        return {"status": "parse_error", "stdout": str(exc), "exit_code": -1}
    if not argv:
        return {"status": "empty", "stdout": "", "exit_code": -1}

    try:
        proc = subprocess.run(
            argv,
            shell=False,
            cwd=WORK_DIR,
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT_SECONDS,
            start_new_session=True,  # 独立进程组，便于杀整棵树
        )
    except subprocess.TimeoutExpired as exc:
        # 强杀进程组：proc 可能已超时，尝试 killpg 兜住 fork 出的子进程。
        try:
            os.killpg(exc.pid, signal.SIGKILL) if hasattr(exc, "pid") else None
        except Exception:
            pass
        return {"status": "timeout", "stdout": "", "exit_code": -1}
    except Exception as exc:
        return {"status": "exec_error", "stdout": type(exc).__name__, "exit_code": -1}

    stdout = shell_exec.redact_output(proc.stdout or "")
    stderr = shell_exec.redact_output(proc.stderr or "")
    combined = (stdout + ("\n" + stderr if stderr else "")).strip()
    combined = shell_exec.truncate_output(combined)
    return {"status": "executed", "stdout": combined, "exit_code": proc.returncode}


@mcp.tool()
def run_shell(command: str) -> str:
    """执行一条只读命令（写命令需审批，禁止命令拒绝）。"""
    result = _run(command, allow_dangerous=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def run_approved_shell(command: str) -> str:
    """执行一条已获用户批准的命令（含写命令；永久禁止的命令仍拒绝）。

    调用入口是 api 的 POST /api/approvals/{id}/execute（审批闸门保护）。
    """
    result = _run(command, allow_dangerous=True)
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 数据分析专用工具（对标 Muse「按需造工具」）
# ---------------------------------------------------------------------------

def _read_data_head(filename: str) -> dict:
    """读取 /work 内数据文件（CSV/JSON）的表头和前几行。只读。"""
    # 路径必须落在 /work 内。
    err = analysis_exec.validate_output_path(filename)
    if err:
        return {"status": "forbidden", "stdout": err, "exit_code": -1}

    import csv as _csv

    path = WORK_DIR.rstrip("/") + "/" + filename.lstrip("/")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            reader = _csv.reader(fh)
            rows = []
            for i, row in enumerate(reader):
                rows.append(row)
                if i >= 5:
                    break
        if not rows:
            return {"status": "empty", "stdout": "（文件为空）", "exit_code": 0}
        header = rows[0]
        body = rows[1:]
        lines = [f"表头：{header}"]
        lines.append(f"共预览 {len(body)} 行：")
        for r in body:
            lines.append(str(r))
        return {"status": "executed", "stdout": "\n".join(lines), "exit_code": 0}
    except FileNotFoundError:
        return {"status": "not_found", "stdout": f"文件不存在：{filename}", "exit_code": -1}
    except Exception as exc:
        return {"status": "exec_error", "stdout": type(exc).__name__, "exit_code": -1}


@mcp.tool()
def read_data_head(filename: str) -> str:
    """读取 /work 内数据文件的表头和前几行（只读，用于了解数据结构）。"""
    result = _read_data_head(filename)
    return json.dumps(result, ensure_ascii=False)


def _run_analysis(script: str) -> dict:
    """执行一段已获批准的数据分析脚本（第二道防线）。

    本工具是「数据分析」唯一允许跑 python 的入口。脚本来自 LLM（不可信），
    即使 api 侧已审批，本侧仍会**再静态校验一遍**（禁外联 / 禁危险内建 /
    限白名单导入）才执行 —— 这是与 run_shell 第二道防线同构的兜底。

    脚本写到 /work/_analysis_tmp.py 再 `python3` 执行，cwd=/work，
    这样脚本里 `read_csv('xxx.csv')` 的相对路径能正确解析到数据文件。
    """
    # 第二道防线：静态校验（与 api 侧 analysis_agent 的校验独立重复）。
    err = analysis_exec.validate_analysis_script(script)
    if err:
        return {"status": "forbidden", "stdout": err, "exit_code": -1}

    # 兜底：headless 容器无 GUI，强制 matplotlib 用 Agg 后端，避免 plt.show()
    # 触发 GUI 错误。放在脚本最前面注入，不依赖模型是否遵守 prompt。
    # 同时配置中文字体（fonts-noto-cjk），避免图表中文标签变豆腐块。
    script = (
        "import matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as _plt_cfg\n"
        "_plt_cfg.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'Noto Sans CJK JP', 'DejaVu Sans']\n"
        "_plt_cfg.rcParams['axes.unicode_minus'] = False\n"
        + script
    )

    script_path = WORK_DIR + "/_analysis_tmp.py"
    try:
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)
    except Exception as exc:
        return {"status": "write_error", "stdout": type(exc).__name__, "exit_code": -1}

    try:
        proc = subprocess.run(
            ["python3", script_path],
            shell=False,
            cwd=WORK_DIR,
            capture_output=True,
            text=True,
            timeout=ANALYSIS_TIMEOUT_SECONDS,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "stdout": "", "exit_code": -1}
    except Exception as exc:
        return {"status": "exec_error", "stdout": type(exc).__name__, "exit_code": -1}
    finally:
        # 清理临时脚本（不留脚本残留）。
        try:
            os.unlink(script_path)
        except Exception:
            pass

    stdout = shell_exec.redact_output(proc.stdout or "")
    stderr = shell_exec.redact_output(proc.stderr or "")
    combined = (stdout + ("\n" + stderr if stderr else "")).strip()
    combined = shell_exec.truncate_output(combined)
    return {"status": "executed", "stdout": combined, "exit_code": proc.returncode}


@mcp.tool()
def run_analysis(script: str) -> str:
    """执行一段已获用户批准的数据分析脚本（禁外联、限白名单导入）。

    调用入口是 api 的 POST /api/approvals/{id}/execute（审批闸门保护）。
    """
    result = _run_analysis(script)
    return json.dumps(result, ensure_ascii=False)
