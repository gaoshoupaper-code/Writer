#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# Writer harness 首次激活脚本
# ════════════════════════════════════════════════════════════════════════════
# 背景：
#   docker-compose.yml 把 evolution/harnesses/ 和 evolution/harness.git/ 挂为
#   共享命名卷，首次启动时这两个卷是空的。
#   .dockerignore 又把 evolution/harnesses/ 排除出镜像（挂卷管理）。
#   → 容器内 harness 工作目录首次为空，需要从宿主 git 仓库恢复初始源码，
#     再 init_work_repo + commit+push，executor 才能 clone 到完整包。
#
# 做的事（幂等，可重复执行）：
#   1. 把宿主 evolution/harnesses/current/ 的初始源码 docker cp 进容器共享卷
#   2. 调 init_work_repo() 创建 bare repo + git init 工作目录
#   3. commit + push 初始 harness 到 bare repo
#   4. 触发 executor pull_production() 拉取
#
# 用法（在服务器宿主上，deploy 用户或 root 均可）：
#   bash scripts/activate-harness.sh
#
# 前置：docker compose up -d 已完成，evolution + executor 容器在跑。
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

C_RED='\033[1;31m'; C_GREEN='\033[1;32m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[1;36m'; C_RESET='\033[0m'
log()  { echo -e "${C_BLUE}▶${C_RESET} $*"; }
ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*" >&2; }

# 定位项目根（脚本在 scripts/ 下，上一级是项目根）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOST_HARNESS_SRC="${PROJECT_ROOT}/evolution/harnesses/current"

EVO_CONTAINER="writer-evolution"
EXEC_CONTAINER="writer-executor"
CONTAINER_HARNESS_DIR="/app/evolution/harnesses/current"

echo -e "${C_BLUE}═══════════════════════════════════════════════════════════════${C_RESET}"
echo -e "${C_BLUE}  harness 首次激活${C_RESET}"
echo -e "${C_BLUE}═══════════════════════════════════════════════════════════════${C_RESET}"

# ── 前置检查 ──────────────────────────────────────────────────────────────────
log "前置检查..."

if [[ ! -d "$HOST_HARNESS_SRC" ]]; then
    err "宿主 harness 源码目录不存在: $HOST_HARNESS_SRC"
    err "确认在项目根目录下执行（含 evolution/harnesses/current/）"
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q "^${EVO_CONTAINER}$"; then
    err "容器 ${EVO_CONTAINER} 未运行，请先 docker compose up -d"
    exit 1
fi
ok "容器检查通过"

# ── 幂等检查：bare repo 是否已有 commit ────────────────────────────────────────
log "检查 harness 是否已激活..."
ALREADY_INIT=$(docker exec "$EVO_CONTAINER" git -C /app/evolution/harness.git log --oneline -1 2>/dev/null || echo "")
if [[ -n "$ALREADY_INIT" ]]; then
    ok "harness 已激活，最新 commit: ${ALREADY_INIT}"
    log "如需强制重新激活，请先清空共享卷后重跑。"
    exit 0
fi
warn "harness 未激活，开始初始化..."

# ── Step 1: 把初始源码从宿主复制进容器共享卷 ────────────────────────────────────
log "Step 1: 复制初始 harness 源码进容器共享卷..."

# 确保容器内目标目录存在
docker exec "$EVO_CONTAINER" mkdir -p "$CONTAINER_HARNESS_DIR"

# 打包宿主源码（排除 .git 和 __pycache__），复制进容器再解包
# 用 tar 流式传输，避免逐文件 docker cp
TAR_TMP=$(mktemp /tmp/harness-src.XXXXXX.tar.gz)
tar -czf "$TAR_TMP" \
    -C "$HOST_HARNESS_SRC" \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    .

# docker cp 到容器临时位置
docker cp "$TAR_TMP" "${EVO_CONTAINER}:/tmp/harness-src.tar.gz"
rm -f "$TAR_TMP"

# 容器内解包到共享卷工作目录
docker exec "$EVO_CONTAINER" bash -c "
    cd ${CONTAINER_HARNESS_DIR}
    # 清空可能残留的空目录内容（首次应为空，保险起见）
    find . -mindepth 1 -not -path './.git/*' -not -name '.git' -delete 2>/dev/null || true
    tar -xzf /tmp/harness-src.tar.gz
    rm -f /tmp/harness-src.tar.gz
"
ok "初始源码已复制到容器共享卷"

# ── Step 2: init_work_repo（创建 bare repo + git init 工作目录）──────────────────
log "Step 2: init_work_repo（创建 bare repo + 配置 remote）..."
docker exec "$EVO_CONTAINER" python -c "
from app.core.git_ops import init_work_repo
init_work_repo()
print('init_work_repo 完成')
"
ok "bare repo + 工作目录已初始化"

# ── Step 3: commit + push 初始 harness ─────────────────────────────────────────
log "Step 3: commit + push 初始 harness..."
COMMIT_HASH=$(docker exec "$EVO_CONTAINER" python -c "
from app.core.git_ops import commit_and_push
h = commit_and_push('initial harness from deploy')
print(h)
")
ok "初始 harness 已提交，commit: ${COMMIT_HASH}"

# ── Step 4: 触发 executor 拉取 ─────────────────────────────────────────────────
log "Step 4: 触发 executor pull_production()..."
if docker ps --format '{{.Names}}' | grep -q "^${EXEC_CONTAINER}$"; then
    docker exec "$EXEC_CONTAINER" python -c "
from app.platform.agent.git_sync import pull_production
pull_production()
print('executor 已拉取生产 harness')
"
    ok "executor 已拉取"
else
    warn "executor 容器未运行，跳过拉取（启动后会自动拉）"
fi

echo ""
echo -e "${C_GREEN}═══════════════════════════════════════════════════════════════${C_RESET}"
echo -e "${C_GREEN}  ✓ harness 首次激活完成！${C_RESET}"
echo -e "${C_GREEN}═══════════════════════════════════════════════════════════════${C_RESET}"
echo ""
echo "验证："
echo "  docker exec ${EVO_CONTAINER} git -C /app/evolution/harness.git log --oneline"
echo "  docker exec ${EXEC_CONTAINER} ls /app/executor/.harness_checkout/production/"
