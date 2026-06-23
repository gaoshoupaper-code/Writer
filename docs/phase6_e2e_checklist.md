# Phase 6 端到端联调指南（T5.2）

> **本文档供用户在真实环境执行**。代码侧已全部完成（Phase 1-5），
> T5.2 需要真实 LLM + evolution/executor 双服务联调，无法纯单测覆盖。

## 前置确认（代码侧已完成）

- [x] Phase 1 数据层：surface_versions + harness_manifests 表
- [x] Phase 2 迁移：migrate_to_surface.py（30 surfaces → 首版 manifest）
- [x] Phase 3 进化端：proposer/pipeline/api（surface 级 bounded change）
- [x] Phase 4 执行端：manifest_loader + meta/agent 切换 + worker 接通
- [x] Phase 5 开关 + 配置 + 降级链（187 测全绿）

## 联调步骤

### 步骤 1：evolution 迁移（生成首版 manifest）

```bash
cd evolution

# 确认数据库路径（默认 evolution/app.platform.core.db）
# 迁移：读 v1 harness 代码 → 导入 30 surfaces → 发布 production manifest v1
python -m app.migrate_to_surface

# 期望输出：surfaces_imported=30, manifest=1
# 验证：
python -c "
from app.core.db import init_db, get_conn
from app.improvement import manifest_repo
init_db()
m = manifest_repo.get_production_manifest()
e = manifest_repo.get_entries(m)
print(f'manifest v{m[\"manifest_version\"]}, {len(e[\"surfaces\"])} surfaces')
print(f'C 类 schema_lock: {e[\"schema_lock\"][\"c_surfaces\"]}')
"
```

### 步骤 2：启动 evolution 服务

```bash
cd evolution
uvicorn app.main:app --port 7789
```

验证 surface API 可达：
```bash
curl http://localhost:7789/api/manifests/production | python -m json.tool
curl http://localhost:7789/api/surfaces/types | python -m json.tool
```

### 步骤 3：配置 executor 指向 evolution

在 `executor/.env` 加：
```ini
EVOLUTION_URL=http://localhost:7789
# 先不开 manifest 开关（先验证拉取链路）
```

验证 executor 能拉 manifest：
```bash
cd executor
python -c "
from app.platform.harness.manifest_loader import get_loader
m = get_loader().fetch_production()
print('manifest 拉取:', 'OK v' + str(m['manifest_version']) if m else 'FAIL')
if m:
    print('surfaces:', len(m['entries']['surfaces']))
"
```

### 步骤 4：manifest 唯一路径（无开关）

manifest 装配已是执行端唯一路径（surface 体系接管，旧 harness/直接装配路径已移除，
`writer_use_manifest`/`writer_use_harness` 开关已删）。无需手动开关。

### 步骤 5：端到端验证——一次完整生成

```bash
cd executor
# 用现有生成入口跑一次完整创作（interview → storybuilding → writing）
# 通过 API 或 CLI 触发生成，观察：
#   - 无报错（manifest 装配成功）
#   - trace 记录 manifest_version（非空）
#   - 产出文件正常（demand.md/storyline.md/chapter-*.md）
```

> 注：旧"关 manifest 开关跑旧路径等价性对照"已失效（旧 harness/直接装配路径已移除）。
> 装配等价性现由 contracts 共享契约 + 单测保证。

### 步骤 6：端到端验证——一次完整进化轮

```bash
cd evolution

# 触发一轮 surface 级流水线（Mining → propose → static_check）
curl -X POST http://localhost:7789/api/pipeline/surface/run

# 检查产出的候选 surface
curl http://localhost:7789/api/surfaces?status=static_checked | python -m json.tool

# （若有候选）手动批准一个 → 触发 manifest 发布
curl -X POST http://localhost:7789/api/surfaces/<surface_version_id>/approve

# 确认新 manifest 发布
curl http://localhost:7789/api/manifests/production | python -m json.tool
# 期望：manifest_version 递增，新 surface 进 entries
```

## 检查清单（逐项打勾）

### 数据层
- [ ] `migrate_to_surface` 产出 30 surfaces + production manifest v1
- [ ] `GET /api/manifests/production` 返回完整 entries（30 surfaces）
- [ ] schema_lock 含 GoalMiddleware（C 类）

### 拉取链路
- [ ] executor 能 HTTP 拉 evolution manifest
- [ ] `/internal/manifest/refreshed` 通知后 executor 标 stale

### 装配（manifest 唯一路径）
- [ ] 生成不报错（manifest 装配成功）
- [ ] trace 记录 manifest_version
- [ ] manifest 拉取失败时报错（不再降级，evolution 单点）

### 进化闭环
- [ ] `/api/pipeline/surface/run` 产出候选 surface
- [ ] 候选过 static_check（C 类过 state_schema 契约）
- [ ] 批准后 manifest 发布新版本
- [ ] 执行端收到 `/internal/manifest/refreshed` 后重拉

## 常见问题

**Q: migrate_to_surface 报"迁移源文件不存在"**
A: 脚本从 `__file__` 算 Writer/ 根，确保在 evolution/ 目录下运行。

**Q: executor 拉 manifest 404**
A: 确认 evolution 已启动 + 已跑迁移 + EVOLUTION_URL 配置正确。

**Q: 装配报"无法拉取 production manifest"**
A: manifest 唯一路径，拉取失败即报错（无降级后备）。确认 evolution 服务在线 + 已发布 manifest。

**Q: 装配报"未知 middleware 规格 class=XXX"**
A: `_middleware_spec_builders` 白名单缺该类。在 meta/agent.py 的该方法加映射。

**Q: C 类 GoalMiddleware 加载失败**
A: 确认执行端有 `app.domains.writing.tools.GoalState`（C 类代码 import 它）。
