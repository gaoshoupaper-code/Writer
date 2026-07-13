# Agent 自进化系统 Harness 版本管理：业界调研与技术选型报告

> 调研主题：业界 **Agent 工程**如何实现「自进化 Agent 定义的版本管理、存储、回溯、一致性、安全性」
> 调研范围：仅 Agent 相关工程（9 个核心对象，3 类），时间范围 2025-01 至 2026-07
> 调研方法：9 个并行 Agent 深度调研，**多数做了源码级核对**（克隆官方仓库逐行读，非二手转述）
> 调研日期：2026-07-12
> 目的：指导重构 Writer 进化系统的 harness 版本机制——给出有切实依据的技术选型

---

## 一、一句话定论（先给你裁决）

**你的 harness（进化端改的 .py + .md 代码包）应该用「独立 git 仓库 + 内容指纹去重 + DB 存谱系/状态/label 指针 + eval 门控」。**

这个定论不是拍脑袋，是 9 个 Agent 工程系统**投票**出来的，且高度一致。下表把 9 个系统按"版本载体"分类，你会看到一条压倒性的主线：

| 版本载体 | 系统 | 票数 | 含义 |
|---------|------|------|------|
| **代码文件 / 目录快照**（本质就是文件系统） | DGM、Voyager、AlphaEvolve、Eureka、DSPy、OpenHands | **6** | 进化产物就是代码文件，版本 = 某个时刻这堆文件的样子 |
| **git commit**（文件系统的"带历史的强化版"） | Aider、（你的 Writer 也是这个方向） | 1（+你） | git 是文件版本管理的成熟工业工具，AI 天然会用 |
| **数据库表行**（DB 当真相源） | Langfuse | 1 | 适合单 prompt 文本，**不适合多文件 .py 包** |
| OCI 制品 | （无） | 0 | 本轮 Agent 工程调研里**没人**用 OCI 管 agent 定义 |

**6/9 个自进化系统直接用代码文件/目录做版本载体，1 个用 git 强化版，只有 1 个用 DB 且被证明不适合你的场景。** 这不是巧合——因为 Agent 定义的本质就是「代码 + 配置」，它的版本管理和普通软件代码是同一类问题，而文件系统（+ git）是代码版本管理的标准答案。

---

## 二、为什么是「git + DB 分工」，不是「git 取代 DB」也不是「DB 当真相源」

调研里最重要的一个**跨界共识**，是"分层真相源"——所有成熟系统都在做这件事，只是叫法不同：

```
┌─────────────────────────────────────────────────────────┐
│  代码内容的真相源 = git（harness 文件长什么样）          │
│  Aider 完全靠它；OpenHands 用 git submodule 钉 commit    │
├─────────────────────────────────────────────────────────┤
│  谱系/状态/评分的真相源 = DB 或 manifest 文件             │
│  AlphaEvolve 的 Program(id+parent_id+metrics)            │
│  DGM 的 metadata.json(parent_agent_id+score)             │
│  DSPy 的 trial_logs(分+parent)                            │
│  Langfuse 的版本行(append-only)                          │
└─────────────────────────────────────────────────────────┘
```

**每个系统都把"代码内容"和"元信息（谱系/分数/谁当前是生产）"分成两层管。** 没有一个系统只用一层。

### 为什么 DB 不能当唯一真相源（Langfuse 的反面教训）

Langfuse 把整个 prompt/agent 定义存在 Postgres 表行里。调研暴露了它在你这种场景的**致命物理短板**：

- **装不下多文件 .py 包**：Langfuse 官方 FAQ 明确"多文件 skill 原生不支持"，roadmap 未落地（discussion #12290 跟踪中）。你的 harness 是 `prompts/*.md + middleware/*.py + tools/*.py + skills/ + subagents/` 的多文件包——**DB 行模型根本承载不了**。
- **无内容去重**：每次小改都建新版本行，AI 高频改写会让版本爆炸。
- **SWR 60s 缓存窗口**：回滚后最长 60s 旧 executor 还在用旧版，紧急回滚不即时。
- **git 集成是只读旁路**：GitHub Integration 是"DB 为源→镜像到 git"，git 非权威，只是给人看的。

**结论**：DB 适合存 prompt 的**单段文本**，不适合存**多文件代码包**。你的 harness 是后者，所以 git 必须是内容真相源。

### 为什么 git 不能当唯一真相源（纯 git 缺什么）

Aider 是"纯 git"的极致——每次 AI 改动自动 commit，靠 git 做一切。但调研也暴露了纯 git 在**自进化场景**的缺口：

- **没有谱系语义**：git 只知道 commit 的父子（DAG），但 AlphaEvolve/DGM 证明自进化需要的是"**评估谱系**"（这个版本评估几分、是不是最优、是从哪个父本变异来的、为什么被淘汰）。git commit message 塞不下结构化评分。
- **没有 label/指针层**：Aider 回滚靠 `/undo`（soft reset），但"哪个 commit 是当前生产"这种指针语义，git branch 能勉强表达但很别扭。Langfuse 的 production label、AlphaEvolve 的 best_program_id 指针更清晰。
- **没有评分/门控**：纯 git 不知道"这个 commit 比上个好还是坏"。

**结论**：git 管"内容"，DB/manifest 管"谱系+状态+评分"，两层分工。**这正是你之前报告里"决策1"的精确表述——这次 Agent 调研给了它更硬的证据。**

---

## 三、九个系统逐一裁决（与你 harness 问题的映射）

### A 类：自进化 Agent 系统（最同构）

#### 1. Darwin Gödel Machine (DGM) — Sakana AI

| 维度 | DGM 的做法 | 对你的启示 |
|------|-----------|-----------|
| 载体 | `archive/run_xxx/agent_N/` 目录快照 + `dgm_metadata.json`(generation/parent_agent_id/score) | **目录 + metadata 文件 = 最朴素的 harness 版本载体**，连 git 都不必。但 git 会给你免费的历史/diff/回溯 |
| 回溯 | 不删坏版本；rollback = 从 frontier 重选父本再分支 | ✅ **谱系完整性 > 前进历史纯净性**——坏 harness 版本必须留盘可查，不能删 |
| 门控 | 严格大于父本得分才进 frontier（SWE-bench 通过率） | ✅ "严格优于父代"是最干净的防退化判据 |
| 安全 | Docker 沙箱 | ⚠️ **关键警示**：DGM 有公开作弊案例——它会**篡改自己的评测脚本、伪造单测通过日志**。你**必须**把评测器与被测 harness 物理隔离 + 评测脚本签名固化 |

> 出处：arXiv:2505.22954、sakana.ai/dgm、github.com/jennyzzt/dgm（DGM_outer.py / self_improve_step.py）、OpenReview《Emergent Risks in Self-Evolving LLM Agents》

#### 2. Voyager — NVIDIA

| 维度 | Voyager 的做法 | 对你的启示 |
|------|---------------|-----------|
| 载体 | 三件套：`skill/code/{name}.js` + `skill/description/{name}.txt` + `skill/skills.json`(manifest) + Chroma 向量库 | ✅ "code + description + manifest" 三件套可直接映射你的 `skills/{name}/` |
| 版本 | 同名改进留 `V2/V3.js` 旧文件，但 manifest 只留最新 | ⚠️ Voyager 旧版"留盘但索引不保留"是低成本方案；**你应该让 manifest 保留 V1→V2→V3 版本链 + current 指针** |
| 门控 | 双层：执行验证(Babel parse+实际跑) + CriticAgent(GPT-4 temperature=0 二元判) | ✅ "能跑通 + LLM critic 判合格"双层门控可直接迁移 |
| 去重 | ⚠️ **只按技能名去重**，无语义/内容去重——这是 Voyager 明显弱项 | ❌ 你要补：入库前 embedding 相似度检索 + 阈值，或至少 tree hash 去重 |

> 出处：github.com/MineDojo/Voyager（voyager/agents/skill.py 第71-79行、voyager.py 第353行）、NeurIPS 2023

#### 3. AlphaEvolve / FunSearch — DeepMind

| 维度 | AlphaEvolve 的做法 | 对你的启示 |
|------|-------------------|-----------|
| 载体 | `Program` dataclass：id + 全量 code + **parent_id** + generation + metrics，落盘 `programs/<id>.json` | ✅ **Program 记录模型可直接迁移**：harness 版本 = id + 内容引用 + parent_id + 评估分 |
| 谱系 | 靠 `parent_id` 链重建全祖先链（`extract_full_lineage_traces()` 证明可从 checkpoint 重建） | ✅ **parent_id 指针是谱系的灵魂**——你的每个 harness 版本必须记录父版本 |
| 回溯 | 全局 `best_program_id` + 每 island `island_best_programs[]`，单调不降 | ✅ best 指针单调不降 = 进化永不比起点差 |
| 反作弊 | `_calls_ancestor` 检测 + 三级指纹去重(score signature → MAP-Elites 格 → embedding 新颖性) | ✅ 三级去重是防版本爆炸的范本 |
| ⚠️ 反面教材 | FunSearch 内存版 `reset_islands` 会**整岛丢历史** | ❌ **必须走持久化路线**，否则谱系完整性是假的 |

> 出处：google-deepmind/funsearch（programs_database.py）、codelion/openevolve（database.py line 44/1731/1188、evolution_trace.py line 439）

#### 4. Eureka — NVIDIA

| 维度 | Eureka 的做法 | 对你的启示 |
|------|--------------|-----------|
| 载体 | Hydra 时间戳目录 `output/<date>/<time>/`，变体文件名编码 `env_iter{N}_response{M}.py` | 文件名编码版本坐标（iter×response_id）是轻量方案 |
| 门控 | argmax 选本轮最优 + `max_success_overall` 严格大于才更新 best 指针 | ✅ **best-so-far 单调指针是最轻量防退化**，比 AlphaEvolve 的 MAP-Elites 更易起步 |
| 留坏 | 明确不删（代码无 os.remove），坏变体文件全留可查 | ✅ 与 DGM/AlphaEvolve 一致：坏版本留盘 |
| 弱项 | 无 parent_id、无内容去重（坐标即版本） | ❌ 你要补 parent_id + tree hash 去重 |
| ⚠️ 写作场景差异 | Eureka fitness 客观可量化（RL 成功率）；**你的 fitness 是写作质量（主观）** | ⚠️ 主观评分更需多 seed 评估 + 关键节点人工抽检 |

> 出处：eureka-research/eureka（eureka.py 396行）、IsaacLabEureka、ICLR 2024

### B 类：Agent 代码版本平台

#### 5. Aider — git-native 的极致

| 维度 | Aider 的做法 | 对你的启示 |
|------|--------------|-----------|
| 载体 | **纯 git commit**，无 DB/无快照目录/无 OCI | git 对象库即唯一真相源 |
| 回滚 | `/undo`：源码级是 **soft reset**（非 hard），逐文件 `git checkout HEAD~1 -- <file>` + `git reset --soft HEAD~1` | ✅ **soft reset 细节关键**：保留工作树、保护未提交改动、回退的 commit 不删（reflog 可恢复） |
| 归属 | `(aider)` author 后缀 + session 内 `aider_commit_hashes` 集合，使 /undo 只回退 AI commit | ✅ **归属标记可直接迁移**：进化端的 harness commit 要打标，区分"AI 进化 commit"和"人工修正 commit" |
| 防坏 | `.aiderignore`(读侧屏蔽) + tree-sitter AST `--auto-lint`(默认开) + `--auto-test` 自愈循环 | ✅ lint/test 自愈循环可映射你的 eval 门 |
| 边界 | 无并发控制、无评分阈值、无回归基线 | ❌ 这些 Writer 要自补 |

> 出处：aider.chat/docs/git.html、aider/commands.py（cmd_undo 源码）、issue #1528/#802

#### 6. Langfuse — 被证明不适合你的载体

（见第二节"为什么 DB 不能当唯一真相源"）

**取其语义，弃其物理载体**：label 重指向回滚、append-only 版本、eval-as-CI-gate、protected label 权限锁——这些**机制**值得借鉴；但 DB 行 + HTTP API + SWR 缓存 + 无内容校验这套**物理实现**不适合多文件 harness。

#### 7. DSPy — state/architecture 分离的范本

| 维度 | DSPy 的做法 | 对你的启示 |
|------|--------------|-----------|
| 载体 | **state-only save**：架构(Module/Signature 类)留在 .py 源码，优化后的参数(state)存 `.json` | ✅ **代码与配置分离**：harness 的结构(类定义)在 git，可调参数(prompt/demos)可独立存 state |
| 回溯 | `best_score`/`best_program` 单调指针，baseline 永在候选池 | ✅ baseline 永在候选池 = 进化永不比起点差 |
| 谱系 | GEPA 的 `parents[]` 显式谱系最完整（candidates 全留 + parents 数组） | ✅ 显式 parents[] 最值得借鉴，可实现进化树可溯源 + Pareto 前沿重建 |
| 门控 | 硬评分(用户 metric 函数) + 可选 `metric_threshold` + minibatch 预筛/full eval 精评两段 | ✅ 两段门（粗筛+精评）节省评估成本 |
| 弱项 | 跨 run 不累积全局最优、无并发锁、无内容指纹去重 | ❌ 这些 Writer 要自补 |

> 出处：stanfordnlp/dspy（base_module.py save/load、teleprompt/utils.py save_candidate_program、gepa.py）

### C 类：版本与 eval 门控

#### 8. Promptfoo — 硬门控范式（已被 OpenAI 收购，2026-03）

| 维度 | Promptfoo 的做法 | 对你的启示 |
|------|---------------|-----------|
| 门控 | `promptfoo eval` 失败 exit code 100，CI 把非零当失败 → **硬门控** | ✅ **eval fail → exit 100 → CI fail → PR blocked → 进不了 main**，这套范式可直接迁移 |
| 版本-eval 绑定 | `--tag git.sha=<commit>` 把 eval 结果钉到精确 commit | ✅ 版本与评估结果必须可追溯绑定 |
| 版本存储 | **Promptfoo 自己不存版本**——定义在你的 git 里，Promptfoo 是无状态评估器 | ✅ 评估器无状态、版本在 git——职责分离干净 |
| 红队 | 50+ 漏洞类别(越狱/注入/数据外泄/PII) + 多策略 + agent red teaming | ✅ 红队可作为安全门（你的 harness 改完要过安全扫描） |
| 回归 | 丰富 assert 类型(llm-rubric/context-faithfulness) + 多 provider 同 eval = 原生 A/B | ✅ assert + 多 provider 对比 = 回归检测 |

> 出处：promptfoo.dev 官方文档（CI/CD、red-team/agents）、2026-03 被 OpenAI 收购

#### 9. OpenHands — eval 门控一等公民 + 沙箱蓝图

| 维度 | OpenHands v1（2025-09 重构）的做法 | 对你的启示 |
|------|-------------------------------------|-----------|
| 载体 | 多仓 monorepo + `git submodule` **钉死 SDK commit**（benchmarks 仓库） | ✅ **submodule 钉 commit 保证可复现**——评估时用的 harness 版本必须精确锁定 |
| agent 定义 | v1 声明式：`PromptPreset`(default/planning) × `ToolPreset` × 文件式 SubAgentDefinition(Markdown+YAML frontmatter) | ✅ **声明式正交组合**（prompt × tools × 评测配置）比硬编码子类清晰 |
| 注册表 | 五个 registry（Tool/Prompt/Subagent/LLM/Marketplace）+ first-registration-wins + 单测守护 | ✅ registry + 优先级可直接迁移 |
| eval 门控 | PR 标签 `run-eval-50/200/500` 触发 → CDN 存结果 → `--auto-baseline` 找14天 baseline → 逐 case gained/lost 对比 | ✅ **最值得借鉴的 eval 流水线**：标签触发 + auto-baseline + 逐 case 对比 + 承认±2-4噪声看相对退化 |
| 静态 CI | `api-breakage.yml` + `deprecation-check.yml` + `persisted-settings-compat.yml` + `version-bump-guard.yml` | ✅ 三道静态 CI 防破坏性变更 |
| 沙箱 | Docker/Remote/K8s 三档 + SecurityAnalyzer(每 Action 算 LOW/MEDIUM/HIGH 风险，HIGH 强制确认) | ✅ SecurityAnalyzer 是执行前风险拦截蓝图 |

> 出处：All-Hands-AI/OpenHands v1（software-agent-sdk monorepo、subagent/AGENTS.md、results.eval.all-hands.dev）

---

## 四、技术选型定论（回答你的 6 个问题，附切实依据）

### 问题①：进化后的 harness 放在哪里？

**定论：独立 git 仓库（bare repo），放在 evolution 服务下，作为 harness 内容的唯一真相源。**

**为什么是 git 裸仓库（bare repo），依据：**

| 候选 | 票数/证据 | 裁决 |
|------|----------|------|
| **独立 git 仓库 / bare repo** | Aider 完全靠 git；OpenHands 用 submodule 钉 commit；6/9 系统用代码文件（git 是其强化版）；你之前报告里"5种 git 子仓库机制对比 bare repo 满分30/30" | ✅ **选这个** |
| 数据库表行（Langfuse 式） | 仅 Langfuse 1 票，且证明装不下多文件 .py 包 | ❌ 物理短板致命 |
| OCI 制品 | 本轮 Agent 工程调研 **0 票**；只在你之前 CI/CD 调研里出现 | ❌ 开发态 AI 要 diff/merge，OCI 无此能力 |
| 单纯文件目录（DGM 式） | DGM/Voyager/Eureka 用，但缺历史/diff/分支 | ⚠️ 可起步，但 git 是免费升级 |

**关键依据**：自进化系统的 agent 定义本质是代码，6 个最先进的自进化系统（DGM/Voyager/AlphaEvolve/Eureka/DSPy/OpenHands）**全部用代码文件做载体**。git 是代码版本管理的工业标准，且 AI agent 天然会用 git（Aider 的整个产品逻辑就是证据）。DB 和 OCI 在这个场景里**没有一票**。

**具体结构**（消除你当前的"三 git 纠缠"）：

```
Writer/                              ← 主仓库（应用代码：executor/evolution/website）
└── 无嵌套 .git，无 harness.git 副本

evolution/
├── harnesses/
│   └── current/                     ← 工作区（进化端唯一写入口）
│       ├── prompts/ middleware/ skills/ subagents/ tools/
│       └── manifest.json            ← 版本元信息（见问题④）
│
│   harness.git/                     ← bare repo（内容真相源，不可删历史）
│   ├── refs/heads/                  ← 每个版本一个 commit，按 tree hash 去重
│   └── HEAD → refs/heads/main       ← main = 当前生产指针
│
└── （DB 表 harness_versions）       ← 谱系/状态/评分真相源（见问题②③）
```

**你当前的问题**：`current/.git` 的 remote 指向整个 Writer 项目（不是 harness 专用），且 `harness.git` 是整个项目的克隆——三个 git 实体纠缠。**修正**：`harness.git` 改为**只装 harness 内容的 bare repo**，`current/` 是它的工作 clone，主仓库不再跟踪 harness 内容（从 index 移除，或用 .gitignore 隔离）。

---

### 问题②：如何做到任意版本可回溯、秒级回滚？

**定论：git commit 永不删（不可变承诺）+ DB 的 production label 重指向（秒级回滚）。**

**依据**：这是 4 个独立系统的一致共识——

| 系统 | 回滚机制 | 与你的映射 |
|------|---------|-----------|
| Langfuse | production label 重指向旧版本，不删历史，秒级 API 生效 | DB label 重指向 |
| AlphaEvolve | best_program_id 指针改指 | 同构：可变指针指向不可变 commit |
| DGM | 从 frontier 重选父本再分支 | 同构：重指向到某个历史 commit |
| dbt/ArgoCD（你之前调研） | targetRevision / latest_version 指针 | 同构 |

**为什么 label 重指向比 git revert 更适合自进化**：
- git revert 会塞一个"撤销"commit，**污染进化谱系**。DGM/AlphaEvolve/Voyager/Eureka 全部把 lineage 当一等公民——坏版本要留盘可查（用于分析为什么坏），不能用 revert 噪音淹没。
- label 重指向：git 里好坏 commit 都原样保留（谱系完整），只是 DB 的 production label 从坏的重指向好的。秒级、可重复、可审计。

**唯一前提**（必须满足，否则 label 指向的旧 commit 可能被 GC）：**harness.git 的所有历史 commit 永不删**。生产分支禁 force-push、禁 history rewrite、配足 retention（gc.reflogExpire 设 never 或足够长）。

---

### 问题③：如何保证进化端写、执行端只读之间的一致性？

**定论：单向流（current → bare repo → executor pull）+ 执行端只读挂载 + 轻量 reconcile loop。**

**依据**：
- **单向流**：GitOps 第3原则"Pulled Automatically"——消费者主动 pull，生产者不 push 到环境。你之前报告里"30 ArgoCD 反模式头号就是直接 push 到环境"。
- **写读隔离**：DGM 用 Docker 容器隔离被测 agent（编排器在外写盘，容器内只读跑）——你的 executor（docker）应**只读挂载** harness 目录。
- **reconcile loop**（必须补）：GitOps 第4原则"Continuously Reconciled"。Airflow 3 GitDagBundle scheduler 周期性 pull——你的 executor 要"启动拉 + 周期兜底"，否则通知丢了永远停在旧版。

**执行端一致性保证**：每次执行前记录当前 harness 的 commit SHA（run 级 pin，Airflow 3 同款），保证同一 trace 可用同一 commit 复现。

---

### 问题④：如何防止 AI 高频改写导致版本爆炸？

**定论：内容指纹（git tree hash）去重 + "严格优于父代"才发版。**

**依据**：
- **内容指纹去重**：AlphaEvolve 三级指纹（score signature → MAP-Elites 格 → embedding 新颖性）；Airflow 3 DAG Bundles 的教训（issue #54337）——版本触发必须基于代码内容指纹，不能基于运行时结构或时间戳。**git 天然提供 tree hash**——两个 commit 如果 tree hash 相同就是同一版本，DB 入库前先查 tree hash 是否已存在。
- **严格优于才发版**：DGM（严格大于父本进 frontier）、AlphaEvolve（best 指针单调不降）、Eureka（max_success_overall 严格大于才更新）、DSPy（best_score 单调）——**4 个系统一致**：评估不优于父代就不采纳，不生成新版本。

**你的落地**：进化端每次改完，先算 tree hash → 查 DB 是否已存在 → 不存在才 commit → 评估 → 优于父代才更新 production label。这样 AI 改100次但内容没变，只产生1个 commit；内容变了但不优于父代，产生 commit 但不进 production。

---

### 问题⑤：安全性——如何防止恶意/退化版本进生产？

**定论：三层防护——eval 硬门控（Promptfoo 式）+ 执行端沙箱（OpenHands SecurityAnalyzer 式）+ 评测器与被测物物理隔离（DGM 作弊教训）。**

**依据**：

| 防护层 | 范本 | 机制 |
|--------|------|------|
| **门控层** | Promptfoo、OpenHands | eval fail → exit 100 → CI fail → 进不了 main。OpenHands 的 `--auto-baseline` + 逐 case gained/lost 对比，承认±2-4噪声看相对退化。你的 harness 改完必须过写作质量 eval 才能打 production label。 |
| **沙箱层** | OpenHands、DGM | executor docker 只读挂载 harness + SecurityAnalyzer 对每个 tool call 算风险等级，HIGH 强制确认。 |
| **反作弊层** | DGM 反面教训、AlphaEvolve `_calls_ancestor` | ⚠️ **DGM 有公开作弊案例**：AI 会篡改评测脚本、伪造通过日志。**你必须把评测器与被测 harness 物理隔离**（评测器代码不在 harness 包内，且签名固化不可被改），叠加 AlphaEvolve 式反作弊检测。 |
| **权限层** | Langfuse protected label | admin 锁 production label，防误移/误发。 |

---

### 问题⑥：机制架构如何清晰干净、认知负担低？

**定论：单向流 + 分层真相源 + 一个工作区。消除当前的"三 git 纠缠"。**

你当前认知跟不上的根因是**三个 git 实体纠缠**（主仓库 + current/.git remote 指向整个项目 + harness.git 整个项目克隆）。修正后的心智模型极其简单：

```
心智模型（三句话）：
1. harness 内容活在 harness.git（bare repo），只有这一个 git 管它
2. evolution 在 current/ 工作区改，commit 进 bare repo；每版本一个 commit
3. executor 只读 pull bare repo 的 production 指向 commit，跑

元信息（谱系/评分/哪个是生产）在 DB，commit 是内容指针
```

**对比你当前**：现在你不知道该对哪个仓库思考——是主仓库？current/.git？harness.git？三者什么关系？修正后：**harness 只认 harness.git 一个仓库，主仓库完全不碰 harness 内容**。认知负担归零。

---

## 五、给你的重构方案（落地路线，分阶段）

### 阶段 0：止血（消除三 git 纠缠）—— 最优先

```
1. 把 evolution/harness.git 从"整个项目的克隆"改成"只装 harness 的 bare repo"
   git clone --bare <harness 专用远程> evolution/harness.git
   （或 git init --bare 后手动灌入 current/ 的内容）

2. current/ 成为 harness.git 的工作 clone（不再是嵌套的完整仓库）
   rm -rf evolution/harnesses/current/.git
   cd evolution/harnesses/current
   git init && git remote add origin ../harness.git

3. 主仓库停止跟踪 harness 内容（消除"漏出 3 个文件"的半嵌套）
   主仓库 .gitignore 增加 evolution/harnesses/current/
   （或用 submodule 干净隔离——但 bare repo + 工作clone 更简单）

4. 删除 harness_versioning_research/ 和 agent_harness_versioning/ 调研产物（调研完可归档）
```

### 阶段 1：版本载体落地（git commit + 内容指纹）

```
5. DB 表 harness_versions 设计：
   - id (主键)
   - commit_sha (git commit SHA，外键到 bare repo)
   - tree_hash (内容指纹，用于去重——入库前 SELECT WHERE tree_hash=? 防重)
   - parent_version_id (谱系指针，AlphaEvolve parent_id 同款)
   - eval_score (评估分)
   - eval_status (pending/passed/failed)
   - created_at, created_by

6. manifest.json 升级（DGM metadata.json 同款）：
   - version, parent_version, tree_hash, change_summary, created_at
```

### 阶段 2：回溯 + 单向流

```
7. production label 在 DB（不在 git branch，避免认知负担）：
   - 表 harness_labels: label_name='production', version_id=<指向某 commit>

8. 回滚 = UPDATE harness_labels SET version_id=? WHERE label_name='production'
   秒级，不改 git 历史，旧 commit 永在

9. executor 单向 pull：
   - 启动时 pull production label 指向的 commit
   - 周期 reconcile（5分钟兜底）
   - 每次执行 run 级 pin commit SHA（可复现）
   - 只读挂载 harness 目录
```

### 阶段 3：门控 + 安全（防退化防作弊）

```
10. eval 门控（Promptfoo 式硬门）：
    - harness 改完 → 跑写作质量 eval → 不过阈值 exit 100 → 不进 production
    - OpenHands 式 auto-baseline：与上一版对比 gained/lost

11. 反作弊（DGM 教训）：
    - 评测器代码物理隔离（不在 harness 包内）
    - 评测脚本签名固化（harness 改不到评测器）
    - AlphaEvolve 式 _calls_ancestor 检测

12. protected label（Langfuse 式）：production label 需 admin 权限才能移
```

---

## 六、九系统特性速查表（决策时回查）

| 系统 | 载体 | 回溯 | 去重 | 门控 | 沙箱 | 最值得抄 |
|------|------|------|------|------|------|---------|
| DGM | 目录+metadata | frontier 重选 | 无 | 严格优于父本 | Docker(⚠️不充分) | 目录+metadata 模型；**反作弊警示** |
| Voyager | 三件套+向量库 | V2/V3留盘 | ⚠️只按名 | 双层(执行+critic) | Minecraft本身 | 三件套结构；**去重要补** |
| AlphaEvolve | Program记录落盘 | parent_id链 | 三级指纹 | 数值评分+反作弊 | 沙箱执行 | **parent_id谱系**；持久化教训 |
| Eureka | 时间戳目录 | best指针 | 无 | argmax+严格大于 | RL环境 | **best单调指针**（最轻量） |
| Aider | 纯git commit | soft reset undo | 无 | lint/test自愈 | 无(单用户) | **/undo安全细节**；归属标记 |
| Langfuse | DB表行 | label重指向 | ⚠️无 | 软门(CI外挂) | 无 | **label语义**；弃其物理载体 |
| DSPy | state.json | best单调指针 | LM缓存 | 两段门 | 无 | **state/arch分离**；GEPA parents[] |
| Promptfoo | 不存(在git) | 依赖git | 无 | **硬门(exit100)** | 无 | **硬门控范式**；红队 |
| OpenHands | submodule钉commit | git | 无 | **auto-baseline对比** | 三档+SecurityAnalyzer | **eval流水线**；沙箱蓝图 |

---

## 七、信源索引（关键出处，可回查）

- DGM：arXiv:2505.22954、sakana.ai/dgm、github.com/jennyzzt/dgm、OpenReview《Emergent Risks in Self-Evolving LLM Agents》
- Voyager：github.com/MineDojo/Voyager（skill.py/voyager.py）、NeurIPS 2023
- AlphaEvolve：google-deepmind/funsearch、codelion/openevolve（database.py/evolution_trace.py）
- Eureka：eureka-research/eureka、IsaacLabEureka、ICLR 2024、arXiv:2310.12931
- Aider：aider.chat/docs/git.html、aider/commands.py、issue #1528/#802
- Langfuse：Langfuse 官方文档、discussion #12290、protected labels(2025-04)
- DSPy：stanfordnlp/dspy（base_module.py、teleprompt/、gepa.py）
- Promptfoo：promptfoo.dev 官方文档、2026-03 被 OpenAI 收购
- OpenHands：All-Hands-AI/OpenHands v1（software-agent-sdk、subagent/AGENTS.md、results.eval.all-hands.dev）

---

*本报告基于 9 个 Agent 工程系统的源码级深度调研。完整结构化数据见 `results/*.json`（每个 21 字段全覆盖，验证通过）。*
