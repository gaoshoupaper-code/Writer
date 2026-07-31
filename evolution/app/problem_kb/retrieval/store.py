"""派生索引存储（向量 + FTS5，REQ-09/DEC-11/AC-24）。

派生索引不是事实真源——删除重建不影响权威数据（AC-24）。本模块管理两类派生索引：

  1. 向量索引（sqlite-vec vec0）：**独立 db 文件** data/problem_kb_vec.db，独立连接。
     与主库解耦，扩展加载失败不影响 evolution 服务启动与主库事务（AC-26 隔离）。
     存 standard_problems 的 embedding（problem_id → float[2048]），供 KNN 重排。

  2. 全文索引（FTS5）：主库 evolution.db 内（FTS5 是 SQLite 内置，无需扩展）。
     表 standard_problems_fts 在 db.py _migrate_problem_kb_tables 建。存标准问题的
     title/description/statement，供 BM25 召回。

降级语义（AC-26/DEC-13）：
  - 向量扩展不可用 → 向量降级（only fts），is_vector_available()=False
  - FTS5 不可用 → 全文降级（only structural + LIKE）
  - 调用方据返回的 availability 标志决定检索路径
"""
from __future__ import annotations

import json
import logging
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any

import app.core.db as db
from app.core.settings import settings
from app.problem_kb.retrieval.embedder import EMBED_DIMENSION, get_embedder

logger = logging.getLogger("evolution.problem_kb.store")

_VEC_DB_FILENAME = "problem_kb_vec.db"


# ════════════════════════════════════════════════════════════════
# 向量存储（独立连接）
# ════════════════════════════════════════════════════════════════


class _VecStore:
    """sqlite-vec 向量索引（独立 db 文件，独立连接）。

    单例。扩展加载失败时 _available=False，所有操作降级为 no-op。
    """

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._available = False
        self._lock = threading.Lock()

    def _connect(self) -> None:
        """建立连接并加载 sqlite-vec 扩展。失败则标记不可用（不抛出）。"""
        if self._conn is not None:
            return
        with self._lock:
            if self._conn is not None:
                return
            try:
                import sqlite_vec  # noqa: F401

                vec_path = self._vec_path()
                vec_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(vec_path), check_same_thread=False, isolation_level=None
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                conn.execute(
                    f"""CREATE VIRTUAL TABLE IF NOT EXISTS standard_problems_vec
                        USING vec0(embedding float[{EMBED_DIMENSION}], problem_id TEXT)"""
                )
                self._conn = conn
                self._available = True
                logger.info("问题知识库向量索引就绪：%s", vec_path)
            except Exception as e:
                # 扩展加载失败（sqlite-vec 未装 / Python 构建不支持 load_extension）
                logger.warning("向量索引不可用，检索降级到 FTS+结构化：%s", e)
                self._available = False
                self._conn = None

    def _vec_path(self) -> Path:
        """向量 db 路径：与 evolution.db 同目录（data/）。"""
        # 复用 evolution.db 的目录，保证 volume 持久化一致
        s = settings
        evodb = Path(s.db_path)
        return evodb.parent / _VEC_DB_FILENAME

    @property
    def available(self) -> bool:
        if not self._available:
            return False
        return True

    def ensure_ready(self) -> None:
        """懒初始化连接（首次访问时）。"""
        if self._conn is None and not self._available:
            # 只尝试一次：_connect 成功会置 _available，失败也置 False，避免反复重试
            self._connect()

    def upsert(self, problem_id: str, embedding: list[float]) -> bool:
        """写入/更新一条向量。返回是否成功（不可用时 False）。"""
        self.ensure_ready()
        if not self._available or self._conn is None:
            return False
        try:
            blob = _vec_to_blob(embedding)
            # vec0 无 UPSERT，先删后插
            with self._lock:
                self._conn.execute(
                    "DELETE FROM standard_problems_vec WHERE problem_id=?", (problem_id,)
                )
                self._conn.execute(
                    "INSERT INTO standard_problems_vec(embedding, problem_id) VALUES (?, ?)",
                    (blob, problem_id),
                )
            return True
        except Exception:
            logger.exception("向量写入失败 problem=%s", problem_id)
            return False

    def delete(self, problem_id: str) -> None:
        self.ensure_ready()
        if not self._available or self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM standard_problems_vec WHERE problem_id=?", (problem_id,)
                )
        except Exception:
            logger.exception("向量删除失败 problem=%s", problem_id)

    def knn(self, query_vec: list[float], k: int = 20) -> list[tuple[str, float]]:
        """KNN 查询。返回 [(problem_id, distance)]，按距离升序。不可用时返回 []。"""
        self.ensure_ready()
        if not self._available or self._conn is None:
            return []
        try:
            blob = _vec_to_blob(query_vec)
            with self._lock:
                rows = self._conn.execute(
                    """SELECT problem_id, distance
                       FROM standard_problems_vec
                       WHERE embedding MATCH ? AND k = ?
                       ORDER BY distance""",
                    (blob, k),
                ).fetchall()
            return [(r["problem_id"], r["distance"]) for r in rows]
        except Exception:
            logger.exception("向量 KNN 查询失败")
            return []

    def count(self) -> int:
        self.ensure_ready()
        if not self._available or self._conn is None:
            return 0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM standard_problems_vec"
                ).fetchone()
                return row["n"] if row else 0
        except Exception:
            return 0


# 全局单例
_vec_store = _VecStore()


def is_vector_available() -> bool:
    """向量索引是否可用（检索方据此决定是否做向量重排，AC-26）。"""
    _vec_store.ensure_ready()
    return _vec_store.available


def _vec_to_blob(vec: list[float]) -> bytes:
    """float 列表 → little-endian double blob（sqlite-vec 要求）。"""
    return struct.pack(f"<{len(vec)}d", *vec)


# ════════════════════════════════════════════════════════════════
# 派生索引同步（标准问题创建/更新时调用）
# ════════════════════════════════════════════════════════════════


def sync_problem_to_index(
    problem_id: str,
    title: str,
    description: str,
    statement: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """标准问题创建/更新后同步到派生索引（FTS5 + 向量）。

    FTS5 写主库（可用 conn 复用调用方事务）；向量需 embedding 调用，走独立连接。
    两者独立失败——任一失败不影响另一个，也不影响主库事实（AC-24）。
    """
    # 1. FTS5 同步（主库）
    _sync_fts(problem_id, title, description, statement, conn)
    # 2. 向量同步（独立库）—— 失败仅降级，不抛出
    _sync_vector(problem_id, title, description, statement)


def _sync_fts(
    problem_id: str,
    title: str,
    description: str,
    statement: str,
    conn: sqlite3.Connection | None,
) -> None:
    """写主库 FTS5 索引。"""
    sql_exec = conn.execute if conn is not None else db.execute
    try:
        sql_exec(
            "DELETE FROM standard_problems_fts WHERE problem_id=?", (problem_id,)
        )
        sql_exec(
            """INSERT INTO standard_problems_fts(problem_id, title, description, statement)
               VALUES (?, ?, ?, ?)""",
            (problem_id, title or "", description or "", statement or ""),
        )
    except Exception:
        logger.exception("FTS5 同步失败 problem=%s", problem_id)


def _sync_vector(
    problem_id: str,
    title: str,
    description: str,
    statement: str,
) -> None:
    """计算 embedding 并写入向量索引。embedder 不可用或写入失败则降级。"""
    embedder = get_embedder()
    if embedder is None:
        return  # 向量降级
    # 语义拼接：标题 + 描述 + 代表性陈述
    text = f"{title} {description} {statement}".strip()
    if not text:
        return
    try:
        vec = embedder.embed_one(text)
        _vec_store.upsert(problem_id, vec)
    except Exception:
        logger.exception("向量索引同步失败 problem=%s（降级）", problem_id)


def remove_problem_from_index(problem_id: str) -> None:
    """标准问题删除/过时时从派生索引移除。"""
    try:
        db.execute("DELETE FROM standard_problems_fts WHERE problem_id=?", (problem_id,))
    except Exception:
        logger.exception("FTS5 删除失败 problem=%s", problem_id)
    _vec_store.delete(problem_id)


def rebuild_all_vectors() -> int:
    """重建全部向量索引（派生索引可重建，AC-24）。返回处理条数。

    遍历所有标准问题，重新计算 embedding 写入向量索引。
    用于派生索引损坏恢复或 embedding 模型升级后重建。
    """
    embedder = get_embedder()
    if embedder is None:
        logger.info("向量重建跳过：embedder 不可用")
        return 0
    _vec_store.ensure_ready()
    if not _vec_store.available:
        return 0
    problems = db.query_all("SELECT problem_id, title, description FROM standard_problems")
    n = 0
    for p in problems:
        # 取该标准问题下代表性陈述（取第一条已确认实例的 statement）
        link = db.query_one(
            """SELECT pi.statement FROM problem_instance_links pil
               JOIN problem_instances pi ON pil.instance_id = pi.instance_id
               WHERE pil.problem_id=? ORDER BY pi.created_at LIMIT 1""",
            (p["problem_id"],),
        )
        statement = link["statement"] if link else ""
        _sync_vector(p["problem_id"], p["title"], p.get("description") or "", statement)
        n += 1
    logger.info("向量索引重建完成：%d 条", n)
    return n


# ════════════════════════════════════════════════════════════════
# FTS5 检索（主库，供 search 调用）
# ════════════════════════════════════════════════════════════════


def fts_search(query: str, limit: int = 20) -> list[tuple[str, float]]:
    """FTS5 BM25 全文召回。返回 [(problem_id, rank)]，rank 越小越相关（BM25 距离）。

    query 经 FTS5 语法规整（中文用 trigram，直接传子串即可）。
    """
    if not query.strip():
        return []
    # 用双引号包裹避免被当 FTS5 语法（如 OR/AND/NOT）解析
    safe = '"' + query.replace('"', '""') + '"'
    try:
        rows = db.query_all(
            """SELECT problem_id, rank
               FROM standard_problems_fts
               WHERE standard_problems_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (safe, limit),
        )
        return [(r["problem_id"], r["rank"]) for r in rows]
    except Exception:
        # FTS5 不可用（极少数 SQLite 构建不含 FTS5），降级
        logger.warning("FTS5 检索失败，降级到结构化过滤")
        return []


def vec_knn(query_vec: list[float], k: int = 20) -> list[tuple[str, float]]:
    """向量 KNN 重排。返回 [(problem_id, distance)]。不可用返回 []。"""
    return _vec_store.knn(query_vec, k)


__all__ = [
    "is_vector_available",
    "sync_problem_to_index",
    "remove_problem_from_index",
    "rebuild_all_vectors",
    "fts_search",
    "vec_knn",
]
