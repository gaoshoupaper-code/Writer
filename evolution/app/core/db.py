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
                status        TEXT NOT NULL,          -- completed / failed / cancelled(running 不入库)
                started_at    TEXT,
                ended_at      TEXT,
                duration_ms   INTEGER,
                event_count   INTEGER DEFAULT 0,
                error         TEXT,
                ingested_at   TEXT NOT NULL           -- evolution 入库时间
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

            -- rules：阈值型规则定义
            CREATE TABLE IF NOT EXISTS rules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                metric      TEXT NOT NULL,            -- 评估指标，如 duration_ms / status / total_tokens
                op          TEXT NOT NULL,            -- > / >= / < / <= / == / !=
                threshold   TEXT NOT NULL,            -- 阈值（字符串存，引擎按 metric 类型转换）
                enabled     INTEGER NOT NULL DEFAULT 1,
                source      TEXT NOT NULL DEFAULT 'manual',  -- manual / llm_candidate
                created_at  TEXT NOT NULL,
                description TEXT
            );

            -- trace_flags：规则命中打标
            CREATE TABLE IF NOT EXISTS trace_flags (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id      TEXT NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
                rule_id       INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
                metric_value  TEXT,                   -- 命中时的实际值
                flagged_at    TEXT NOT NULL,
                UNIQUE(trace_id, rule_id)
            );
            CREATE INDEX IF NOT EXISTS idx_flags_trace ON trace_flags(trace_id);

            -- trace_scores：LLM-judge 打分结果（第二期）
            CREATE TABLE IF NOT EXISTS trace_scores (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id      TEXT NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
                score         REAL,                   -- 0~1 综合分（越高越好）
                verdict       TEXT,                   -- pass / review / fail
                rubric_json   TEXT,                   -- 各维度评分明细 JSON
                summary       TEXT,                   -- LLM 总结
                scored_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scores_trace ON trace_scores(trace_id);

            -- judgment_runs：LLM-judge 评估任务记录（防重复评、可追溯）
            CREATE TABLE IF NOT EXISTS judgment_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id      TEXT NOT NULL,
                status        TEXT NOT NULL,          -- pending / done / error
                error         TEXT,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                UNIQUE(trace_id)                      -- 同一 trace 只评一次（重评需删记录）
            );

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

            -- Phase 2/3：badcase 诊断候选 + A/B 实验（先建表，Phase 2/3 填充）
            CREATE TABLE IF NOT EXISTS improvement_candidates (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id          TEXT NOT NULL,
                layer             TEXT NOT NULL,
                target            TEXT NOT NULL,
                prompt_name       TEXT,                -- 归因到的 prompt
                diagnosis         TEXT,                -- 诊断结论
                candidate_version_id INTEGER,          -- 生成的候选 prompt 版本
                status            TEXT NOT NULL DEFAULT 'pending',  -- pending/ab_testing/approved/rejected
                created_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ab_experiments (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id      INTEGER,
                prompt_name       TEXT NOT NULL,
                production_version INTEGER,
                candidate_version  INTEGER,
                test_set_id       INTEGER,
                production_scores_json TEXT,
                candidate_scores_json  TEXT,
                verdict           TEXT,                -- win / lose / tie
                status            TEXT NOT NULL DEFAULT 'running',
                created_at        TEXT NOT NULL
            );

            -- Phase 3：A/B 回放测试集
            CREATE TABLE IF NOT EXISTS replay_test_sets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL UNIQUE,
                description   TEXT,
                prompts_json  TEXT NOT NULL,           -- [{request, expected_category}]
                created_at    TEXT NOT NULL
            );

            -- Phase 0 T0.1：judge 方差校准结果（定 A/B seed 数 N 的科学依据）
            CREATE TABLE IF NOT EXISTS judge_calibration (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                layer         TEXT NOT NULL,           -- content / subagent
                target        TEXT NOT NULL,           -- content 时='novel'; subagent 时=agent_name
                metric        TEXT NOT NULL,           -- 维度名
                sample_count  INTEGER NOT NULL,        -- M（校准跑了几次）
                scores_json   TEXT NOT NULL,           -- [s1, s2, ...] 原始分数
                mean          REAL NOT NULL,
                std           REAL NOT NULL,           -- 标准差 σ
                recommended_n INTEGER NOT NULL,        -- 据此 σ 推荐的 seed 数
                calibrated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_judge_cal_dim ON judge_calibration(layer, target, metric);

            -- Phase 2 T2.4：harness 版本管理（D2 代码定义 + S8 文件系统+git）
            -- ⚠️ DEPRECATED（Phase 6 T5.3，2026-06-23）：surface_versions + harness_manifests 取代。
            -- 整体 harness 被 surface 体系（A/B/C 三类 bounded change）取代。
            -- 本表保留只读（历史记录 + Phase 1-4 整体 harness 路径后备），不再写入新版本。
            -- 一个 harness 版本 = 一个 WriterHarness 实现（代码文件）。
            -- label 互斥（复用 prompt 模式）：production/latest/candidate 同一时刻只指向一个版本。
            CREATE TABLE IF NOT EXISTS harness_versions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                version         INTEGER NOT NULL,       -- 单调递增
                code_path       TEXT NOT NULL,          -- 文件系统路径（harnesses/<id>/harness.py）
                git_commit      TEXT,                   -- git commit hash（可空，未提交时）
                parent_version  INTEGER,                -- 从哪个版本进化来（进化谱系）
                source          TEXT NOT NULL DEFAULT 'initial',  -- initial / proposed / approved
                labels          TEXT DEFAULT '',        -- 逗号分隔：production/latest/candidate
                signature_id    INTEGER,                -- 针对哪个失败签名进化（proposed 时填）
                proposer_meta   TEXT,                   -- JSON：proposer 模型/耗时/diff 摘要
                status          TEXT NOT NULL DEFAULT 'draft',  -- draft/sandbox_validating/static_checked/ab_testing/approved/rejected
                created_at      TEXT NOT NULL,
                UNIQUE(version)
            );
            CREATE INDEX IF NOT EXISTS idx_harness_labels ON harness_versions(labels);
            CREATE INDEX IF NOT EXISTS idx_harness_status ON harness_versions(status);

            -- Phase 3 T3.1：badcase 独立记录（D20 立即写表+延迟触发）
            -- 现状嵌在 evaluation_runs/evaluation_scores，抽出独立表便于聚合计数。
            CREATE TABLE IF NOT EXISTS badcase_records (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id      TEXT NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
                layer         TEXT NOT NULL,           -- content / subagent
                target        TEXT NOT NULL,           -- novel / agent_name
                metric        TEXT NOT NULL,           -- 维度名
                score         REAL NOT NULL,
                evidence      TEXT,                    -- judge 依据（来自 evaluation_scores）
                signature_id  INTEGER,                 -- 匹配到的失败签名（NULL=待匹配，D15）
                created_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_badcase_dim ON badcase_records(layer, target, metric, signature_id);
            CREATE INDEX IF NOT EXISTS idx_badcase_trace ON badcase_records(trace_id);

            -- Phase 3 T3.2：失败签名（D8 Mining 产物，D12 LLM 提炼）
            CREATE TABLE IF NOT EXISTS failure_signatures (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                layer           TEXT NOT NULL,
                target          TEXT NOT NULL,
                metric          TEXT NOT NULL,
                signature_text  TEXT NOT NULL,         -- LLM 提炼的人话描述
                -- S10 组件归因（proposer 据此改 harness）
                target_component  TEXT NOT NULL,       -- prompt / skill / middleware / subagent
                target_ref        TEXT NOT NULL,       -- 具体哪个：writing_system / RevisionLimitMiddleware / ...
                status          TEXT NOT NULL DEFAULT 'open',  -- open/mining/proposed/resolved
                badcase_count   INTEGER DEFAULT 0,
                created_at      TEXT NOT NULL,
                updated_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_signature_dim ON failure_signatures(layer, target, metric, status);

            -- Phase 4 T4.4：harness A/B 实验（D6 N seed + S11 完整统计量）
            CREATE TABLE IF NOT EXISTS harness_experiments (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_version INTEGER NOT NULL,     -- harness_versions.version（候选）
                prod_version      INTEGER,              -- 对比的 production 版本
                signature_id      INTEGER,              -- 针对哪个签名
                test_set_id       INTEGER,              -- 用了哪个测试集
                seed_count        INTEGER NOT NULL,     -- N（D22 校准定）
                -- 完整统计量（S11）
                prod_scores_json  TEXT,                 -- [s1, s2, ...]
                cand_scores_json  TEXT,
                prod_mean REAL, prod_std REAL,
                cand_mean REAL, cand_std REAL,
                ci_low REAL, ci_high REAL,             -- 候选均值的置信区间
                p_value REAL,
                verdict           TEXT,                -- win / lose / tie
                confidence        REAL,                -- 置信度 0~1
                static_check_passed INTEGER,           -- D10 静态检查结果（0/1/NULL未跑）
                status            TEXT NOT NULL DEFAULT 'running',  -- running/done/error
                created_at        TEXT NOT NULL,
                finished_at       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_exp_candidate ON harness_experiments(candidate_version);

            -- Phase 6（self-harness 对齐重构）：surface 统一版本表
            -- 一个 (surface_type, surface_name, scope) = 一条"surface 线"，多个 version。
            -- 替代 prompt_versions（A 类文本）+ 未来 B 类 JSON 参数 + C 类受限 Python。
            -- label 废弃（决策 D5：manifest 统一接管），仅留 source/status 追踪。
            -- UNIQUE 含 scope：同名 surface（如 ContextAssembler/permissions）在不同 scope
            -- 是不同的线（参数不同），必须 scope 维度区分，否则跨 scope 冲突。
            CREATE TABLE IF NOT EXISTS surface_versions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                surface_type      TEXT NOT NULL,         -- prompt/skill/description/middleware_params/permissions/stateful_middleware（见 surface_registry）
                surface_name      TEXT NOT NULL,         -- 具体名：writing_system / StorylineSingleLineLimit / GoalMiddleware / ...
                scope             TEXT NOT NULL,         -- meta/storybuilding/detail-outline/writing/interview/global（归属 subagent）
                version           INTEGER NOT NULL,      -- 同 (surface_type, surface_name, scope) 下单调递增
                content           TEXT NOT NULL,         -- A 类=文本 / B 类=JSON / C 类=受限 Python（由 content_kind 决定）
                content_kind      TEXT NOT NULL,         -- text / json / python（校验分发依据）
                config            TEXT DEFAULT '{}',     -- 附属配置（如 model temperature，JSON）
                commit_message    TEXT,                  -- 版本说明（proposer 改了啥）
                source            TEXT NOT NULL DEFAULT 'manual',  -- manual/proposed/ab_winner/migrated
                status            TEXT NOT NULL DEFAULT 'draft',   -- draft/static_checked/ab_testing/approved/rejected
                parent_version    INTEGER,               -- 从哪个版本进化来（进化谱系）
                signature_id      INTEGER,               -- 针对哪个失败签名（proposed 时填）
                proposer_meta     TEXT,                  -- JSON：proposer 模型/diff 摘要
                static_check_passed INTEGER,             -- 0/1/NULL（C 类必填，A/B 可 NULL）
                created_at        TEXT NOT NULL,
                UNIQUE(surface_type, surface_name, scope, version)
            );
            CREATE INDEX IF NOT EXISTS idx_sv_surface ON surface_versions(surface_type, surface_name);
            CREATE INDEX IF NOT EXISTS idx_sv_scope ON surface_versions(scope);
            CREATE INDEX IF NOT EXISTS idx_sv_status ON surface_versions(status);

            -- Phase 6：harness manifest（部署快照）
            -- 一份 manifest = 各 surface 当前 approved 版本的指针聚合。
            -- manifest 是 approved 聚合产物（决策 D7），不是被编辑对象；同时刻只有一个 production。
            CREATE TABLE IF NOT EXISTS harness_manifests (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                manifest_version  INTEGER NOT NULL,      -- 单调递增（部署版本号）
                parent_version    INTEGER,               -- 上一版 manifest（进化谱系）
                entries_json      TEXT NOT NULL,         -- {surfaces:[{surface_type,surface_name,scope,version}], schema_lock:{channels,c_surfaces}}
                status            TEXT NOT NULL DEFAULT 'draft',  -- draft/production/retired（同时刻只一个 production）
                change_summary    TEXT,                  -- 本版改了哪些 surface（相对 parent）
                created_at        TEXT NOT NULL,
                UNIQUE(manifest_version)
            );
            CREATE INDEX IF NOT EXISTS idx_hm_status ON harness_manifests(status);
            """
        )
        conn.commit()

        # 第二期幂等迁移：给已存在的 rules 表补候选规则字段
        _migrate_rules_columns(conn)
        # Phase 3 幂等迁移：给 runs 表补 owner_user_id 列（D2/D16 按用户隔离）
        _migrate_runs_owner_user_id(conn)
        # Phase 4 幂等迁移：prompt 版本管理表（T9 langfuse 式）
        _migrate_prompt_tables(conn)
        # Phase 6 幂等迁移：failure_signatures 加 surface_type/surface_scope 列（决策 D4/D9）
        _migrate_failure_signatures_surface_columns(conn)
        # Phase 1：初始化归因映射（幂等，仅空表时填充）
        _seed_agent_prompt_map(conn)


# 第二期候选规则字段（manual 规则默认 approved 直接生效）
_RULES_EXTRA_COLUMNS = [
    ("status", "TEXT NOT NULL DEFAULT 'approved'"),  # pending / approved / rejected
    ("confidence", "REAL"),                          # LLM 置信度 0~1
    ("evidence", "TEXT"),                            # LLM 推理依据
    ("source_trace_id", "TEXT"),                     # 候选规则源自哪个 trace
]


def _migrate_rules_columns(conn: sqlite3.Connection) -> None:
    """幂等迁移：给已存在的 rules 表补第二期字段。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(rules)").fetchall()}
    missing = [(name, ddl) for name, ddl in _RULES_EXTRA_COLUMNS if name not in existing]
    if not missing:
        return
    with _lock:
        for name, ddl in missing:
            conn.execute(f"ALTER TABLE rules ADD COLUMN {name} {ddl}")
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


def _migrate_prompt_tables(conn: sqlite3.Connection) -> None:
    """幂等迁移：prompt 版本管理表（Phase 4 T9）。

    表由 init_db 的 executescript 创建（IF NOT EXISTS），这里只处理存量库的
    兜底：确认表存在。prompts/prompt_versions 是新表，旧库不会有，executescript
    已覆盖。此函数保留为占位，供未来字段演进扩展。
    """
    # prompts/prompt_versions 表已由 executescript 创建（IF NOT EXISTS）。
    # 此处无需额外操作，保留为扩展点。
    return


def _migrate_failure_signatures_surface_columns(conn: sqlite3.Connection) -> None:
    """幂等迁移：failure_signatures 加 surface_type/surface_scope 列（Phase 6 决策 D4/D9）。

    决策 D9（签名带 surface 类型）：failure_signatures 新增 surface_type（A/B/C 层
    + 具体类型，如 'prompt'/'stateful_middleware'）和 surface_scope（归属 subagent）。
    旧的 target_component/target_ref 保留向后兼容（mining 现有逻辑），proposer 优先
    读 surface_type。

    存量数据无 surface_type → DEFAULT NULL（proposer 读到 NULL 时回退到 target_component）。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(failure_signatures)").fetchall()}
    missing: list[tuple[str, str]] = []
    if "surface_type" not in existing:
        missing.append(("surface_type", "TEXT"))
    if "surface_scope" not in existing:
        missing.append(("surface_scope", "TEXT"))
    if not missing:
        return
    with _lock:
        for name, ddl in missing:
            conn.execute(f"ALTER TABLE failure_signatures ADD COLUMN {name} {ddl}")
        conn.commit()


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
