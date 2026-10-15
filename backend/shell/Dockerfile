# Shell 执行容器：受限命令执行沙箱。
#
# 安全基线（与 browser 容器同构，见 docs/终端执行-方案.md §1/§5）：
#   - 非 root（USER shelluser）
#   - 无 docker.sock、无宿主端口、无额外 capability
#   - no-new-privileges（在 compose 里声明）
#   - 独立 shell_internal 网络（在 compose 里声明）
#   - 专用 /work 工作目录（compose 挂载 ./data/shell-work）
#   - ★ 不挂 env_file / environment —— 否则白名单里的 env 命令会泄密钥。
#
# 分类器源码（shell_exec.py）从 backend/app 复制进本镜像，与 api 侧共用
# 同一份逻辑，但运行时物理独立（第二道防线）。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 白名单命令对应的基础工具：coreutils(ls/cat/head/tail/wc/du/df/date/echo/mkdir/touch/cp/mv)
# + procps(ps/free/uptime) + findutils(find) + tar + unzip。
# 这些都是 Debian slim 里默认已装或极小的包，不引入重量级依赖。
RUN apt-get update && apt-get install -y --no-install-recommends \
      coreutils procps findutils tar unzip ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /shell
COPY shell/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ★ 分类器源码与 api 侧共用同一份：从 backend/app 复制进来。
COPY app/shell_exec.py shell_exec.py
COPY shell/mcp_server.py shell/server.py ./

RUN mkdir -p /work && useradd --create-home shelluser && \
    chown -R shelluser:shelluser /shell /work
USER shelluser

EXPOSE 8767
CMD ["python3", "server.py"]
