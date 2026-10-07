#!/usr/bin/env bash
# ============================================================
#  阶段一 RAG 知识库数据备份（2026-10-07 新增）
#
#  为什么需要：data/chroma（向量库）与 data/uploads（原始文档）
#  是整个项目里唯一不可再生的数据 —— 代码在 GitHub 上有，数据没有。
#  此前这两份数据零备份，误删/磁盘故障即彻底丢失。
#
#  备份策略：tar.gz 打包到 ~/backups，滚动保留最近 14 份。
#
#  ⚠️ 局限（必须知道）：备份落在同一块系统盘上，只能防"误删/改坏"，
#     防不了磁盘损坏或整机丢失。真正的容灾需要异地副本（Azure Blob），
#     见 docs/SERVER-CHANGES.md 的待办清单。
# ============================================================
set -euo pipefail

SRC="$HOME/phase1-rag/data"
DEST="$HOME/backups"
KEEP=14
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$DEST"

# 只打包这两个目录；显式列出是为了避免把 data/_backup_* 自己也卷进归档。
tar -czf "$DEST/rag-data-$STAMP.tar.gz" -C "$SRC" chroma uploads

# 滚动清理：按时间倒序，保留最新 $KEEP 份
ls -1t "$DEST"/rag-data-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

SIZE="$(du -h "$DEST/rag-data-$STAMP.tar.gz" | cut -f1)"
COUNT="$(ls -1 "$DEST"/rag-data-*.tar.gz | wc -l)"
echo "[$(date '+%F %T %Z')] backup ok -> rag-data-$STAMP.tar.gz (${SIZE}), 现有 ${COUNT} 份"
