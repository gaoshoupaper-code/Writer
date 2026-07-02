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

            -- Phase 8（compose 配置化重构，决策 #12/#17/#18）：
            -- harness_snapshots 从存 tar 整包 → 存 config_json（配置快照全量，tar 废弃）。
            -- schema_lock 废弃（重跑式 A/B，无 replay 需求，决策 #12）。
            -- source_commit 记对应 git commit（源码在 bare repo，决策 D7a/D10b）。
            -- 老库（有 tar_blob 列）由 _migrate_harness_snapshots_config 幂等迁移。

            -- harness_snapshots：配置快照（替代 tar 整包，决策 #18）
            -- 一份快照 = 某个版本的完整 HarnessConfig JSON（不可变）。
            -- 同时刻只有一个 status='production'（发布时旧 production 降 retired）。
            -- config_json 存完整配置对象（prompts/middleware/processors 全在里面）。
            -- source_commit 记 git commit hash（executor 按此 pull 对应源码）。
            CREATE TABLE IF NOT EXISTS harness_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                version         INTEGER NOT NULL UNIQUE,        -- 配置版本号，单调递增
                parent_version  INTEGER,                         -- 上一版快照（进化谱系）
                config_json     TEXT NOT NULL,                   -- 完整 HarnessConfig JSON（不可变快照）
                source_commit   TEXT,                             -- 对应 git commit hash（executor pull 用）
                change_summary  TEXT,                             -- 相对 parent 改了哪些
                status          TEXT NOT NULL DEFAULT 'production', -- production/retired（同时刻只一个 production）
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hs_status ON harness_snapshots(status);
            CREATE INDEX IF NOT EXISTS idx_hs_version ON harness_snapshots(version);

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
                shipped_version INTEGER,                 -- ship 了则指向 harness_snapshots.version
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
        # Phase 4 幂等迁移：prompt 版本管理表（T9 langfuse 式）
        _migrate_prompt_tables(conn)
        # Phase 8 幂等迁移：harness_snapshots tar→config_json（compose 配置化，决策 #18）
        _migrate_harness_snapshots_config(conn)
        # 驱动器模式幂等迁移：evolve_sessions 补 phase + 文档路径列（D16）
        _migrate_evolve_sessions_driver_fields(conn)
        # 三功能解耦：evolve_sessions 补 eval_ref 列（关联评估报告，决策 S6/T2）
        _migrate_evolve_sessions_eval_ref(conn)
        # Phase 1：初始化归因映射（幂等，仅空表时填充）
        _seed_agent_prompt_map(conn)


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


def _migrate_harness_snapshots_config(conn: sqlite3.Connection) -> None:
    """幂等迁移：harness_snapshots 从 tar→config_json（Phase 8 compose，决策 #18）。

    老库有 tar_blob(NOT NULL)/tar_size/schema_lock 列。Phase 8 新 schema 用
    config_json/source_commit，不要 tar_blob/schema_lock。

    迁移策略（SQLite 不支持 ALTER COLUMN 改约束，需重建表）：
      1. 检测老列（tar_blob）是否存在
      2. 重建表：新建 Phase 8 schema 的临时表 → 复制老数据（config_json=NULL，标 retired）
         → DROP 老表 → 重命名临时表
      3. 老快照行 config_json IS NULL → 视为废弃，新代码查询时过滤

    幂等：检测 config_json 列存在且 tar_blob 列不存在则跳过（已迁移）。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(harness_snapshots)").fetchall()}

    # 已迁移：有 config_json 且无 tar_blob
    if "config_json" in existing and "tar_blob" not in existing:
        return

    # 新库（CREATE TABLE 已是 Phase 8 schema）：无 tar_blob 也无 config_json 冲突
    if "tar_blob" not in existing and "config_json" in existing:
        return

    with _lock:
        # 老库重建表：tar_blob NOT NULL 阻止新 INSERT，必须去掉
        conn.executescript(
            """
            CREATE TABLE harness_snapshots_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                version         INTEGER NOT NULL UNIQUE,
                parent_version  INTEGER,
                config_json     TEXT,
                source_commit   TEXT,
                change_summary  TEXT,
                status          TEXT NOT NULL DEFAULT 'production',
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hs_status_new ON harness_snapshots_new(status);
            CREATE INDEX IF NOT EXISTS idx_hs_version_new ON harness_snapshots_new(version);

            INSERT INTO harness_snapshots_new (id, version, parent_version, change_summary, status, created_at)
            SELECT id, version, parent_version, change_summary,
                   CASE WHEN 1=1 THEN 'retired' END, created_at
            FROM harness_snapshots;

            DROP TABLE harness_snapshots;
            ALTER TABLE harness_snapshots_new RENAME TO harness_snapshots;
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hs_status ON harness_snapshots(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hs_version ON harness_snapshots(version)")
        conn.commit()
        logger.info(
            "harness_snapshots 迁移完成：重建表为 config_json schema（Phase 8 compose，决策 #18）。"
            "老 tar 快照已标 retired（config_json=NULL）。"
        )


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
