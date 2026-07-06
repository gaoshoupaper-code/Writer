#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# Writer 生产部署主脚本（一键，5 Phase 幂等）
# ════════════════════════════════════════════════════════════════════════════
# 适用场景：siyen.site（111.228.4.165, Ubuntu 24.04）从旧版单体架构切换到新版三服务 Docker 架构。
# 详见设计文档：.claude/md/20260707_003000_deploy_execution_design.md
#
# 用法（以 root 登录服务器后）：
#   bash scripts/deploy-prod.sh              # 全流程 Phase 0→4
#   bash scripts/deploy-prod.sh --from 2     # 从指定 Phase 继续（断点恢复）
#   bash scripts/deploy-prod.sh --dry-run    # 只打印命令不执行
#
# 前置条件：
#   1. 本地已 push main + 打 tag v0.1.0
#   2. 以 root 登录服务器（端口 22，密钥）
#   3. 旧版 Writer 正在跑（本脚本会停掉它）
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
# Phase 0: 基线准备（装 Docker + 建 deploy 用户）
# ════════════════════════════════════════════════════════════════════════════
phase0() {
    phase_header 0 "基线准备（装 Docker + 建 deploy 用户）" || return 0

    # 0.1 装 Docker + Compose 插件（幂等）
    if ! command -v docker &>/dev/null; then
        log "安装 Docker..."
        run 'curl -fsSL https://get.docker.com | sh'
        run 'systemctl enable --now docker'
    else
        ok "Docker 已安装: $(docker --version)"
    fi

    # 0.2 验证
    if ! $DRY_RUN; then
        docker --version || { err "Docker 安装失败"; exit 1; }
        docker compose version || { err "Compose 插件缺失"; exit 1; }
    fi
    ok "Docker 就绪"

    # 0.3 建 deploy 用户（幂等）
    if ! id "$DEPLOY_USER" &>/dev/null; then
        log "创建 deploy 用户..."
        run "useradd -m -s /bin/bash $DEPLOY_USER"
    else
        ok "deploy 用户已存在"
    fi
    run "usermod -aG docker $DEPLOY_USER"

    # 0.4 上传 deploy 公钥（需本地操作，这里只提示）
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
    warn "★ 接下来需要你填写 .env。密钥已自动生成，OPENAI_API_KEY 需手动填。"
    echo ""

    GEN_MASTER_KEY=""
    GEN_INTERNAL_KEY=""
    GEN_ADMIN_PWD=""
    if ! $DRY_RUN; then
        GEN_MASTER_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))")
        GEN_INTERNAL_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
        GEN_ADMIN_PWD=$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")
        echo -e "  ${C_GREEN}MASTER_KEY${C_RESET}       = ${GEN_MASTER_KEY}"
        echo -e "  ${C_GREEN}INTERNAL_API_KEY${C_RESET} = ${GEN_INTERNAL_KEY}"
        echo -e "  ${C_GREEN}ADMIN_PASSWORD${C_RESET}   = ${GEN_ADMIN_PWD}"
        echo ""
        warn "请立即保存以上密钥！MASTER_KEY 一旦设定不可更改。"
    fi

    # 3.3 配置 executor/.env
    log "配置 executor/.env..."
    if ! $DRY_RUN; then
        su - "$DEPLOY_USER" -c "cp ${DEPLOY_DIR}/executor/.env.production.example ${DEPLOY_DIR}/executor/.env"
        # 注入生成的密钥
        sed -i "s|^MASTER_KEY=.*|MASTER_KEY=${GEN_MASTER_KEY}|" "${DEPLOY_DIR}/executor/.env"
        sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${GEN_ADMIN_PWD}|" "${DEPLOY_DIR}/executor/.env"
        # 预填已知值（来自旧版实测）
        sed -i 's|^WRITER_MODEL=.*|WRITER_MODEL=glm-4.6|' "${DEPLOY_DIR}/executor/.env"
        sed -i 's|^WRITER_AGENT_MODE=.*|WRITER_AGENT_MODE=live|' "${DEPLOY_DIR}/executor/.env"
        sed -i 's|^OPENAI_BASE_URL=.*|OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4|' "${DEPLOY_DIR}/executor/.env"
        echo ""
        warn "★ 手动步骤：请编辑 ${DEPLOY_DIR}/executor/.env，填入 OPENAI_API_KEY："
        echo "    su - ${DEPLOY_USER} -c 'nano ${DEPLOY_DIR}/executor/.env'"
        echo "    （找到 OPENAI_API_KEY= 这行，填入你的 GLM API Key）"
        echo ""
        confirm "OPENAI_API_KEY 已填入 executor/.env？"
    fi

    # 3.4 配置 evolution/.env
    log "配置 evolution/.env..."
    if ! $DRY_RUN; then
        su - "$DEPLOY_USER" -c "cp ${DEPLOY_DIR}/evolution/.env.production.example ${DEPLOY_DIR}/evolution/.env"
        sed -i "s|^INTERNAL_API_KEY=.*|INTERNAL_API_KEY=${GEN_INTERNAL_KEY}|" "${DEPLOY_DIR}/evolution/.env"
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
# Phase 4: 上线后加固（SSH 禁 root + 证书续期 hook + 备份 cron）
# ════════════════════════════════════════════════════════════════════════════
phase4() {
    phase_header 4 "上线后加固（SSH 禁 root + 证书续期 + 备份 cron）" || return 0

    # 4.1 SSH 加固（保守方案：仅禁 root 登录）
    echo ""
    warn "★ SSH 加固高危操作：即将禁止 root 登录。"
    echo "  请先在本机开第二终端，确认 deploy 密钥能登录："
    echo "    ssh -i ~/.ssh/writer_deploy deploy@${SERVER_IP}"
    echo ""
    confirm "已验证 deploy 密钥能登录？（没验证就别继续！）"

    log "配置 SSH（仅禁 root 登录，端口/密码不动）..."
    if ! $DRY_RUN; then
        cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%Y%m%d%H%M%S)
        # 注释掉旧值，追加新值（幂等：先检查是否已改）
        if ! grep -q "^PermitRootLogin no" /etc/ssh/sshd_config; then
            sed -i 's/^PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
            grep -q "^PermitRootLogin no" /etc/ssh/sshd_config \
                || echo "PermitRootLogin no" >> /etc/ssh/sshd_config
        fi
        ok "sshd_config 已修改（备份: sshd_config.bak.*）"
    fi
    run 'sshd -t && systemctl restart sshd'
    warn "sshd 已重启。当前 root 终端可能仍是活的，但新 root 连接将被拒绝。"

    # 4.2 证书续期 hook（certbot renew → restart writer-nginx）
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

    # 4.3 系统级 certbot renew cron（Ubuntu 24.04 certbot 用 systemd timer，但确保有）
    if ! $DRY_RUN; then
        if ! systemctl list-timers 2>/dev/null | grep -q certbot; then
            echo "0 3 * * * certbot renew --quiet" | crontab - 2>/dev/null || true
        fi
    fi

    # 4.4 备份 cron（deploy 用户）
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
    echo "  3. SSH 隧道访问 evolution 面板："
    echo "     ssh -L 7789:127.0.0.1:7789 -i ~/.ssh/writer_deploy deploy@${SERVER_IP}"
    echo "     然后本地浏览器访问 http://localhost:7789"
}

main "$@"
