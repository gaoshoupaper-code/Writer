#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# docker-entrypoint.sh —— evolution 容器启动前初始化
# ════════════════════════════════════════════════════════════════════════════
# 三项初始化：
#
# 1. harness 独立 git 仓库初始化（去 DB 重构）
#    1a. 旧版迁移：volume 有 current/ 无 repo/ 时，清旧 bare repo + 归档 current/
#    1b. repo/ 种子初始化：repo/ 不存在时从镜像种子 /app/harness_seed/repo 复制
#    1c. git 仓库初始化：init_work_repo() 确保 repo/ 有 .git、remote 配好、
#        main 已 push 到 bare repo（executor 才能 pull）
#
# 2. golden 基准层同步（原有逻辑）
#    golden 是镜像只读模板，每次启动从 seed 全量覆盖到 volume。
# ════════════════════════════════════════════════════════════════════════════
set -e

# ── 1. harness 独立仓库初始化 ──
# 1a. 旧版迁移检测：重构前 volume 里是 current/（旧结构）+ 旧 bare repo 历史。
#     重构后改用 repo/（独立 git 仓库）。检测到 current/ 存在 = 旧版升级：
#       - 清掉旧 bare repo（旧 commit 历史已废弃，registry.json 重建为新谱系）
#       - 让 entrypoint 从种子初始化全新的 repo/
#     正常新部署/已迁移环境无 current/，跳过本段。
REPO_DIR="/app/evolution/harnesses/repo"
CURRENT_DIR="/app/evolution/harnesses/current"
BARE_REPO="/app/evolution/harness.git"
SEED_REPO="/app/harness_seed/repo"
if [ -d "$CURRENT_DIR" ] && [ ! -d "$REPO_DIR" ]; then
  echo "[entrypoint] 检测到旧版结构 current/，执行去 DB 重构迁移..."
  if [ -d "$BARE_REPO" ]; then
    echo "[entrypoint] 清理旧 bare repo（旧 commit 历史已废弃）：$BARE_REPO"
    rm -rf "$BARE_REPO"
  fi
  echo "[entrypoint] 归档旧 current/ → current.legacy/"
  mv "$CURRENT_DIR" "${CURRENT_DIR}.legacy" 2>/dev/null || true
fi

# 1b. repo/ 种子初始化：repo/ 不存在时从镜像种子复制（首次部署或刚完成旧版迁移）。
#     已存在的 repo/ 不覆盖（保留进化历史）。
if [ ! -d "$REPO_DIR" ]; then
  if [ -d "$SEED_REPO" ]; then
    echo "[entrypoint] repo/ 不存在，从种子初始化：$SEED_REPO → $REPO_DIR"
    mkdir -p /app/evolution/harnesses
    cp -r "$SEED_REPO" "$REPO_DIR"
    echo "[entrypoint] repo/ 种子复制完成"
  else
    echo "[entrypoint] ⚠ repo/ 不存在且无种子（$SEED_REPO），将尝试空目录初始化"
    mkdir -p "$REPO_DIR"
  fi
fi

# 1c. git 仓库初始化（repo/ 有 .git 则跳过，无则 init + 首次 commit + push）
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
