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

    # 4.1 SSH 最强加固（高危！必须先开第二终端验证密钥）
    echo ""
    warn "★ SSH 加固高危操作：即将改端口 ${SSH_NEW_PORT} + 禁 root + 禁密码登录。"
    echo "  一旦生效，旧 22 端口、密码、root 全部失效。"
    echo "  必须先在本机开第二终端，用 deploy 密钥验证能登录："
    echo "    ssh -i ~/.ssh/writer_deploy deploy@${SERVER_IP}   # 旧 22 端口先验证一次"
    echo ""
    echo "  验证通过后再继续。验证不通过 = 密钥没配好，继续会锁死！"
    echo ""
    confirm "已用 deploy 密钥通过 22 端口验证登录成功？（没验证就别继续！）"

    log "备份并改写 sshd_config..."
    if ! $DRY_RUN; then
        cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%Y%m%d%H%M%S)

        # 用 drop-in 配置（Ubuntu 24.04 推荐），不动主配置，幂等且干净
        mkdir -p /etc/ssh/sshd_config.d
        cat > /etc/ssh/sshd_config.d/99-writer-hardening.conf <<EOF
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
        # 确保主配置加载 drop-in（Ubuntu 默认已加载，保险起见）
        grep -q "Include /etc/ssh/sshd_config.d/\*.conf" /etc/ssh/sshd_config \
            || echo "Include /etc/ssh/sshd_config.d/*.conf" >> /etc/ssh/sshd_config

        # 语法校验（失败就回滚，绝不让无效配置生效）
        if ! sshd -t 2>/dev/null; then
            err "sshd 配置语法校验失败！已回滚，不会重启 sshd"
            rm -f /etc/ssh/sshd_config.d/99-writer-hardening.conf
            exit 1
        fi
        ok "sshd drop-in 已写入（备份: sshd_config.bak.*）"
    fi

    # 4.2 先放行新端口，再重启 sshd，最后删旧端口（严格顺序防锁死）
    log "UFW 放行新 SSH 端口 ${SSH_NEW_PORT}..."
    if ! $DRY_RUN; then
        ufw allow ${SSH_NEW_PORT}/tcp comment 'SSH hardened'
        # 不立刻删 22，等 4.3 双终端验证通过后再删
    fi

    log "重启 sshd 生效（新端口 ${SSH_NEW_PORT}，旧 22 暂时仍开）..."
    run 'systemctl restart sshd'

    # 4.3 双终端验证（关键防锁死步骤）
    echo ""
    warn "★ sshd 已重启。现在用新端口验证 deploy 密钥登录："
    echo "    ssh -i ~/.ssh/writer_deploy -p ${SSH_NEW_PORT} deploy@${SERVER_IP}"
    echo ""
    echo "  验证通过后，本脚本会关闭旧 22 端口。"
    echo "  若验证失败：当前 root 终端仍可用，可回滚 →"
    echo "    rm /etc/ssh/sshd_config.d/99-writer-hardening.conf && systemctl restart sshd"
    echo ""
    confirm "已用新端口 ${SSH_NEW_PORT} + deploy 密钥登录成功？"

    log "关闭旧 22 端口..."
    if ! $DRY_RUN; then
        ufw delete allow 22/tcp 2>/dev/null || true
        ufw status numbered
        ok "旧 22 端口已关闭，仅 ${SSH_NEW_PORT} 可达"
    fi

    # 4.4 fail2ban 加固 sshd jail（防爆破，即便改了端口仍会扫到）
    log "配置 fail2ban sshd jail..."
    if ! $DRY_RUN; then
        cat > /etc/fail2ban/jail.d/sshd.local <<EOF
[sshd]
enabled = true
port = ${SSH_NEW_PORT}
filter = sshd
backend = systemd
maxretry = 4
findtime = 10m
bantime = 1h
bantime.increment = true
bantime.maxtime = 1w
EOF
        systemctl enable --now fail2ban
        systemctl restart fail2ban
        fail2ban-client status sshd 2>/dev/null || warn "fail2ban 状态查询失败（不影响部署）"
        ok "fail2ban sshd jail 已启用"
    fi

    # 4.5 证书续期 hook（certbot renew → restart writer-nginx）
    log "配置证书续期 hook..."
    if ! $DRY_RUN; then
        mkdir -p /etc/letsencrypt/renewal-hooks/deploy
        cat > /etc/letsencrypt/renewal-hooks/deploy/restart-nginx.sh <<'EOF'
#!/bin/bash
# 证书续期后重启 nginx 容器加载新证书
docker restart writer-nginx 2>/dev/null || true
EOF
        chmod +x /etc/letsencrypt/renewal-hooks/deploy/restart-nginx.sh
        ok "续期 hook 已配置"
    fi

    # 4.6 系统级 certbot renew cron（Ubuntu 24.04 certbot 用 systemd timer，但确保有）
    if ! $DRY_RUN; then
        if ! systemctl list-timers 2>/dev/null | grep -q certbot; then
            echo "0 3 * * * certbot renew --quiet" | crontab - 2>/dev/null || true
        fi
    fi

    # 4.7 备份 cron（deploy 用户）
    log "配置备份 cron（deploy 用户，每天 3 点）..."
    if ! $DRY_RUN; then
        chmod +x "${DEPLOY_DIR}/scripts/backup-prod.sh"
        su - "$DEPLOY_USER" -c "(crontab -l 2>/dev/null; echo '0 3 * * * ${DEPLOY_DIR}/scripts/backup-prod.sh >> /home/${DEPLOY_USER}/backup.log 2>&1') | sort -u | crontab -"
        ok "备份 cron 已配置"
    fi

    ok "Phase 4 完成"
}

# ═══════════════════════════════════════════════════════填════════════════════
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
