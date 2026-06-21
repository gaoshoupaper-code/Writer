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

from app.settings import settings

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


# 模块级单例连接。SQLite 单文件 + 全局锁，足够 monitoring 的量级。
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
                ingested_at   TEXT NOT NULL           -- monitoring 入库时间
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
            """
        )
        conn.commit()

        # 第二期幂等迁移：给已存在的 rules 表补候选规则字段
        _migrate_rules_columns(conn)
        # Phase 3 幂等迁移：给 runs 表补 owner_user_id 列（D2/D16 按用户隔离）
        _migrate_runs_owner_user_id(conn)
        # Phase 4 幂等迁移：prompt 版本管理表（T9 langfuse 式）
        _migrate_prompt_tables(conn)
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


# Phase 1 T1.6：subagent → prompt 归因映射初始数据（已核实确切 prompt 名）
# 依据：backend/app/domains/writing/expert_agent/agents/*.py + evaluators/*.py 的 load_prompt
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
