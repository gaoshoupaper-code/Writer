"""元数据持久层（SQLite）。

承载多用户隔离所需的全部关系数据（D1 决策）：
  users / invite_codes / sessions / workspaces / threads / styles

设计要点：
- 进程内单例 Database，sqlite3 启用 check_same_thread=False + 自带锁。
  FastAPI 同步端点在线程池中调用，sqlite3 的连接级串行足够；
  异步端点（SSE）通过 asyncio.to_thread 间接调用。
- 所有写操作用单连接 + 显式事务，保证原子性。
- WAL 模式提升并发读。
- schema 走幂等迁移函数，首启动自动建表 + 引导管理员。

注：本模块只管元数据。checkpoint 分库（D2）和工作区文件（物理隔离）
    在各自服务里实现，不在这里。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    verify_password,
)

from pathlib import Path  # noqa: E402


def _now() -> str:
    return datetime.now(UTC).isoformat()


def workspace_dir(workspace_root: Path, owner_id: str, workspace_id: str) -> Path:
    """用户维度物理隔离：workspace/<owner_id>/<workspace_id>/。"""
    return Path(workspace_root) / owner_id / workspace_id


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id              TEXT PRIMARY KEY,
    username             TEXT UNIQUE NOT NULL,
    password_hash        TEXT NOT NULL,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    -- API key：AES-256-GCM 密文（nonce||ciphertext+tag，urlsafe-base64）
    encrypted_api_key    TEXT,
    api_key_base_url     TEXT,
    -- 冻结标记：1 表示禁用，无法登录
    disabled             INTEGER NOT NULL DEFAULT 0,
    workspace_quota      INTEGER NOT NULL DEFAULT 5,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code          TEXT PRIMARY KEY,
    created_by    TEXT NOT NULL REFERENCES users(user_id),
    used_by       TEXT REFERENCES users(user_id),
    is_admin_code INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    used_at       TEXT,
    revoked_at    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id     TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL REFERENCES users(user_id),
    outline_name     TEXT NOT NULL,
    active_style_id  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id    TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    owner_id     TEXT NOT NULL REFERENCES users(user_id),
    session_name TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS styles (
    style_id              TEXT PRIMARY KEY,
    owner_id              TEXT NOT NULL REFERENCES users(user_id),
    name                  TEXT NOT NULL,
    meta_style            TEXT NOT NULL DEFAULT '',
    storybuilding_style   TEXT NOT NULL DEFAULT '',
    detail_outline_style  TEXT NOT NULL DEFAULT '',
    writing_style         TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON workspaces(owner_id);
CREATE INDEX IF NOT EXISTS idx_threads_owner ON threads(owner_id);
CREATE INDEX IF NOT EXISTS idx_threads_workspace ON threads(workspace_id);
CREATE INDEX IF NOT EXISTS idx_styles_owner ON styles(owner_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_invite_codes_created_by ON invite_codes(created_by);
"""


class Database:
    """元数据库单例。线程安全（连接级串行 + 大锁兜底）。"""

    def __init__(self, db_path: str | Path, master_key: bytes) -> None:
        self.db_path = Path(db_path)
        self.master_key = master_key
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit；显式 BEGIN/COMMIT 控制事务
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    # ── 事务上下文 ──────────────────────────────────────────
    def transaction(self):
        """返回上下文管理器：进入 BEGIN，正常退出 COMMIT，异常 ROLLBACK。"""
        return _Tx(self)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        """关闭连接（测试清理 / 进程退出时用）。"""
        try:
            self._conn.close()
        except Exception:
            pass


class _Tx:
    def __init__(self, db: Database) -> None:
        self._db = db

    def __enter__(self) -> sqlite3.Connection:
        self._db._lock.acquire()
        self._db.conn.execute("BEGIN")
        return self._db.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._db.conn.execute("COMMIT")
            else:
                self._db.conn.execute("ROLLBACK")
        finally:
            self._db._lock.release()


# ════════════════════════════════════════════════════════════
#  Repository：薄数据访问层。每个方法一次 SQL，不带业务逻辑。
# ════════════════════════════════════════════════════════════


class UserRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self, *, username: str, password: str, is_admin: bool = False,
        workspace_quota: int = 5,
    ) -> dict:
        user_id = uuid4().hex
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO users (user_id, username, password_hash, is_admin, "
                "workspace_quota, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, hash_password(password),
                 1 if is_admin else 0, workspace_quota, now, now),
            )
        return self.get_by_id(user_id)  # type: ignore[return-value]

    def get_by_id(self, user_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_by_username(self, username: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None

    def verify(self, username: str, password: str) -> dict | None:
        """返回用户 dict 若凭证正确且未禁用，否则 None。"""
        user = self.get_by_username(username)
        if not user or user["disabled"]:
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        return user

    def has_admin(self) -> bool:
        row = self.db.conn.execute(
            "SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1"
        ).fetchone()
        return row is not None

    def list_all(self) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET disabled = ?, updated_at = ? WHERE user_id = ?",
                (1 if disabled else 0, _now(), user_id),
            )

    def set_password(self, user_id: str, password: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE user_id = ?",
                (hash_password(password), _now(), user_id),
            )

    # ── API key ──
    def set_api_key(self, user_id: str, api_key: str, base_url: str | None) -> None:
        encrypted = encrypt_secret(api_key, self.db.master_key)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET encrypted_api_key = ?, api_key_base_url = ?, "
                "updated_at = ? WHERE user_id = ?",
                (encrypted, base_url, _now(), user_id),
            )

    def clear_api_key(self, user_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET encrypted_api_key = NULL, api_key_base_url = NULL, "
                "updated_at = ? WHERE user_id = ?",
                (_now(), user_id),
            )

    def get_api_key_plain(self, user_id: str) -> tuple[str | None, str | None]:
        """返回 (解密后的 api_key, base_url)。无 key 返回 (None, None)。"""
        row = self.db.conn.execute(
            "SELECT encrypted_api_key, api_key_base_url FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row or not row["encrypted_api_key"]:
            return None, None
        plain = decrypt_secret(row["encrypted_api_key"], self.db.master_key)
        return plain, row["api_key_base_url"]

    def has_api_key(self, user_id: str) -> bool:
        row = self.db.conn.execute(
            "SELECT encrypted_api_key FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row and row["encrypted_api_key"])

    def workspace_count(self, user_id: str) -> int:
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM workspaces WHERE owner_id = ?", (user_id,)
        ).fetchone()
        return int(row["c"]) if row else 0


class InviteCodeRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self, *, created_by: str, count: int = 1, is_admin_code: bool = False,
    ) -> list[str]:
        import secrets as _s
        codes: list[str] = []
        now = _now()
        with self.db.transaction() as conn:
            for _ in range(count):
                code = _s.token_urlsafe(16)
                conn.execute(
                    "INSERT INTO invite_codes "
                    "(code, created_by, is_admin_code, created_at) VALUES (?, ?, ?, ?)",
                    (code, created_by, 1 if is_admin_code else 0, now),
                )
                codes.append(code)
        return codes

    def get(self, code: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM invite_codes WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None

    def is_usable(self, code: str) -> bool:
        """有效 = 存在 + 未用 + 未吊销。"""
        row = self.get(code)
        if not row:
            return False
        return row["used_by"] is None and row["revoked_at"] is None

    def mark_used(self, code: str, user_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE invite_codes SET used_by = ?, used_at = ? "
                "WHERE code = ? AND used_by IS NULL AND revoked_at IS NULL",
                (user_id, _now(), code),
            )
            return cur.rowcount > 0

    def revoke(self, code: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE invite_codes SET revoked_at = ? "
                "WHERE code = ? AND revoked_at IS NULL",
                (_now(), code),
            )
            return cur.rowcount > 0

    def list_all(self) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM invite_codes ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, user_id: str, ttl_days: int) -> str:
        import secrets as _s
        session_id = _s.token_urlsafe(32)
        now = _now()
        from datetime import timedelta
        expires = (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, user_id, created_at, expires_at, "
                "last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, now, expires, now),
            )
        return session_id

    def get(self, session_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def touch(self, session_id: str, ttl_days: int) -> None:
        """滚动续期：刷新 last_seen 与 expires_at。"""
        from datetime import timedelta
        now = datetime.now(UTC)
        expires = (now + timedelta(days=ttl_days)).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? "
                "WHERE session_id = ?",
                (now.isoformat(), expires, session_id),
            )

    def delete(self, session_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            return cur.rowcount > 0

    def purge_expired(self) -> int:
        now = _now()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (now,)
            )
            return cur.rowcount


# ════════════════════════════════════════════════════════════
#  Workspace / Thread / Style Repository（替代全局 JSON 索引）
# ════════════════════════════════════════════════════════════


class WorkspaceRepository:
    """作品表访问层（替代 workspaces.json）。所有读写带 owner_id。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self, *, owner_id: str, outline_name: str,
    ) -> dict:
        workspace_id = uuid4().hex
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO workspaces (workspace_id, owner_id, outline_name, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (workspace_id, owner_id, outline_name, now, now),
            )
        return self.get(workspace_id, owner_id)  # type: ignore[return-value]

    def get(self, workspace_id: str, owner_id: str) -> dict | None:
        """按 owner 过滤取单条。返回 dict 含 session_count。"""
        row = self.db.conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = ? AND owner_id = ?",
            (workspace_id, owner_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["session_count"] = self._session_count(workspace_id)
        return d

    def get_any(self, workspace_id: str) -> dict | None:
        """不过滤 owner（管理员代访问用）。"""
        row = self.db.conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["session_count"] = self._session_count(workspace_id)
        return d

    def list_by_owner(self, owner_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM workspaces WHERE owner_id = ? ORDER BY updated_at DESC",
            (owner_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["session_count"] = self._session_count(d["workspace_id"])
            result.append(d)
        return result

    def touch(self, workspace_id: str, owner_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE workspaces SET updated_at = ? "
                "WHERE workspace_id = ? AND owner_id = ?",
                (_now(), workspace_id, owner_id),
            )
            return cur.rowcount > 0

    def delete(self, workspace_id: str, owner_id: str) -> bool:
        with self.db.transaction() as conn:
            # 级联删 threads（外键虽设了，但显式删更稳）
            conn.execute(
                "DELETE FROM threads WHERE workspace_id = ? AND owner_id = ?",
                (workspace_id, owner_id),
            )
            cur = conn.execute(
                "DELETE FROM workspaces WHERE workspace_id = ? AND owner_id = ?",
                (workspace_id, owner_id),
            )
            return cur.rowcount > 0

    def set_active_style(
        self, workspace_id: str, owner_id: str, style_id: str | None,
    ) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE workspaces SET active_style_id = ?, updated_at = ? "
                "WHERE workspace_id = ? AND owner_id = ?",
                (style_id, _now(), workspace_id, owner_id),
            )
            return cur.rowcount > 0

    def clear_style_reference(self, style_id: str) -> int:
        """删除某风格时，把所有引用它的工作区 active_style_id 置空。返回影响行数。"""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE workspaces SET active_style_id = NULL "
                "WHERE active_style_id = ?",
                (style_id,),
            )
            return cur.rowcount

    def count_by_owner(self, owner_id: str) -> int:
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM workspaces WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        return int(row["c"]) if row else 0

    def _session_count(self, workspace_id: str) -> int:
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM threads WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return int(row["c"]) if row else 0


class ThreadRepository:
    """线程表访问层（替代 threads.json）。所有读写带 owner_id。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self, *, workspace_id: str, owner_id: str, session_name: str | None,
    ) -> dict:
        existing = self.list_by_workspace(workspace_id, owner_id)
        name = (session_name or "").strip()
        if not name:
            name = f"会话 {len(existing) + 1}"
        thread_id = uuid4().hex
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO threads (thread_id, workspace_id, owner_id, "
                "session_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, workspace_id, owner_id, name, now, now),
            )
        return self.get(thread_id, owner_id)  # type: ignore[return-value]

    def get(self, thread_id: str, owner_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM threads WHERE thread_id = ? AND owner_id = ?",
            (thread_id, owner_id),
        ).fetchone()
        return dict(row) if row else None

    def get_any(self, thread_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_by_workspace(self, workspace_id: str, owner_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM threads WHERE workspace_id = ? AND owner_id = ? "
            "ORDER BY updated_at DESC",
            (workspace_id, owner_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_by_owner(self, owner_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM threads WHERE owner_id = ? ORDER BY updated_at DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_name(
        self, thread_id: str, owner_id: str, session_name: str,
    ) -> dict | None:
        name = session_name.strip()
        if not name:
            raise ValueError("Session name cannot be empty")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE threads SET session_name = ?, updated_at = ? "
                "WHERE thread_id = ? AND owner_id = ?",
                (name, _now(), thread_id, owner_id),
            )
            if cur.rowcount == 0:
                return None
            # 触发 workspace updated_at
            row = conn.execute(
                "SELECT workspace_id FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE workspaces SET updated_at = ? WHERE workspace_id = ?",
                    (_now(), row["workspace_id"]),
                )
        return self.get(thread_id, owner_id)

    def touch(self, thread_id: str, owner_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE threads SET updated_at = ? WHERE thread_id = ? AND owner_id = ?",
                (_now(), thread_id, owner_id),
            )
            row = conn.execute(
                "SELECT workspace_id FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE workspaces SET updated_at = ? WHERE workspace_id = ?",
                    (_now(), row["workspace_id"]),
                )
            return cur.rowcount > 0

    def delete(self, thread_id: str, owner_id: str) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT workspace_id FROM threads WHERE thread_id = ? AND owner_id = ?",
                (thread_id, owner_id),
            ).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
            conn.execute(
                "UPDATE workspaces SET updated_at = ? WHERE workspace_id = ?",
                (_now(), row["workspace_id"]),
            )
            return True


class StyleRepository:
    """风格表访问层（替代 styles.json）。完全私有（D7），所有读写带 owner_id。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self, *, owner_id: str, name: str, meta_style: str = "",
        storybuilding_style: str = "", detail_outline_style: str = "",
        writing_style: str = "",
    ) -> dict:
        style_id = uuid4().hex
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO styles (style_id, owner_id, name, meta_style, "
                "storybuilding_style, detail_outline_style, writing_style, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (style_id, owner_id, name, meta_style, storybuilding_style,
                 detail_outline_style, writing_style, now),
            )
        return self.get(style_id, owner_id)  # type: ignore[return-value]

    def get(self, style_id: str, owner_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM styles WHERE style_id = ? AND owner_id = ?",
            (style_id, owner_id),
        ).fetchone()
        return dict(row) if row else None

    def get_any(self, style_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM styles WHERE style_id = ?", (style_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_by_owner(self, owner_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM styles WHERE owner_id = ? ORDER BY created_at DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update(self, style_id: str, owner_id: str, **fields) -> dict | None:
        allowed = {
            "name", "meta_style", "storybuilding_style",
            "detail_outline_style", "writing_style",
        }
        sets = []
        vals: list = []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k} = ?")
                vals.append(v)
        if not sets:
            return self.get(style_id, owner_id)
        with self.db.transaction() as conn:
            sets_sql = ", ".join(sets)
            cur = conn.execute(
                f"UPDATE styles SET {sets_sql} WHERE style_id = ? AND owner_id = ?",
                (*vals, style_id, owner_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(style_id, owner_id)

    def delete(self, style_id: str, owner_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM styles WHERE style_id = ? AND owner_id = ?",
                (style_id, owner_id),
            )
            return cur.rowcount > 0


# ── 单例访问 ──────────────────────────────────────────────
# 在 app 启动时（lifespan / main 初始化）调用 init_database() 注入实例。
# 其余代码通过 get_database() 取用，避免在模块导入期读 .env。

_database: Database | None = None


def init_database(db: Database) -> None:
    global _database
    _database = db


def get_database() -> Database:
    if _database is None:
        raise RuntimeError("Database not initialized; call init_database() first")
    return _database
