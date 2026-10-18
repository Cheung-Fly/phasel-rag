#!/usr/bin/env bash
# 启动前安全检查：只报告状态，不打印任何密钥。
# 用法：bash scripts/preflight.sh      退出码 1 = 存在必须修复的问题
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; fail=1; }
warn() { echo "  ! $*"; }

[ -f .env ] || { bad ".env 不存在：先执行 cp .env.example .env 并填写"; exit 1; }
perm=$(stat -c '%a' .env)
[ "$perm" = "600" ] && ok ".env 权限 600" || warn ".env 权限为 $perm，建议 chmod 600 .env"

set -a; . ./.env; set +a

[ -n "${LLM_API_KEY:-${DASHSCOPE_API_KEY:-}}" ] && ok "已配置模型密钥" \
  || warn "未配置 LLM_API_KEY（也可启动后在前端“模型”面板添加）"

[ "${#POSTGRES_PASSWORD}" -ge 16 ] && ok "POSTGRES_PASSWORD 长度足够" \
  || bad "POSTGRES_PASSWORD 为空或少于 16 位"

if [ -z "${CREDENTIAL_ENCRYPTION_KEY:-}" ]; then
  bad "CREDENTIAL_ENCRYPTION_KEY 为空（凭证库、MCP 配置、模型密钥都依赖它）"
elif printf '%s' "$CREDENTIAL_ENCRYPTION_KEY" | grep -Eq '^[A-Za-z0-9_-]{43}=$'; then
  ok "CREDENTIAL_ENCRYPTION_KEY 格式正确（请离线备份，丢失后加密数据无法恢复）"
else
  bad "CREDENTIAL_ENCRYPTION_KEY 不是有效的 Fernet 密钥"
fi

if [ "${AUTH_MODE:-session}" = "local" ]; then
  if docker compose config 2>/dev/null | grep -q "host_ip: 0.0.0.0"; then
    bad "AUTH_MODE=local 无鉴权，但有端口绑定到了 0.0.0.0"
  else
    ok "AUTH_MODE=local，且端口仅绑定 127.0.0.1"
  fi
  warn "local 模式没有登录：不要把服务直接暴露到公网；远程使用请走 SSH 隧道或带认证的反向代理"
elif [ "${AUTH_MODE:-}" = "password" ]; then
  [ "${#ACCESS_PASSWORD}" -ge 12 ] && ok "ACCESS_PASSWORD 长度满足要求" \
    || bad "AUTH_MODE=password 需要 12 位以上的 ACCESS_PASSWORD"
  [ "${AUTH_COOKIE_SECURE:-false}" = "true" ] && ok "AUTH_COOKIE_SECURE=true" \
    || warn "远程访问应经 HTTPS 并设置 AUTH_COOKIE_SECURE=true，否则口令明文传输"
else
  [ "${AUTH_COOKIE_SECURE:-false}" = "true" ] && ok "AUTH_COOKIE_SECURE=true" \
    || warn "session 模式经 HTTPS 访问时应设置 AUTH_COOKIE_SECURE=true"
fi

if [ -n "${MCP_SERVERS_JSON:-}" ] && [ "${MCP_SERVERS_JSON}" != "{}" ]; then
  warn "MCP_SERVERS_JSON 为全局配置，所有用户共享；推荐改用前端 MCP 面板（加密、按用户隔离）"
fi

docker compose config --quiet 2>/dev/null && ok "docker compose 配置有效" || bad "docker compose 配置无效"

echo
[ "$fail" = 0 ] && echo "检查通过" || echo "存在必须修复的问题"
exit "$fail"
