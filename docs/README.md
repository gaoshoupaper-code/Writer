# Writer 项目全景图

> 这是 docs/ 的入口。从这里你能看到整个系统长什么样、各部分怎么咬合。
> 想深入看系统怎么运转、机制细节，进入 [**系统心智模型图**](系统心智模型.md)。
>
> **本文件是"活文档"**：只反映项目现在的样子，不记变更历史。
> 如果发现内容和代码对不上，说明文档失准，让 AI 按规范补齐。

---

## 一句话定位

**Writer 是一个"AI 帮人写小说/剧本"的系统。** 用户在桌面端 App 里提需求，
后端跑一条 AI 流水线，把需求一步步加工成完整的作品。

> **桌面端改造说明**：用户入口已从浏览器 Web（原 `frontend/` Next.js）迁移为
> Windows 桌面端 App（`desktop/` Tauri 2 + React）。原 `frontend/` 已废弃，
> UI 代码迁入 `desktop/src/`。新增 `website/`（Astro 官网 + 下载页，由 executor 服务器托管）。
> 桌面端是**纯远程客户端**——不携带后端代码，通过 Rust 中继连服务器 executor。

---

## 整个系统长什么样

```
┌─────────────┐     请求      ┌──────────────────────────────┐
│  frontend/  │ ────────────► │        executor/             │
│  前端网页    │               │  后端核心(FastAPI服务)        │
│ (Next.js)   │ ◄──────────── │                              │
│             │     返回剧本    │  ┌────────────────────────┐  │
└─────────────┘               │  │  domains/writing/      │  │
   用户在这里                    │  │  写作流水线(本次重点)   │  │
   操作浏览器                    │  └────────────────────────┘  │
                              │  ┌────────────────────────┐  │
                              │  │  platform/             │  │
                              │  │  公共底座(所有域共用)    │  │
                              │  └────────────────────────┘  │
                              └──────────────────────────────┘
                                  │ ①trace完成通知        ▲ ⑤prompt更新通知
                                  ▼ ②拉取trace内容        │ ⑥标记缓存stale重拉
                              ┌──────────────────────────────┐
                              │        evolution/            │
                              │  进化系统(诊断+优化+回灌)      │
                              │  ③诊断问题 ④优化prompt        │
                              └──────────────────────────────┘
                              (用户不直接用，通过HTTP与executor联动)

           ┌──────────────────────────────────┐
           │          contracts/              │
           │  共享契约(两端共用的数据格式定义)   │
           │  executor 和 evolution 都依赖它   │
           └──────────────────────────────────┘
              ▲                       ▲
              │ import                │ import
              └──────┐         ┌──────┘
            executor/         evolution/

scripts/  = 工具脚本(分层检查器 + 桌面端发布 publish.sh + 管理 CLI manage.py)
docs/     = 你正在看的这套文档

desktop/  = 桌面端 App（Tauri 2 + Vite + React）—— 用户操作入口
           纯远程客户端：通过 Rust reqwest 中继连服务器 executor
           UI 代码从原 frontend/ 迁入（含离线缓存、自动更新）
website/  = 官网（Astro 静态站）—— 项目介绍 + 桌面端下载页
           由 executor 服务器 nginx 顺带托管，内容用 Content Collections 管理
```

**executor 和 evolution 的联动（进化闭环）**：
1. executor 跑完一次生成，通知 evolution「有新 trace 了」
2. evolution 从 executor 拉取 trace 内容（HTTP，不读文件系统）
3. evolution 诊断 trace 发现问题，优化 prompt
4. 优化后的 prompt 批准上线（打 production label）
5. evolution 通知 executor「有新版本了」
6. executor 标记缓存 stale，下次构建 agent 时拉取新版 prompt

---

## 五大块各干什么

### 1. frontend/ — 用户看到的网页
- **是什么**：一个 Next.js 网页应用（前端，front-end，用户直接看到、操作的那一层）。
- **干嘛的**：用户在浏览器里输入需求、看生成结果。它自己不做任何 AI 计算，
  只是把用户的请求转发给后端，再把后端返回的剧本显示出来。
- **可类比成**：餐厅里的"服务员"——接单、传菜，但不进厨房。
- **状态**：🟡 待补详细文档。

### 2. executor/ — 后端核心（最重要的块）
- **是什么**：一个 FastAPI（一种 Python 写后端服务的框架）服务。所有 AI 计算都在这里。
- **干嘛的**：接收前端请求 → 调用 AI 流水线生成剧本 → 返回结果。
- **内部结构**：
  - `app/main.py` = 服务的入口（启动点，把所有路由和组件装起来）。
  - `app/routers/` = API（接口，程序对外提供的服务入口）入口，每个文件管一类请求
    （如 `screenplay.py` 管"生成剧本"、`character.py` 管"角色"）。
  - `app/platform/` = 公共底座，所有业务域共用的基础设施（运行时、追踪、鉴权等）。
  - `app/domains/writing/` = **写作流水线**（本套文档的重点，见下钻）。
  - `app/domains/image/` = 图像域（待补）。
- **状态**：writing 域已详写 ✅，其余待补。

### 3. evolution/ — 进化系统（诊断 + 优化 + 回灌）
- **是什么**：一个独立的服务，**用户不直接用**，通过 HTTP 与 executor 联动。
- **干嘛的**：观察 executor 的运行记录（trace），分析"哪次生成得好、哪次不好"，
  优化 prompt，并把优化结果回灌给 executor（让改进及时生效）。
- **内部结构**（5 层 + 启动入口）：
  - `app/ingestion/` = 采集层（拉取 trace、投影、入库）
  - `app/diagnosis/` = 诊断层（规则标红、LLM 评估、规律挖掘）
  - `app/improvement/` = 改进层（优化 prompt、A/B 实验、版本管理）
  - `app/view/` = 查看层（统计、详情、可视化看板）
  - `app/core/` = 公共底座（数据库、配置、LLM 调用）
- **与 executor 的关系**：通过 HTTP 双向联动（不读对方文件系统）——
  executor 通知 evolution 有新 trace，evolution 拉取内容；evolution 优化完通知 executor 刷新。
- **状态**：✅ 已详写（见文件大地图）。

### 4. contracts/ — 共享契约（两端的数据格式协议）
- **是什么**：一个独立的 Python 包，定义 executor 和 evolution **共同遵守的数据格式**。
- **干嘛的**：它是两端的"合同"——executor 写 trace 数据时按这里的格式写，
  evolution 读 trace 数据时按这里的格式读。没有它，两边各自定义格式，
  改一边忘改另一边就会对不上。现在格式定义集中在这一处，改一次两端同步生效。
- **内容**：
  - `contracts/trace/` = trace 数据 schema（执行轨迹的数据结构定义）。
  - `contracts/api/` = 两端通信的 API 模型（请求/响应的数据格式）。
- **关键约束**：contracts 自己**不依赖** executor 也不依赖 evolution（它是被依赖的一方，
  不能反过来依赖）。这条铁律由 `check_layering.py` 机器校验。
- **状态**：✅ 已建立（Phase 1 重构）。

### 5. scripts/ — 工具脚本
- **是什么**：开发者用的辅助脚本。
- **干嘛的**：目前只有一个 `check_layering.py`（分层检查器），用来检查代码有没有
  违反"分层铁律"（防止代码依赖关系乱套）。Phase 1 后它还额外检查 contracts 是否
  保持独立（不反向依赖两端）。
- **状态**：🟡 待补详细文档。

---

## 一个请求怎么流转（核心数据流）

以"用户点生成剧本"为例，一次请求的完整旅程：

```
1. 用户在 frontend 网页填好需求，点"生成"
       ↓
2. frontend 把请求发到 executor 的某个 API（HTTP 请求）
       ↓
3. executor 的 router（在 app/routers/screenplay.py）收到请求
       ↓
4. router 调用 MetaAgentService（在 domains/writing/agent.py）
   这是写作流水线的"服务壳"
       ↓
5. 服务壳构建运行时上下文，调 harness 包（evolution/harnesses/current/）装配 agent，
   再编排 SSE 流式生成。流水线四岗位（访谈员→故事建筑师→分场细化员→正文写手）
   的装配逻辑都在 harness 包内，每个环节产出一份中间文件，传给下一环
       ↓
6. 流水线跑完，剧本成品返回给 router
       ↓
7. router 通过 HTTP 响应把剧本返回 frontend
       ↓
8. frontend 把剧本显示给用户
```

**关键认知**：真正"干活"的是第 5 步的那条流水线。它就是 `domains/writing/` 要讲的故事。

---

## 下钻：系统怎么运转

想看系统怎么协作、请求怎么流转、核心机制内部怎么转，进入**系统心智模型图**：
这是 7 张 Mermaid 图组成的分层下钻体系（全局视图 → 端级请求流 → 机制深潜）。

| 想看什么 | 去哪看 |
|---------|--------|
| **系统全貌**（三端协作 + 数据流向） | [系统心智模型.md · 图1](系统心智模型.md) |
| **executor 怎么运转**（写作请求穿过哪些组件） | [系统心智模型.md · 图2](系统心智模型.md) |
| **evolution 怎么运转**（trace 摄入 + 进化闭环） | [系统心智模型.md · 图3](系统心智模型.md) |
| **前端结构**（Web/桌面/官网三应用） | [系统心智模型.md · 图4](系统心智模型.md) |
| **核心机制深潜**（memory / harness / writing 内部） | [系统心智模型.md · 图5-7](系统心智模型.md) |

---

## 自测：你看懂了吗？

读完上面这些，试着回答：
1. 如果用户点"生成剧本"，请求最先到哪个块？（答：frontend → executor）
2. evolution 和用户直接打交道吗？（答：不，它只观察 executor）
3. 真正生成剧本的代码在哪个目录？（答：executor/app/domains/writing/）
4. 想知道 executor 平台层有哪些中间件，去哪看？（答：系统心智模型.md · 图6）

答得上来 = 你已经建立了全局心智。

---

## 文档怎么维护

这套文档靠 **AI 自动维护**：每次开发/修改/重构后，AI 必须按 `AGENTS.md` 里的
"文档同步铁律"更新系统心智模型图。你不需要手动记改动，只需偶尔抽查文档准不准。
（详见项目根的 `AGENTS.md` 第一节"文档同步铁律"。）
