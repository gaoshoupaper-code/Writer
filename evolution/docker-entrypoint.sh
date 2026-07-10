#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# docker-entrypoint.sh —— evolution 容器启动前初始化（2026-07-10）
# ════════════════════════════════════════════════════════════════════════════
# 解决"golden 基准层被 volume 覆盖，rebuild 后新 case 不生效"的问题。
#
# 背景：docker-compose 把 data/ 整个挂命名 volume（evolution_data）。Docker 行为
# 是首次挂载复制镜像内容，之后 volume 覆盖镜像。所以 data/evalset/golden/ 下
# 新增的 case 即使打进镜像，启动时也会被 volume 旧内容覆盖，看不到。
#
# 方案：镜像构建时把 golden 层额外复制到 volume 外的 seed 目录（/app/evalset_seed/），
# 容器启动（volume 已挂载）后从这里全量同步覆盖到 data/evalset/golden/。
# - golden：每次启动从 seed 全量覆盖（镜像只读模板 = 权威源，随 rebuild 更新）
# - growing：不碰（运行时 promote 写入的数据，持久化在 volume）
# - evolution.db：不碰（持久化在 volume）
#
# 幂等：可重复执行；rm -rf + cp 保证 golden 与镜像完全一致。
# ════════════════════════════════════════════════════════════════════════════
set -e

SEED_DIR="/app/evalset_seed/golden"
TARGET_DIR="/app/evolution/data/evalset/golden"

if [ -d "$SEED_DIR" ]; then
  echo "[entrypoint] 同步 golden 种子到 volume：$SEED_DIR → $TARGET_DIR"
  mkdir -p /app/evolution/data/evalset
  rm -rf "$TARGET_DIR"
  cp -r "$SEED_DIR" "$TARGET_DIR"
  echo "[entrypoint] golden 同步完成：$(ls "$TARGET_DIR" | wc -l) 个 case"
else
  echo "[entrypoint] 无 golden 种子目录（$SEED_DIR），跳过同步"
fi

# 交接给 CMD（uvicorn）
exec "$@"
