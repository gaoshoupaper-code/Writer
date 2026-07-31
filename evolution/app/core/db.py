"""SQLite 数据层：连接 + 建表 + 迁移。

数据模型 C2（三表含投影）：
- runs            1 trace 1 行，对应 TraceRunSummary
- nodes           1 trace N 行，projector 投影出的树节点（run/agent/llm/tool/skill/todo/error）
                  高频查询字段为独立列，便于 GROUP BY 统计/聚类
- event_payloads  1 trace N 行，原始事件流 + 大字段正文（input/output）
- rules           规则定义（阈值型）
- trace_flags     规则命中打标（trace_id × rule_id）

详见设计文档 `.claude/md/20260619_211000_监测服务设计.md`。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.settings import settings

# SQLite 连接需跨线程共享（FastAPI 线程池 + 后台扫描），用 check_same_thread=False。
# 写操作通过一把全局锁串行化，避免 SQLite "database is locked"。
# 用 RLock（可重入）：init_db 持锁后调用迁移函数，迁移函数内部也需加锁，必须可重入。
_lock = threading.RLock()

logger = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 外键约束开启（trace_flags → runs/rules 等）
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 模块级单例连接。SQLite 单文件 + 全局锁，足够 evolution 的量级。
_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    """获取全局 SQLite 连接（单例）。"""
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def init_db() -> None:
    """建表（幂等）。应用启动时调用一次。"""
    conn = get_conn()
    # 重命名迁移必须在 executescript 之前：否则 executescript 的
    # CREATE TABLE IF NOT EXISTS evidence_dossiers 会先建空表，使 RENAME 冲突。
    _migrate_rename_evidence_packs_to_dossiers(conn)
    with _lock:
        conn.executescript(
            """
            -- runs：trace 根，1 trace 1 行
            CREATE TABLE IF NOT EXISTS runs (
                trace_id      TEXT PRIMARY KEY,
                workspace_id  TEXT NOT NULL,
                thread_id     TEXT,
                session_name  TEXT,
                endpoint      TEXT,
                status        TEXT NOT NULL,          -- running / completed / failed / cancelled / interrupted（recorder.create_run 即写 running 行）
                started_at    TEXT,
                ended_at      TEXT,
                duration_ms   INTEGER,
                event_count   INTEGER DEFAULT 0,
                error         TEXT,
                ingested_at   TEXT NOT NULL,          -- evolution 入库时间
                ingested_seq  INTEGER DEFAULT 0       -- 已从执行端拉取到的最大事件 sequence（增量高水位，D7）
            );

            -- nodes：projector 投影出的树节点，1 trace N 行
            CREATE TABLE IF NOT EXISTS nodes (
                node_id           TEXT NOT NULL,
                trace_id          TEXT NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
                parent_node_id    TEXT,
                kind              TEXT NOT NULL,       -- run/agent/llm/tool/skill/todo/error
                label             TEXT,
                status            TEXT,
                agent_name        TEXT,               -- DeepAgent 编排维度（聚类用）
                agent_role        TEXT,               -- main / subagent
                depth             INTEGER,
                started_at        TEXT,
                ended_at          TEXT,
                duration_ms       INTEGER,
                model_name        TEXT,
                tool_name         TEXT,
                skill_name        TEXT,
                usage_input       INTEGER,            -- token，独立列便于聚合
                usage_output      INTEGER,
                usage_total       INTEGER,
                chain_summary     TEXT,
                error             TEXT,
                PRIMARY KEY (trace_id, node_id)
            );
            CREATE INDEX IF NOT EXISTS idx_nodes_trace ON nodes(trace_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
            CREATE INDEX IF NOT EXISTS idx_nodes_agent ON nodes(agent_name);
            CREATE INDEX IF NOT EXISTS idx_nodes_tool ON nodes(tool_name);

            -- event_payloads：原始事件流 + 大字段正文
            CREATE TABLE IF NOT EXISTS event_payloads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id      TEXT NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
                sequence      INTEGER,
                type          TEXT,
                timestamp     TEXT,
                payload_json  TEXT NOT NULL           -- 整条事件 JSON
            );
            CREATE INDEX IF NOT EXISTS idx_events_trace ON event_payloads(trace_id, sequence);

            -- prompts：prompt 线（Phase 4 T9，langfuse 式版本管理）
            -- ⚠️ DEPRECATED（Phase 6 T5.3，2026-06-23）：surface_versions 表取代。
            -- prompt 现为 surface_type='prompt'，由 harness_manifests 统一接管（决策 D5）。
            -- 本表保留只读（历史记录），不再写入。迁移见 migrate_to_surface.py。
            -- 一个 name 对应一条 prompt，多个 version。
            CREATE TABLE IF NOT EXISTS prompts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,     -- prompt 名（一条"prompt 线"）
                type        TEXT NOT NULL DEFAULT 'text',  -- text / chat
                created_at  TEXT NOT NULL
            );

            -- prompt_versions：prompt 的具体版本
            -- version 单调递增，labels 做发布别名（production/latest/staging）。
            -- label 互斥：同 prompt_id 下一个 label 同时只指向一个 version。
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_id       INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                version         INTEGER NOT NULL,     -- 单调递增
                content         TEXT NOT NULL,         -- prompt 正文
                config          TEXT DEFAULT '{}',     -- 模型配置（temperature 等）JSON
                labels          TEXT DEFAULT '',       -- 逗号分隔：production,latest,staging
                commit_message  TEXT,                  -- 版本说明
                source          TEXT NOT NULL DEFAULT 'manual',  -- manual / optimized / ab_winner
                created_at      TEXT NOT NULL,
                UNIQUE(prompt_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_pid ON prompt_versions(prompt_id);

            -- Phase 1 双层评估：评估分数（内容维度 + subagent 维度）
            CREATE TABLE IF NOT EXISTS evaluation_scores (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id      TEXT NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
                layer         TEXT NOT NULL,           -- content / subagent
                target        TEXT NOT NULL,           -- content 时='novel'; subagent 时=agent_name
                metric        TEXT NOT NULL,           -- 维度名
                score         REAL NOT NULL,           -- 0~1
                evidence      TEXT,                    -- judge 打分依据
                scored_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_eval_scores_trace ON evaluation_scores(trace_id);
            CREATE INDEX IF NOT EXISTS idx_eval_scores_layer ON evaluation_scores(layer);

            -- Phase 1 双层评估：评估任务记录（防重复评、可追溯）
            CREATE TABLE IF NOT EXISTS evaluation_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id      TEXT NOT NULL,
                status        TEXT NOT NULL,           -- pending / done / error
                error         TEXT,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                UNIQUE(trace_id)                       -- 同一 trace 只评一次（重评需删记录）
            );

            -- Phase 1 T1.6：subagent → prompt 归因映射（配置表）
            CREATE TABLE IF NOT EXISTS agent_prompt_map (
                agent_name    TEXT NOT NULL,           -- interview/storybuilding/detail-outline/writing
                prompt_name   TEXT NOT NULL,           -- 对应 prompts 表的 name
                role          TEXT NOT NULL DEFAULT 'primary',  -- primary / evaluation
                PRIMARY KEY (agent_name, prompt_name)
            );

            -- ── 去_DB 重构：harness_snapshots / version_changes 表已废弃 ──
            -- 版本管理迁移到 registry.json（独立 git 仓库内）。
            -- 幂等清理：升级时自动 DROP 旧表（数据已迁移到 registry.json）。
            DROP TABLE IF EXISTS version_changes;
            DROP TABLE IF EXISTS harness_snapshots;

            -- Phase 8 adapt（AEGIS 进化循环，决策 E3a）：
            -- adapt_rounds 存历轮 landscape/scores/shipped edits，planner 查跨轮连续性。
            -- 一个 session = 一次 /api/adapt/start，含多轮（T=3-5）。
            CREATE TABLE IF NOT EXISTS adapt_rounds (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,           -- 一次 adapt 启动的 session（uuid）
                round           INTEGER NOT NULL,        -- 轮次（0-based）
                landscape       TEXT,                    -- 本轮 landscape（planner 产出）
                candidates_json TEXT,                    -- 候选摘要 JSON（edits+manifest，不含 config 全量）
                round_outcome   TEXT,                    -- shipped/rejected/idle
                shipped_version INTEGER,                 -- ship 了则指向 registry.json 的版本号
                baseline_version INTEGER,                -- 基线 config 版本（E6a）
                baseline_scores TEXT,                    -- JSON：基线 per-task 分数
                candidate_scores TEXT,                   -- JSON：候选 per-task 分数
                critic_verdict  TEXT,                    -- JSON：critic 判决
                created_at      TEXT NOT NULL,
                UNIQUE(session_id, round)
            );
            CREATE INDEX IF NOT EXISTS idx_ar_session ON adapt_rounds(session_id);

            -- evolve_sessions：进化流水线 session（驱动器模式，D16）。
            -- baseline_trace 现为输入（历史 trace 池，D4）；新字段 phase + 文档路径。
            CREATE TABLE IF NOT EXISTS evolve_sessions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id         TEXT NOT NULL,           -- uuid
                case_id            TEXT NOT NULL,           -- evalset case 标识
                status             TEXT NOT NULL,           -- running/done/failed
                phase              TEXT,                    -- 当前流水线阶段（D-guard 6 阶段）
                baseline_trace     TEXT,                    -- baseline trace_id（输入，历史 trace 池）
                candidate_trace    TEXT,                    -- candidate trace_id（run_test 产）
                baseline_score     REAL,                    -- verifier 分数（overall 均值）
                candidate_score    REAL,                    -- verifier分数（overall 均值）
                eval_report_path   TEXT,                    -- baseline 评估诊断文档路径（D16）
                design_doc_path    TEXT,                    -- 方案设计文档路径
                change_log_path    TEXT,                    -- 执行改动记录路径
                candidate_eval_path TEXT,                   -- candidate 评估诊断文档路径
                report_json        TEXT,                    -- 对比报告 JSON
                created_at         TEXT NOT NULL,
                updated_at         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_es_session ON evolve_sessions(session_id);
            CREATE INDEX IF NOT EXISTS idx_es_case ON evolve_sessions(case_id);

            -- manual_tests：手动单次测试记录（决策 D3/D5/D-Q7）
            -- 一次手动测试 = 选数据集 + 选 Agent 版本 → 跑一次 → 一条 trace。
            -- trace_id 为引用（trace 是唯一真源），pending/running 或无 trace 失败时为 NULL。
            -- version_type: working / snapshot；version_id: 快照 version 号，working 时 NULL。
            -- retry_of: 重试指向原失败 test_id（首发为 NULL，决策 D11）。
            CREATE TABLE IF NOT EXISTS manual_tests (
                test_id        TEXT PRIMARY KEY,            -- uuid
                case_id        TEXT NOT NULL,               -- evalset case 标识
                version_type   TEXT NOT NULL,               -- working / snapshot
                version_id     INTEGER,                     -- 快照 version 号；working 时 NULL
                trace_id       TEXT,                        -- 关联 trace id；pending/running 时 NULL
                task_id        TEXT,                        -- executor /internal/ab/run 轮询句柄
                status         TEXT NOT NULL,               -- pending / running / done / failed
                error          TEXT,                        -- 失败摘要；非 failed 时 NULL
                retry_of       TEXT,                        -- 重试指向原 test_id；首发 NULL
                created_at     TEXT NOT NULL                -- 创建时间（ISO8601）
            );
            CREATE INDEX IF NOT EXISTS idx_mt_status ON manual_tests(status);
            CREATE INDEX IF NOT EXISTS idx_mt_created ON manual_tests(created_at);

            -- evaluation_sessions：评估 Agent 产出的评估报告（决策 S4/T6）。
            -- 评估从进化流水线抽离为独立顶层 Agent（T1-T11/S1）。
            -- 一条评估 = 评估一条 trace 的流程+内容两大维度，产出诊断报告。
            -- trace_id 是贯穿三功能（测试→评估→进化）的公共外键。
            -- agent_version_*：冷存被评估 trace 对应的 Agent 版本（从 manual_tests 反查，
            --   冷存一份避免每次 JOIN，加速进化入口「选已评估 trace」列表查询）。
            CREATE TABLE IF NOT EXISTS evaluation_sessions (
                eval_id            TEXT PRIMARY KEY,         -- 评估 session id
                trace_id           TEXT NOT NULL,            -- 被评估的 trace
                agent_version_type TEXT,                     -- 'working' | 'snapshot'
                agent_version_id   INTEGER,                  -- snapshot 版本号；working 时 NULL
                status             TEXT NOT NULL DEFAULT 'running',  -- running|done|failed
                scores_json        TEXT,                     -- 内容层评分 + 流程硬指标（JSON）
                findings_json      TEXT,                     -- 问题清单数组（每条含 dimension/severity/evidence_type/finding/evidence）
                report_md          TEXT,                     -- 可读报告全文（内联）
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_eval_trace ON evaluation_sessions(trace_id);

            -- ── 数据闭环（设计 20260706）：分层数据集 + promote 闸门 + benchmark 矩阵 + 反思库 ──

            -- dataset_meta：评估集 case 元数据（分层 golden/growing + 版本化）。
            -- demand.md 内容仍是文件真源；本表只存"文件无法表达"的元数据（决策 A1/A4）。
            -- layer=golden 的 case 锁定在某 demand_revision（git commit hash），改内容=新 revision。
            CREATE TABLE IF NOT EXISTS dataset_meta (
                case_id          TEXT PRIMARY KEY,         -- 与目录名一致（如 case-001）
                layer            TEXT NOT NULL,            -- golden | growing
                source_trace_id  TEXT,                     -- 来自哪条生产 trace（growing 才有）
                demand_revision  TEXT,                     -- demand.md 内容的 git commit hash（golden 锁定用）
                promoted_at      TEXT,                     -- 入 growing / 升级 golden 的时间
                created_by       TEXT NOT NULL DEFAULT 'manual',  -- manual | annotator | maintainer
                status           TEXT NOT NULL DEFAULT 'active',  -- active | archived
                updated_at       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dm_layer ON dataset_meta(layer);

            -- promote_tasks：生产 trace → 数据集的标注任务（决策 A2，promote 闸门）。
            -- judge_scheduler 后台扫描未 judge 的生产 trace → 调 eval_agent/scoring → 写本表。
            -- 标注者在 UI 上决策（收/丢 + 归类），accept 则入 growing。
            CREATE TABLE IF NOT EXISTS promote_tasks (
                task_id        TEXT PRIMARY KEY,           -- uuid
                trace_id       TEXT NOT NULL,              -- 待标注的生产 trace
                owner_user_id  TEXT,                       -- trace 的用户来源（从 runs 冷存）
                status         TEXT NOT NULL DEFAULT 'pending',  -- pending|judging|needs_confirm|annotated|rejected|promoted
                judge_scores   TEXT,                       -- LLM-judge 打分 JSON（自动填）
                judge_verdict  TEXT,                       -- auto_promote | needs_human | auto_reject
                annotator      TEXT,                       -- 标注者（人工填）
                decision       TEXT,                       -- accept | reject（人工填）
                target_case_id TEXT,                       -- 归入哪个已有 case（accept 时填）
                new_case_title TEXT,                       -- 新建 case 的标题（accept 新建时填）
                created_at     TEXT NOT NULL,
                decided_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pt_status ON promote_tasks(status);

            -- benchmark_runs：case × 版本 × 评估 矩阵（决策 A3/D13，跨版本 leaderboard）。
            -- benchmark runner 手动触发后，对 golden 全 case × 指定版本跑测试 + 评估 → 写本表。
            -- golden_revision 相同的行之间分数可比（D8 重跑历史保证可比性）。
            CREATE TABLE IF NOT EXISTS benchmark_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id        TEXT NOT NULL,             -- 一次触发（发版/升级）= 一个 batch（uuid）
                case_id         TEXT NOT NULL,
                harness_version INTEGER NOT NULL,          -- 版本号（对应 registry.json）
                golden_revision TEXT NOT NULL,             -- 跑在哪个 golden revision 上
                trace_id        TEXT,                      -- 跑出来的 trace（NULL=未完成/失败）
                eval_id         TEXT,                      -- 关联评估 session（NULL=未评估）
                scores_json     TEXT,                      -- 评估分数快照（JSON）
                status          TEXT NOT NULL DEFAULT 'pending',  -- pending|running|evaluating|done|failed
                retries         INTEGER DEFAULT 0,
                error           TEXT,
                ran_at          TEXT NOT NULL,             -- 批次触发时间
                finished_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_br_batch ON benchmark_runs(batch_id);
            CREATE INDEX IF NOT EXISTS idx_br_version ON benchmark_runs(harness_version);
            CREATE INDEX IF NOT EXISTS idx_br_golden_rev ON benchmark_runs(golden_revision);

            -- reflection_library：失败 trace 自动归纳的反思库（决策 A8/D19，Reflexion/ExpeL 式）。
            -- eval_agent 完成后若 badcase → 归纳失败模式 → 写本表。
            -- 进化 Agent 启动时按评估问题分类查询，注入上下文。
            CREATE TABLE IF NOT EXISTS reflection_library (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category      TEXT NOT NULL,               -- 节奏|人物|AI味|套路|...
                pattern       TEXT NOT NULL,               -- 失败模式描述
                symptom       TEXT,                        -- 识别特征（如何发现）
                suggestion    TEXT,                        -- 改进建议
                source_traces TEXT,                        -- 来源 trace id 列表 JSON
                hit_count     INTEGER DEFAULT 0,           -- 被进化引用次数
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_rl_category ON reflection_library(category);

            -- llm_configs：大模型 API 配置（多配置管理，2026-07-08）。
            -- 可保存多个配置（deepseek/glm/openai 各一条），其中 is_active=1 的唯一一条
            -- 被 runtime 读取（llm.py judge + model_factory.py agent）。api_key AES-256-GCM 加密。
            -- 桌面端配置页 CRUD，测试连通性时按 id 读库解密。
            -- scope 分家（2026-07-18）：'evolution'=进化 Agent 评估用 / 'executor'=executor 写作用，
            -- 两个 scope 各自维护独立的 is_active=1 激活项。
            CREATE TABLE IF NOT EXISTS llm_configs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,                   -- 配置名（用户起，如 "deepseek-主力"）
                api_key_enc TEXT,                             -- AES-256-GCM 加密（nonce||ciphertext+tag, urlsafe-b64）；空=待填
                base_url    TEXT NOT NULL,                    -- 如 https://api.deepseek.com
                model       TEXT NOT NULL,                    -- 如 deepseek-chat
                is_active    INTEGER NOT NULL DEFAULT 0,      -- 1=当前激活（scope 内唯一，事务保证）
                scope       TEXT NOT NULL DEFAULT 'evolution', -- evolution=评估 / executor=写作
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            -- ⚠️ 索引不在 executescript 里建：CREATE TABLE IF NOT EXISTS 不会给存量库补
            -- scope 列，若此处建 ON(scope,is_active) 会因列不存在而崩，进而连累整个
            -- executescript 让服务起不来（2026-07-18 启动崩溃根因）。索引统一由
            -- _migrate_llm_configs_scope 幂等管理（确保 scope 列已存在后再建）。

            -- user_cache：executor 用户列表的本地缓存（trace 历史观测功能）。
            -- evolution 不维护用户主数据，定时从 executor /internal/users 拉取，
            -- 供 trace 历史列表把 owner_user_id 映射成可读 username。
            CREATE TABLE IF NOT EXISTS user_cache (
                user_id     TEXT PRIMARY KEY,                 -- executor users.user_id
                username    TEXT NOT NULL,                    -- 可读用户名
                disabled    INTEGER NOT NULL DEFAULT 0,       -- 1=executor 侧已禁用
                synced_at   TEXT NOT NULL                     -- 最近同步时间（ISO8601）
            );

            -- evolve_messages：进化对话消息（对话式共创工作台，决策 T6）。
            -- 一个 session 的全部对话消息（user/assistant/system/tool），按 seq 排序。
            -- role=user 为用户输入；role=assistant 为 Agent 回复（含 markdown + 内嵌引用）。
            -- tool_events 存该消息触发的工具调用摘要（assistant 消息专属）。
            -- related_points 存该消息涉及的进化点 id 列表（用于浮窗↔对话双向高亮联动）。
            CREATE TABLE IF NOT EXISTS evolve_messages (
                id              TEXT PRIMARY KEY,             -- uuid
                session_id      TEXT NOT NULL,                -- FK evolve_sessions（逻辑外键）
                role            TEXT NOT NULL,                -- user / assistant / system / tool
                content         TEXT NOT NULL,                -- 消息正文（markdown）
                tool_events     TEXT,                         -- JSON：工具调用列表（assistant 专属）
                related_points  TEXT,                         -- JSON：涉及的进化点 id 列表（联动高亮）
                seq             INTEGER NOT NULL,             -- 会话内序号（从 1 递增）
                created_at      TEXT NOT NULL,
                UNIQUE(session_id, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_em_session ON evolve_messages(session_id, seq);

            -- evolve_points：进化点（对话式共创工作台，决策 T7）。
            -- Agent 在 conversing 阶段通过工具调用 propose/update/reject 进化点，
            -- 用户在对话中拍板每个点的方案。status 三态：proposed/accepted/rejected。
            -- 拍板（finalize）后从 accepted 进化点生成 design_doc.md（决策 T3）。
            CREATE TABLE IF NOT EXISTS evolve_points (
                id              TEXT PRIMARY KEY,             -- uuid，Agent 调 propose 时生成
                session_id      TEXT NOT NULL,                -- FK evolve_sessions
                seq             INTEGER NOT NULL,             -- 会话内序号（浮窗排序）
                target          TEXT NOT NULL,                -- 要改的要素（meta_system.md / RetryMiddleware 等）
                problem         TEXT NOT NULL,                -- 为什么改（含 finding 引用）
                options         TEXT NOT NULL,                -- JSON：[{description, pros, cons, expected_impact}, ...]
                recommendation  TEXT,                         -- 推荐哪个 option + 理由
                note            TEXT,                         -- Agent 补充说明
                status          TEXT NOT NULL DEFAULT 'proposed',  -- proposed / accepted / rejected
                chosen_option   INTEGER,                      -- 用户选了第几个 option（accepted 时，0-based）
                user_note       TEXT,                         -- 用户附加说明
                accepted_at     TEXT,                         -- accept/reject 时间
                design_ref      INTEGER,                      -- 拍板后映射到 design_doc 的 change 序号
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ep_session ON evolve_points(session_id, seq);
            CREATE INDEX IF NOT EXISTS idx_ep_status ON evolve_points(session_id, status);

            -- evidence_dossiers：证据卷宗（Evidence Dossier，2026-07，原名 evidence_packs）。
            -- 一条 trace × 一个编译规则版本 = 一行；同 trace 可有多版本（追加，不覆盖）。
            -- 证据卷宗是评估 Agent 与进化 Agent 的共享事实底座，替代各自直读 trace。
            -- 四层结构：manifest（清单）/facts（事实）/semantic（语义归纳）/index（回钻索引）。
            -- 另存 eval_view/evolve_view 两个角色工作页投影（从同一事实底座投影，不携带独立事实）。
            -- provenance：trace_time=运行时产物修订（保真最高）/compile_time_snapshot=编译时快照（历史 trace 降级）。
            -- status 状态机：pending|compiling|ready|partial|failed|superseded。
            -- 2026-07-27 重命名 evidence_packs → evidence_dossiers（统一"证据卷宗"术语）。
            CREATE TABLE IF NOT EXISTS evidence_dossiers (
                pack_id             TEXT PRIMARY KEY,           -- uuid（DB 列名沿用 pack_id，代码层 alias 为 dossier_id）
                trace_id            TEXT NOT NULL,              -- FK runs（逻辑外键）
                owner_user_id       TEXT NOT NULL,              -- 继承自 runs，权限边界
                version             INTEGER NOT NULL,           -- 同 trace 的版本号（1,2,3...）
                is_current          INTEGER NOT NULL DEFAULT 0, -- 1=当前推荐版本
                status              TEXT NOT NULL DEFAULT 'pending',  -- pending|compiling|ready|partial|failed|superseded
                provenance          TEXT NOT NULL,              -- trace_time|compile_time_snapshot
                compile_rule_version TEXT NOT NULL,             -- 编译规则版本（用于判断是否需重编译）
                manifest_json       TEXT,                       -- 清单层：契约/版本/完整度/适用维度
                facts_json          TEXT,                       -- 事实层：阶段/委派/产物/错误/review链/指标（客观）
                semantic_json       TEXT,                       -- 语义层：阶段摘要/对齐/重点候选（LLM产出，必引证据）
                index_json          TEXT,                       -- 索引层：可回钻 node/segment/artifact ID
                eval_view_json      TEXT,                       -- 评估工作页投影
                evolve_view_json    TEXT,                       -- 进化工作页投影
                failure_reason      TEXT,                       -- failed/partial 时的原因
                llm_calls_used      INTEGER NOT NULL DEFAULT 0, -- 编译消耗的 LLM 调用数（成本控制）
                created_at          TEXT NOT NULL,
                finished_at         TEXT,                       -- 编译完成/失败时间
                UNIQUE(trace_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_edo_trace ON evidence_dossiers(trace_id);
            CREATE INDEX IF NOT EXISTS idx_edo_current ON evidence_dossiers(trace_id, is_current);
            CREATE INDEX IF NOT EXISTS idx_edo_status ON evidence_dossiers(status);

            -- evaluation_dossiers：评估卷宗（2026-07-27，需求：进化证据分级可见性重构）。
            -- 一次评估的不可变交付物，包含评估结论及本次实际引用的冻结证据片段。
            -- 是进化 Agent 唯一可见的 trace 派生输入（评估完成时原子封存）。
            -- 一个评估尝试（evaluation_sessions.eval_id）最多产出一份评估卷宗（UNIQUE 约束）。
            -- 永久绑定启动时的证据卷宗版本（source_dossier_id + source_dossier_version 不可变）。
            CREATE TABLE IF NOT EXISTS evaluation_dossiers (
                dossier_id          TEXT PRIMARY KEY,           -- 评估卷宗 id（uuid）
                eval_attempt_id     TEXT NOT NULL,              -- 关联评估尝试（evaluation_sessions.eval_id，逻辑外键）
                source_dossier_id   TEXT NOT NULL,              -- 绑定的证据卷宗 id（不可变）
                source_dossier_version INTEGER NOT NULL,        -- 绑定的证据卷宗版本（不可变）
                trace_id            TEXT NOT NULL,              -- 冗余便于查询（逻辑 FK runs）
                owner_user_id       TEXT NOT NULL,              -- 继承自 runs
                conclusions_json    TEXT,                       -- 各维度结论 + 引用的冻结证据片段
                findings_json       TEXT,                       -- 问题 finding（每条带 evidence_ref）
                positive_patterns_json TEXT,                    -- 正向可复用模式（每条带 evidence_ref）
                scores_json         TEXT,                       -- 评分（沿用 evaluation_sessions.scores_json 结构）
                report_md           TEXT,                       -- 可读报告
                frozen_evidence_json TEXT,                      -- 本次评估实际引用的冻结证据片段（{evidence_id: 片段}，供进化归因，需求 §22）
                completeness_status TEXT NOT NULL,              -- complete / incomplete（契约覆盖判定）
                seal_status         TEXT NOT NULL DEFAULT 'sealed',  -- sealed（唯一终态；封存失败则不入此表）
                created_at          TEXT NOT NULL,
                UNIQUE(eval_attempt_id)                         -- 一个尝试最多一份卷宗
            );
            CREATE INDEX IF NOT EXISTS idx_evdo_source ON evaluation_dossiers(source_dossier_id);
            CREATE INDEX IF NOT EXISTS idx_evdo_trace ON evaluation_dossiers(trace_id);

            -- ════════════════════════════════════════════════════════════════
            -- 问题知识库（一期：历史问题—计划—结果轨迹，需求 20260731_135839）
            -- 双层模型：不可变问题实例账本（problem_instances）+ 可治理标准问题库（standard_problems）
            -- 权威存储在本库 SQLite；语义向量/FTS 仅作派生索引，不是事实真源（REQ-09/DEC-11）。
            -- ════════════════════════════════════════════════════════════════

            -- problem_instances：不可变问题实例账本（REQ-01.1/DEC-01）。
            -- 每条来自一份 sealed 评估卷宗的一个 finding，保留来源证据/评估/时间/上下文，
            -- 归并操作不得覆盖或删除该事实。UNIQUE(dossier_id, finding_id) 防重复收录（AC-42）。
            CREATE TABLE IF NOT EXISTS problem_instances (
                instance_id         TEXT PRIMARY KEY,           -- 问题实例 id（uuid）
                dossier_id          TEXT NOT NULL,              -- FK evaluation_dossiers（来源评估卷宗）
                trace_id            TEXT NOT NULL,              -- FK runs（冗余便于频率统计与筛选）
                finding_id          TEXT NOT NULL,              -- 评估卷宗内的 finding id（f01/f02…）
                severity            TEXT NOT NULL,              -- high/medium/low（来自 finding，归并不改写）
                dimension           TEXT NOT NULL DEFAULT '未分类',  -- 评估维度（协作拓扑/错误保障/资源消耗/内容质量）
                statement           TEXT NOT NULL,              -- 问题陈述快照（来自 finding.finding，不可变）
                frozen_evidence_ref TEXT,                       -- JSON：证据引用快照（finding.evidence_ref/evidence），受控回钻边界
                classification_json TEXT,                       -- JSON：多轴分类 {location:{agent,component,stage}, affected_mechanism, failure_nature, task_scenario}，未知值统一"未分类"（REQ-02/AC-32）
                raw_description     TEXT,                       -- 原始问题描述（词表无适用类别时保留，REQ-02.4）
                created_at          TEXT NOT NULL,
                UNIQUE(dossier_id, finding_id)                  -- 一个卷宗内同一 finding 只收录一次（幂等，AC-42）
            );
            CREATE INDEX IF NOT EXISTS idx_pinst_dossier ON problem_instances(dossier_id);
            CREATE INDEX IF NOT EXISTS idx_pinst_trace ON problem_instances(trace_id);
            CREATE INDEX IF NOT EXISTS idx_pinst_severity ON problem_instances(severity);

            -- standard_problems：可治理标准问题库（REQ-01.4/DEC-01）。
            -- 聚合已确认问题实例，承载生命周期、正式频率与历史方案。只有用户确认后才形成（DEC-28）。
            CREATE TABLE IF NOT EXISTS standard_problems (
                problem_id          TEXT PRIMARY KEY,           -- 标准问题 id（uuid）
                title               TEXT NOT NULL,              -- 标准问题标题
                description         TEXT,                       -- 标准问题描述
                classification_json TEXT,                       -- JSON：多轴分类（聚合，REQ-02）
                severity            TEXT NOT NULL DEFAULT '未分类',  -- 聚合严重度高水位
                lifecycle_status    TEXT NOT NULL DEFAULT '开放',  -- 开放/观察中/已控制/已过时（REQ-08/DEC-07）
                formal_frequency    INTEGER NOT NULL DEFAULT 0, -- 正式频率：已确认关联的独立 trace 数（REQ-01.5/DEC-09）
                suspect_count       INTEGER NOT NULL DEFAULT 0, -- 疑似出现数：待确认候选计数（与正式频率分开，REQ-01.5）
                retrieval_count     INTEGER NOT NULL DEFAULT 0, -- 被检索命中次数（利用率统计，REQ-01.5）
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sp_status ON standard_problems(lifecycle_status);
            CREATE INDEX IF NOT EXISTS idx_sp_severity ON standard_problems(severity);

            -- problem_merge_candidates：候选归并（REQ-03/DEC-05）。
            -- Agent 判断某实例可能属于某标准问题但未确认；或没有相似历史时提出新标准问题候选。
            -- 只计入疑似出现，不污染正式频率（REQ-03.5）。非阻塞——失败不丢实例事实（AC-14）。
            CREATE TABLE IF NOT EXISTS problem_merge_candidates (
                candidate_id            TEXT PRIMARY KEY,       -- 候选 id（uuid）
                instance_id             TEXT NOT NULL,          -- FK problem_instances
                target_problem_id       TEXT,                   -- 目标标准问题（归并已有候选时填；新标准问题候选为 NULL）
                is_new_problem_proposal INTEGER NOT NULL DEFAULT 0,  -- 1=新标准问题候选（待确认，REQ-03.2/AC-41）
                match_method            TEXT,                   -- 匹配方法（如 rrf_hybrid/structural_fts）
                match_model_version     TEXT,                   -- 匹配模型/规则版本（AC-45 可追溯）
                confidence              REAL,                    -- 置信度 0..1
                match_evidence          TEXT,                   -- 匹配依据文本（命中的轴/关键词/分数）
                status                  TEXT NOT NULL DEFAULT 'pending',  -- pending/confirmed/rejected/superseded
                decided_by              TEXT,                    -- 治理操作者（确认/否决时填）
                decided_at              TEXT,                    -- 治理时间
                created_at              TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES problem_instances(instance_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cand_status ON problem_merge_candidates(status);
            CREATE INDEX IF NOT EXISTS idx_cand_target ON problem_merge_candidates(target_problem_id);
            CREATE INDEX IF NOT EXISTS idx_cand_instance ON problem_merge_candidates(instance_id);

            -- problem_instance_links：已确认归并关系（REQ-01.4）。
            -- 与候选分离——确认后才写入此表并计入正式频率。UNIQUE(instance_id) 一个实例最多归一个标准问题。
            CREATE TABLE IF NOT EXISTS problem_instance_links (
                link_id         TEXT PRIMARY KEY,               -- 链接 id（uuid）
                instance_id     TEXT NOT NULL UNIQUE,           -- FK problem_instances（一个实例最多一个标准问题）
                problem_id      TEXT NOT NULL,                  -- FK standard_problems
                confirmed_by    TEXT NOT NULL,                  -- 确认操作者
                confirmed_at    TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES problem_instances(instance_id),
                FOREIGN KEY (problem_id) REFERENCES standard_problems(problem_id)
            );
            CREATE INDEX IF NOT EXISTS idx_link_problem ON problem_instance_links(problem_id);

            -- evolution_point_ownership：进化点一对一归属（REQ-01.3/DEC-20/AC-33）。
            -- 一个进化点必须且只能归属于一个提出它的目标问题。point_id 即 evolve_points.id（PK 强制一对一）。
            -- 表达"为什么提出该计划"，与是否采纳无关（accept/reject 不动此表）。
            CREATE TABLE IF NOT EXISTS evolution_point_ownership (
                point_id            TEXT PRIMARY KEY,           -- FK evolve_points.id（PK 即 UNIQUE，强制一对一归属）
                problem_id          TEXT,                       -- 目标标准问题（归属解析不到时为 NULL，可后续治理补录）
                source_instance_id  TEXT,                       -- 触发该点的 finding 对应问题实例（可空）
                created_at          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_epo_problem ON evolution_point_ownership(problem_id);

            -- current_problem_cards：当前问题卡冻结快照（REQ-04.8/DEC-15/AC-27）。
            -- 历史检索前冻结结构化当前问题分析；后续历史比较只追加不覆盖此快照。
            CREATE TABLE IF NOT EXISTS current_problem_cards (
                card_id             TEXT PRIMARY KEY,           -- 卡 id（uuid）
                session_id          TEXT NOT NULL,              -- FK evolve_sessions
                instance_id         TEXT,                       -- 关联问题实例（本地分析时可能为空）
                problem_group       TEXT NOT NULL,              -- 当前问题组 id（同根因/同机制归组，REQ-04.4/AC-28）
                frozen_snapshot_json TEXT NOT NULL,             -- JSON：问题陈述/直接证据/症状/Agent组件阶段/任务场景/影响严重度/根因假设及置信度/替代解释/未知项（不可变）
                retrieval_state     TEXT NOT NULL DEFAULT 'frozen',  -- frozen/retrieved/degraded（检索后追加状态）
                created_at          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_card_session ON current_problem_cards(session_id);
            CREATE INDEX IF NOT EXISTS idx_card_group ON current_problem_cards(problem_group);
            """
        )
        conn.commit()

        # Phase 7 幂等迁移：DROP 废弃的 surface_versions/harness_manifests（D10=b1）
        _drop_legacy_harness_tables(conn)
        # DROP 已移除功能（rules/experiments/prompts管理/judge）的孤儿表
        _drop_orphan_diagnosis_tables(conn)

        # Phase 3 幂等迁移：给 runs 表补 owner_user_id 列（D2/D16 按用户隔离）
        _migrate_runs_owner_user_id(conn)
        # HITL 幂等迁移：给 runs 表补 ingested_seq 列（D7 增量高水位）
        _migrate_runs_ingested_seq(conn)
        # 进化端自观测：给 runs 表补 run_purpose 列（区分 executor/evolution trace）
        _migrate_runs_run_purpose(conn)
        # trace 稳定性重构：runs 表补心跳 + interrupted_reason 列（设计 20260720_203000）
        _migrate_runs_heartbeat(conn)
        # trace 稳定性重构：session 三表补 self_trace_id 列（设计 20260720_203000）
        _migrate_self_trace_id_columns(conn)
        # Phase 4 幂等迁移：prompt 版本管理表（T9 langfuse 式）
        _migrate_prompt_tables(conn)
        # 驱动器模式幂等迁移：evolve_sessions 补 phase + 文档路径列（D16）
        _migrate_evolve_sessions_driver_fields(conn)
        # 三功能解耦：evolve_sessions 补 eval_ref 列（关联评估报告，决策 S6/T2）
        _migrate_evolve_sessions_eval_ref(conn)
        # 数据闭环：manual_tests 补 origin_layer 列（golden|growing，进化区分验证/探索，决策 A6）
        _migrate_manual_tests_origin_layer(conn)
        _init_trace_v2_tables(conn)
        # Phase 1：初始化归因映射（幂等，仅空表时填充）
        _seed_agent_prompt_map(conn)
        # 多配置管理：llm_config（单数，单行）→ llm_configs（复数，多行 + is_active）
        _migrate_llm_configs_multi(conn)
        # scope 分家：llm_configs 加 scope 列 + 现有数据复制成双份（evolution + executor）
        _migrate_llm_configs_scope(conn)
        # user_cache 表由 executescript CREATE IF NOT EXISTS 直接建（新表无需 ALTER 迁移）

        # 评估尝试演化：evaluation_sessions 加列（bound_dossier / 资源消耗 / 失败原因 / 封存回填）
        _migrate_evaluation_sessions_attempt_fields(conn)
        # 进化输入绑定：evolve_sessions 加 bound_eval_dossier_id 列（永久绑定评估卷宗）
        _migrate_evolve_sessions_bound_eval_dossier(conn)
        # 评估卷宗冻结证据片段（阶段 D：供进化归因，需求 §22）
        _migrate_evaluation_dossiers_frozen_evidence(conn)
        # 终态对账查询索引（FR-004/EDGE-003：加速 reconcile/migrations 启动查询）
        _migrate_evaluation_sessions_mislabeled_index(conn)
        # 历史误标纠正审计表（FR-006/DEC-003/RSK-003：可审计可回滚）
        _migrate_eval_correction_audit_table(conn)
        # 问题知识库一期：6 张表靠 CREATE IF NOT EXISTS 自补；此处做幂等加列/索引演进（需求 20260731）
        _migrate_problem_kb_tables(conn)


def _migrate_evolve_sessions_driver_fields(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 evolve_sessions 表补驱动器模式新列（D16）。

    新字段：phase / eval_report_path / design_doc_path / change_log_path /
    candidate_eval_path。新库建表已含（executescript CREATE），存量库靠此 ALTER 补。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evolve_sessions)").fetchall()}
    new_cols = {
        "phase": "TEXT",
        "eval_report_path": "TEXT",
        "design_doc_path": "TEXT",
        "change_log_path": "TEXT",
        "candidate_eval_path": "TEXT",
    }
    missing = {c: t for c, t in new_cols.items() if c not in existing}
    if not missing:
        return
    with _lock:
        for col, coltype in missing.items():
            conn.execute(f"ALTER TABLE evolve_sessions ADD COLUMN {col} {coltype}")
        conn.commit()


def _migrate_evolve_sessions_eval_ref(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 evolve_sessions 表补 eval_ref 列（三功能解耦，决策 S6/T2）。

    eval_ref 关联 evaluation_sessions.eval_id——进化强前置（T2）需先有评估报告。
    新库建表未含此列（沿用 D16 schema），存量库靠此 ALTER 补。
    status 字段值域从 running/done/failed 扩展为 4 态（S6）：
      running / pending_review / published / discarded（沿用同一列，不改类型）。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evolve_sessions)").fetchall()}
    if "eval_ref" in existing:
        return
    with _lock:
        conn.execute("ALTER TABLE evolve_sessions ADD COLUMN eval_ref TEXT")
        conn.commit()


def _migrate_rename_evidence_packs_to_dossiers(conn: sqlite3.Connection) -> None:
    """幂等迁移：evidence_packs → evidence_dossiers（统一"证据卷宗"术语，2026-07-27）。

    存量库里有 evidence_packs 旧表 → RENAME 成 evidence_dossiers（SQLite 原生支持，
    保留全部数据 + 索引自动跟随）。新库由 executescript 直接建 evidence_dossiers，
    此函数检测新表已存在则跳过。

    必须在 init_db() 的 executescript 之前调用：否则 executescript 的
    CREATE TABLE IF NOT EXISTS evidence_dossiers 会先建空表，使 RENAME 落空。
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    has_old = "evidence_packs" in tables
    has_new = "evidence_dossiers" in tables
    if has_old and not has_new:
        with _lock:
            conn.execute("ALTER TABLE evidence_packs RENAME TO evidence_dossiers")
            # RENAME 后旧索引名仍指向新表（SQLite 自动跟随），重建为 idx_edo_* 命名更清晰。
            for old_idx, new_idx, ddl in (
                ("idx_epk_trace", "idx_edo_trace",
                 "CREATE INDEX IF NOT EXISTS idx_edo_trace ON evidence_dossiers(trace_id)"),
                ("idx_epk_current", "idx_edo_current",
                 "CREATE INDEX IF NOT EXISTS idx_edo_current ON evidence_dossiers(trace_id, is_current)"),
                ("idx_epk_status", "idx_edo_status",
                 "CREATE INDEX IF NOT EXISTS idx_edo_status ON evidence_dossiers(status)"),
            ):
                conn.execute(f"DROP INDEX IF EXISTS {old_idx}")
                conn.execute(ddl)
            conn.commit()
        logger.info("迁移：evidence_packs → evidence_dossiers（重命名完成）")


def _migrate_evaluation_sessions_attempt_fields(conn: sqlite3.Connection) -> None:
    """幂等迁移：evaluation_sessions 演变为「评估尝试」，补列（2026-07-27）。

    语义变更：evaluation_sessions 从「评估结果行」变为「评估尝试」（承载任务生命周期
    + 资源消耗）。评估产物（结论 + 引用证据）拆到独立的不可变 evaluation_dossiers 表。

    新列：
    - bound_dossier_id：启动时绑定的证据卷宗 id（不可变，阶段 C 评估按卷宗启动后回填）。
    - sealed_dossier_id：成功封存的评估卷宗 id（评估成功后回填，NULL=未成功封存）。
    - model_calls_used / tokens_used / runtime_ms：资源消耗（阶段 F 资源上限用）。
    - model_calls_limit / tokens_limit / runtime_limit：对应硬上限（可配置）。
    - failure_reason / stop_reason：失败/停止原因（需求 §41 尝试留痕）。

    status 值域沿用同一列：running/done/failed/cancelled → 后续阶段 C 扩展为
    queued/running/completed/failed/stopped/interrupted 六态（值域迁移随 C 阶段落地）。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evaluation_sessions)").fetchall()}
    new_cols = {
        "bound_dossier_id": "TEXT",
        "sealed_dossier_id": "TEXT",
        "model_calls_used": "INTEGER NOT NULL DEFAULT 0",
        "tokens_used": "INTEGER NOT NULL DEFAULT 0",
        "runtime_ms": "INTEGER NOT NULL DEFAULT 0",
        "model_calls_limit": "INTEGER",
        "tokens_limit": "INTEGER",
        "runtime_limit": "INTEGER",
        "failure_reason": "TEXT",
        "stop_reason": "TEXT",
    }
    missing = {c: t for c, t in new_cols.items() if c not in existing}
    if not missing:
        return
    with _lock:
        for col, coltype in missing.items():
            conn.execute(f"ALTER TABLE evaluation_sessions ADD COLUMN {col} {coltype}")
        conn.commit()


def _migrate_evolve_sessions_bound_eval_dossier(conn: sqlite3.Connection) -> None:
    """幂等迁移：evolve_sessions 补 bound_eval_dossier_id 列（2026-07-27）。

    进化会话永久绑定启动时选定的评估卷宗 id（需求 §42：禁止会话创建后切换输入，
    绝不按"最新评估"动态解析）。新进化只消费 evaluation_dossiers，不再按 trace_id 拼接。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evolve_sessions)").fetchall()}
    if "bound_eval_dossier_id" in existing:
        return
    with _lock:
        conn.execute("ALTER TABLE evolve_sessions ADD COLUMN bound_eval_dossier_id TEXT")
        conn.commit()


def _migrate_evaluation_dossiers_frozen_evidence(conn: sqlite3.Connection) -> None:
    """幂等迁移：evaluation_dossiers 补 frozen_evidence_json 列（阶段 D，2026-07-27）。

    评估卷宗封存时把本次评估实际引用的证据片段冻结进来（{evidence_id: 片段}），
    让进化 Agent 只读评估卷宗即可归因（需求 §22），无需回钻证据卷宗。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evaluation_dossiers)").fetchall()}
    if "frozen_evidence_json" in existing:
        return
    with _lock:
        conn.execute("ALTER TABLE evaluation_dossiers ADD COLUMN frozen_evidence_json TEXT")
        conn.commit()


def _migrate_evaluation_sessions_mislabeled_index(conn: sqlite3.Connection) -> None:
    """幂等迁移：evaluation_sessions 加部分索引，加速终态对账查询（FR-004/EDGE-003）。

    reconcile/migrations 查询「sealed_dossier_id 非空且 status != 'completed'」
    在启动 hot path 跑；无索引会全表扫描。部分索引只覆盖有 sealed 的行，体积小。
    CREATE INDEX IF NOT EXISTS 天然幂等。
    """
    with _lock:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_sealed_status "
            "ON evaluation_sessions(sealed_dossier_id, status) "
            "WHERE sealed_dossier_id IS NOT NULL AND sealed_dossier_id != ''"
        )
        conn.commit()


def _migrate_eval_correction_audit_table(conn: sqlite3.Connection) -> None:
    """幂等迁移：建 eval_correction_audit 表（FR-006 / DEC-003 可审计纠正）。

    历史误标评估一次性纠正必须保留可审计、可回滚证据（RSK-003）。本表逐条记录
    每次纠正的 eval_id / 前后状态 / sealed_dossier_id / 判据 / 快照 / 时间，
    审计写入与 UPDATE 同事务，保证「改了就有据」。
    """
    with _lock:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS eval_correction_audit (
                audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                eval_id       TEXT NOT NULL,
                status_before TEXT,
                status_after  TEXT NOT NULL,
                sealed_dossier_id TEXT,
                criterion     TEXT NOT NULL,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                corrected_at  TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_audit_eval ON eval_correction_audit(eval_id)"
        )
        conn.commit()


def _migrate_problem_kb_tables(conn: sqlite3.Connection) -> None:
    """幂等迁移：问题知识库一期表演进（需求 20260731_135839）。

    6 张核心表（problem_instances / standard_problems / problem_merge_candidates /
    problem_instance_links / evolution_point_ownership / current_problem_cards）
    靠 executescript 内 CREATE IF NOT EXISTS 自补，本函数仅承载未来的加列/索引演进。

    当前职责：确保向量/FTS 派生索引表存在（供 retrieval/store.py 使用）。
    派生索引不是事实真源，删除重建不影响权威数据（AC-24）。
    """
    with _lock:
        # FTS5 全文索引（标准问题召回）。tokenize=trigram 支持中文子串匹配。
        # 用 content='standard_problems' 外部内容表，避免数据双写；重建即可恢复。
        conn.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS standard_problems_fts
               USING fts5(
                   problem_id UNINDEXED,
                   title,
                   description,
                   statement,
                   tokenize='trigram'
               )"""
        )
        conn.commit()



def _migrate_runs_owner_user_id(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 runs 表补 owner_user_id 列（Phase 3 D2/D16）。

    存量数据无 user_id → DEFAULT 'unknown'（T7）。新数据由 importer 从
    run_start.input 提取写入。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "owner_user_id" in existing:
        return
    with _lock:
        conn.execute("ALTER TABLE runs ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT 'unknown'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner_user_id)")
        conn.commit()


def _migrate_runs_ingested_seq(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 runs 表补 ingested_seq 列（HITL D7 增量高水位）。

    存量数据默认 0（下次扫描会全量重拉校准）。新数据由 importer 摄入时写入。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "ingested_seq" in existing:
        return
    with _lock:
        conn.execute("ALTER TABLE runs ADD COLUMN ingested_seq INTEGER DEFAULT 0")
        conn.commit()


def _migrate_runs_run_purpose(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 runs 表补 run_purpose 列（进化端自观测迁移 D2）。

    区分 trace 来源：执行端写入（user_generation/optimization）vs 进化端自产
    （evolution_eval/evolution_evolve）。存量数据均为执行端摄入，回填
    user_generation（符合事实）。下游统计面板按 run_purpose 过滤，避免执行端
    与进化端 trace 串味。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "run_purpose" in existing:
        return
    with _lock:
        conn.execute("ALTER TABLE runs ADD COLUMN run_purpose TEXT NOT NULL DEFAULT 'user_generation'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_purpose ON runs(run_purpose)")
        conn.commit()


def _migrate_runs_heartbeat(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 runs 表补 last_heartbeat_at + interrupted_reason 列。

    trace 稳定性重构（设计 20260720_203000）：Pull 主导架构下 runs.status 成为
    唯一真相源，需要两个配套字段：

    - last_heartbeat_at：recorder.drain_loop 每次写事务合并刷新（心跳），后台
      _heartbeat_timeout_scanner 检测超过 10s 未刷新的 running trace 标 interrupted。
      存量数据 NULL（已结束的 trace 不需要心跳）。
    - interrupted_reason：进入 interrupted 状态的来源，便于排查：
      process_restart（进程重启时 recover_pending 标记）/
      heartbeat_timeout（后台扫描标记）/
      user_marked（用户在 UI 手动收敛）。仅 interrupted 状态下非 NULL。

    不加索引：_heartbeat_timeout_scanner 查 WHERE status='running'，running 行数
    极少（只有正在跑的 trace），全表扫描几毫秒，无需为低频查询加索引。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    missing = [c for c in ("last_heartbeat_at", "interrupted_reason") if c not in existing]
    if not missing:
        return
    with _lock:
        if "last_heartbeat_at" in missing:
            conn.execute("ALTER TABLE runs ADD COLUMN last_heartbeat_at TEXT")
        if "interrupted_reason" in missing:
            conn.execute("ALTER TABLE runs ADD COLUMN interrupted_reason TEXT")
        conn.commit()


def _migrate_self_trace_id_columns(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 evolve_sessions / evaluation_sessions / manual_tests 补 self_trace_id 列。

    trace 稳定性重构（设计 20260720_203000）：原 session_id → trace_id 映射纯内存
    （recorder._session_trace），进程重启即丢，导致 stop 端点无法收敛 trace 状态。
    持久化到 DB 后，trace 详情页停止按钮可通过 self_trace_id 反查活跃 session。

    语义说明（命名分裂 R5）：
    - self_trace_id：本次进化/评估/测试过程的**自观测录像** trace_id（recorder 生成）
    - evolve_sessions.baseline_trace / manual_tests.trace_id：被改进/被测的**对象** trace_id
      两者语义不同，不能混用。evolve_sessions.candidate_trace 也是"对象"语义（run_test 产）。

    存量数据 NULL（符合 D6：只保证新数据准确，不回填历史）。
    """
    table_columns = {
        "evolve_sessions": "self_trace_id",
        "evaluation_sessions": "self_trace_id",
        "manual_tests": "self_trace_id",
    }
    for table, col in table_columns.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col in existing:
            continue
        with _lock:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            conn.commit()


def _migrate_manual_tests_origin_layer(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 manual_tests 表补 origin_layer 列（数据闭环决策 A6）。

    origin_layer 标记本次测试跑在哪个数据集层上（golden | growing）。
    start_test 时从 dataset_meta.layer 推导写入；进化 Agent 据此区分
    验证（golden，不能退化）vs 探索（growing，找新方向）。
    存量数据回填 NULL（语义未知，不臆测）。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(manual_tests)").fetchall()}
    if "origin_layer" in existing:
        return
    with _lock:
        conn.execute("ALTER TABLE manual_tests ADD COLUMN origin_layer TEXT")
        conn.commit()


def _migrate_prompt_tables(conn: sqlite3.Connection) -> None:
    """幂等迁移：prompt 版本管理表（Phase 4 T9）。

    表由 init_db 的 executescript 创建（IF NOT EXISTS），这里只处理存量库的
    兜底：确认表存在。prompts/prompt_versions 是新表，旧库不会有，executescript
    已覆盖。此函数保留为占位，供未来字段演进扩展。
    """
    # prompts/prompt_versions 表已由 executescript 创建（IF NOT EXISTS）。
    # 此处无需额外操作，保留为扩展点。
    return


# Phase 1 T1.6：subagent → prompt 归因映射初始数据（已核实确切 prompt 名）
# 依据：executor/app/domains/writing/expert_agent/agents/*.py + evaluators/*.py 的 load_prompt
_AGENT_PROMPT_SEED = [
    ("interview", "interview_system", "primary"),
    ("storybuilding", "storybuilding_system", "primary"),
    ("storybuilding", "storybuilding_evaluation", "evaluation"),
    ("detail-outline", "detail_outline_system", "primary"),
    ("detail-outline", "detail_outline_evaluation", "evaluation"),
    ("writing", "writing_system", "primary"),
    ("writing", "writing_evaluation", "evaluation"),
]


def _seed_agent_prompt_map(conn: sqlite3.Connection) -> None:
    """初始化归因映射（幂等：仅表为空时填充，避免覆盖用户修改）。"""
    existing = conn.execute("SELECT count(*) AS c FROM agent_prompt_map").fetchone()
    if existing and existing[0] > 0:
        return
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn.executemany(
            """INSERT OR IGNORE INTO agent_prompt_map (agent_name, prompt_name, role)
               VALUES (?, ?, ?)""",
            [(a, p, r) for a, p, r in _AGENT_PROMPT_SEED],
        )
        conn.commit()


def _migrate_llm_configs_multi(conn: sqlite3.Connection) -> None:
    """幂等迁移：llm_config（单数，单行）→ llm_configs（复数，多行 + is_active）。

    多配置管理（2026-07-08）：从"全局唯一一行"升级为"可保存多个配置"。
    迁移逻辑（4 种情况）：
      1. 新表已有数据 → 已迁移过，return（幂等）
      2. 旧表存在且有数据 → 把 id=1 那行迁到新表（is_active=1），密文原样拷贝，
         然后 DROP 旧表
      3. 新表空 + 旧表空（或旧表不存在）→ 仅靠 CREATE TABLE IF NOT EXISTS 建新表，无需搬运
      4. 旧表不存在（全新库）→ 同 3

    注意：密文（api_key_enc）直接拷贝，无需解密再加密——加密格式未变，主密钥未变。
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    new_exists = "llm_configs" in tables
    old_exists = "llm_config" in tables

    # 情况 1：新表已有数据 → 已迁移
    if new_exists:
        cnt = conn.execute("SELECT count(*) FROM llm_configs").fetchone()[0]
        if cnt > 0:
            # 新表有数据，若旧表还在（异常残留）则清掉
            if old_exists:
                with _lock:
                    conn.execute("DROP TABLE IF EXISTS llm_config")
                    conn.commit()
            return

    # 走到这里：新表为空（可能刚 CREATE）。搬运旧表数据（若有）
    if old_exists:
        row = conn.execute(
            "SELECT name, api_key_enc, base_url, model, updated_at FROM llm_config WHERE id = 1"
        ).fetchone()
        if row:
            name = row[0] or "default"
            api_key_enc = row[1]  # 可能为 NULL（占位未填）
            base_url = row[2] or ""
            model = row[3] or ""
            updated_at = row[4] or datetime.now(UTC).isoformat()
            created_at = updated_at  # 旧表无 created_at，用 updated_at 兜底
            with _lock:
                conn.execute(
                    """INSERT INTO llm_configs
                       (id, name, api_key_enc, base_url, model, is_active, created_at, updated_at)
                       VALUES (1, ?, ?, ?, ?, 1, ?, ?)""",
                    (name, api_key_enc, base_url, model, created_at, updated_at),
                )
                conn.execute("DROP TABLE llm_config")
                conn.commit()
            logger.info("llm_config → llm_configs 迁移完成（搬运 1 行，is_active=1）。")
        else:
            # 旧表存在但空：直接 DROP
            with _lock:
                conn.execute("DROP TABLE llm_config")
                conn.commit()
    # 情况 3/4：新表空 + 旧表空/不存在 → 新表已由 CREATE TABLE IF NOT EXISTS 建好，无需动作


def _migrate_llm_configs_scope(conn: sqlite3.Connection) -> None:
    """幂等迁移：给 llm_configs 加 scope 列，并把现有数据复制成两份（D16 + T4）。

    背景：原本进化端 Agent（评估）与 executor（写作）共用同一份激活配置。
    分家后两个 scope 各自维护独立的激活配置：
      - scope='evolution'：进化 Agent 做 evaluate/evolve 时用
      - scope='executor'：executor 给用户写正文时用

    迁移分两阶段（都幂等，新旧库均安全）：
      阶段 A（仅存量库执行）：加 scope 列 + 复制现有行到 executor scope。
        - 新库 CREATE TABLE 已含 scope 列 → 阶段 A 跳过。
        - 存量库无 scope 列 → ALTER 加列（DEFAULT 'evolution'，现有行归 evolution），
          再把每行复制一份到 executor scope（副本 name 加"（执行端副本）"后缀，
          副本 is_active 与原行一致）。
      阶段 B（无条件执行）：DROP 旧单列索引 + 建复合 (scope, is_active) 索引。
        - 必须在阶段 A 之后，确保 scope 列已存在才建索引（否则 sqlite 报
          no such column，这正是 2026-07-18 启动崩溃的根因）。

    为什么索引不放进 init_db 的 executescript：CREATE TABLE IF NOT EXISTS 不改
    存量表结构，若 executescript 里建 ON(scope,is_active)，存量库会在 scope
    列尚未 ALTER 加上时就崩，且连累整段 executescript 中断，服务起不来。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(llm_configs)").fetchall()}

    # ── 阶段 A：加列 + 复制数据（仅存量库：scope 列缺失时执行）──
    if "scope" not in existing:
        with _lock:
            conn.execute("ALTER TABLE llm_configs ADD COLUMN scope TEXT NOT NULL DEFAULT 'evolution'")
            rows = conn.execute(
                "SELECT name, api_key_enc, base_url, model, is_active, created_at, updated_at "
                "FROM llm_configs"
            ).fetchall()
            for name, api_key_enc, base_url, model, is_active, created_at, updated_at in rows:
                conn.execute(
                    """INSERT INTO llm_configs
                       (name, api_key_enc, base_url, model, is_active, scope, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'executor', ?, ?)""",
                    (f"{name}（执行端副本）", api_key_enc, base_url, model, is_active, created_at, updated_at),
                )
            conn.commit()
        logger.info(
            "llm_configs 加 scope 列完成，现有 %d 行已复制到 executor scope（含后缀命名）。",
            len(rows),
        )

    # ── 阶段 B：索引重建（无条件幂等，确保 scope 列已在）──
    # 新库首次启动也走这里：executescript 不再建 llm_configs 索引，统一由此补齐。
    with _lock:
        conn.execute("DROP INDEX IF EXISTS idx_llm_configs_active")  # 旧单列索引（若存在）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_configs_scope_active ON llm_configs(scope, is_active)"
        )
        conn.commit()


def _drop_legacy_harness_tables(conn: sqlite3.Connection) -> None:
    """幂等迁移：DROP 废弃的 surface_versions + harness_manifests（Phase 7，D10=b1）。

    harness 定义从 DB 行变成 Agent 包目录（evolution/harnesses/current/）。
    surface 级版本管理被整包级快照（harness_snapshots）取代。

    幂等：DROP TABLE IF EXISTS 重复执行不报错。
    """
    existing = {row[1] for row in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    legacy = {"surface_versions", "harness_manifests"}
    if not (legacy & existing):
        return  # 已迁移过（两表都不存在）
    with _lock:
        for table in sorted(legacy & existing):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            logger.info("DROP 废弃表: %s（Phase 7 harness 包化重构）", table)
        conn.commit()


def _drop_orphan_diagnosis_tables(conn: sqlite3.Connection) -> None:
    """幂等迁移：DROP 已移除功能的孤儿表。

    rules / experiments / prompts 版本管理 / judge 评分链路已整体移除，
    其专属表（rules、trace_flags、trace_scores、judgment_runs、
    improvement_candidates、ab_experiments、replay_test_sets、judge_calibration、
    badcase_records、failure_signatures）成为孤儿，一并 DROP。

    注意：prompts / prompt_versions 表保留（agent_package 直查，新前端 /agent 页依赖）。
    幂等：DROP TABLE IF EXISTS 重复执行不报错。
    """
    orphan_tables = [
        "failure_signatures", "badcase_records",  # 先删有外键依赖倾向的
        "judge_calibration", "replay_test_sets", "ab_experiments",
        "improvement_candidates", "judgment_runs", "trace_scores",
        "trace_flags", "rules",
    ]
    with _lock:
        for table in orphan_tables:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()


def _init_trace_v2_tables(conn: sqlite3.Connection) -> None:
    """建立 Trace V2 的治理表，并以加法迁移保留历史三表。"""
    with _lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trace_receipts (
                trace_id TEXT PRIMARY KEY REFERENCES runs(trace_id) ON DELETE CASCADE,
                contiguous_seq INTEGER NOT NULL DEFAULT 0,
                max_seen_seq INTEGER NOT NULL DEFAULT 0,
                missing_ranges_json TEXT NOT NULL DEFAULT '[]',
                manifest_json TEXT,
                manifest_status TEXT NOT NULL DEFAULT 'missing',
                receipt_revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS integrity_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                conflict_key TEXT NOT NULL,
                existing_hash TEXT NOT NULL,
                received_hash TEXT NOT NULL,
                received_at TEXT NOT NULL,
                UNIQUE(trace_id, conflict_key, received_hash)
            );
            -- CON-010 / DEC-012 / AC-015：取消来源 ready 卷宗人工确认审计。
            -- 记录每一次"来源运行已取消的 ready 卷宗经人工确认进入下游"的操作，
            -- 保留操作者、确认时间、取消来源、目标下游，证明非自动调度。
            CREATE TABLE IF NOT EXISTS cancel_origin_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_trace_id TEXT NOT NULL,
                source_status TEXT NOT NULL,
                dossier_id TEXT NOT NULL,
                target_downstream TEXT NOT NULL,   -- evaluation | evolution
                submitted_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payload_objects (
                payload_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sensitivity TEXT NOT NULL,
                expires_at TEXT,
                storage_path TEXT NOT NULL,
                sealed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trace_payload_links (
                trace_id TEXT NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
                event_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                payload_id TEXT NOT NULL REFERENCES payload_objects(payload_id) ON DELETE CASCADE,
                PRIMARY KEY(trace_id, event_id, field_name)
            );
            CREATE INDEX IF NOT EXISTS idx_payload_expiry ON payload_objects(expires_at);
            CREATE TABLE IF NOT EXISTS access_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS access_audit_no_update
            BEFORE UPDATE ON access_audit
            BEGIN
                SELECT RAISE(ABORT, 'access_audit is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS access_audit_no_delete
            BEFORE DELETE ON access_audit
            BEGIN
                SELECT RAISE(ABORT, 'access_audit is append-only');
            END;
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                artifact_type TEXT NOT NULL,
                workspace_id TEXT,
                logical_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(workspace_id, artifact_type, logical_key)
            );
            CREATE TABLE IF NOT EXISTS artifact_revisions (
                artifact_revision_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                parent_revision_id TEXT,
                payload_id TEXT NOT NULL REFERENCES payload_objects(payload_id),
                content_hash TEXT NOT NULL,
                producer_trace_id TEXT,
                producer_event_id TEXT,
                harness_version TEXT,
                provenance TEXT NOT NULL DEFAULT 'trace_time',
                source_trace_id TEXT,
                support_event_ids_json TEXT NOT NULL DEFAULT '[]',
                support_payload_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lineage_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_type TEXT NOT NULL,
                from_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                to_type TEXT NOT NULL,
                to_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(from_type, from_id, relation, to_type, to_id)
            );
            CREATE TABLE IF NOT EXISTS consumption_rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                consumer_workload TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                integrity_status TEXT NOT NULL,
                missing_fields_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outcome_records (
                outcome_id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                outcome_type TEXT NOT NULL,
                payload_id TEXT,
                actor_user_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS score_records (
                score_id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                rubric_id TEXT NOT NULL,
                rubric_version TEXT NOT NULL,
                score_json TEXT NOT NULL,
                supersedes_score_id TEXT,
                actor_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS release_events_v2 (
                release_event_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL,
                status TEXT NOT NULL,
                candidate_id TEXT,
                actor_user_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiment_runs_v2 (
                experiment_id TEXT PRIMARY KEY,
                source_evaluation_dossier_id TEXT NOT NULL,
                baseline_revision_id TEXT,
                candidate_revision_id TEXT NOT NULL,
                status TEXT NOT NULL,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                formula_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TRIGGER IF NOT EXISTS outcome_records_no_update
            BEFORE UPDATE ON outcome_records BEGIN
                SELECT RAISE(ABORT, 'outcome_records is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS outcome_records_no_delete
            BEFORE DELETE ON outcome_records BEGIN
                SELECT RAISE(ABORT, 'outcome_records is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS score_records_no_update
            BEFORE UPDATE ON score_records BEGIN
                SELECT RAISE(ABORT, 'score_records is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS score_records_no_delete
            BEFORE DELETE ON score_records BEGIN
                SELECT RAISE(ABORT, 'score_records is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS release_events_v2_no_update
            BEFORE UPDATE ON release_events_v2 BEGIN
                SELECT RAISE(ABORT, 'release_events_v2 is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS release_events_v2_no_delete
            BEFORE DELETE ON release_events_v2 BEGIN
                SELECT RAISE(ABORT, 'release_events_v2 is append-only');
            END;
            """
        )
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(event_payloads)").fetchall()}
        if "event_id" not in event_columns:
            conn.execute("ALTER TABLE event_payloads ADD COLUMN event_id TEXT")
        if "event_hash" not in event_columns:
            conn.execute("ALTER TABLE event_payloads ADD COLUMN event_hash TEXT")
        if "payload_refs_json" not in event_columns:
            conn.execute("ALTER TABLE event_payloads ADD COLUMN payload_refs_json TEXT NOT NULL DEFAULT '{}'")
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        for column, ddl in (
            ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
            ("service", "TEXT"),
            ("workload", "TEXT"),
            ("integrity_status", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("evidence_status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("evidence_gaps_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("coverage_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("run_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("external_refs_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("links_json", "TEXT NOT NULL DEFAULT '[]'"),
            # 四维正交生命周期（DEC-008，CON-009 兼容扩展，旧数据默认值）：
            #   trace_phase —— recording/sealing/sealed/degraded；旧记录为 NULL。
            #   cancel_audit —— CancelAudit 的 JSON；未取消为 NULL。
            #   lifecycle_revision —— 单调递增，桌面据此拒绝旧快照覆盖；旧记录为 0。
            ("trace_phase", "TEXT"),
            ("cancel_audit", "TEXT"),
            ("lifecycle_revision", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in run_columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {ddl}")
        dossier_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(evidence_dossiers)").fetchall()
        }
        if "compile_trace_id" not in dossier_columns:
            conn.execute("ALTER TABLE evidence_dossiers ADD COLUMN compile_trace_id TEXT")
        payload_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(payload_objects)").fetchall()
        }
        if "deleted_at" not in payload_columns:
            conn.execute("ALTER TABLE payload_objects ADD COLUMN deleted_at TEXT")
        revision_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(artifact_revisions)").fetchall()
        }
        for column, ddl in (
            ("provenance", "TEXT NOT NULL DEFAULT 'trace_time'"),
            ("source_trace_id", "TEXT"),
            ("support_event_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("support_payload_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if column not in revision_columns:
                conn.execute(f"ALTER TABLE artifact_revisions ADD COLUMN {column} {ddl}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_event_trace_id ON event_payloads(trace_id, event_id) WHERE event_id IS NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_event_trace_sequence ON event_payloads(trace_id, sequence)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_workload_started ON runs(workload, started_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_integrity ON runs(integrity_status)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifact_revisions_source "
            "ON artifact_revisions(source_trace_id, provenance)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_trace_type ON event_payloads(trace_id, type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lineage_from ON lineage_edges(from_type, from_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lineage_to ON lineage_edges(to_type, to_id)")
        conn.commit()


def execute(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
    """执行单条写/读语句（线程安全）。"""
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


@contextmanager
def transaction():
    """给需要 receipt、事件和投影原子一致的写路径使用。"""
    conn = get_conn()
    with _lock:
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def executemany(sql: str, params_seq: list[tuple[Any, ...]]) -> sqlite3.Cursor:
    """批量执行（线程安全）。"""
    conn = get_conn()
    with _lock:
        cur = conn.executemany(sql, params_seq)
        conn.commit()
        return cur


def query_all(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
    """查询多行。"""
    conn = get_conn()
    with _lock:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
    """查询单行。"""
    rows = query_all(sql, params)
    return rows[0] if rows else None


# ════════════════════════════════════════════════════════════
#  LLM 配置访问层（桌面化改造，2026-07-07）
# ════════════════════════════════════════════════════════════

# master_key 进程内缓存（启动时加载一次，避免每次解密都解析）。
_master_key_cache: bytes | None = None


def get_master_key() -> bytes:
    """获取 evolution AES 主密钥（懒加载，进程内缓存）。

    从 settings.evolution_master_key 加载。首次调用时解析并缓存。
    未配置时抛 RuntimeError（启动检查应在 settings 层拦截）。
    """
    global _master_key_cache
    if _master_key_cache is not None:
        return _master_key_cache
    from app.core.security import load_master_key
    from app.core.settings import settings
    if not settings.evolution_master_key:
        raise RuntimeError(
            "evolution_master_key 未配置。请在 evolution/.env 设置 "
            "EVOLUTION_MASTER_KEY（生成：python -c \"import secrets; print(secrets.token_hex(32))\"）"
        )
    _master_key_cache = load_master_key(settings.evolution_master_key)
    return _master_key_cache


class LlmConfigsRepository:
    """LLM 配置访问层（多配置管理 + scope 分家，2026-07-18）。

    支持 save 多个配置（deepseek/glm/openai 等），api_key AES-256-GCM 加密存储。
    scope 维度（2026-07-18 分家）：
      - 'evolution'：进化 Agent 做 evaluate/evolve 时用（默认，向后兼容）
      - 'executor'：executor 给用户写正文时用
    每个 scope 各自维护一条 is_active=1 激活项（activate/自动激活均用事务保证 scope 内唯一）。

    消费方：
      - llm.py + model_factory.py → get_active('evolution')（评估侧，默认）
      - ingestion.py active-key → get_active('executor')（executor 写作侧）
      - config/api.py → list_all(scope)/get_active_safe(scope)/create(scope)/...
    """

    @staticmethod
    def list_all(scope: str = "evolution") -> list[dict[str, Any]]:
        """返回指定 scope 下所有配置（不回显 key 明文）。

        Args:
            scope: 'evolution'（默认）或 'executor'
        Returns:
            [{id, name, base_url, model, has_key, key_hint, is_active, scope, created_at, updated_at}, ...]
            key_hint 为 key 尾 4 位脱敏（供用户辨识），无 key 时为 None。
            按 is_active DESC, created_at ASC 排序（激活项置顶）。
        """
        rows = query_all(
            """SELECT id, name, api_key_enc, base_url, model, is_active, scope, created_at, updated_at
               FROM llm_configs
               WHERE scope = ?
               ORDER BY is_active DESC, created_at ASC""",
            (scope,),
        )
        return [_row_to_safe(r) for r in rows]

    @staticmethod
    def get_active(scope: str = "evolution") -> tuple[str, str, str] | None:
        """读取指定 scope 的激活配置（解密后的明文）。

        Args:
            scope: 'evolution'（默认）或 'executor'
        Returns:
            (api_key, base_url, model) 三元组；未配置（无激活行或 key 为空）返回 None。
        """
        row = query_one(
            "SELECT api_key_enc, base_url, model FROM llm_configs "
            "WHERE scope = ? AND is_active = 1 LIMIT 1",
            (scope,),
        )
        if not row or not row["api_key_enc"]:
            return None
        from app.core.security import decrypt_secret
        api_key = decrypt_secret(row["api_key_enc"], get_master_key())
        base_url = row["base_url"] or ""
        model = row["model"] or ""
        return api_key, base_url, model

    @staticmethod
    def get_active_safe(scope: str = "evolution") -> dict[str, Any]:
        """读取指定 scope 的激活配置（不回显 key，供桌面端 GET /config/llm 用）。

        Args:
            scope: 'evolution'（默认）或 'executor'
        Returns:
            {has_key, name, base_url, model, updated_at}；无激活配置时 has_key=False 兜底。
        """
        row = query_one(
            """SELECT name, api_key_enc, base_url, model, updated_at
               FROM llm_configs WHERE scope = ? AND is_active = 1 LIMIT 1""",
            (scope,),
        )
        if not row or not row["api_key_enc"]:
            return {"has_key": False, "name": None, "base_url": "", "model": "", "updated_at": None}
        return {
            "has_key": True,
            "name": row["name"] or "default",
            "base_url": row["base_url"] or "",
            "model": row["model"] or "",
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def get_decrypted(id: int) -> tuple[str, str, str] | None:
        """按 id 读取配置（解密明文），供测试连通性用。

        注意：按 id 操作，与 scope 无关（id 全局唯一）。测试逻辑正交于归属 scope。

        Returns:
            (api_key, base_url, model)；不存在或 key 为空返回 None。
        """
        row = query_one(
            "SELECT api_key_enc, base_url, model FROM llm_configs WHERE id = ?",
            (id,),
        )
        if not row or not row["api_key_enc"]:
            return None
        from app.core.security import decrypt_secret
        api_key = decrypt_secret(row["api_key_enc"], get_master_key())
        return api_key, row["base_url"] or "", row["model"] or ""

    @staticmethod
    def get_safe_by_id(id: int) -> dict[str, Any] | None:
        """按 id 读取配置安全视图（不回显 key 明文，含 scope）。

        供按 id 的端点（update/activate）回读完整项用——避免依赖 list_all(scope)，
        因为 update/activate 按 id 操作时调用方不一定知道 scope。
        Returns:
            安全视图 dict；不存在返回 None。
        """
        row = query_one(
            """SELECT id, name, api_key_enc, base_url, model, is_active, scope, created_at, updated_at
               FROM llm_configs WHERE id = ?""",
            (id,),
        )
        if not row:
            return None
        return _row_to_safe(row)

    @staticmethod
    def create(*, name: str, api_key: str, base_url: str, model: str, scope: str = "evolution") -> int:
        """新建配置（加密 key）。若该 scope 下为空则自动设为激活。

        Args:
            scope: 'evolution'（默认）或 'executor'
        Returns:
            新行 id。
        """
        from app.core.security import encrypt_secret
        encrypted = encrypt_secret(api_key, get_master_key())
        now = datetime.now(UTC).isoformat()
        conn = get_conn()
        with _lock:
            # 该 scope 是否首条 → 自动激活
            cnt = conn.execute(
                "SELECT count(*) FROM llm_configs WHERE scope = ?", (scope,)
            ).fetchone()[0]
            is_active = 1 if cnt == 0 else 0
            cur = conn.execute(
                """INSERT INTO llm_configs
                   (name, api_key_enc, base_url, model, is_active, scope, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, encrypted, base_url, model, is_active, scope, now, now),
            )
            conn.commit()
            return cur.lastrowid

    @staticmethod
    def update(
        id: int,
        *,
        name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> bool:
        """部分更新配置。api_key 为 None/空字符串表示不改 key。

        Returns:
            True 表示命中行已更新；False 表示 id 不存在。
        """
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if api_key:  # 非空才改 key
            from app.core.security import encrypt_secret
            sets.append("api_key_enc = ?")
            params.append(encrypt_secret(api_key, get_master_key()))
        if base_url is not None:
            sets.append("base_url = ?")
            params.append(base_url)
        if model is not None:
            sets.append("model = ?")
            params.append(model)
        if not sets:
            # 无字段可改，检查行是否存在
            row = query_one("SELECT id FROM llm_configs WHERE id = ?", (id,))
            return row is not None
        sets.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(id)
        conn = get_conn()
        with _lock:
            cur = conn.execute(
                f"UPDATE llm_configs SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def delete(id: int) -> bool:
        """删除配置。若删的是激活项且该 scope 下还有其它行 → 自动激活同 scope id 最小的一条。

        注意：scope 归属由被删行的 scope 字段决定，自动补激活也只在同 scope 内进行。
        Returns:
            True 表示命中行已删；False 表示 id 不存在。
        """
        conn = get_conn()
        with _lock:
            row = conn.execute(
                "SELECT is_active, scope FROM llm_configs WHERE id = ?", (id,)
            ).fetchone()
            if not row:
                return False
            was_active = row[0] == 1
            scope = row[1]
            conn.execute("DELETE FROM llm_configs WHERE id = ?", (id,))
            if was_active:
                # 自动激活同 scope 剩余中 id 最小的一条
                nxt = conn.execute(
                    "SELECT id FROM llm_configs WHERE scope = ? ORDER BY id ASC LIMIT 1",
                    (scope,),
                ).fetchone()
                if nxt:
                    conn.execute("UPDATE llm_configs SET is_active = 1 WHERE id = ?", (nxt[0],))
            conn.commit()
            return True

    @staticmethod
    def activate(id: int) -> bool:
        """设为激活（事务内先把同 scope 全置 0 再置 1，保证 is_active 在 scope 内唯一）。

        注意：scope 由被激活行的 scope 字段隐含决定，无需调用方传。
        Returns:
            True 表示命中行已激活；False 表示 id 不存在。
        """
        conn = get_conn()
        with _lock:
            row = conn.execute("SELECT scope FROM llm_configs WHERE id = ?", (id,)).fetchone()
            if not row:
                return False
            scope = row[0]
            # 只清零同 scope 的激活项，不影响另一 scope 的激活状态
            conn.execute("UPDATE llm_configs SET is_active = 0 WHERE scope = ?", (scope,))
            conn.execute("UPDATE llm_configs SET is_active = 1 WHERE id = ?", (id,))
            conn.commit()
            return True


def _row_to_safe(row: dict[str, Any]) -> dict[str, Any]:
    """把 llm_configs 行转为安全视图（不回显 key 明文，附 key_hint 脱敏）。

    key_hint：key 明文尾 4 位（供用户辨识不同 key），无 key 时 None。
    解密失败（如主密钥变更）时 key_hint=None、has_key=False，不抛错。
    """
    has_key = bool(row.get("api_key_enc"))
    key_hint = None
    if has_key:
        try:
            from app.core.security import decrypt_secret
            plain = decrypt_secret(row["api_key_enc"], get_master_key())
            key_hint = plain[-4:] if len(plain) >= 4 else plain
        except Exception:
            # 解密失败：密钥可能已变更。标 has_key=False 让用户重新填。
            has_key = False
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"] or "",
        "model": row["model"] or "",
        "has_key": has_key,
        "key_hint": key_hint,
        "is_active": bool(row["is_active"]),
        "scope": row["scope"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class LlmConfigRepository:
    """[已废弃] 旧单行配置访问层（2026-07-08 多配置管理改造）。

    保留为薄包装，委托 LlmConfigsRepository，避免遗漏旧调用点。
    新代码请直接用 LlmConfigsRepository。
    """

    @staticmethod
    def get_active() -> tuple[str, str, str] | None:
        """读激活配置（委托新仓库，默认 evolution scope）。"""
        return LlmConfigsRepository.get_active("evolution")

    @staticmethod
    def get_safe() -> dict[str, Any]:
        """读激活配置安全视图（委托新仓库，默认 evolution scope）。"""
        return LlmConfigsRepository.get_active_safe("evolution")

    @staticmethod
    def save(*, api_key: str, base_url: str, model: str, name: str = "default") -> None:
        """保存配置（向后兼容：若已存在 evolution 激活项则更新它，否则新建并激活）。"""
        conn = get_conn()
        with _lock:
            row = conn.execute(
                "SELECT id FROM llm_configs WHERE scope = 'evolution' AND is_active = 1 LIMIT 1"
            ).fetchone()
        if row:
            LlmConfigsRepository.update(
                row[0], api_key=api_key, base_url=base_url, model=model, name=name
            )
        else:
            LlmConfigsRepository.create(
                api_key=api_key, base_url=base_url, model=model, name=name, scope="evolution"
            )

    @staticmethod
    def clear() -> None:
        """清空所有配置。"""
        execute("DELETE FROM llm_configs")
