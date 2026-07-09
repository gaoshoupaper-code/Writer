#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# Writer 生产部署主脚本（一键，5 Phase 幂等）—— 入侵后安全重建版
# ════════════════════════════════════════════════════════════════════════════
# 适用场景：
#   A) 重做系统后的干净服务器（推荐，Phase 1 清理步骤会幂等跳过）
#   B) 从旧版单体架构切换到新版三服务 Docker 架构
# 详见设计文档：.claude/md/20260707_003000_deploy_execution_design.md
#
# 用法（以 root 登录服务器后）：
#   bash scripts/deploy-prod.sh              # 全流程 Phase 0→4
#   bash scripts/deploy-prod.sh --from 2     # 从指定 Phase 继续（断点恢复）
#   bash scripts/deploy-prod.sh --dry-run    # 只打印命令不执行
#
# 前置条件：
#   1. 本地已 push main + 打 tag v0.1.0（历史已清除 monitoring.db 等敏感残留）
#   2. 以 root 登录服务器（重做系统后默认端口 22）
#   3. DNS 已把 siyen.site 指向当前服务器 IP（重做系统后 IP 可能变，核对 SERVER_IP）
#
# 安全加固（本脚本自动执行，区别于旧版「保守加固」）：
#   Phase 0  系统更新 + unattended-upgrades 自动安全补丁 + ufw 防火墙 + fail2ban
#   Phase 4  SSH 改端口 22222 + 禁 root + 禁密码 + AllowUsers + 加固 fail2ban sshd jail
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── 全局变量（按需修改）──────────────────────────────────────────────────────
readonly DEPLOY_TAG="${DEPLOY_TAG:-v0.1.0}"
readonly DOMAIN="siyen.site"
readonly SERVER_IP="111.228.4.165"
readonly CERTBOT_EMAIL="17699237427@163.com"
readonly DEPLOY_USER="deploy"
readonly REPO_URL="https://github.com/gaoshoupaper-code/Writer.git"
readonly DEPLOY_DIR="/home/${DEPLOY_USER}/Writer"
# SSH 安全：新端口（避开 22 端口默认爆破扫描），旧版 22 在 Phase 4 加固后关闭
readonly SSH_NEW_PORT="${SSH_NEW_PORT:-22222}"

# ── 运行模式 ──────────────────────────────────────────────────────────────────
DRY_RUN=false
START_PHASE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift;;
        --from)    START_PHASE="$2"; shift 2;;
        *) echo "未知参数: $1"; exit 1;;
    esac
done

# ── 工具函数 ──────────────────────────────────────────────────────────────────
C_RED='\033[1;31m'; C_GREEN='\033[1;32m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[1;36m'; C_RESET='\033[0m'

log()  { echo -e "${C_BLUE}▶${C_RESET} $*"; }
ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*" >&2; }

# run：执行命令（dry-run 模式只打印）
run() {
    if $DRY_RUN; then
        echo -e "  ${C_YELLOW}[dry-run]${C_RESET} $*"
    else
        eval "$@"
    fi
}

# confirm：高危操作前确认（dry-run 自动跳过）
confirm() {
    local msg="$1"
    if $DRY_RUN; then
        echo -e "  ${C_YELLOW}[dry-run]${C_RESET} 跳过确认: $msg"
        return 0
    fi
    echo -e "${C_YELLOW}⚠ ${msg}${C_RESET}"
    read -r -p "  确认执行？[y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { err "用户取消"; exit 1; }
}

# phase_header：打印 Phase 标题（断点恢复时跳过已完成的）
phase_header() {
    local num="$1"; local title="$2"
    if [[ "$num" -lt "$START_PHASE" ]]; then
        echo -e "${C_YELLOW}⏭  Phase $num 已跳过（断点恢复从 Phase $START_PHASE 开始）${C_RESET}"
        return 1
    fi
    echo ""
    echo -e "${C_BLUE}═══════════════════════════════════════════════════════════════${C_RESET}"
    echo -e "${C_BLUE}  Phase $num: $title${C_RESET}"
    echo -e "${C_BLUE}═══════════════════════════════════════════════════════════════${C_RESET}"
    return 0
}

# ── Phase 4 专用：只读状态探针（dry-run 也可安全调用）──────────────────────────
# drop-in 配置文件路径（与 phase4 内写入路径一致，改一处即可）
readonly SSH_DROPIN="/etc/ssh/sshd_config.d/99-writer-hardening.conf"

# sshd_listening_on <port>：sshd 是否正在监听指定端口（只读）
sshd_listening_on() {
    local port="$1"
    ss -H -tlnp 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
}

# ufw_allows <port>：ufw 是否放行了指定 tcp 端口（只读）
ufw_allows() {
    local port="$1"
    ufw status 2>/dev/null | grep -qE "(^|[^0-9])${port}/tcp"
}

# dropin_exists：本次加固 drop-in 文件是否存在（只读）
dropin_exists() {
    [[ -f "$SSH_DROPIN" ]]
}

# dropin_has_port：drop-in 是否已声明目标新端口（只读，用于幂等判断）
dropin_has_port() {
    dropin_exists && grep -qE "^Port\s+${SSH_NEW_PORT}\s*$" "$SSH_DROPIN"
}

# rollback_sshd_hardening <did_write>：删本次写入的 drop-in 并重启 sshd 恢复 22
#   <did_write> = 1 表示本次确实 cat 写过 drop-in，才允许删；=0 不删（防误删他人配置）
rollback_sshd_hardening() {
    local did_write="${1:-0}"
    err "sshd 加固失败，启动自动回滚…"
    if [[ "$did_write" == "1" ]]; then
        rm -f "$SSH_DROPIN"
        warn "已删除本次写入的 drop-in: $SSH_DROPIN"
    fi
    # sshd 可能因新配置起不来，restart 让它在旧配置（22 端口）上恢复
    systemctl restart sshd 2>/dev/null || true
    sleep 2
    if sshd_listening_on 22; then
        warn "已回滚：sshd 在 22 端口监听。请用 ssh -p 22 root@${SERVER_IP} 重连后排查"
    else
        err "sshd 回滚后 22 端口仍无监听——可能需要云控制台 VNC 救场"
    fi
}

# ════════════════════════════════════════════════════════════════════════════
# Phase 0: 基线准备（系统加固 + 装 Docker + 建 deploy 用户 + 防火墙）
# ════════════════════════════════════════════════════════════════════════════
phase0() {
    phase_header 0 "基线准备（系统加固 + Docker + deploy 用户 + 防火墙）" || return 0

    # 0.1 系统更新（重做系统后首要：补齐所有已知 CVE）
    log "系统更新（apt upgrade，修补已知漏洞）..."
    run 'apt-get update -qq'
    run 'apt-get upgrade -y'
    run 'apt-get install -y ca-certificates curl gnupg ufw fail2ban unattended-upgrades'

    # 0.2 启用自动安全补丁（每日自动安装安全更新，防再次因未打补丁被入侵）
    log "启用 unattended-upgrades 自动安全补丁..."
    if ! $DRY_RUN; then
        dpkg-reconfigure -fnoninteractive unattended-upgrades >/dev/null 2>&1 || true
        # 确保配置允许安全更新
        cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
        ok "自动安全补丁已启用"
    fi

    # 0.3 装 Docker + Compose 插件（幂等）
    if ! command -v docker &>/dev/null; then
        log "安装 Docker..."
        run 'curl -fsSL https://get.docker.com | sh'
        run 'systemctl enable --now docker'
    else
        ok "Docker 已安装: $(docker --version)"
    fi

    # 0.4 验证 Docker
    if ! $DRY_RUN; then
        docker --version || { err "Docker 安装失败"; exit 1; }
        docker compose version || { err "Compose 插件缺失"; exit 1; }
    fi
    ok "Docker 就绪"

    # 0.5 建 deploy 用户（幂等）
    if ! id "$DEPLOY_USER" &>/dev/null; then
        log "创建 deploy 用户..."
        run "useradd -m -s /bin/bash $DEPLOY_USER"
    else
        ok "deploy 用户已存在"
    fi
    run "usermod -aG docker $DEPLOY_USER"

    # 0.6 配置 UFW 防火墙（默认拒绝，只放行 SSH/HTTP/HTTPS）
    log "配置 UFW 防火墙..."
    if ! $DRY_RUN; then
        # 默认策略
        ufw --force reset >/dev/null 2>&1
        ufw default deny incoming
        ufw default allow outgoing
        # 放行端口（注意：此时 SSH 还是 22 端口，先放行 22 防锁死，Phase 4 加固后再放行 22222 并删 22）
        ufw allow 22/tcp comment 'SSH (temp, will switch to 22222 in Phase 4)'
        ufw allow 80/tcp comment 'HTTP'
        ufw allow 443/tcp comment 'HTTPS'
        ufw --force enable
        ufw status verbose
        ok "UFW 已启用（放行 22/80/443）"
    fi

    # 0.7 上传 deploy 公钥（需本地操作，这里只提示）
    echo ""
    warn "★ 手动步骤：请在本机（新终端）执行，上传 deploy 公钥："
    echo "    # 本地先生成密钥（若没有）："
    echo "    ssh-keygen -t ed25519 -f ~/.ssh/writer_deploy -N ''"
    echo "    # 上传到 deploy 用户："
    echo "    ssh-copy-id -i ~/.ssh/writer_deploy.pub deploy@${SERVER_IP}"
    echo ""
    confirm "deploy 公钥已上传，能用 'ssh deploy@${SERVER_IP}' 登录？"

    ok "Phase 0 完成"
}

# ════════════════════════════════════════════════════════════════════════════
# Phase 1: 清理旧架构（停服务 + 删 systemd + 删目录 + 清 cron）
# ════════════════════════════════════════════════════════════════════════════
phase1() {
    phase_header 1 "清理旧架构（停服务 + 删 systemd + 删目录 + 清 cron）" || return 0

    confirm "即将停掉并删除旧版 Writer 服务（/root/Writer 838M + 数据全丢），确认？"

    # 1.1 停服务（顺序：先应用后 nginx）
    log "停止旧服务..."
    run 'systemctl stop writer-backend writer-frontend 2>/dev/null || true'
    run 'systemctl stop nginx 2>/dev/null || true'

    # 1.2 禁自启
    run 'systemctl disable writer-backend writer-frontend 2>/dev/null || true'

    # 1.3 删 systemd unit
    log "删除 systemd unit..."
    run 'rm -f /etc/systemd/system/writer-backend.service'
    run 'rm -f /etc/systemd/system/writer-frontend.service'
    run 'systemctl daemon-reload'

    # 1.4 删 nginx 旧站点（保留 nginx 二进制，决策：A 保留）
    log "删除 nginx 旧站点配置..."
    run 'rm -f /etc/nginx/sites-enabled/writer'
    # nginx 留着二进制但不跑站点（重启后会空转在 80，给 certbot webroot 或后续用）
    run 'nginx -t 2>/dev/null && systemctl start nginx 2>/dev/null || true'

    # 1.5 删旧代码 + 旧库 + 备份
    log "删除旧代码目录和备份..."
    run 'rm -rf /root/Writer'
    run 'rm -rf /root/backup'
    run 'rm -f /usr/local/bin/writer-backup.sh'

    # 1.6 清 cron
    log "清理旧 cron..."
    run 'crontab -r 2>/dev/null || true'

    ok "Phase 1 完成（旧架构已清理）"
}

# ════════════════════════════════════════════════════════════════════════════
# Phase 2: 签发 HTTPS 证书（certbot standalone）
# ════════════════════════════════════════════════════════════════════════════
phase2() {
    phase_header 2 "签发 HTTPS 证书（certbot standalone）" || return 0

    # 幂等：已有证书则跳过
    if [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
        ok "证书已存在，跳过签发"
        return 0
    fi

    # 2.1 装 certbot
    if ! command -v certbot &>/dev/null; then
        log "安装 certbot..."
        run 'apt-get update -qq && apt-get install -y certbot'
    fi

    # 2.2 确保 80 端口空出（停 nginx）
    log "临时停止 nginx 以让 certbot 占用 80 端口..."
    run 'systemctl stop nginx 2>/dev/null || true'

    # 2.3 签发
    log "签发证书（域名: ${DOMAIN}）..."
    confirm "certbot 将占用 80 端口验证域名所有权，确认 DNS 已指向 ${SERVER_IP}？"
    run "certbot certonly --standalone -d ${DOMAIN} --email ${CERTBOT_EMAIL} --agree-tos --no-eff-email"

    # 2.4 验证
    if ! $DRY_RUN; then
        [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]] \
            && ok "证书签发成功" \
            || { err "证书签发失败"; exit 1; }
    fi

    # 2.5 权限
    run 'chmod -R 755 /etc/letsencrypt/live /etc/letsencrypt/archive'

    ok "Phase 2 完成"
}

# ════════════════════════════════════════════════════════════════════════════
# Phase 3: 部署新版（deploy 用户拉代码 + 配 .env + compose build/up）
# ════════════════════════════════════════════════════════════════════════════
phase3() {
    phase_header 3 "部署新版（拉代码 + 配 .env + compose build/up）" || return 0

    # Phase 3 需要切到 deploy 用户执行。本脚本以 root 跑，故用 su - deploy -c 包裹。
    # 但密钥生成和 .env 配置需要交互，所以本 Phase 拆成「root 准备」+「deploy 执行」两段。

    # 3.1 root 侧：克隆代码（若不存在）
    if [[ ! -d "${DEPLOY_DIR}/.git" ]]; then
        log "以 deploy 身份克隆代码..."
        run "su - ${DEPLOY_USER} -c 'git clone ${REPO_URL} ${DEPLOY_DIR}'"
    else
        ok "代码目录已存在"
    fi
    run "su - ${DEPLOY_USER} -c 'cd ${DEPLOY_DIR} && git fetch --tags && git checkout ${DEPLOY_TAG}'"

    # 3.2 root 侧：生成密钥（强随机）
    log "生成密钥（若 .env 未配置）..."
    echo ""
    warn "★ 接下来生成系统密钥。LLM API Key 不进服务器（用户各自在客户端填）。"
    echo ""

    GEN_MASTER_KEY=""
    GEN_EVOLUTION_MASTER_KEY=""
    GEN_NOTIFY_TOKEN=""
    GEN_ADMIN_PWD=""
    if ! $DRY_RUN; then
        GEN_MASTER_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))")
        GEN_EVOLUTION_MASTER_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))")
        GEN_NOTIFY_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
        GEN_ADMIN_PWD=$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")
        echo -e "  ${C_GREEN}MASTER_KEY (executor)${C_RESET}       = ${GEN_MASTER_KEY}"
        echo -e "  ${C_GREEN}EVOLUTION_MASTER_KEY${C_RESET}        = ${GEN_EVOLUTION_MASTER_KEY}"
        echo -e "  ${C_GREEN}NOTIFY_TOKEN${C_RESET}                = ${GEN_NOTIFY_TOKEN}"
        echo -e "  ${C_GREEN}ADMIN_PASSWORD${C_RESET}              = ${GEN_ADMIN_PWD}"
        echo ""
        warn "请立即保存以上密钥！MASTER_KEY / EVOLUTION_MASTER_KEY 一旦设定不可更改。"
        echo ""
        warn "★ 还需手动配置 ALLOWED_USER_IDS（你的 executor user_id，逗号分隔）。"
        echo "  获取方式：登录 executor 后访问 GET /api/auth/me，取 user_id 字段。"
    fi

    # 3.3 配置 executor/.env
    # 架构说明：普通用户写作时用各自在客户端填的 key（DB 加密存储，MASTER_KEY 解密），
    # 不依赖服务器全局 OPENAI_API_KEY。故此处置空，key 永不集中存服务器。
    # 桌面化改造（2026-07-07）：删掉硬编码默认 model/base_url（清理项，需求决策）。
    log "配置 executor/.env（LLM key + 默认值全置空——用户各自填）..."
    if ! $DRY_RUN; then
        su - "$DEPLOY_USER" -c "cp ${DEPLOY_DIR}/executor/.env.production.example ${DEPLOY_DIR}/executor/.env"
        # 注入生成的密钥
        sed -i "s|^MASTER_KEY=.*|MASTER_KEY=${GEN_MASTER_KEY}|" "${DEPLOY_DIR}/executor/.env"
        sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${GEN_ADMIN_PWD}|" "${DEPLOY_DIR}/executor/.env"
        # 预填已知值
        sed -i 's|^WRITER_AGENT_MODE=.*|WRITER_AGENT_MODE=live|' "${DEPLOY_DIR}/executor/.env"
        # 桌面化改造：不再注入硬编码默认 model/base_url，全留空（用户各自在桌面端填）
        sed -i 's|^OPENAI_API_KEY=.*|OPENAI_API_KEY=|' "${DEPLOY_DIR}/executor/.env"
        sed -i 's|^OPENAI_BASE_URL=.*|OPENAI_BASE_URL=|' "${DEPLOY_DIR}/executor/.env"
        sed -i 's|^WRITER_MODEL=.*|WRITER_MODEL=|' "${DEPLOY_DIR}/executor/.env"
        echo ""
        ok "executor/.env 已配置（LLM 默认值全清空——用户在桌面端各自填）"
        echo ""
    fi

    # 3.4 配置 evolution/.env（桌面化改造 2026-07-07）
    log "配置 evolution/.env..."
    if ! $DRY_RUN; then
        su - "$DEPLOY_USER" -c "cp ${DEPLOY_DIR}/evolution/.env.production.example ${DEPLOY_DIR}/evolution/.env"
        # 桌面化鉴权三项（替换旧 INTERNAL_API_KEY 机制）
        sed -i "s|^EVOLUTION_MASTER_KEY=.*|EVOLUTION_MASTER_KEY=${GEN_EVOLUTION_MASTER_KEY}|" "${DEPLOY_DIR}/evolution/.env"
        sed -i "s|^NOTIFY_TOKEN=.*|NOTIFY_TOKEN=${GEN_NOTIFY_TOKEN}|" "${DEPLOY_DIR}/evolution/.env"
        # ALLOWED_USER_IDS 需手动填（脚本无法自动获取 user_id，提示用户）
        echo ""
        warn "★ 请手动编辑 ${DEPLOY_DIR}/evolution/.env 填入 ALLOWED_USER_IDS"
        echo "  （你的 executor user_id，逗号分隔多个）"
        echo ""
    fi

    # 3.5 挂证书（软链接）
    log "挂载证书到 certs/..."
    if ! $DRY_RUN; then
        su - "$DEPLOY_USER" -c "mkdir -p ${DEPLOY_DIR}/certs"
        ln -sf "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" "${DEPLOY_DIR}/certs/fullchain.pem"
        ln -sf "/etc/letsencrypt/live/${DOMAIN}/privkey.pem" "${DEPLOY_DIR}/certs/privkey.pem"
        # 让 deploy 用户能读软链接目标
        chown -R "$DEPLOY_USER":"$DEPLOY_USER" "${DEPLOY_DIR}/certs"
    fi

    # 3.6 构建 + 启动
    log "docker compose build（首次较慢，约 5-10 分钟）..."
    run "su - ${DEPLOY_USER} -c 'cd ${DEPLOY_DIR} && docker compose build'"

    log "docker compose up -d..."
    run "su - ${DEPLOY_USER} -c 'cd ${DEPLOY_DIR} && docker compose up -d'"

    # 3.7 验证容器
    log "检查容器状态..."
    if ! $DRY_RUN; then
        sleep 10  # 给容器一点启动时间
        su - "$DEPLOY_USER" -c "cd ${DEPLOY_DIR} && docker compose ps"
    fi

    # 3.8 首次激活 harness
    log "首次激活 harness..."
    if ! $DRY_RUN; then
        # 等容器健康
        log "等待容器健康（最多 60s）..."
        for i in $(seq 1 12); do
            if curl -sf http://localhost:7789/health >/dev/null 2>&1; then
                ok "evolution 健康检查通过"
                break
            fi
            sleep 5
        done
        if [[ -f "${DEPLOY_DIR}/scripts/activate-harness.sh" ]]; then
            run "bash ${DEPLOY_DIR}/scripts/activate-harness.sh"
        else
            warn "activate-harness.sh 未找到，跳过（可后续手动执行）"
        fi
    fi

    ok "Phase 3 完成"
}

# ════════════════════════════════════════════════════════════════════════════
# Phase 4: 上线后加固（SSH 最强加固 + 防火墙切端口 + fail2ban + 证书续期 + 备份）
# ════════════════════════════════════════════════════════════════════════════
phase4() {
    phase_header 4 "上线后加固（SSH 最强 + ufw 切端口 + fail2ban + 续期 + 备份）" || return 0

    # 4.0 状态探针（纯只读，断点恢复时让用户看清当前在哪一步）
    log "Phase 4 当前状态探针："
    if ! $DRY_RUN; then
        echo -n "  sshd 监听端口: "; ss -H -tlnp 2>/dev/null | grep -oE 'sshd' >/dev/null \
            && ss -H -tlnp 2>/dev/null | awk '{print $4}' | grep -oE '[0-9]+$' | sort -u | tr '\n' ' ' \
            || echo "(未检测到 sshd)"
        echo ""
        echo -n "  drop-in 已写入: "; dropin_exists && echo "是（$(dropin_has_port && echo "Port=$SSH_NEW_PORT" || echo "内容不符)")" || echo "否"
        echo -n "  ufw 放行 22:   "; ufw_allows 22    && echo "是" || echo "否"
        echo -n "  ufw 放行 ${SSH_NEW_PORT}: "; ufw_allows "${SSH_NEW_PORT}" && echo "是" || echo "否"
    fi

    # 4.1 SSH 加固（原子 + 幂等 + 自动回滚；高危！需先开第二终端验证密钥）
    #
    # 不变量：在 22222 端到端人工验证通过前，绝不动 22 的可达性。
    # 这样哪怕中途 sshd 切端口失败 + drop-in 回滚，22 这条退路始终通。

    # ① 先确认密钥已就绪（前置守卫，dry-run 跳过）
    echo ""
    warn "★ SSH 加固高危操作：即将改端口 ${SSH_NEW_PORT} + 禁 root + 禁密码登录。"
    echo "  一旦生效，旧 22 端口、密码、root 全部失效。"
    echo "  必须先在本机开第二终端，用 deploy 密钥通过当前端口验证能登录："
    echo "    ssh -i ~/.ssh/writer_deploy deploy@${SERVER_IP}   # 旧 22 端口先验证一次"
    echo ""
    echo "  验证通过后再继续。验证不通过 = 密钥没配好，继续会锁死！"
    echo ""
    confirm "已用 deploy 密钥通过 22 端口验证登录成功？（没验证就别继续！）"

    # ② 备份 sshd_config（幂等：用固定标记名，避免每次重跑堆时间戳备份）
    log "备份 sshd_config（幂等，仅首次）..."
    if ! $DRY_RUN; then
        if [[ ! -f /etc/ssh/sshd_config.bak.writer ]]; then
            cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.writer
            ok "已备份 → /etc/ssh/sshd_config.bak.writer"
        else
            ok "备份已存在，跳过（/etc/ssh/sshd_config.bak.writer）"
        fi
    fi

    # ③ 放行新端口 22222（幂等）
    log "UFW 放行新 SSH 端口 ${SSH_NEW_PORT}（幂等）..."
    if ! $DRY_RUN; then
        if ufw_allows "${SSH_NEW_PORT}"; then
            ok "${SSH_NEW_PORT}/tcp 已放行，跳过"
        else
            ufw allow "${SSH_NEW_PORT}/tcp" comment 'SSH hardened'
            ok "${SSH_NEW_PORT}/tcp 已放行"
        fi
    fi

    # ④ 安全网：确认旧端口 22 仍放行（幂等）
    #    切换全程保持 22 可达——这是 drop-in 回滚后 sshd 回 22 时的救生通道
    log "安全网：确认 22/tcp 仍放行（切端口期间的救生通道）..."
    if ! $DRY_RUN; then
        if ufw_allows 22; then
            ok "22/tcp 已放行（安全网就位）"
        else
            ufw allow 22/tcp comment 'SSH safety-net until 22222 verified'
            ok "22/tcp 已补放行（安全网）"
        fi
    fi

    # ⑤ 写 drop-in（幂等：内容已匹配则跳过；否则写入并标记本次改动）
    log "写 sshd drop-in 加固配置（幂等）..."
    DID_WRITE_DROPIN=0
    if ! $DRY_RUN; then
        mkdir -p /etc/ssh/sshd_config.d
        if dropin_has_port; then
            ok "drop-in 已存在且 Port=${SSH_NEW_PORT}，跳过"
        else
            cat > "$SSH_DROPIN" <<EOF
# Writer 安全加固（drop-in，覆盖主配置）
Port ${SSH_NEW_PORT}
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AllowUsers ${DEPLOY_USER}
KbdInteractiveAuthentication no
PermitEmptyPasswords no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
X11Forwarding no
AllowTcpForwarding no
AllowAgentForwarding no
EOF
            DID_WRITE_DROPIN=1
            # 确保主配置加载 drop-in（Ubuntu 默认已加载，保险起见）
            grep -q "Include /etc/ssh/sshd_config.d/\*.conf" /etc/ssh/sshd_config \
                || echo "Include /etc/ssh/sshd_config.d/*.conf" >> /etc/ssh/sshd_config
            ok "drop-in 已写入"
        fi
    fi

    # ⑥ sshd 语法校验（失败：仅当本次写了 drop-in 才回滚）
    log "sshd -t 语法校验..."
    if ! $DRY_RUN; then
        if ! sshd -t 2>/dev/null; then
            err "sshd 配置语法校验失败！"
            if [[ "$DID_WRITE_DROPIN" == "1" ]]; then
                rollback_sshd_hardening "$DID_WRITE_DROPIN"
            else
                warn "drop-in 非本次写入，未自动删除——请人工检查 $SSH_DROPIN"
            fi
            exit 1
        fi
        ok "sshd 语法校验通过"
    fi

    # ⑦ 重启 sshd 生效
    log "重启 sshd（切换到 ${SSH_NEW_PORT}）..."
    run 'systemctl restart sshd'

    # ⑧ 本地自测：机器级验证 22222 真的在监听（失败自动回滚）
    if ! $DRY_RUN; then
        sleep 2
        if sshd_listening_on "${SSH_NEW_PORT}"; then
            ok "本地自测通过：sshd 已监听 ${SSH_NEW_PORT}"
        else
            err "本地自测失败：sshd 未在 ${SSH_NEW_PORT} 监听（可能起不来）"
            rollback_sshd_hardening "$DID_WRITE_DROPIN"
            exit 1
        fi
    fi

    # ⑨ 人工双终端验证（最后一道防线：外部可达性）
    echo ""
    warn "★ sshd 已在 ${SSH_NEW_PORT} 监听。现在开第二终端验证 deploy 密钥登录："
    echo "    ssh -i ~/.ssh/writer_deploy -p ${SSH_NEW_PORT} deploy@${SERVER_IP}"
    echo ""
    echo "  验证通过后，本脚本会关闭旧 22 端口。"
    echo "  若验证失败：当前 root 终端仍可用，可手动回滚 →"
    echo "    rm ${SSH_DROPIN} && systemctl restart sshd"
    echo ""
    confirm "已用新端口 ${SSH_NEW_PORT} + deploy 密钥登录成功？"

    # ⑩ 删旧端口 22（仅 ⑨ 通过后；幂等）
    log "关闭旧 22 端口（幂等）..."
    if ! $DRY_RUN; then
        if ufw_allows 22; then
            ufw delete allow 22/tcp 2>/dev/null || true
            ok "22/tcp 已关闭，仅 ${SSH_NEW_PORT} 可达"
        else
            ok "22/tcp 本就未放行，跳过"
        fi
        ufw status numbered
    fi

    # 4.2 fail2ban 加固 sshd jail（防爆破，即便改了端口仍会扫到；幂等）
    log "配置 fail2ban sshd jail（幂等）..."
    if ! $DRY_RUN; then
        local jail_file="/etc/fail2ban/jail.d/sshd.local"
        local jail_content="[sshd]
enabled = true
port = ${SSH_NEW_PORT}
filter = sshd
backend = systemd
maxretry = 4
findtime = 10m
bantime = 1h
bantime.increment = true
bantime.maxtime = 1w"
        if [[ -f "$jail_file" ]] && [[ "$(cat "$jail_file")" == "$jail_content" ]]; then
            ok "jail 配置已是目标内容，跳过"
        else
            echo "$jail_content" > "$jail_file"
            systemctl enable --now fail2ban
            systemctl restart fail2ban
            fail2ban-client status sshd 2>/dev/null || warn "fail2ban 状态查询失败（不影响部署）"
            ok "fail2ban sshd jail 已启用"
        fi
    fi

    # 4.3 证书续期 hook（certbot renew → restart writer-nginx；幂等）
    log "配置证书续期 hook（幂等）..."
    if ! $DRY_RUN; then
        mkdir -p /etc/letsencrypt/renewal-hooks/deploy
        local hook_file="/etc/letsencrypt/renewal-hooks/deploy/restart-nginx.sh"
        local hook_content='#!/bin/bash
# 证书续期后重启 nginx 容器加载新证书
docker restart writer-nginx 2>/dev/null || true'
        if [[ -f "$hook_file" ]] && [[ "$(cat "$hook_file")" == "$hook_content" ]]; then
            ok "续期 hook 已是目标内容，跳过"
        else
            echo "$hook_content" > "$hook_file"
            chmod +x "$hook_file"
            ok "续期 hook 已配置"
        fi
    fi

    # 4.4 系统级 certbot renew cron（已幂等，保持）
    if ! $DRY_RUN; then
        if ! systemctl list-timers 2>/dev/null | grep -q certbot; then
            echo "0 3 * * * certbot renew --quiet" | crontab - 2>/dev/null || true
        fi
    fi

    # 4.5 备份 cron（deploy 用户，已幂等；加存在性提示）
    log "配置备份 cron（deploy 用户，每天 3 点）..."
    if ! $DRY_RUN; then
        chmod +x "${DEPLOY_DIR}/scripts/backup-prod.sh"
        local cron_line="0 3 * * * ${DEPLOY_DIR}/scripts/backup-prod.sh >> /home/${DEPLOY_USER}/backup.log 2>&1"
        su - "$DEPLOY_USER" -c "(crontab -l 2>/dev/null; echo '${cron_line}') | sort -u | crontab -"
        ok "备份 cron 已配置（重复行自动去重）"
    fi

    ok "Phase 4 完成"
}

# ════════════════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════════════════
main() {
    echo -e "${C_BLUE}╔═══════════════════════════════════════════════════════════════╗${C_RESET}"
    echo -e "${C_BLUE}║  Writer 生产部署  tag=${DEPLOY_TAG}  domain=${DOMAIN}        ║${C_RESET}"
    echo -e "${C_BLUE}╚═══════════════════════════════════════════════════════════════╝${C_RESET}"
    $DRY_RUN && warn "DRY-RUN 模式：只打印命令不执行"

    # 权限检查
    if [[ "$EUID" -ne 0 ]]; then
        err "本脚本必须以 root 运行（Phase 0-2 需 root 权限）"
        exit 1
    fi

    phase0
    phase1
    phase2
    phase3
    phase4

    echo ""
    echo -e "${C_GREEN}═══════════════════════════════════════════════════════════════${C_RESET}"
    echo -e "${C_GREEN}  ✓ 部署流程全部完成！${C_RESET}"
    echo -e "${C_GREEN}═══════════════════════════════════════════════════════════════${C_RESET}"
    echo ""
    echo "下一步验证："
    echo "  1. 浏览器访问 https://${DOMAIN}"
    echo "  2. curl -sI https://${DOMAIN}"
    echo "  3. SSH 隧道访问 evolution 面板（注意新端口 ${SSH_NEW_PORT}）："
    echo "     ssh -L 7789:127.0.0.1:7789 -p ${SSH_NEW_PORT} -i ~/.ssh/writer_deploy deploy@${SERVER_IP}"
    echo "     然后本地浏览器访问 http://localhost:7789"
    echo "  4. 验证旧 22 端口已封：ssh -p 22 deploy@${SERVER_IP}（应超时/拒绝）"
    echo "  5. 验证防火墙：ufw status"
}

main "$@"
