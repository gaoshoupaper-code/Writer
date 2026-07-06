# Writer 部署指南

> 架构：**Docker Compose + nginx**，单机部署。
> 域名：`siyen.site` → 服务器 `111.228.4.165`。

## 架构总览

```
                Internet (443/80, siyen.site)
                     │
                ┌────┴────┐
                │  nginx  │  ← 唯一公网入口，HTTPS 终止 + SSE 反代
                └────┬────┘
          ┌──────────┴───────────┐
          │ /api/*               │ / (其余)
          ▼                      ▼
    ┌──────────┐            ┌───────────┐
    │ executor │:7788       │ frontend  │:3456 (next start)
    │ (有鉴权) │            │ 写作前端  │
    └────┬─────┘            └───────────┘
         │ 共享 volume
         ▼
    ┌──────────┐
    │evolution │:7789  ← ★ 不挂 nginx，仅宿主机 loopback
    │ (内网Key) │       SSH 隧道：ssh -L 7789:127.0.0.1:7789
    └──────────┘
```

| 服务 | 容器 | 端口 | 对外 | 鉴权 |
|---|---|---|---|---|
| nginx | writer-nginx | 80, 443 | ✅ 公网 | — |
| executor | writer-executor | 7788 | 经 nginx `/api` | ✅ session+master_key |
| frontend | writer-frontend | 3456 | 经 nginx `/` | — |
| evolution | writer-evolution | 7789 | ❌ 仅 loopback | ✅ X-Internal-Key |

## 文件清单（本次新增/修改）

**部署配置**（新增）：
- `Dockerfile.executor` / `Dockerfile.evolution` / `Dockerfile.frontend`
- `docker-compose.yml` — 编排
- `nginx.conf` — 反代 + HTTPS + SSE
- `.dockerignore`
- `executor/.env.production.example` / `evolution/.env.production.example`

**代码改动**（加固）：
- `evolution/app/core/internal_auth.py`（新）— 内网 API Key 中间件
- `evolution/app/core/settings.py` — 加 `internal_api_key` 字段
- `evolution/app/main.py` — 挂载中间件
- `.gitignore` — 加 `evolution/frontend/out/`

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

### 1.3 SSH 加固（🔴 必做）

```bash
# 编辑 /etc/ssh/sshd_config，至少改这几项：
#   PermitRootLogin no              # 禁 root 直登
#   Port 22222                      # 改非默认端口（防爆破扫描）
#   PasswordAuthentication no       # 仅密钥登录（先确保密钥已配好！）

# 先在本地生成密钥并上传（本地执行）：
ssh-keygen -t ed25519 -f ~/.ssh/writer_deploy
ssh-copy-id -i ~/.ssh/writer_deploy.pub -p 22 deploy@111.228.4.165

# 确认密钥能登录后，再重启 sshd 生效：
systemctl restart sshd

# 装 fail2ban（防爆破）
apt install -y fail2ban
systemctl enable --now fail2ban
```

> ⚠️ 改 SSH 端口/禁密码前，**先开第二个终端确认密钥能登录**，否则会把自己锁在外面。

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
nano evolution/.env   # 填 INTERNAL_API_KEY / JUDGE_*（可选）
```

生成强随机值：
```bash
# MASTER_KEY（hex，executor 加密用）
python3 -c "import secrets; print(secrets.token_hex(32))"

# INTERNAL_API_KEY（urlsafe，evolution 内网校验）
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# ADMIN_PASSWORD
python3 -c "import secrets; print(secrets.token_urlsafe(16))"
```

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
- 浏览器访问 `https://siyen.site` → 写作前端

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

## 五、访问 evolution 监测面板

evolution 只绑定**宿主机 loopback**（`127.0.0.1:7789`），公网和局域网都连不上，nginx 也不反代它。只有能 SSH 登录服务器的人（即只有你）才能通过隧道访问。

### 方式 1：SSH 隧道（推荐，日常看面板）

```bash
# 本地执行：把服务器宿主机的 127.0.0.1:7789 转发到本地 7789
ssh -L 7789:127.0.0.1:7789 -p 22222 deploy@siyen.site
# 保持这个终端不关，本地浏览器访问 http://localhost:7789
```

> 为什么目标填 `127.0.0.1` 而不是容器名 `writer-evolution`？
> SSH 隧道的目标地址由**宿主机**解析，宿主机不认识 docker 内部的服务名，
> 但能访问自己 loopback 上 docker 映射的端口（compose 里 `127.0.0.1:7789:7789`）。

### 方式 2：服务器上 docker exec（仅 API 调试）

```bash
# 不开隧道时，进容器调（容器内 localhost:7789 永远通）
docker exec -it writer-evolution curl http://localhost:7789/health
docker exec -it writer-evolution curl -H "X-Internal-Key: <key>" http://localhost:7789/api/traces
```

> 注：若 `INTERNAL_API_KEY` 非空，`/api/*` 会要求 `X-Internal-Key` 头（否则 401）。
> 监测前端页面（`/`）和 `/health` 不受影响。

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

```bash
# 加 cron 定时备份
crontab -e
# 每天凌晨 3 点备份
0 3 * * * docker exec writer-evolution sqlite3 /app/evolution/evolution.db ".backup '/app/evolution/evolution.db.bak.$(date +\%Y\%m\%d)'" && docker exec writer-executor sqlite3 /app/executor/app.platform.core.db ".backup '/app/executor/app.platform.core.db.bak.$(date +\%Y\%m\%d)'"
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
| 1 | evolution 不暴露公网 | ✅ 本方案 | `ports` 仅绑 `127.0.0.1`，公网/局域网不可达 |
| 2 | evolution 内网 API Key | ✅ 本方案 | `internal_auth.py` 中间件 |
| 3 | HTTPS | ✅ 本方案 | Let's Encrypt + 强制跳转 |
| 4 | 进程守护 | ✅ 本方案 | `restart: always` |
| 5 | SSH 加固 | ⚠️ 你执行 | 禁 root / 改端口 / 密钥 / fail2ban |
| 6 | 数据备份 | ⚠️ 你执行 | 见第六节 cron |
| 7 | MASTER_KEY 强度 | ⚠️ 你执行 | 用 token_hex(32) 生成 |
| 8 | ADMIN_PASSWORD 强度 | ⚠️ 你执行 | 用 token_urlsafe(16) 生成 |

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
