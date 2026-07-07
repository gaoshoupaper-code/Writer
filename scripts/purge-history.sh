#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# Writer git 历史敏感文件清除脚本（高危，手动执行）
# ════════════════════════════════════════════════════════════════════════════
# 背景：
#   monitoring.db（监测数据库）曾在 commit 3018ff4 误入 git 历史。
#   仓库为公开 GitHub 仓库，该文件已随 push 暴露在公网历史中。
#   本脚本用 git filter-repo 从所有历史 commit 中彻底移除它。
#
# ⚠️ 高危警告：
#   1. 本脚本会改写 git 历史 + force push，不可逆。
#   2. 改写后所有协作者必须重新 clone（旧 clone 含泄漏数据）。
#   3. GitHub 的 fork/cache 可能仍保留旧历史一段时间，但主仓库历史会被覆盖。
#   4. 执行前必须备份：git bundle create backup.bundle --all
#
# 用法（在本机项目根目录）：
#   bash scripts/purge-history.sh --dry-run   # 先预览会删什么
#   bash scripts/purge-history.sh             # 真正执行（含 force push）
#
# 前置：
#   1. pip install git-filter-repo  （或 apt install git-filter-repo）
#   2. 已备份仓库（git bundle create backup.bundle --all）
#   3. 确认 monitoring.db 是唯一要清的（脚本默认只清它，可扩展）
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

C_RED='\033[1;31m'; C_GREEN='\033[1;32m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[1;36m'; C_RESET='\033[0m'
log()  { echo -e "${C_BLUE}▶${C_RESET} $*"; }
ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*" >&2; }

echo -e "${C_RED}═══════════════════════════════════════════════════════════════${C_RESET}"
echo -e "${C_RED}  ⚠  GIT 历史清除（高危，不可逆）  ⚠${C_RESET}"
echo -e "${C_RED}═══════════════════════════════════════════════════════════════${C_RESET}"
$DRY_RUN && warn "DRY-RUN 模式：只预览，不执行" || warn "执行模式：将改写历史 + force push"

# 要清除的文件路径（相对仓库根）
PURGE_PATHS=("monitoring.db")

# Step 1: 检查 git-filter-repo 是否可用
log "检查 git-filter-repo..."
if ! command -v git-filter-repo &>/dev/null; then
    err "未找到 git-filter-repo。请先安装："
    echo "    pip install git-filter-repo   # 或 apt install git-filter-repo"
    exit 1
fi
ok "git-filter-repo 可用"

# Step 2: 备份（非 dry-run 必做）
if ! $DRY_RUN; then
    BACKUP="backup-$(date +%Y%m%d%H%M%S).bundle"
    log "创建备份: ${BACKUP}..."
    git bundle create "$BACKUP" --all
    ok "已备份到 ${BACKUP}（恢复用：git clone ${BACKUP} -b main）"
fi

# Step 3: 显示当前哪些 commit 含目标文件
log "扫描含目标文件的历史 commit..."
for p in "${PURGE_PATHS[@]}"; do
    echo "  ${C_YELLOW}${p}${C_RESET} 出现在："
    git log --all --oneline -- "$p" | sed 's/^/    /'
done

if $DRY_RUN; then
    echo ""
    warn "DRY-RUN 结束。确认无误后去掉 --dry-run 正式执行。"
    exit 0
fi

# Step 4: 确认
echo ""
warn "即将从所有历史中移除: ${PURGE_PATHS[*]}"
warn "并 force push 到 origin 所有分支。此操作不可逆。"
read -r -p "  输入大写 YES 确认继续: " ans
[[ "$ans" == "YES" ]] || { err "用户取消"; exit 1; }

# Step 5: 执行 filter-repo（--force 因为我们有 remote）
log "执行 git filter-repo..."
ARGS=()
for p in "${PURGE_PATHS[@]}"; do
    ARGS+=("--path" "$p" "--invert-paths")
done
git filter-repo "${ARGS[@]}" --force

# Step 6: 重新关联 remote（filter-repo 会移除 origin）
log "重新关联 origin remote..."
git remote add origin https://github.com/gaoshoupaper-code/Writer.git

# Step 7: force push 所有分支 + tag
log "force push 主分支..."
git push origin --force --all
log "force push tag..."
git push origin --force --tags

echo ""
ok "历史清除完成"
warn "★ 后续必做："
echo "  1. 本机和其他机器的旧 clone 已失效，全部重新 clone"
echo "  2. GitHub 网页检查 monitoring.db 在历史中已消失"
echo "  3. monitoring.db 本身是历史监测数据，非密钥，无需吊销任何凭据"
echo "     （但如果你曾在其中存过敏感内容，需另行评估）"
