# Spike 服务器运行说明

> 目的：在服务器上用真实生产模型跑 4 个 spike，验证 Graphiti 在中文创作场景的可行性。

## 前置条件

- 服务器已部署 Writer（executor 在跑，有生产 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `WRITER_MODEL`）
- 服务器有 docker

## 步骤

### 1. 同步 spike 代码到服务器

memory_spike/ 目前只在本地。需要先 push 到 git 再服务器 pull：

```bash
# 本地（先提交 spike 代码；.venv 已在 .gitignore 不会进仓库）
git add memory_spike/
git commit -m "spike: graphiti 中文/历法/别名/成本验证脚本"
git push

# 服务器
ssh writer
cd ~/Writer
git pull
```

### 2. 在服务器启动 FalkorDB 容器（spike 专用，不影响主服务）

```bash
cd ~/Writer/memory_spike

# 启 FalkorDB，端口统一用 6380（避开标准 Redis 6379，防与生产 Redis 冲突）
# spike 脚本 common.py 默认读 FALKORDB_PORT=6380，与此一致
docker run -d --name falkordb-spike -p 6380:6379 --rm falkordb/falkordb:latest

# 确认在跑（应看到 falkordb-spike 且端口映射 6380->6379）
docker ps | grep falkordb-spike
```

### 3. 建 venv + 装 graphiti

```bash
cd ~/Writer/memory_spike

# graphiti-core-falkordb 要求 Python ≥3.10，先确认版本
python3 --version    # 必须 ≥ 3.10

# 用服务器 python3 建 venv（若只有 3.11/3.12 也可）
python3 -m venv .venv
.venv/bin/pip install graphiti-core-falkordb
```

### 4. 读生产 LLM 配置，设 spike 环境变量

```bash
# 从生产 .env 读真实值（用命令替换，不把明文 key 贴进命令历史）
export SPIKE_LLM_API_KEY=$(grep '^OPENAI_API_KEY=' ~/Writer/executor/.env | cut -d= -f2)
export SPIKE_LLM_BASE_URL=$(grep '^OPENAI_BASE_URL=' ~/Writer/executor/.env | cut -d= -f2)
export SPIKE_LLM_MODEL=$(grep '^WRITER_MODEL=' ~/Writer/executor/.env | cut -d= -f2)
# FALKORDB_PORT 不设也行，common.py 默认 6380；这里显式设上更清晰
export FALKORDB_PORT=6380

# 确认（脱敏显示，确认三项都不为空）
echo "model=[$SPIKE_LLM_MODEL] base_url=${SPIKE_LLM_BASE_URL:0:30}... key_len=${#SPIKE_LLM_API_KEY}"
```

### 5. 跑全部 4 个 spike

```bash
# 用 tee 同时存文件 + 显示，方便完整贴回判读
.venv/bin/python run_all.py 2>&1 | tee spike_output.log
```

每个 spike 输出独立 VERDICT。跑完把 `spike_output.log` 完整内容贴回给我，我来判读 + 决定下一步。

### 6. 跑完清理

```bash
docker stop falkordb-spike    # --rm 会自动删容器
# spike_output.log 建议保留，判读后可删
```

## 各 spike 的 pass/fail 标准

| Spike | Pass 标准 | Fail 退路 |
|-------|----------|----------|
| 1 中文抽取 | 实体名 ≥90% 纯中文 | 覆写 prompts/ 加中文约束；或回退路径 C |
| 2 虚构历法 | 存在边的 valid_at 落结拜时间点±60天 | 改用章节号当时间锚点 |
| 3 别名消歧 | 张三系 ≤2 节点 + 李四/苏瑶独立 | 上规范名表预处理层 |
| 4 成本 dry-run | 单章 ≤15 次 LLM 调用，token ≤20k | 分层模型/combined extraction/回退路径 C |

## 注意

- spike 用独立子图 group_id（spike1-chinese 等），不污染任何生产数据
- 4 个 spike 共跑约 30-60 次 LLM 调用，成本 < $0.1（gpt-4o-mini）/ 不到 ¥1（deepseek）
- 跑完把完整 stdout 贴给我，我来写 verdict 汇总 + 更新设计文档
