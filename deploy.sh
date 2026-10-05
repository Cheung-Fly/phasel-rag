#!/usr/bin/env bash
# ============================================================
#  阶段一 一键部署 + 自检脚本（阿里百炼版）
#  用法：bash ~/phase1-rag/deploy.sh
#  前提：~/phase1-rag/.env 里已填 DASHSCOPE_API_KEY
# ============================================================
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
ok()   { echo "${GRN}  ✓${RST} $*"; }
bad()  { echo "${RED}  ✗${RST} $*"; }
warn() { echo "${YEL}  !${RST} $*"; }

echo "=============================================="
echo " 阶段一 RAG 部署自检（阿里百炼）"
echo " 目录: $APP_DIR"
echo "=============================================="
echo

# ---------- 0. 前置检查 ----------
echo "[0/5] 前置检查"

if [ ! -f .env ]; then
  bad ".env 不存在。请先 cp .env.example .env 并填入密钥"
  exit 1
fi
ok ".env 存在 (权限 $(stat -c '%a' .env))"

# 读出密钥是否已填（不打印密钥本身）
set -a; . ./.env; set +a

# 兼容检测：如果 .env 还是旧的 Groq/Google 模板，直接提示重写
if [ -n "${GROQ_API_KEY:-}${GOOGLE_API_KEY:-}" ] && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
  bad ".env 仍是旧的 Groq/Google 模板，但代码已改为阿里百炼。"
  warn "请执行： cp ${APP_DIR}/.env.example ${APP_DIR}/.env && chmod 600 ${APP_DIR}/.env && nano ${APP_DIR}/.env"
  exit 1
fi

if [ -z "${DASHSCOPE_API_KEY:-}" ]; then
  bad "DASHSCOPE_API_KEY 为空"
  warn "申请：https://bailian.console.aliyun.com/  → API-KEY 管理"
  MISSING=1
else
  ok "DASHSCOPE_API_KEY 已填写 (${#DASHSCOPE_API_KEY} 字符, 前缀 ${DASHSCOPE_API_KEY:0:4}...)"
fi
[ "${MISSING:-0}" = "1" ] && { echo; bad "密钥未填，已停止。填好后重跑本脚本。"; exit 1; }

echo "  chat 模型      : ${LLM_MODEL:-qwen-plus}"
echo "  embedding 模型 : ${EMBEDDING_MODEL:-text-embedding-v4} (dim=${EMBEDDING_DIM:-1024})"
echo "  embedding 批大小: ${EMBEDDING_BATCH_SIZE:-10}   ← 百炼单次上限 10 行，不可调大"
echo

command -v docker >/dev/null || { bad "docker 未安装"; exit 1; }
docker compose version >/dev/null 2>&1 || { bad "docker compose 插件不可用"; exit 1; }
ok "docker $(docker version --format '{{.Server.Version}}') / compose $(docker compose version --short 2>/dev/null || echo v2)"

# compose 的 user: 需要 UID/GID 可见。
# 注意：bash 里 UID 是只读变量，`export UID=...` 必然报 "readonly variable"，
# 所以这里用自定义变量 COMPOSE_USER 传 "uid:gid"。
RUN_UID="$(id -u)"
RUN_GID="$(id -g)"
export COMPOSE_USER="${RUN_UID}:${RUN_GID}"
ok "运行身份 UID=$RUN_UID GID=$RUN_GID (COMPOSE_USER=$COMPOSE_USER)"

echo
echo "  磁盘: $(df -h / | awk 'NR==2{print $4" 可用"}')"
echo "  内存: $(free -h | awk '/^Mem:/{print $7" 可用"}')"
echo "  交换: $(free -h | awk '/^Swap:/{print $2}')"
echo

# ---------- 1. 构建 ----------
echo "[1/5] 构建镜像（首次约 2-5 分钟）"
if docker compose build 2>&1 | tail -3; then
  ok "镜像构建完成"
else
  bad "构建失败，请查看上方输出"
  exit 1
fi

# ---------- 2. 启动 ----------
echo
echo "[2/5] 启动服务"
if docker compose up -d 2>&1 | tail -5; then
  ok "已发出启动指令"
else
  bad "启动失败"
  exit 1
fi

# ---------- 3. 等待健康 ----------
echo
echo "[3/5] 等待健康检查（最长 90 秒）"
HEALTHY=0
for i in $(seq 1 30); do
  STATE=$(docker inspect --format '{{.State.Health.Status}}' rag-api 2>/dev/null || echo "unknown")
  if [ "$STATE" = "healthy" ]; then HEALTHY=1; break; fi
  printf "\r  等待中... %2ds 状态=%s   " $((i*3)) "$STATE"
  sleep 3
done
echo
if [ "$HEALTHY" = "1" ]; then
  ok "容器状态 healthy"
else
  warn "未在 90 秒内 healthy，最近日志："
  docker compose logs --tail 25 api
  echo
  bad "请根据上方日志排查（常见：API Key 无效 / 网络不通）"
  exit 1
fi

# ---------- 4. 接口自检 ----------
echo
echo "[4/5] 接口自检"
H=$(curl -s --max-time 10 localhost:8000/health || echo "")
if echo "$H" | grep -q '"status":"ok"'; then
  ok "/health -> $H"
else
  bad "/health 未返回 ok: $H"
  exit 1
fi

D=$(curl -s --max-time 10 localhost:8000/api/documents || echo "")
if echo "$D" | grep -q 'documents'; then
  ok "/api/documents 可访问"
else
  bad "/api/documents 异常: $D"
fi

echo
echo "  --- 验证 embedding 能否真正调用百炼 API ---"
# 这一步真正打百炼的接口，是"密钥是否有效"的权威判据
PROBE=$(docker exec rag-api python -c "
try:
    from app.llm import get_embeddings
    v = get_embeddings().embed_query('连通性测试')
    print('EMBED_OK', len(v))
except Exception as e:
    print('EMBED_FAIL', type(e).__name__, str(e)[:300])
" 2>&1 | tail -3)
if echo "$PROBE" | grep -q 'EMBED_OK'; then
  ok "Embedding 调用成功，实际返回维度: $(echo "$PROBE" | grep -o 'EMBED_OK [0-9]*' | awk '{print $2}')"
  ACTUAL_DIM=$(echo "$PROBE" | grep -o 'EMBED_OK [0-9]*' | awk '{print $2}')
  if [ "$ACTUAL_DIM" != "${EMBEDDING_DIM:-1024}" ]; then
    warn "实际维度 ($ACTUAL_DIM) 与配置 EMBEDDING_DIM (${EMBEDDING_DIM:-1024}) 不一致！"
    warn "请把 .env 的 EMBEDDING_DIM 改成 $ACTUAL_DIM，然后删除 data/chroma 重新入库"
  fi
else
  bad "Embedding 调用失败: $PROBE"
  warn "常见原因：DASHSCOPE_API_KEY 无效 / 百炼服务未开通 / 模型名错误"
  warn "建议：浏览器打开 https://bailian.console.aliyun.com/ 确认已开通并创建 API-KEY"
fi

echo
echo "  --- 验证 LLM 能否真正调用百炼 API ---"
PROBE2=$(docker exec rag-api python -c "
try:
    from app.llm import get_chat_model
    r = get_chat_model().invoke('只回复两个字：连通')
    print('LLM_OK', r.content[:40])
except Exception as e:
    print('LLM_FAIL', type(e).__name__, str(e)[:300])
" 2>&1 | tail -3)
if echo "$PROBE2" | grep -q 'LLM_OK'; then
  ok "LLM 调用成功: $(echo "$PROBE2" | sed 's/LLM_OK //')"
else
  bad "LLM 调用失败: $PROBE2"
  warn "常见原因：DASHSCOPE_API_KEY 无效 / 模型名 ${LLM_MODEL:-qwen-plus} 不可用 / 免费额度用尽"
fi

echo
echo "  --- 验证 embedding 分批逻辑（百炼 10 行上限）---"
PROBE3=$(docker exec rag-api python -c "
try:
    from app.llm import get_embeddings
    vs = get_embeddings().embed_documents(['测试%d' % i for i in range(25)])
    print('BATCH_OK', len(vs))
except Exception as e:
    print('BATCH_FAIL', type(e).__name__, str(e)[:300])
" 2>&1 | tail -3)
if echo "$PROBE3" | grep -q 'BATCH_OK'; then
  ok "25 条批量向量化成功（分 3 批），说明 10 行上限处理正确"
else
  bad "批量向量化失败: $PROBE3"
  warn "若报 rows 超限，说明 EMBEDDING_BATCH_SIZE 被改大了，改回 10"
fi

# ---------- 5. 结果 ----------
echo
echo "[5/5] 完成"
echo
echo "=============================================="
echo " 下一步：上传一份文档做端到端验证"
echo "=============================================="
cat <<'TIP'

  # 1) 本地把测试文档传上来（在你自己的 Windows 上执行）
  #    scp -i "C:\Users\30651\.ssh\rag-vm_key.pem" .\testdoc.md azureuser@20.89.90.58:~/

  # 2) 上传入库
  curl -s -X POST localhost:8000/api/documents/upload \
    -F "file=@$HOME/testdoc.md" | python3 -m json.tool

  # 3) 提问
  curl -s -X POST localhost:8000/api/chat \
    -H 'Content-Type: application/json' \
    -d '{"question":"这份文档主要讲了什么？","session_id":"test-001"}' \
    | python3 -m json.tool

  # 4) 查看历史
  curl -s localhost:8000/api/chat/test-001/history | python3 -m json.tool

TIP
echo "查看实时日志:  cd ~/phase1-rag && docker compose logs -f api"
echo "查看资源占用:  docker stats --no-stream"
