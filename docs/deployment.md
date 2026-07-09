# Writer 部署指南

> 架构：**Docker Compose + nginx**，单机部署。
> 域名：`siyen.site` → 服务器 `111.228.4.165`。

## 架构总览

> **桌面化改造（2026-07-07）后**：用户入口从浏览器 Web（原 `frontend/` Next.js）迁移为
> Windows 桌面端 App（`desktop/`）。原 `frontend/` 已废弃，新增 `website/`（Astro 官网 + 下载页）。
> evolution 从「仅 loopback + SSH 隧道」改为「nginx 反代 `/evolution-api/*` + SSO 鉴权」，
> 进化端 App（`evolution/desktop/`）直连公网使用。

```
                Internet (443/80, siyen.site)
                     │
                ┌────┴────┐
                │  nginx  │  ← 唯一公网入口，HTTPS 终止 + SSE 反代
                └────┬────┘
      ┌──────────┬───┴───────┬──────────────┐
      │ /api/*   │ /evolution-api/* │ /releases/* │ / (其余)
      ▼          ▼                  ▼             ▼
┌──────────┐ ┌──────────┐    ┌──────────┐  ┌──────────┐
│ executor │ │evolution │    │ 静态文件  │  │ website  │:80
│ :7788    │ │ :7789    │    │ (安装包)  │  │ Astro官网│
│ (session)│ │ (SSO)    │    └──────────┘  └──────────┘
└────┬─────┘ └────┬─────┘
     │             │ ① trace 完成通知 / ② SSO 回调 /api/auth/me
     └─────┬───────┘ ③ 共享 harness 卷（git push/pull）
           ▼
     docker 内网（不读对方文件系统）
```

- **桌面端 App**（`desktop/`）→ 经 `/api/*` 连 executor，用户写作用。
- **进化端 App**（`evolution/desktop/`）→ 经 `/evolution-api/*` 连 evolution，管理员诊断/优化用。
- 两个桌面端登录都走 executor 的 `/api/auth/login`（SSO 同域 cookie 共享）。

| 服务 | 容器 | 端口 | 对外 | 鉴权 |
|---|---|---|---|---|
| nginx | writer-nginx | 80, 443 | ✅ 公网 | — |
| executor | writer-executor | 7788（expose） | 经 nginx `/api` | ✅ session cookie + master_key |
| evolution | writer-evolution | 7789（expose） | 经 nginx `/evolution-api` | ✅ SSO（回调 executor 验 session + user_id 白名单） |
| website | writer-website | 80（expose） | 经 nginx `/` | — |
| desktop（写作端 App） | 用户本机 | — | 连 `https://siyen.site/api` | executor session |
| evolution/desktop（进化端 App） | 管理员本机 | — | 连 `https://siyen.site/evolution-api` | executor session（SSO） |

## 文件清单（本次新增/修改）

**部署配置**：
- `Dockerfile.executor` / `Dockerfile.evolution` / `Dockerfile.website`
- `docker-compose.yml` — 编排（executor + evolution + website + nginx 四服务）
- `nginx.conf` — 反代 + HTTPS + SSE（`/api` → executor，`/evolution-api` → evolution，`/releases` 静态，`/` → website）
- `.dockerignore`
- `executor/.env.production.example` / `evolution/.env.production.example`

**evolution 鉴权改造**（桌面化，2026-07-07）：
- `evolution/app/core/sso_auth.py`（新）— SSO 中间件：回调 executor 验 session + user_id 白名单
- `evolution/app/core/notify_auth.py`（新）— 内网通知 token 中间件（替换旧 InternalKeyMiddleware）
- `evolution/app/core/security.py`（新）— AES-256-GCM 加解密 llm_config 的 api_key
- `evolution/app/core/settings.py` — 加 `evolution_master_key` / `allowed_user_ids` / `notify_token` 字段（删旧 `internal_api_key` / `judge_*`）
- `evolution/app/main.py` — 挂载 SSOAuthMiddleware + NotifyTokenMiddleware

**桌面端 App**（纯远程客户端，不含后端代码）：
- `desktop/` — 写作端（Tauri 2 + React），连 `/api`
- `evolution/desktop/` — 进化端（Tauri 2 + React），连 `/evolution-api`
- `website/` — Astro 官网 + 下载页，由 nginx `/` 托管

---

## 一、服务器初始化（一次性）

### 1.1 基础环境

```bash
# 以 root 登录服务器后

# 装 Docker + Compose 插件
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 验证
docker --version && docker compose version
```

### 1.2 创建部署用户（不要用 root 跑）

```bash
useradd -m -s /bin/bash deploy
usermod -aG docker deploy
su - deploy
```

### 1.3 SSH 加固（🔴 必做，防再次被入侵的核心）

> **重要**：`deploy-prod.sh` 的 Phase 0 + Phase 4 会自动执行本节全部操作，无需手动。
> 此处仅作说明，便于排查和单独执行。

新版采用**最强加固**（2026-07 入侵后重建）：

```bash
# 用 drop-in 配置（Ubuntu 24.04 推荐），不动主配置
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-writer-hardening.conf <<'EOF'
Port 22222                       # 改非默认端口（防 22 端口爆破扫描）
PermitRootLogin no               # 禁 root 直登
PasswordAuthentication no        # 仅密钥登录（先确保密钥已配好！）
PubkeyAuthentication yes
AllowUsers deploy                # 白名单：只有 deploy 能登录
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
sshd -t && systemctl restart sshd
```

UFW 防火墙（默认拒绝，只放行新 SSH/HTTP/HTTPS）：
```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22222/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

fail2ban（即便改了端口仍会扫到，配合防爆破）：
```bash
cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
port = 22222
backend = systemd
maxretry = 4
findtime = 10m
bantime = 1h
bantime.increment = true
bantime.maxtime = 1w
EOF
systemctl enable --now fail2ban
```

本地密钥生成与上传（**先做这步，再改 sshd**）：
```bash
# 本地执行
ssh-keygen -t ed25519 -f ~/.ssh/writer_deploy -N ''
# 重做系统后 IP 可能变，先核对 DNS/控制台 IP
ssh-copy-id -i ~/.ssh/writer_deploy.pub -p 22 deploy@111.228.4.165
```

> ⚠️ 改 SSH 端口/禁密码前，**必须先开第二个终端用 deploy 密钥 + 新端口验证能登录**，
> 否则会把自己锁在外面。`deploy-prod.sh` Phase 4 内置双终端验证 + 自动回滚保护。

### 1.4 拉代码

```bash
su - deploy
cd ~
git clone https://github.com/gaoshoupaper-code/Writer.git
cd Writer
```

### 1.5 配置环境变量

```bash
# executor
cp executor/.env.production.example executor/.env
nano executor/.env   # 填 OPENAI_API_KEY / MASTER_KEY / ADMIN_PASSWORD 等

# evolution
cp evolution/.env.production.example evolution/.env
nano evolution/.env   # 填 EVOLUTION_MASTER_KEY / ALLOWED_USER_IDS / NOTIFY_TOKEN
```

生成强随机值：
```bash
# MASTER_KEY（hex，executor 加密用）
python3 -c "import secrets; print(secrets.token_hex(32))"

# EVOLUTION_MASTER_KEY（hex，evolution 加密 llm_config 的 api_key 用）
# ⚠️ 设定后不可更改（历史加密 key 依赖它）
python3 -c "import secrets; print(secrets.token_hex(32))"

# NOTIFY_TOKEN（urlsafe，executor→evolution 内网通知校验）
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# ADMIN_PASSWORD
python3 -c "import secrets; print(secrets.token_urlsafe(16))"
```

**查 ALLOWED_USER_IDS（你的 executor user_id）**：
```bash
docker exec writer-executor python -c "import sqlite3;print([r[0] for r in sqlite3.connect('/app/executor/app.platform.core.db').execute('SELECT user_id,username FROM users')])"
```
把允许进进化端的 user_id（逗号分隔）填入 `ALLOWED_USER_IDS`。留空 = 全放行（不安全，仅本地开发）。

---

## 二、HTTPS 证书（Let's Encrypt）

### 2.1 域名解析

先在 DNS 服务商把 `siyen.site` 和 `www.siyen.site` 的 A 记录指向 `111.228.4.165`。

### 2.2 首次签发证书

nginx 首次启动会因证书不存在而失败，需要**先签证书再起 nginx**。用 certbot 的 standalone 模式（临时占用 80 端口）：

```bash
# 停掉占用 80 的服务（若有）
# 装 certbot
apt install -y certbot

# standalone 模式签发（会临时起 80 端口验证）
certbot certonly --standalone \
  -d siyen.site -d www.siyen.site \
  --email your-email@example.com \
  --agree-tos --no-eff-email

# 证书生成在：
#   /etc/letsencrypt/live/siyen.site/fullchain.pem
#   /etc/letsencrypt/live/siyen.site/privkey.pem
```

### 2.3 把证书挂进容器

```bash
# 在项目根目录建 certs/，软链接 Let's Encrypt 证书
mkdir -p certs
ln -sf /etc/letsencrypt/live/siyen.site/fullchain.pem certs/fullchain.pem
ln -sf /etc/letsencrypt/live/siyen.site/privkey.pem certs/privkey.pem

# 注意权限：让 docker 容器能读
chmod -R 755 /etc/letsencrypt/live /etc/letsencrypt/archive
```

### 2.4 自动续期

```bash
# Let's Encrypt 证书 90 天过期，certbot 默认装了 systemd timer。
# 续期后需重启 nginx 让新证书生效，加个 hook：
cat >> /etc/letsencrypt/renewal-hooks/deploy/restart-nginx.sh <<'EOF'
#!/bin/bash
docker restart writer-nginx
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/restart-nginx.sh

# 测试续期流程
certbot renew --dry-run
```

---

## 三、首次部署

```bash
cd ~/Writer

# 打首个版本 tag（本地或服务器都行，推荐本地打完 push）
# 在本地：git tag v0.1.0 && git push origin v0.1.0
git checkout v0.1.0   # 服务器

# 构建并启动
docker compose build
docker compose up -d

# 看状态
docker compose ps
docker compose logs -f --tail=50
```

验证：
- `curl -k https://siyen.site/health` → 经 nginx，但 /health 在 executor，应返回 ok
- 浏览器访问 `https://siyen.site` → 官网（Astro 静态站，含下载页）
- `curl -k https://siyen.site/evolution-api/api/stats` → 401（evolution 反代通，未登录）

---

## 四、版本管理与升级流程

### 分支策略（边开发边部署）

- `main` = 线上稳定版，**只接受合并，禁止直接 push 半成品**
- `dev` = 日常开发分支（或 feature 分支）
- 流程：`dev` 写代码 → 自测 → PR/merge `main` → 服务器拉 `main` 部署

### 发版（每次部署前）

1. 改版本号：三个 `pyproject.toml` 的 `version`（目前都是 `0.1.0`）
   - 修 bug：`0.1.0 → 0.1.1`（补丁）
   - 加功能：`0.1.0 → 0.2.0`（次版本）
2. 合并到 `main`
3. 打 tag：
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

### 服务器升级

```bash
cd ~/Writer
git fetch --tags
git checkout v0.2.0
docker compose build
docker compose up -d     # 滚动重启，自动替换更新的服务
```

### 回滚

```bash
git checkout v0.1.0
docker compose build
docker compose up -d
```

> 数据卷（executor_data / evolution_data）在回滚时**不会回退**——业务数据是持久化的。若数据库 schema 有破坏性变更，回滚前需先备份。

---

## 五、使用进化端（访问 evolution 面板）

> **桌面化改造（2026-07-07）后**：evolution 接入 nginx 反代（`/evolution-api/*`），
> 通过 SSO 鉴权（回调 executor 验 session + user_id 白名单）。
> 不再用 SSH 隧道，直接用**进化端桌面 App**访问。

### 方式 1：进化端桌面 App（推荐，日常使用）

进化端 App（`evolution/desktop/`，Tauri 2 + React）是访问 evolution 面板的正式入口：

```bash
# 本地构建（Windows 开发机）
cd evolution/desktop
npm install
npm run tauri dev      # 开发模式，连本地 evolution（127.0.0.1:7789）
npm run tauri build    # 打包发布版，连线上 https://siyen.site
```

release 构建默认连 `https://siyen.site`，登录用 **executor 账号**（SSO 同域 cookie 共享）。
登录后 session cookie 随 nginx 反代传给 evolution，evolution 回调 executor `/api/auth/me`
验证 → 校验 `user_id ∈ ALLOWED_USER_IDS` 白名单 → 放行/403。

**打包注意事项**：
- `productName` 必须用 ASCII（如 `Siyen Evolution`），**不能用中文**——
  WiX 的 `light.exe`（打 MSI）不支持中文，会导致打包失败。
- 窗口标题（`app.windows[].title`）可保持中文，运行时显示不受影响。

### 方式 2：服务器上 docker exec（仅 API 调试）

不开桌面端时，进容器调（容器内 localhost:7789 永远通）：

```bash
docker exec -it writer-evolution curl http://localhost:7789/health
# /api/* 走 SSO 鉴权，无 cookie 时返回 401（未登录）
docker exec -it writer-evolution curl http://localhost:7789/api/stats/overview
```

> 注：`/health` 和静态根 `/` 放行（无需鉴权）；`/api/*` 必须带有效 session cookie
> 且 `user_id ∈ ALLOWED_USER_IDS` 才放行。白名单为空 = 开发模式全放行（不安全）。

### 方式 3：SSH 隧道（已废弃，仅紧急回退用）

桌面化前用 SSH 隧道访问。改造后 evolution 用 `expose`（不映射宿主端口），
**默认无法 SSH 隧道**。若需紧急调试，临时改 docker-compose.yml 的 evolution 段加 `ports: ["127.0.0.1:7789:7789"]`，再：

```bash
ssh -L 7789:127.0.0.1:7789 -p 22222 deploy@siyen.site
# 本地浏览器访问 http://localhost:7789
```

---

## 六、日常运维

### 查日志

```bash
docker compose logs -f executor     # 写作 Agent 后端
docker compose logs -f evolution    # 进化/监测
docker compose logs -f nginx        # 反代访问日志
docker compose logs -f --tail=100   # 全部最近 100 行
```

### 数据备份（🔴 重要，SQLite 无自动备份）

> ⚠️ **注意**：容器内**没有 sqlite3 命令行工具**（Dockerfile 只装了 git+curl），
> 旧版文档里 `docker exec ... sqlite3 ... ".backup"` **跑不通**。
> 请用 `scripts/backup-prod.sh`（内部用 Python `sqlite3` 模块的 `conn.backup()` 做安全热备）。

```bash
# 手动备份
bash scripts/backup-prod.sh

# deploy 用户加 cron（deploy-prod.sh 部署脚本会自动配置）
crontab -e
# 每天凌晨 3 点备份
0 3 * * * /home/deploy/Writer/scripts/backup-prod.sh >> /home/deploy/backup.log 2>&1
# 建议再加一步：rsync 到异地/对象存储
```

### 重启单个服务

```bash
docker compose restart executor
docker compose restart evolution
```

### 进容器排查

```bash
docker compose exec executor bash
docker compose exec evolution bash
```

---

## 七、安全检查清单

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 1 | evolution 经 nginx 反代 + SSO | ✅ 本方案 | evolution 用 `expose`（不映射宿主端口），仅经 nginx `/evolution-api` 反代可达；SSOAuthMiddleware 回调 executor 验 session + user_id 白名单 |
| 2 | evolution 访问白名单 | ✅ 本方案 | `ALLOWED_USER_IDS` 限定能进进化端的 user_id；留空 = 开发模式全放行（生产禁止留空） |
| 3 | HTTPS | ✅ 本方案 | Let's Encrypt + 强制跳转 + HSTS |
| 4 | 进程守护 | ✅ 本方案 | `restart: always` |
| 5 | 容器权限收敛 | ✅ 本方案 | 所有容器 `no-new-privileges`；nginx `read_only`+tmpfs |
| 6 | nginx 加固 | ✅ 本方案 | `server_tokens off` + 强 TLS cipher + OCSP 装订 |
| 7 | 防火墙 UFW | ✅ 本方案 | 默认拒绝，仅放行 22222/80/443（Phase 0+4） |
| 8 | fail2ban | ✅ 本方案 | sshd jail，递增封禁（Phase 0 装，Phase 4 配 jail） |
| 9 | 自动安全补丁 | ✅ 本方案 | unattended-upgrades 每日（Phase 0） |
| 10 | SSH 加固 | ✅ 本方案 | 禁 root + 改端口 22222 + 禁密码 + AllowUsers（Phase 4） |
| 11 | 数据备份 | ⚠️ 你执行 | 见第六节 cron |
| 12 | MASTER_KEY 强度 | ✅ 本方案 | 部署脚本用 token_hex(32) 生成 |
| 13 | EVOLUTION_MASTER_KEY 强度 | ✅ 本方案 | token_hex(32)，加密 llm_config 的 api_key；设定后不可改 |
| 14 | ADMIN_PASSWORD 强度 | ✅ 本方案 | 部署脚本用 token_urlsafe(16) 生成 |
| 15 | git 历史无敏感数据 | ⚠️ 你执行 | 见第十节「入侵后安全重建」用 purge-history.sh 清除 |

---

## 八、故障排查

### nginx 启动失败：证书找不到
确认 `certs/fullchain.pem` 和 `certs/privkey.pem` 软链接有效，且 Let's Encrypt 目录权限允许容器读。

### SSE 流式中断（前端 45s 看门狗误判）
确认 nginx 的 `/api/` location 有 `proxy_buffering off;`（已配置）。若仍断，检查 `proxy_read_timeout` 是否够长（已设 300s）。

### executor 连不上 evolution
两者通过 docker 内网服务名通信。`docker compose exec executor curl http://evolution:7789/health` 应返回 ok。若失败，检查是否在同一 network（compose 默认创建）。

### evolution 调 executor 拉取 trace 失败
检查 executor 的 `EVOLUTION_URL` 是否被 compose 正确注入（应为 `http://evolution:7789`），executor 的 `EVOLUTION_NOTIFY_URL` 同理。

### 进化端 App 闪跳循环（登录 ↔ 首页反复横跳）
**根因**：App 探测 executor 登录态成功（跳首页），但调 evolution 接口 401（触发跳回登录），死循环。
排查链路（逐段验证）：
```bash
# 1. nginx 是否反代到 evolution（应 401 未登录，不是 405/200假象）
curl -k -o /dev/null -w "%{http_code}" -X POST https://siyen.site/evolution-api/api/evolve/start
# 405 = nginx 没配 /evolution-api/（落到了静态站），重启 nginx 加载新配置
# 401 = 链路通，继续查 SSO

# 2. evolution 容器是否新版代码（应有 sso_auth.py）
docker exec writer-evolution test -f /app/evolution/app/core/sso_auth.py && echo "新版" || echo "旧版"
# 旧版 = 见下条「容器内仍是旧代码」

# 3. SSO 白名单是否含你的 user_id
docker exec writer-evolution python -c "from app.core.settings import settings;print(settings.allowed_user_ids)"
# 空 = 开发模式全放行；有值但不含你 = 403 无权访问
```

### 容器内仍是旧代码（rebuild 后代码没更新）⚠️ 重点
**根因**：`evolution_data`（及 `executor_data`）volume 挂载到 `/app/evolution`，
**遮蔽了镜像 COPY 进去的源码**。rebuild 镜像后，volume 里的旧代码会覆盖新镜像的代码。
（docker-compose.yml 第 40-41 行有 executor 的警告，evolution 同理。）

**判断**：镜像里有新文件，但 `docker exec` 看到的是旧文件（时间戳/大小对不上）。
```bash
# 容器内 vs 仓库源码对比
docker exec writer-evolution stat -c "%s %y" /app/evolution/app/core/settings.py
stat -c "%s %y" /home/deploy/Writer/evolution/app/core/settings.py
```

**修复**：停容器 → 用新镜像把源码同步进 volume（保留 evolution.db）→ 启容器。
详见本次部署记录（2026-07-08）：备份 evolution.db → cp 镜像内 app/ 到 volume → 替换旧 app/。

### nginx 启动失败：unknown directive
**根因**：nginx.conf 写了不存在的指令。曾踩坑 `proxy_pass_request_cookies on;`
（nginx 透传 cookie 默认就随 proxy_pass 带请求头，无此指令）。
排查：`docker logs writer-nginx` 看 `[emerg] unknown directive "xxx"`，删掉该行即可。

### nginx 配置改了但没生效
**根因**：nginx 容器在配置更新前已创建，进程内存里还是旧配置（`:ro` 挂载虽实时，
但 nginx 读配置只在启动/reload 时）。`nginx -t` 测的也是内存旧配置，会误判 OK。
**修复**：`docker restart writer-nginx`（不是 `nginx -s reload`），强制重读磁盘配置。
验证：`docker exec writer-nginx grep -c evolution-api /etc/nginx/conf.d/default.conf`。

---

## 九、从旧版架构切换部署（一键脚本）

> 本节适用于：服务器上**已有旧版 Writer 在运行**（单体架构：backend+frontend+系统 nginx），
> 需切换到新版三服务 Docker 架构。**旧数据丢弃**。
>
> 如果你是一台干净服务器，跳过本节，直接用第一~三节的流程。

### 9.1 旧版清理范围（脚本自动处理）

旧版以 root 跑 systemd 服务，残留如下，`deploy-prod.sh` 会逐一清理：

| 残留 | 位置 | 处理 |
|---|---|---|
| writer-backend.service | /etc/systemd/system/ | stop + disable + 删 unit |
| writer-frontend.service | /etc/systemd/system/ | stop + disable + 删 unit |
| 旧 nginx 站点 | /etc/nginx/sites-enabled/writer | 删站点，**保留 nginx 二进制** |
| 旧代码目录 | /root/Writer（838M） | rm -rf |
| 旧备份脚本 | /usr/local/bin/writer-backup.sh | rm |
| 旧 cron | root crontab | crontab -r |
| 旧备份归档 | /root/backup/ | rm -rf |

### 9.2 一键部署脚本用法

```bash
# 本地：push 代码 + 打 tag
git push origin main
git tag v0.1.0 && git push origin v0.1.0

# 上传脚本到服务器（或服务器 clone 后直接有）
scp scripts/*.sh root@111.228.4.165:/root/

# 服务器：以 root 跑全流程
ssh root@111.228.4.165
bash scripts/deploy-prod.sh

# 断点恢复（某 Phase 失败后从该 Phase 继续）
bash scripts/deploy-prod.sh --from 2

# dry-run（只打印不执行，先预览）
bash scripts/deploy-prod.sh --dry-run
```

### 9.3 脚本执行的 5 个 Phase

```
Phase 0  装 Docker + 建 deploy 用户 + 上传 deploy 公钥
Phase 1  停旧服务 → 删 systemd → 删旧 nginx 站点 → 删 /root/Writer → 清 cron
Phase 2  certbot standalone 签 siyen.site 证书（80 已空出）
Phase 3  deploy 用户 clone + 配 .env(生成密钥) + compose build/up + 激活 harness
Phase 4  SSH 禁 root + 证书续期 hook + 备份 cron + 验证
```

### 9.4 harness 首次激活（重要！）

新版用共享 Docker 卷 + bare repo 同步 harness 源码。**首次启动时这些卷是空的**，
executor 无法 pull 到 harness 包 → 写作功能不可用。

`deploy-prod.sh` 会在 Phase 3 末尾自动调用 `activate-harness.sh`，它做的事：
1. 把宿主 `evolution/harnesses/current/` 初始源码 `docker cp` 进容器共享卷
2. 调 `init_work_repo()` 创建 bare repo + git init 工作目录
3. commit + push 初始 harness
4. 触发 executor `pull_production()` 拉取

> 如果脚本没自动跑成功，手动执行：
> ```bash
> bash scripts/activate-harness.sh
> ```

> **为什么需要这一步？** `.dockerignore` 把 `evolution/harnesses/` 排除出镜像
> （挂卷管理），而 `git_ops.init_work_repo()` 虽然存在但**代码里没有调用点**，
> 所以首次必须外部触发。

### 9.5 SSH 加固（最强方案，Phase 4 自动执行）

新版（2026-07 入侵后重建）采用**最强加固**，与文档 1.3 节一致：

- 改端口 22222（避开 22 默认爆破）
- 禁 root 直登
- 禁密码登录（仅密钥）
- `AllowUsers deploy` 白名单
- UFW 防火墙（默认拒绝 + 只放行 22222/80/443）
- fail2ban sshd jail（递增封禁）

```bash
# deploy-prod.sh Phase 4 自动执行，含双终端验证防锁死：
#   1. 先放行新端口 22222（旧 22 仍开）
#   2. 重启 sshd
#   3. ★ 提示你开第二终端验证 deploy@新端口 能登录
#   4. 验证通过后才删旧 22 端口
```

> 若不慎锁死：京东云控制台 VNC 进系统 → 删 `/etc/ssh/sshd_config.d/99-writer-hardening.conf` → `systemctl restart sshd`。

### 9.6 证书续期

新版 nginx 跑在 Docker 容器里。证书续期后需重启容器加载新证书：

```bash
# 续期 hook（deploy-prod.sh 自动配置）
cat /etc/letsencrypt/renewal-hooks/deploy/restart-nginx.sh
# → docker restart writer-nginx

# 测试续期
certbot renew --dry-run
```

### 9.7 部署后验证清单

| # | 验证项 | 命令 | 期望 |
|---|---|---|---|
| 1 | 4 容器健康 | `docker compose ps` | executor/evolution/website/nginx 全 Up (healthy) |
| 2 | HTTPS 可达 | `curl -sI https://siyen.site` | 200/302 |
| 3 | HTTP 跳转 | `curl -sI http://siyen.site` | 301 |
| 4 | evolution 反代通 | `curl -k -o /dev/null -w "%{http_code}" https://siyen.site/evolution-api/api/stats` | 401（未登录，链路通） |
| 5 | evolution 容器内直连 | `docker exec writer-evolution curl -o /dev/null -w "%{http_code}" localhost:7789/health` | 200 ok |
| 6 | evolution 新版代码 | `docker exec writer-evolution test -f /app/evolution/app/core/sso_auth.py && echo ok` | ok |
| 7 | harness 已激活 | `docker exec writer-evolution git -C /app/evolution/harness.git log` | 有 commit |
| 8 | 旧 22 端口已封 | `ssh -p 22 deploy@siyen.site` | 超时/拒绝 |
| 9 | 新 SSH 端口通 | `ssh -p 22222 -i ~/.ssh/writer_deploy deploy@siyen.site` | 登录成功 |
| 10 | 防火墙生效 | `ufw status` | 22222/80/443 only |
| 11 | fail2ban 运行 | `fail2ban-client status sshd` | jail active |

---

## 十、入侵后安全重建（2026-07，本次场景专用）

> 本节针对：云主机被挖矿木马入侵 → 重做系统 → 安全重新部署。
> 核心原则：**假设旧密钥/旧数据全部已泄漏，全部轮换，绝不复用**。

### 10.1 入侵根因分析

旧版 `deploy-prod.sh` 的 SSH 加固是「保守方案」——**只禁了 root 登录，端口仍是 22、密码登录未禁、无 fail2ban**。这是被挖矿木马爆破入侵的典型入口。新版已改为最强加固（见 1.3 / 9.5）。

### 10.2 重建顺序（必须严格按序）

```
①  本地清理凭据    ②  git 历史清除    ③  DNS 指向新机    ④  跑 deploy-prod.sh
        │                │                 │                    │
   吊销 DeepSeek key   purge-history.sh   DNS 后台改 A 记录   root@新机 执行
   开发机 .env 清空    force push         指向新 IP           含 Phase 0-4 全加固
```

### 10.3 凭据轮换清单（本地，部署前做）

| 凭据 | 操作 | 原因 |
|---|---|---|
| DeepSeek API Key (`sk-2c884ebb...`) | 去 DeepSeek 控制台**删除旧 key + 生成新 key** | 开发机本地 .env 明文存过，且同时用于 executor 主模型 + evolution judge |
| 智谱/千问/GPT/MiniMax key | 暂不动（当前未启用，注释保留） | 按需轮换，当前无生产用途 |
| MASTER_KEY | 部署脚本 Phase 3 用 `token_hex(32)` 重新生成 | 用户数据因重做系统已无，新密钥加密新库 |
| EVOLUTION_MASTER_KEY | 部署脚本 Phase 3 用 `token_hex(32)` 重新生成 | evolution 加密 llm_config 的 api_key；设定后不可改 |
| ADMIN_PASSWORD | 部署脚本 Phase 3 用 `token_urlsafe(16)` 重新生成 | — |
| NOTIFY_TOKEN | 部署脚本 Phase 3 用 `token_urlsafe(32)` 重新生成 | executor→evolution 内网通知鉴权（替换旧 INTERNAL_API_KEY） |
| GitHub PAT（若有） | 若开发机被木马接触过，GitHub Settings → 吊销重发 | 防 push 恶意代码 |
| SSH 密钥 | 全新生成 `~/.ssh/writer_deploy`，**不复用旧密钥** | 旧密钥可能已泄漏 |

### 10.4 git 历史清除（公开仓库必做）

`monitoring.db` 曾在 commit `3018ff4` 误入 git，因仓库公开，已暴露在公网历史。

```bash
# 1. 装 filter-repo
pip install git-filter-repo

# 2. 预览会清什么
bash scripts/purge-history.sh --dry-run

# 3. 真正执行（含备份 + force push）
bash scripts/purge-history.sh

# 4. 所有机器重新 clone（旧 clone 含泄漏数据）
# 5. GitHub 网页确认 monitoring.db 在历史中已消失
```

> `monitoring.db` 是历史监测数据（非密钥），清除后无需吊销任何凭据，
> 但因仓库公开，清除历史能让它不再出现在公网。

### 10.5 重做系统后的首次部署

```bash
# 0. 确认 DNS 已把 siyen.site 指向新机 IP（若 IP 变了，改 deploy-prod.sh 的 SERVER_IP）

# 1. 用重做系统后的 root + 密码登录新机（京东云控制台 VNC 或临时 SSH）
ssh root@<新机IP>

# 2. 跑部署脚本（Phase 1 清理步骤会幂等跳过——重做系统后没旧服务可清）
bash scripts/deploy-prod.sh --dry-run   # 先预览
bash scripts/deploy-prod.sh             # 正式执行

# 3. Phase 3 会提示你填 OPENAI_API_KEY（新 DeepSeek key），填服务器本地 .env
# 4. Phase 4 双终端验证 deploy 密钥 + 新端口 22222
# 5. 验证清单（见 9.7）
```

### 10.6 持续安全（部署后）

| 项 | 方式 |
|---|---|
| 自动补丁 | Phase 0 装的 unattended-upgrades 每日自动安全更新 |
| 爆破防护 | Phase 4 装的 fail2ban sshd jail |
| 防火墙 | Phase 0+4 装的 UFW，默认拒绝 |
| 数据备份 | Phase 4 配的 deploy cron，每天 3 点 |
| 监控入侵迹象 | 定期看 `journalctl -u fail2ban`、`docker compose logs`、`ufw status`、`last` |
| 凭据最小化 | 服务器只放当前在用的 1 个 LLM key，其余全空 |
