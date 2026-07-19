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
                status        TEXT NOT NULL,          -- awaiting_input / completed / failed / cancelled (running 不入库)
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
        # Phase 4 幂等迁移：prompt 版本管理表（T9 langfuse 式）
        _migrate_prompt_tables(conn)
        # 驱动器模式幂等迁移：evolve_sessions 补 phase + 文档路径列（D16）
        _migrate_evolve_sessions_driver_fields(conn)
        # 三功能解耦：evolve_sessions 补 eval_ref 列（关联评估报告，决策 S6/T2）
        _migrate_evolve_sessions_eval_ref(conn)
        # 数据闭环：manual_tests 补 origin_layer 列（golden|growing，进化区分验证/探索，决策 A6）
        _migrate_manual_tests_origin_layer(conn)
        # Phase 1：初始化归因映射（幂等，仅空表时填充）
        _seed_agent_prompt_map(conn)
        # 多配置管理：llm_config（单数，单行）→ llm_configs（复数，多行 + is_active）
        _migrate_llm_configs_multi(conn)
        # scope 分家：llm_configs 加 scope 列 + 现有数据复制成双份（evolution + executor）
        _migrate_llm_configs_scope(conn)
        # user_cache 表由 executescript CREATE IF NOT EXISTS 直接建（新表无需 ALTER 迁移）


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


def execute(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
    """执行单条写/读语句（线程安全）。"""
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


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
