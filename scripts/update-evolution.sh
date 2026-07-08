#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# update-evolution.sh —— evolution 后端增量更新（一行命令，2026-07-08）
# ════════════════════════════════════════════════════════════════════════════
# 解决"手动部署步骤太多"的痛点：备份 db → 迁移 db 路径 → 拉代码 → 重建 → 启动 → 验证。
# 全程不碰 executor/website/nginx，不删 volume，不丢数据。
#
# 用法（SSH 登录服务器后，以 deploy 用户运行）：
#   bash scripts/update-evolution.sh           # 拉远程 main 最新
#   bash scripts/update-evolution.sh --tag vX.Y.Z  # 拉指定 tag
#   bash scripts/update-evolution.sh --dry-run    # 只打印不执行
#
# 前置：
#   - 已通过 deploy-prod.sh 完成首次部署（docker-compose.yml 等就绪）
#   - 以 deploy 用户运行（有 docker 权限 + ~/Writer 仓库）
#
# 幂等：可重复跑。旧 db 迁移只做一次（检测到旧路径有 db 且新路径无才迁移）。
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── 配置 ──────────────────────────────────────────────────────────────────────
DEPLOY_DIR="${HOME}/Writer"
CONTAINER="writer-evolution"
# 容器内 db 路径（2026-07-08 改为 data/ 子目录）
NEW_DB="/app/evolution/data/evolution.db"
OLD_DB="/app/evolution/evolution.db"

# ── 参数解析 ──────────────────────────────────────────────────────────────────
DRY_RUN=false
GIT_REF="main"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift;;
        --tag)     GIT_REF="$2"; shift 2;;
        *) echo "未知参数: $1"; echo "用法: bash scripts/update-evolution.sh [--tag vX.Y.Z] [--dry-run]"; exit 1;;
    esac
done

# ── 工具函数 ──────────────────────────────────────────────────────────────────
C_BLUE='\033[1;36m'; C_GREEN='\033[1;32m'; C_YELLOW='\033[1;33m'; C_RED='\033[1;31m'; C_RESET='\033[0m'
log()  { echo -e "${C_BLUE}▶${C_RESET} $*"; }
ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*" >&2; }

run() {
    if $DRY_RUN; then
        echo -e "  ${C_YELLOW}[dry-run]${C_RESET} $*"
    else
        eval "$@"
    fi
}

# ── 前置检查 ──────────────────────────────────────────────────────────────────
log "前置检查..."
[[ -d "$DEPLOY_DIR/.git" ]] || { err "$DEPLOY_DIR 不是 git 仓库，请在 ~/Writer 下运行"; exit 1; }
command -v docker >/dev/null || { err "docker 未安装"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose 插件缺失"; exit 1; }
ok "前置检查通过"

# ── Step 1: 备份当前 db（必做，防数据丢失）────────────────────────────────────
log "Step 1: 备份数据库..."
BACKUP_FILE="${HOME}/evolution.db.bak.$(date +%Y%m%d_%H%M%S)"
# 容器可能在跑也可能没跑，分别处理
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    # 容器在跑：从容器里拷
    # 判断 db 在旧路径还是新路径
    if docker exec "$CONTAINER" test -f "$OLD_DB" 2>/dev/null; then
        run "docker cp ${CONTAINER}:${OLD_DB} ${BACKUP_FILE}"
        warn "检测到 db 在旧路径 ${OLD_DB}，稍后会迁移到 ${NEW_DB}"
        DB_AT="old"
    elif docker exec "$CONTAINER" test -f "$NEW_DB" 2>/dev/null; then
        run "docker cp ${CONTAINER}:${NEW_DB} ${BACKUP_FILE}"
        DB_AT="new"
    else
        warn "容器内未找到 db（可能是首次部署或空库），跳过备份"
        BACKUP_FILE=""
        DB_AT="none"
    fi
else
    # 容器没跑：直接从 volume 挂载点找（旧路径优先）
    warn "容器 ${CONTAINER} 未运行，尝试从 volume 找 db"
    # 用临时容器检查 volume 内容
    VOL_HAS_OLD=$(docker run --rm -v writer_evolution_data:/data alpine sh -c 'test -f /data/evolution.db && echo yes || echo no' 2>/dev/null || echo no)
    VOL_HAS_NEW=$(docker run --rm -v writer_evolution_data:/data alpine sh -c 'test -f /data/data/evolution.db && echo yes || echo no' 2>/dev/null || echo no)
    if [[ "$VOL_HAS_OLD" == "yes" ]]; then
        run "docker run --rm -v writer_evolution_data:/data -v ${HOME}:/backup alpine cp /data/evolution.db /backup/$(basename $BACKUP_FILE)"
        warn "检测到 db 在旧路径，稍后会迁移到 data/ 子目录"
        DB_AT="old"
    elif [[ "$VOL_HAS_NEW" == "yes" ]]; then
        run "docker run --rm -v writer_evolution_data:/data -v ${HOME}:/backup alpine cp /data/data/evolution.db /backup/$(basename $BACKUP_FILE)"
        DB_AT="new"
    else
        warn "volume 中未找到 db（可能是首次部署），跳过备份"
        BACKUP_FILE=""
        DB_AT="none"
    fi
fi
[[ -n "$BACKUP_FILE" ]] && ok "已备份到 ${BACKUP_FILE}" || ok "无需备份"

# ── Step 2: 迁移旧 db 到 data/ 子目录（一次性，幂等）──────────────────────────
# 仅当 db 在旧路径（/app/evolution/evolution.db）且新路径不存在时执行
if [[ "${DB_AT:-}" == "old" ]]; then
    log "Step 2: 迁移 db 到 data/ 子目录..."
    # 容器结构：旧 volume 挂在 /app/evolution，迁移后 volume 改挂 /app/evolution/data
    # 迁移逻辑：停容器 → 用临时容器把 db 从 volume 根移到 data/ 子目录 → 新 compose 挂载点生效
    run "docker compose -f ${DEPLOY_DIR}/docker-compose.yml stop ${CONTAINER/service-/}" 2>/dev/null || \
        run "cd ${DEPLOY_DIR} && docker compose stop evolution"
    # volume 名是 writer_evolution_data，把根目录的 evolution.db 移到 data/
    run "docker run --rm -v writer_evolution_data:/data alpine sh -c 'mkdir -p /data/data && if [ -f /data/evolution.db ] && [ ! -f /data/data/evolution.db ]; then mv /data/evolution.db /data/data/evolution.db; fi'"
    ok "db 已迁移到 data/ 子目录（后续 compose volume 挂载点 /app/evolution/data 生效）"
else
    ok "Step 2: 跳过 db 迁移（已是新路径或无需迁移）"
fi

# ── Step 3: 拉最新代码 ────────────────────────────────────────────────────────
log "Step 3: 拉取代码（${GIT_REF}）..."
run "cd ${DEPLOY_DIR} && git fetch origin"
run "cd ${DEPLOY_DIR} && git checkout ${GIT_REF}"
[[ "$GIT_REF" == "main" ]] && run "cd ${DEPLOY_DIR} && git pull origin main"
if ! $DRY_RUN; then
    cd "$DEPLOY_DIR"
    CURRENT_COMMIT=$(git rev-parse --short HEAD)
    ok "代码已更新到 ${GIT_REF} @ ${CURRENT_COMMIT}"
fi

# ── Step 4: 重建 evolution 镜像（只 build evolution，不碰其它服务）─────────────
log "Step 4: 重建 evolution 镜像（约 3-5 分钟）..."
run "cd ${DEPLOY_DIR} && docker compose build evolution"

# ── Step 5: 启动 evolution（只 up evolution，executor/website/nginx 不动）──────
log "Step 5: 启动 evolution..."
run "cd ${DEPLOY_DIR} && docker compose up -d evolution"

# ── Step 6: 验证 ──────────────────────────────────────────────────────────────
if ! $DRY_RUN; then
    log "Step 6: 验证（等容器启动）..."
    sleep 8

    # 健康检查
    if docker ps --format '{{.Names}}\t{{.Status}}' | grep "$CONTAINER" | grep -qi "up\|healthy"; then
        ok "容器 ${CONTAINER} 运行中"
    else
        err "容器 ${CONTAINER} 未正常运行，查看日志：docker logs ${CONTAINER}"
        docker logs --tail 30 "$CONTAINER" 2>&1 || true
        err "部署失败。可用备份恢复：docker cp ${BACKUP_FILE:-<备份文件>} ${CONTAINER}:${NEW_DB}"
        exit 1
    fi

    # 验证 db 迁移：llm_configs 表应存在，旧表 llm_config 应不存在
    log "验证数据库迁移..."
    MIGRATE_CHECK=$(docker exec "$CONTAINER" python -c "
import sqlite3
conn = sqlite3.connect('${NEW_DB}')
tabs = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'llm_config%'\").fetchall()]
print(','.join(tabs))
" 2>/dev/null || echo "ERROR")

    if [[ "$MIGRATE_CHECK" == *"llm_configs"* ]]; then
        ok "迁移成功：llm_configs 表存在"
        if [[ "$MIGRATE_CHECK" == *"llm_config"* ]] && [[ "$MIGRATE_CHECK" != *"llm_configs"* ]]; then
            warn "注意：旧表 llm_config 仍存在（可能是迁移前的残留，新代码已不使用）"
        fi
    elif [[ "$MIGRATE_CHECK" == "ERROR" ]]; then
        warn "无法验证迁移（db 可能尚未初始化，或容器内 python 报错）"
    else
        warn "llm_configs 表未找到（表内容: $MIGRATE_CHECK），可能是首次部署空库"
    fi

    # 最终状态
    echo ""
    echo -e "${C_GREEN}══════════════════════════════════════════════════${C_RESET}"
    echo -e "${C_GREEN}  evolution 更新完成${C_RESET}"
    echo -e "${C_GREEN}══════════════════════════════════════════════════${C_RESET}"
    [[ -n "$BACKUP_FILE" ]] && echo "  数据库备份：${BACKUP_FILE}"
    echo "  当前版本：${GIT_REF} @ ${CURRENT_COMMIT:-unknown}"
    echo "  服务状态：docker compose -f ${DEPLOY_DIR}/docker-compose.yml ps"
    echo ""
else
    echo ""
    echo -e "${C_YELLOW}[dry-run] 以上为将执行的步骤，实际未执行任何操作${C_RESET}"
fi
