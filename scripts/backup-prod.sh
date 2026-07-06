#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# Writer 生产数据库备份脚本（新架构 Docker 版）
# ════════════════════════════════════════════════════════════════════════════
# 背景：
#   新架构用 Docker 命名卷持久化 SQLite，容器内无 sqlite3 命令行工具
#   （Dockerfile 只装了 git+curl，但有 Python sqlite3 模块）。
#   故用 Python 的 conn.backup() 做安全热备，再 docker cp 出宿主。
#
# 备份内容：
#   - executor 元数据库（app.platform.core.db）：用户/作品/session
#   - evolution 数据库（evolution.db）：trace/监测/harness 版本
#
# 用法（deploy 用户 cron 每天 3 点自动跑）：
#   bash scripts/backup-prod.sh
# 手动跑：
#   bash scripts/backup-prod.sh
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

C_RED='\033[1;31m'; C_GREEN='\033[1;32m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[1;36m'; C_RESET='\033[0m'
log()  { echo -e "${C_BLUE}▶${C_RESET} $*"; }
ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*" >&2; }

# ── 配置 ──────────────────────────────────────────────────────────────────────
BACKUP_DIR="${HOME}/backup"
TS=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=15

EVO_CONTAINER="writer-evolution"
EXEC_CONTAINER="writer-executor"

# 容器内数据库路径
EXEC_DB="/app/executor/app.platform.core.db"
EVO_DB="/app/evolution/evolution.db"

# 容器内临时 backup 输出路径
EXEC_TMP="/tmp/exec-backup.db"
EVO_TMP="/tmp/evo-backup.db"

# ── 主流程 ────────────────────────────────────────────────────────────────────
echo "$(date '+%F %T') Writer 备份开始"

mkdir -p "$BACKUP_DIR"

# ── executor 元数据库 ─────────────────────────────────────────────────────────
if docker ps --format '{{.Names}}' | grep -q "^${EXEC_CONTAINER}$"; then
    log "备份 executor 数据库..."
    # 用 Python sqlite3 的 backup() 做安全热备（WAL 模式下也一致）
    docker exec "$EXEC_CONTAINER" python -c "
import sqlite3
src = sqlite3.connect('${EXEC_DB}', uri=True)
dst = sqlite3.connect('${EXEC_TMP}')
src.backup(dst)
dst.close()
src.close()
print('executor db backup done')
" 2>/dev/null || { err "executor 数据库备份失败"; exit 1; }

    docker cp "${EXEC_CONTAINER}:${EXEC_TMP}" "${BACKUP_DIR}/executor-${TS}.db"
    docker exec "$EXEC_CONTAINER" rm -f "$EXEC_TMP"
    SIZE=$(du -h "${BACKUP_DIR}/executor-${TS}.db" | cut -f1)
    ok "executor → ${BACKUP_DIR}/executor-${TS}.db (${SIZE})"
else
    err "容器 ${EXEC_CONTAINER} 未运行，跳过 executor 备份"
fi

# ── evolution 数据库 ──────────────────────────────────────────────────────────
if docker ps --format '{{.Names}}' | grep -q "^${EVO_CONTAINER}$"; then
    log "备份 evolution 数据库..."
    docker exec "$EVO_CONTAINER" python -c "
import sqlite3
src = sqlite3.connect('${EVO_DB}', uri=True)
dst = sqlite3.connect('${EVO_TMP}')
src.backup(dst)
dst.close()
src.close()
print('evolution db backup done')
" 2>/dev/null || { err "evolution 数据库备份失败"; exit 1; }

    docker cp "${EVO_CONTAINER}:${EVO_TMP}" "${BACKUP_DIR}/evolution-${TS}.db"
    docker exec "$EVO_CONTAINER" rm -f "$EVO_TMP"
    SIZE=$(du -h "${BACKUP_DIR}/evolution-${TS}.db" | cut -f1)
    ok "evolution → ${BACKUP_DIR}/evolution-${TS}.db (${SIZE})"
else
    err "容器 ${EVO_CONTAINER} 未运行，跳过 evolution 备份"
fi

# ── 滚动清理 ──────────────────────────────────────────────────────────────────
CLEANED=$(find "$BACKUP_DIR" -name "*.db" -mtime +${RETENTION_DAYS} -delete -print | wc -l)
[[ "$CLEANED" -gt 0 ]] && log "清理 ${CLEANED} 个超过 ${RETENTION_DAYS} 天的旧备份"

# ── 统计 ──────────────────────────────────────────────────────────────────────
TOTAL=$(du -sh "$BACKUP_DIR" | cut -f1)
COUNT=$(find "$BACKUP_DIR" -name "*.db" | wc -l)
ok "备份完成：共 ${COUNT} 份，总占用 ${TOTAL}"

echo "$(date '+%F %T') Writer 备份结束"
