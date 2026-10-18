"""受限 shell 命令的分类、校验、脱敏与摘要。

这是「终端执行」能力的第一道防线（api 侧）与第二道防线（shell 容器侧）
**共用的一份源码**。设计要点：

- classify() 只认 argv 结构，**不执行 shell**。调用方必须 `shell=False`，
  用 shlex.split 拆 argv 后再匹配 —— 从根上杜绝 shell 注入。
- 默认拒绝：白名单之外一律 forbidden；拼接符（`;`/`&&`/`||`/`|`/`$(`/反引号）
  一律 forbidden。
- 读路径命令（cat/grep/find/head/tail）强制路径落在 /work 内，否则 forbidden。
- 本模块是纯函数集合，便于单测；不 import 任何重型依赖。
"""
from __future__ import annotations

import posixpath
import re
import shlex
from pathlib import PurePosixPath

# shell 容器内的专用工作目录。路径参数 resolve 后必须落在它之内。
WORK_DIR = "/work"

# ---------------------------------------------------------------------------
# 白名单（代码内常量，不是 .env —— 改白名单 = 改代码 = 走 CI/重建镜像）
# ---------------------------------------------------------------------------

# 只读命令：直接执行，不经审批。argv[0] 精确匹配。
READONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "du", "df", "free", "ps", "pwd",
    "find", "grep", "env", "echo", "date", "uptime", "uname", "whoami", "id",
}

# 写命令：走审批闸门。
DANGEROUS_COMMANDS = {"mkdir", "touch", "cp", "mv", "tar", "unzip"}

# 永久拒绝：连审批都不给。破坏性 / 逃逸 / 能触碰容器边界。
FORBIDDEN_COMMANDS = {
    "rm", "dd", "mkfs", "shutdown", "reboot", "docker", "kubectl", "ssh",
    "scp", "curl", "wget", "kill", "pkill", "killall", "mount", "umount",
    "chmod", "chown", "chroot", "nc", "ncat", "socat", "iptables", "su",
    "sudo", "systemctl", "service", "crontab",
}

# 拼接符与重定向：一律 forbidden。禁掉拼接符是最粗暴也最有效的一刀。
# （`>` / `>>` / `<` 也禁 —— 写重定向走审批也仍然危险，直接归 forbidden 更安全；
#   要写文件请用白名单里的 touch/mkdir/cp 这类「有明确语义」的命令。）
FORBIDDEN_METACHARS = {"`", "$(", ";", "&&", "||", "|", ">", "<"}

# 读路径命令：这些命令的参数里若出现「看起来是文件路径」的 token，
# 必须 resolve 后落在 /work 内，否则 forbidden。
PATH_READING_COMMANDS = {"cat", "grep", "find", "head", "tail"}

# find 允许的只读选项。带其它选项（尤其 -exec / -delete）一律 forbidden。
FIND_ALLOWED_FLAGS = {"-name", "-maxdepth", "-mindepth", "-type", "-iname", "-print"}

# 敏感词脱敏：命中则把「值」遮蔽。用于 env 输出与任意 stdout。
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|authorization|cookie|session)"
)

# 输出硬上限（字符）。超出截断。
MAX_OUTPUT_CHARS = 4000


class _Forbidden(Exception):
    """分类为 forbidden 的命令（含原因）。"""


def _looks_like_path(token: str) -> bool:
    """判断一个 argv token 是否「看起来是文件路径」。

    只做保守判断：以 /、./、../ 开头，或含 / 且非纯选项，视为路径。
    """
    if token.startswith(("/", "./", "../")):
        return True
    if "/" in token and not token.startswith("-"):
        return True
    return False


def _validate_path_arg(token: str) -> None:
    """路径参数必须 resolve 后落在 /work 内，否则 forbidden。"""
    try:
        # 相对路径相对 /work 解析；绝对路径直接解析。
        base = PurePosixPath(WORK_DIR)
        candidate = PurePosixPath(token)
        if not candidate.is_absolute():
            candidate = base / candidate
        # ★ 必须归一化：`/work/../../etc/passwd` 这种带 .. 的路径，
        #   PurePosixPath 不会自动消除 ..，不 normpath 就会被「前缀匹配」误放行。
        #   ★ 用 posixpath.normpath 而非 os.path.normpath：shell 容器是 Linux，
        #   路径是 POSIX 语义；os.path 在本机 Windows 上会按反斜杠/盘符处理，
        #   把 /work/x.txt 误转成 \work\x.txt，导致合法路径被判越界。
        resolved = PurePosixPath(posixpath.normpath(str(candidate)))
        work = PurePosixPath(WORK_DIR)
        if not (resolved == work or str(resolved).startswith(str(work) + "/")):
            raise _Forbidden(f"路径越界：{token}")
    except _Forbidden:
        raise
    except Exception as exc:
        raise _Forbidden(f"路径解析失败：{token}") from exc


def _scan_path_args(argv: list[str]) -> None:
    """对读路径命令的 argv 做路径越界扫描。"""
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        if _looks_like_path(token):
            _validate_path_arg(token)


def _contains_forbidden_metachars(command: str) -> bool:
    for meta in FORBIDDEN_METACHARS:
        if meta in command:
            return True
    return False


def classify(command: str) -> str:
    """把一条命令字符串分类为 "readonly" | "dangerous" | "forbidden"。

    纯函数：不执行、不读写文件系统（除路径字符串规范化外无副作用）。
    返回 "forbidden" 时无具体原因（原因在 classify_detail 里，供日志/审计用）。
    """
    try:
        kind, _ = classify_detail(command)
        return kind
    except _Forbidden:
        return "forbidden"


def classify_detail(command: str) -> tuple[str, str]:
    """返回 (分类, 附加信息)。

    分类 ∈ {readonly, dangerous, forbidden}；附加信息用于审计/日志（forbidden 时是原因）。
    """
    text = str(command or "").strip()
    if not text:
        raise _Forbidden("空命令")

    # 1) 拼接符 / 重定向：先于一切判断，防止「只读命令 + 拼接逃逸」。
    if _contains_forbidden_metachars(text):
        raise _Forbidden("命令拼接或重定向被禁止")

    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise _Forbidden(f"无法解析命令：{exc}") from exc
    if not argv:
        raise _Forbidden("空命令")

    cmd = argv[0]
    base = cmd.split("/")[-1] if "/" in cmd else cmd

    # 2) 永久拒绝。
    if base in FORBIDDEN_COMMANDS:
        raise _Forbidden(f"命令 {base} 被永久禁止")

    # 3) 写命令 → dangerous。
    if base in DANGEROUS_COMMANDS:
        # 写命令的路径参数也强制落在 /work 内。
        _scan_path_args(argv)
        return "dangerous", f"写命令 {base}"

    # 4) 只读命令 → 白名单 + 路径校验。
    if base in READONLY_COMMANDS:
        # 读路径命令强制路径落在 /work 内。
        if base in PATH_READING_COMMANDS:
            _scan_path_args(argv)
        # find 只允许白名单内的只读选项；-exec/-delete 等一律拒。
        if base == "find":
            for token in argv[1:]:
                if token.startswith("-") and token not in FIND_ALLOWED_FLAGS:
                    raise _Forbidden(f"find 选项 {token} 被禁止")
        # echo 允许（无重定向，已被上面的拼接符检查兜住重定向）。
        return "readonly", f"只读命令 {base}"

    # 5) 其它一切 → forbidden（默认拒绝）。
    raise _Forbidden(f"命令 {base} 不在白名单内")


def classify_or_forbidden(command: str) -> str:
    """分类，异常/未知一律返回 forbidden（供 shell 侧第二道防线兜底）。"""
    try:
        kind, _ = classify_detail(command)
        return kind
    except _Forbidden:
        return "forbidden"
    except Exception:
        return "forbidden"


def redact_output(text: str) -> str:
    """对命令输出做敏感词脱敏。

    策略：命中敏感 key 的行，把 `key=value` / `key: value` 的 value 遮蔽。
    保守起见，命中即遮蔽整行中 '=' 或 ':' 之后的部分。
    """
    if not text:
        return text
    out_lines = []
    for line in text.splitlines():
        if _SENSITIVE_KEY_RE.search(line):
            # 遮蔽 '=' 或 ':' 之后的值
            for sep in ("=", ":"):
                idx = line.find(sep)
                if idx != -1:
                    line = line[: idx + 1] + "********"
                    break
            else:
                line = line + " ********"
        out_lines.append(line)
    return "\n".join(out_lines)


def truncate_output(text: str) -> str:
    """输出硬上限截断。"""
    if not text:
        return text
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n…（输出过长已截断）"


def summarize(command: str) -> str:
    """为 dangerous 命令生成「人话翻译」摘要（后端模板，不信任 LLM）。

    用于审批卡片：让用户不用懂 shell 语法也能判断是否批准。
    """
    text = str(command or "").strip()
    try:
        argv = shlex.split(text)
    except ValueError:
        argv = text.split()
    if not argv:
        return "（无法解析的命令）"
    base = argv[0].split("/")[-1]
    args = argv[1:]

    templates = {
        "mkdir": lambda a: f"创建目录 {a[0] if a else '（未指定）'}",
        "touch": lambda a: f"创建空文件 {a[0] if a else '（未指定）'}",
        "cp": lambda a: f"把 {a[0] if a else '（源未指定）'} 复制为 {a[1] if len(a) > 1 else '（目标未指定）'}",
        "mv": lambda a: f"把 {a[0] if a else '（源未指定）'} 移动/重命名为 {a[1] if len(a) > 1 else '（目标未指定）'}",
        "tar": lambda a: f"打包/解包 tar 归档（{text}）",
        "unzip": lambda a: f"解压 zip 归档 {a[0] if a else '（未指定）'}",
    }
    if base in templates:
        try:
            return templates[base](args)
        except Exception:
            return f"执行 {base} 命令（{text}）"
    return f"执行命令：{text}"


def is_path_within_work(token: str) -> bool:
    """供测试与 shell 侧复用的路径校验布尔接口。"""
    try:
        _validate_path_arg(token)
        return True
    except _Forbidden:
        return False
