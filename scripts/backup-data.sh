#!/usr/bin/env bash
# ============================================================
#  阶段一 RAG 数据备份
#  2026-10-07 更新：按「四容器架构」重定备份范围
#
#  为什么要重新定范围：拆成四容器后，不可再生数据的种类变多了 ——
#  原脚本只备 chroma + uploads，会漏掉新出现的 PostgreSQL 会话存档。
#
#  备份对象与理由：
#    1) PostgreSQL（会话存档）→ pg_dump 逻辑导出
#       这是唯一"删了就真没了"的业务数据（对话历史）。
#       ★ 必须用 pg_dump 而不是直接拷数据目录：运行中的 PostgreSQL
#         数据文件随时在变，直接 cp 得到的很可能是恢复不了的坏快照。
#    2) Chroma（向量库）→ 整目录打包
#       向量理论上可由 uploads 里的原文重新算出来，但重算要再花
#       embedding 的钱和时间，所以也备。属于"半可再生"。
#    3) uploads（用户上传的原始文件）→ 整目录打包
#       完全不可再生。
#
#  不备份 Redis：它只是热缓存，真相源在 PostgreSQL，
#  缓存丢了会在下次读取时自动回填，备份它没有意义。
#
#  保留策略：每类各留最近 14 份，超出自动删除。
#  局限：备份文件与源数据在同一块磁盘上，防不了整机/磁盘丢失。
#        异地备份（Azure Blob）仍是待办项，见 docs/SERVER-CHANGES.md。
# ============================================================
set -euo pipefail

ROOT=/home/azureuser/phase1-rag
DEST=/home/azureuser/backups
STAMP=$(date +%Y%m%d-%H%M%S)
KEEP=14

mkdir -p "$DEST"

# ---------- 1) PostgreSQL 逻辑备份 ----------
# 容器内以 root 执行 pg_dump，走 unix socket，官方镜像的 pg_hba 对 local 是 trust，
# 因此不需要密码。--clean --if-exists 让导出文件可直接覆盖恢复到已有库。
docker exec rag-postgres pg_dump -U rag -d ragdb --clean --if-exists \
  | gzip > "$DEST/rag-postgres-$STAMP.sql.gz"

# ---------- 2) Chroma 向量 + 3) uploads 原始文件 ----------
tar czf "$DEST/rag-vector-uploads-$STAMP.tar.gz" \
  -C "$ROOT/data" chroma-server uploads

# ---------- 校验：0 字节说明备份没成功 ----------
# 这一步很重要：备份脚本失败却不报错，等到真要恢复时才发现文件是空的，
# 那才是真正的灾难。宁可现在就吵。
for f in "$DEST/rag-postgres-$STAMP.sql.gz" "$DEST/rag-vector-uploads-$STAMP.tar.gz"; do
  if [ ! -s "$f" ]; then
    echo "[ERROR] 备份文件为空，备份失败: $f" >&2
    exit 1
  fi
done

# ---------- 滚动清理，每类只留最近 $KEEP 份 ----------
ls -1t "$DEST"/rag-postgres-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t "$DEST"/rag-vector-uploads-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "[$(date '+%F %T')] 备份完成 postgres=$(du -h "$DEST/rag-postgres-$STAMP.sql.gz" | cut -f1) vector+uploads=$(du -h "$DEST/rag-vector-uploads-$STAMP.tar.gz" | cut -f1) 目录合计=$(du -sh "$DEST" | cut -f1)"
