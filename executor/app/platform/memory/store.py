"""NWM 记忆存储层（SQLite + sqlite-vec + FTS5）。

每个作品一个 memory.db 文件（D-D2-2），文件内含 8 类 typed record 表 +
对应向量虚拟表 + 统一 FTS5 全文索引。隔离在文件层，表内不重复 workspace_id。

为什么不用 Graphiti：
  Graphiti 是 generic entity/edge graph，缺叙事学类型化结构（论文 §6 的核心批评）。
  本模块用强类型关系表存 typed records，valid_at/invalid_at 表达有效期，
  SQL 窗口函数取"最新有效切片"（论文 §3.3），SQL JOIN 做 one-hop 图扩展。

为什么同步 sqlite3 不用 aiosqlite：
  sqlite-vec 的 load_extension 必须在 connection 上同步调用，aiosqlite 包装后
  访问底层 connection 取 connection 句柄较绕。记忆操作已在 asyncio.to_thread 中
  执行（events.py 回调），同步阻塞不影响事件循环。

设计依据：设计文档 D-D2-1（store.py 职责）、D-D3-1（registry 合并语义）、D-D3-2（PlotPromise 状态机）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Sequence

import sqlite3

from app.platform.core.settings import MEMORY_EMBED_DIMENSION, get_settings

logger = logging.getLogger(__name__)

# ── 8 类 typed record 的元数据 ────────────────────────────────────────
# (表名, 实体名列, 各表专属语义字段列表)
# 实体名列：用于 registry 视图 PARTITION BY 的键（角色名/物品名/...）。
# 章节摘要/场景/叙事功能没有单一实体名（按章节/场景聚合），PARTITION 用 source_chapter/scene_ref。

_RECORD_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    # 表名: (实体名列, 语义字段)
    "chapter_digest":    ("source_chapter", ("summary", "key_events", "keyword_index")),
    "scene":             ("scene_id",       ("location", "participants", "event_order", "reveal_order", "summary")),
    "character_state":   ("name",           ("goal", "knowledge", "unknowns", "status", "location", "relationship_deltas")),
    "relationship_state":("char_a",         ("char_b", "relation_type", "polarity", "relationship_desc")),
    "object_state":      ("name",           ("owner", "location", "condition")),
    "plot_promise":      ("promise_id",     ("thread_id", "structural_role", "status", "setup_chapter", "payoff_chapter", "promised_payoff", "resolution")),
    "narrative_function":("scene_ref",      ("focalized_observer", "dramatic_beat", "turn_or_reversal", "reader_knowledge", "summary")),
    "world_fact":        ("fact",           ("category", "scope", "valid_chapter_range")),
}

# 所有 record 表共享的时序/溯源字段（论文 §3.2 evidence-backed + §3.3 temporal）
_COMMON_COLUMNS: tuple[str, ...] = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT",
    "source_chapter INTEGER NOT NULL",   # 抽取自第几章（因果锚点）
    "valid_at INTEGER NOT NULL",         # 从第几章开始有效
    "invalid_at INTEGER",                # 第几章被取代（NULL=仍有效）
    "evidence_span TEXT NOT NULL",       # evidence-backed 原文引用
    "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
)

_DIM = MEMORY_EMBED_DIMENSION


def _build_ddl() -> list[str]:
    """生成全部 DDL 语句（8 表 + 8 向量表 + FTS5 + 索引）。

    迁移幂等：每条都带 IF NOT EXISTS。
    """
    statements: list[str] = []

    # ── 8 类 typed record 表 ──
    # 实体列：registry 视图 PARTITION BY 的键。
    # chapter_digest 的实体列复用公共列 source_chapter（每章一条，不另设列），避免重复。
    for table, (entity_col, fields) in _RECORD_TYPES.items():
        common_col_names = {c.split()[0] for c in _COMMON_COLUMNS}  # 取列名（去掉类型）
        cols = list(_COMMON_COLUMNS)
        # 实体列不在公共列里时才显式加（chapter_digest 的 source_chapter 已在公共列）
        if entity_col not in common_col_names:
            cols.append(f"{entity_col} TEXT NOT NULL DEFAULT ''")
        cols += [f"{f} TEXT" for f in fields]
        statements.append(f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(cols) + "\n)")
        # 索引：source_chapter（cutoff 过滤）+ 实体列（registry 分区）
        statements.append(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_chapter ON {table}(source_chapter)"
        )

    # ── 8 个向量虚拟表（sqlite-vec，与 record 表 1:1）──
    for table in _RECORD_TYPES:
        statements.append(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table}_vec USING vec0("
            f"embedding float[{_DIM}], record_id INTEGER)"
        )

    # ── 统一 FTS5 全文索引（词汇检索）──
    # tokenize=trigram：3-gram 切分，对 CJK 友好（3+ 字 query 走 BM25 排序）。
    # 2 字中文词（林晚/玉佩）trigram 命中不了，fts_search 内部对这些短 query 退化为 LIKE 兜底。
    # external content 模式同步复杂，这里用独立 FTS5 表由应用层同步写入。
    statements.append(
        "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5("
        "record_type, "           # character_state / scene / ...
        "entity_name, "           # 实体名（角色名/场景ID/...）
        "search_text, "           # 语义拼接的可检索文本（D-D1-3）
        "source_chapter UNINDEXED, "
        "rowid_of_record UNINDEXED, "  # 指向 record 表的 id
        "tokenize='trigram'"
        ")"
    )

    return statements


class MemoryStore:
    """单个作品记忆库的同步访问封装。

    生命周期：由 MemoryStorePool 惰性创建，LRU 驱逐时关闭。
    线程安全：单连接 + check_same_thread=False，外部锁由调用方保证（pool 层）。
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        """建立连接并加载 sqlite-vec 扩展。首次调用时建表。"""
        if self._conn is not None:
            return self._conn

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：记忆操作在 to_thread 里，跨线程复用同一连接。
        # isolation_level=None：手动事务（BEGIN/COMMIT），避免 autocommit 模式与 DDL 冲突。
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row  # 结果按列名访问
        # WAL：读写并发（检索不阻塞抽取写入）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下 NORMAL 足够安全且更快

        # 加载 sqlite-vec 扩展（pysqlite3 已在 app/__init__.py 注入，此处必有 enable_load_extension）
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as e:
            conn.close()
            raise RuntimeError(f"sqlite-vec 扩展加载失败：{e}") from e

        # 建表（幂等）
        for stmt in _build_ddl():
            conn.execute(stmt)

        self._conn = conn
        logger.info("MemoryStore 初始化完成：%s", self._db_path)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            return self._connect()
        return self._conn

    def close(self) -> None:
        """关闭连接（LRU 驱逐/进程退出时调用）。"""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ────────────────────────────────────────────────────────────
    # CRUD 原语
    # ────────────────────────────────────────────────────────────

    def store_record(
        self,
        record_type: str,
        entity_name: str,
        fields: dict[str, Any],
        *,
        source_chapter: int,
        evidence_span: str,
        search_text: str,
        embedding: list[float] | None = None,
        valid_at: int | None = None,
    ) -> int:
        """写入一条 typed record + 同步 FTS5 + 可选向量。

        Args:
            record_type: _RECORD_TYPES 的 key（character_state / scene / ...）。
            entity_name: 实体名（角色名/场景ID/...），用于 registry 分区。
            fields: 该 record 类型的语义字段 dict（key 必须在 _RECORD_TYPES[record_type][1] 内）。
            source_chapter: 抽取自第几章。
            evidence_span: 原文引用（evidence-backed）。
            search_text: 语义拼接文本（FTS5 检索 + embed 输入）。
            embedding: search_text 的向量（None 时不写向量表，检索降级到纯 BM25）。
            valid_at: 生效章节（默认 = source_chapter）。

        Returns:
            新记录的 id（FTS5 和向量表用它关联）。

        Raises:
            ValueError: record_type 非法 / 字段名非法。
        """
        if record_type not in _RECORD_TYPES:
            raise ValueError(f"非法 record_type: {record_type}，合法: {list(_RECORD_TYPES)}")

        _entity_col, allowed_fields = _RECORD_TYPES[record_type]
        # 过滤非法字段名（防 extractor 输出注入未知列）
        clean_fields = {k: _stringify(v) for k, v in fields.items() if k in allowed_fields}
        bad = set(fields) - set(allowed_fields)
        if bad:
            logger.warning("record_type=%s 忽略未知字段：%s", record_type, bad)

        if valid_at is None:
            valid_at = source_chapter

        # 构造列/值：实体列 + 语义字段 + 公共字段，统一去重（chapter_digest 的实体列
        # source_chapter 与公共列重名时，只保留一个，值取 entity_name——但 chapter_digest
        # 的 entity_name 实际就是章节号字符串，公共 source_chapter 已是 int，这里特殊处理）
        col_value: list[tuple[str, Any]] = []
        if _entity_col == "source_chapter":
            # chapter_digest：实体列即公共列，不重复加，entity_name 仅用于 FTS5
            pass
        else:
            col_value.append((_entity_col, entity_name))
        col_value.extend(clean_fields.items())
        col_value.extend([
            ("source_chapter", source_chapter),
            ("valid_at", valid_at),
            ("invalid_at", None),
            ("evidence_span", evidence_span),
        ])

        col_list = ", ".join(c for c, _ in col_value)
        placeholders = ", ".join(["?"] * len(col_value))
        values = [v for _, v in col_value]

        with self._lock:
            conn = self.conn
            conn.execute("BEGIN")
            try:
                cur = conn.execute(
                    f"INSERT INTO {record_type} ({col_list}) VALUES ({placeholders})",
                    values,
                )
                record_id = cur.lastrowid
                # 同步 FTS5
                conn.execute(
                    "INSERT INTO records_fts(record_type, entity_name, search_text, source_chapter, rowid_of_record)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (record_type, entity_name, search_text, source_chapter, record_id),
                )
                # 可选向量
                if embedding is not None:
                    conn.execute(
                        f"INSERT INTO {record_type}_vec(embedding, record_id) VALUES (?, ?)",
                        (_vec_to_blob(embedding), record_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return record_id

    def update_plot_promise_status(
        self, promise_id: str, *, status: str, payoff_chapter: int, resolution: str | None = None
    ) -> bool:
        """更新 PlotPromise 状态机（open → closed）。

        论文 §3.2：promise 的 open/closed 是 first-class 状态字段，兑现时 UPDATE 同一条记录。
        Returns: True 若匹配到记录并更新。
        """
        with self._lock:
            conn = self.conn
            cur = conn.execute(
                "UPDATE plot_promise SET status=?, payoff_chapter=?, resolution=? "
                "WHERE promise_id=? AND status='open'",
                (status, payoff_chapter, resolution, promise_id),
            )
            return cur.rowcount > 0

    def get_current_state(
        self,
        record_type: str,
        *,
        cutoff: int,
        entity: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """registry 视图：取某类型 record 的"最新有效切片"（论文 §3.3 核心）。

        对替换语义字段（status/location/goal）：取每个实体的最新一条 record。
        对累积语义字段（knowledge）：应用层聚合（调用方自行从多条 record 合并）。

        Args:
            record_type: record 类型。
            cutoff: 因果截止章节（只看 source_chapter <= cutoff 且有效）。
            entity: 限定实体名（None=全部实体）。
            limit: 返回上限。
        """
        if record_type not in _RECORD_TYPES:
            raise ValueError(f"非法 record_type: {record_type}")
        entity_col = _RECORD_TYPES[record_type][0]
        # 窗口函数：按实体分区，按 source_chapter 倒序取最新一条。
        # cutoff 出现两次：source_chapter 过滤（只看已写章节）+ invalid_at 过滤（仍有效）。
        if entity:
            rows = self.conn.execute(
                f"""
                SELECT * FROM (
                  SELECT *,
                    ROW_NUMBER() OVER(PARTITION BY {entity_col} ORDER BY source_chapter DESC) rn
                  FROM {record_type}
                  WHERE source_chapter <= ? AND (invalid_at IS NULL OR invalid_at > ?)
                    AND {entity_col} = ?
                ) WHERE rn = 1 LIMIT ?
                """,
                [cutoff, cutoff, entity, limit],
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""
                SELECT * FROM (
                  SELECT *,
                    ROW_NUMBER() OVER(PARTITION BY {entity_col} ORDER BY source_chapter DESC) rn
                  FROM {record_type}
                  WHERE source_chapter <= ? AND (invalid_at IS NULL OR invalid_at > ?)
                ) WHERE rn = 1 LIMIT ?
                """,
                [cutoff, cutoff, limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def fts_search(self, query: str, *, cutoff: int, limit: int = 20) -> list[dict]:
        """阶段2 词汇检索（FTS5 trigram + LIKE 兜底）。

        trigram tokenizer 对 3+ 字 query 走 BM25 排序；2 字中文词（林晚/玉佩）
        trigram 命中不了，退化为 LIKE 子串匹配（无 BM25 排序，按 source_chapter 倒序）。

        Returns: list of {record_type, entity_name, source_chapter, rowid_of_record, rank}
        rank 越小越相关。
        """
        # 裁剪查询到单个 token（FTS5 MATCH 语法对中文短语敏感，取最长连续 CJK 段）
        query = query.strip()
        if not query:
            return []

        # 主路：trigram BM25
        rows = self.conn.execute(
            """
            SELECT record_type, entity_name, source_chapter, rowid_of_record, rank
            FROM records_fts
            WHERE records_fts MATCH ? AND source_chapter <= ?
            ORDER BY rank
            LIMIT ?
            """,
            [query, cutoff, limit],
        ).fetchall()

        # 兜底：trigram 对短词（<3字）命中为空时退化为 LIKE
        if not rows and len(query) < 3:
            rows = self.conn.execute(
                """
                SELECT record_type, entity_name, source_chapter, rowid_of_record, 0 AS rank
                FROM records_fts
                WHERE search_text LIKE ? AND source_chapter <= ?
                ORDER BY source_chapter DESC
                LIMIT ?
                """,
                [f"%{query}%", cutoff, limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def vec_search(
        self,
        record_type: str,
        embedding: list[float],
        *,
        cutoff: int,
        limit: int = 20,
    ) -> list[dict]:
        """阶段2 向量检索（sqlite-vec KNN）。

        只在指定 record 类型的向量表里搜（按类型分表存向量）。
        cutoff 过滤：JOIN record 表按 source_chapter。

        Returns: list of {record_id, distance, ...record字段}
        distance 越小越相关（L2 距离）。
        """
        rows = self.conn.execute(
            f"""
            SELECT v.record_id, v.distance, r.*
            FROM {record_type}_vec v
            JOIN {record_type} r ON r.id = v.record_id
            WHERE v.embedding MATCH ? AND k = ?
              AND r.source_chapter <= ?
            ORDER BY v.distance
            """,
            [_vec_to_blob(embedding), limit, cutoff],
        ).fetchall()
        return [dict(r) for r in rows]

    def fetch_records_by_ids(self, record_type: str, ids: Sequence[int]) -> list[dict]:
        """按 id 批量取 record（one-hop JOIN 扩展后的回填用）。"""
        if not ids:
            return []
        placeholders = ", ".join(["?"] * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM {record_type} WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_record_types(self) -> list[str]:
        """返回所有 record 类型名（供 harness record_type_policy 用）。"""
        return list(_RECORD_TYPES)

    def count_records(self, record_type: str, *, cutoff: int | None = None) -> int:
        """统计某类型 record 数（供 trace 埋点 + 健康度判断）。"""
        if cutoff is not None:
            row = self.conn.execute(
                f"SELECT COUNT(*) c FROM {record_type} WHERE source_chapter <= ?",
                [cutoff],
            ).fetchone()
        else:
            row = self.conn.execute(f"SELECT COUNT(*) c FROM {record_type}").fetchone()
        return row["c"] if row else 0


# ── 向量序列化：sqlite-vec 要求 float[] 转 BLOB（little-endian float32）──
def _vec_to_blob(vec: list[float]) -> bytes:
    """float list → little-endian float32 BLOB（sqlite-vec 的 MATCH 参数格式）。"""
    import struct

    return struct.pack(f"{len(vec)}f", *vec)


def _stringify(v: Any) -> str:
    """record 语义字段统一转字符串（list/dict 转 JSON）。"""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, dict)):
        import json
        return json.dumps(v, ensure_ascii=False)
    return str(v)


# ════════════════════════════════════════════════════════════════════
# MemoryStorePool — LRU 连接池（照搬 checkpoint_pool 范式）
# ════════════════════════════════════════════════════════════════════

class MemoryStorePool:
    """每个 workspace 一个 MemoryStore 的 LRU 缓存。

    线程安全：asyncio + lock（get 可在事件循环中调，内部 to_thread 访问 store）。
    生命周期：进程单例，由 init_memory_store_pool 初始化。
    """

    def __init__(self, memory_root: Path, max_open: int = 32) -> None:
        self.root = Path(memory_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_open = max_open
        self._stores: OrderedDict[str, MemoryStore] = OrderedDict()
        self._lock = asyncio.Lock()

    def _db_path(self, workspace_id: str) -> Path:
        return self.root / f"{workspace_id}.db"

    async def get(self, workspace_id: str) -> MemoryStore:
        """获取某 workspace 的 store，惰性创建。线程安全。"""
        async with self._lock:
            if workspace_id in self._stores:
                self._stores.move_to_end(workspace_id)
                return self._stores[workspace_id]

            while len(self._stores) >= self.max_open:
                _evict_id, evict = self._stores.popitem(last=False)
                evict.close()

            store = MemoryStore(self._db_path(workspace_id))
            # 实际连接在首次操作时惰性建立（_connect），这里不立即连避免持锁过久
            self._stores[workspace_id] = store
            return store

    async def drop(self, workspace_id: str) -> None:
        """删作品记忆：关闭 store + 删 db 文件。"""
        async with self._lock:
            store = self._stores.pop(workspace_id, None)
        if store is not None:
            store.close()
        db_path = self._db_path(workspace_id)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    async def aclose_all(self) -> None:
        async with self._lock:
            stores = list(self._stores.values())
            self._stores.clear()
        for s in stores:
            s.close()


# ── 单例 ────────────────────────────────────────────────────────────
_pool: MemoryStorePool | None = None


def init_memory_store_pool(pool: MemoryStorePool) -> None:
    global _pool
    _pool = pool


def get_memory_store_pool() -> MemoryStorePool:
    if _pool is None:
        raise RuntimeError("MemoryStorePool 未初始化，请先 init_memory_store_pool()")
    return _pool


def resolve_memory_root() -> Path:
    """解析 memory_dir 为绝对路径。

    跟随 data 目录：DB_PATH 若是绝对路径，memory_dir 相对它解析；
    否则相对进程工作目录。容器内 compose 把两者都指向 /app/executor/data。
    """
    s = get_settings()
    memory_dir = Path(s.memory_dir)
    if memory_dir.is_absolute():
        return memory_dir
    # 相对路径：尝试相对 DB_PATH 的目录（保证与元数据库同卷）
    db_path = Path(s.db_path)
    if db_path.is_absolute():
        return db_path.parent / memory_dir
    # 都相对：相对工作目录（executor 进程根）
    return Path.cwd() / memory_dir


__all__ = [
    "MemoryStore",
    "MemoryStorePool",
    "init_memory_store_pool",
    "get_memory_store_pool",
    "resolve_memory_root",
]
