#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# docker-entrypoint.sh —— evolution 容器启动前初始化
# ════════════════════════════════════════════════════════════════════════════
# 两项初始化（均幂等）：
#
# 1. harness 独立 git 仓库初始化（去 DB 重构）
#    harness_data volume 挂载 /app/evolution/harnesses，首次挂载复制镜像内容
#    （只拷文件不拷 .git）。entrypoint 调 init_work_repo() 确保：
#    repo/ 有 .git、remote 配好、main 已 push 到 bare repo（executor 才能 pull）。
#
# 2. golden 基准层同步（原有逻辑）
#    golden 是镜像只读模板，每次启动从 seed 全量覆盖到 volume。
# ════════════════════════════════════════════════════════════════════════════
set -e

# ── 1. harness 独立仓库初始化 ──
echo "[entrypoint] 初始化 harness 独立 git 仓库..."
cd /app/evolution && python -c "
from app.core.git_ops import init_work_repo
init_work_repo()
print('[entrypoint] harness 仓库初始化完成')
" || echo "[entrypoint] ⚠ harness 仓库初始化失败（非致命，继续启动）"

# ── 2. golden 基准层同步 ──
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
