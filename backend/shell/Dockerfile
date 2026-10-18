# Shell 执行容器：受限命令执行沙箱。
#
# 安全基线（与 browser 容器同构）：
#   - 非 root（USER shelluser）
#   - 无 docker.sock、无宿主端口、无额外 capability
#   - no-new-privileges（在 compose 里声明）
#   - 独立 shell_internal 网络（在 compose 里声明）
#   - 专用 /work 工作目录（compose 挂载 ./data/shell-work）
#   - ★ 不挂 env_file / environment —— 否则白名单里的 env 命令会泄密钥。
#
# 分类器源码（shell_exec.py）从 backend/app 复制进本镜像，与 api 侧共用
# 同一份逻辑，但运行时物理独立（第二道防线）。

# Node 来自官方镜像，仅用于 `npx skills`（命令分类器只放行这一个包）。
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 白名单命令对应的基础工具：coreutils(ls/cat/head/tail/wc/du/df/date/echo/mkdir/touch/cp/mv)
# + procps(ps/free/uptime) + findutils(find) + tar + unzip。
# 这些都是 Debian slim 里默认已装或极小的包，不引入重量级依赖。
RUN apt-get update && apt-get install -y --no-install-recommends \
      coreutils procps findutils tar unzip ca-certificates \
      fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# 容器以任意 uid 运行，HOME 与 npm 缓存放到可写的 /tmp。
ENV HOME=/tmp/home npm_config_cache=/tmp/npm-cache npm_config_update_notifier=false

WORKDIR /shell
COPY shell/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ★ 分类器源码与 api 侧共用同一份：从 backend/app 复制进来。
COPY app/shell_exec.py shell_exec.py
COPY app/analysis_exec.py analysis_exec.py
COPY shell/mcp_server.py shell/server.py ./

RUN mkdir -p /work && useradd --create-home shelluser && \
    chown -R shelluser:shelluser /shell /work
USER shelluser

EXPOSE 8767
CMD ["python3", "server.py"]
