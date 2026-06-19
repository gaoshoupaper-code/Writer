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

from app.platform.core.security import (
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
    -- 当前激活的模型名（用户自填，如 glm-4.6 / deepseek-v3）
    active_model         TEXT,
    -- 冻结标记：1 表示禁用，无法登录
    disabled             INTEGER NOT NULL DEFAULT 0,
    workspace_quota      INTEGER NOT NULL DEFAULT 5,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_configs (
    config_id    TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL REFERENCES users(user_id),
    name         TEXT NOT NULL,
    -- AES-256-GCM 加密的 API key
    api_key_enc  TEXT NOT NULL,
    base_url     TEXT,
    model        TEXT NOT NULL,
    -- 当前激活的配置：每用户只能有 1 条 is_active=1
    is_active    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
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
    title            TEXT NOT NULL,
    domain           TEXT NOT NULL DEFAULT 'writing',
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

-- 文生图产物元数据（DD7a：单表 images，字段内嵌评估数据）
CREATE TABLE IF NOT EXISTS images (
    image_id        TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL REFERENCES workspaces(workspace_id),
    owner_id        TEXT NOT NULL REFERENCES users(user_id),
    round           INTEGER NOT NULL,
    version_id      TEXT NOT NULL,
    sample_index    INTEGER NOT NULL,
    direction       TEXT,
    prompt          TEXT,
    file_path       TEXT NOT NULL,
    is_final        INTEGER NOT NULL DEFAULT 0,
    agent_analysis  TEXT,
    user_score      INTEGER,
    user_note       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Skills 自进化系统元数据（DD7b：DB 存元数据，文件存 SKILL.md 正文）
CREATE TABLE IF NOT EXISTS skills (
    skill_id        TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL REFERENCES users(user_id),
    name            TEXT NOT NULL,
    scene_tag       TEXT,
    description     TEXT NOT NULL DEFAULT '',
    revision_count  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON workspaces(owner_id);
CREATE INDEX IF NOT EXISTS idx_threads_owner ON threads(owner_id);
CREATE INDEX IF NOT EXISTS idx_threads_workspace ON threads(workspace_id);
CREATE INDEX IF NOT EXISTS idx_styles_owner ON styles(owner_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_invite_codes_created_by ON invite_codes(created_by);
CREATE INDEX IF NOT EXISTS idx_provider_configs_owner ON provider_configs(owner_id);
CREATE INDEX IF NOT EXISTS idx_images_workspace ON images(workspace_id);
CREATE INDEX IF NOT EXISTS idx_images_round ON images(workspace_id, round);
CREATE INDEX IF NOT EXISTS idx_images_final ON images(workspace_id, is_final);
CREATE INDEX IF NOT EXISTS idx_images_owner ON images(owner_id);
CREATE INDEX IF NOT EXISTS idx_skills_owner ON skills(owner_id);
CREATE INDEX IF NOT EXISTS idx_provider_configs_active ON provider_configs(owner_id, is_active);
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
        self._migrate()

    def _migrate(self) -> None:
        """幂等迁移：为旧库补列/改列。

        ``IF NOT EXISTS`` 在 SQLite 里不能用于 ``ALTER ADD COLUMN``，用
        ``PRAGMA table_info`` 探测后再补。``RENAME COLUMN`` 同理先探测旧列名是否存在。

        workspace 补列（DD2）：
        - ``outline_name`` → ``title``：字段语义通用化（image workspace 非"大纲名"）
        - ``domain``：能力域隔离（writing/image/...），存量默认 'writing'
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(users)")}
        if "active_model" not in cols:
            self._conn.execute("ALTER TABLE users ADD COLUMN active_model TEXT")

        ws_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(workspaces)")}
        # outline_name → title（旧库才有 outline_name；全新库 _SCHEMA 直接是 title）
        if "outline_name" in ws_cols and "title" not in ws_cols:
            self._conn.execute("ALTER TABLE workspaces RENAME COLUMN outline_name TO title")
        # domain 列（DD2）：存量默认 'writing'
        if "domain" not in ws_cols:
            self._conn.execute(
                "ALTER TABLE workspaces ADD COLUMN domain TEXT NOT NULL DEFAULT 'writing'"
            )

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

    # ── API key + model（users 表的 active 配置缓存，真实源是 provider_configs）──
    def set_api_key(
        self, user_id: str, api_key: str, base_url: str | None, model: str | None = None,
    ) -> None:
        encrypted = encrypt_secret(api_key, self.db.master_key)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET encrypted_api_key = ?, api_key_base_url = ?, "
                "active_model = COALESCE(?, active_model), updated_at = ? WHERE user_id = ?",
                (encrypted, base_url, model, _now(), user_id),
            )

    def clear_api_key(self, user_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET encrypted_api_key = NULL, api_key_base_url = NULL, "
                "active_model = NULL, updated_at = ? WHERE user_id = ?",
                (_now(), user_id),
            )

    def get_api_key_plain(self, user_id: str) -> tuple[str | None, str | None, str | None]:
        """返回 (解密后的 api_key, base_url, model)。无 key 返回 (None, None, None)。"""
        row = self.db.conn.execute(
            "SELECT encrypted_api_key, api_key_base_url, active_model FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row or not row["encrypted_api_key"]:
            return None, None, None
        plain = decrypt_secret(row["encrypted_api_key"], self.db.master_key)
        return plain, row["api_key_base_url"], row["active_model"]

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


class ProviderConfigRepository:
    """API 配置历史（每用户多条）。Key 加密存储，列表不暴露明文。

    active 配置（is_active=1）会同步回 users 表，供 build_writer_model 使用。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def list_by_owner(self, owner_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT config_id, owner_id, name, base_url, model, is_active, "
            "created_at, last_used_at FROM provider_configs WHERE owner_id = ? "
            "ORDER BY is_active DESC, last_used_at DESC NULLS LAST, created_at DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, config_id: str, owner_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM provider_configs WHERE config_id = ? AND owner_id = ?",
            (config_id, owner_id),
        ).fetchone()
        return dict(row) if row else None

    def get_active(self, owner_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM provider_configs WHERE owner_id = ? AND is_active = 1",
            (owner_id,),
        ).fetchone()
        return dict(row) if row else None

    def create(
        self, *, owner_id: str, name: str, api_key: str,
        base_url: str | None, model: str, activate: bool = True,
    ) -> dict:
        from uuid import uuid4
        config_id = uuid4().hex
        now = _now()
        encrypted = encrypt_secret(api_key, self.db.master_key)
        with self.db.transaction() as conn:
            if activate:
                conn.execute(
                    "UPDATE provider_configs SET is_active = 0 WHERE owner_id = ?",
                    (owner_id,),
                )
            conn.execute(
                "INSERT INTO provider_configs (config_id, owner_id, name, api_key_enc, "
                "base_url, model, is_active, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (config_id, owner_id, name, encrypted, base_url, model,
                 1 if activate else 0, now, now if activate else None),
            )
        if activate:
            self._sync_to_users(owner_id)
        return self.get(config_id, owner_id)  # type: ignore[return-value]

    def update(
        self, config_id: str, owner_id: str, *,
        name: str | None = None, api_key: str | None = None,
        base_url: str | None = None, model: str | None = None,
    ) -> dict | None:
        sets, vals = [], []
        if name is not None:
            sets.append("name = ?"); vals.append(name)
        if api_key is not None:
            sets.append("api_key_enc = ?"); vals.append(encrypt_secret(api_key, self.db.master_key))
        if base_url is not None:
            sets.append("base_url = ?"); vals.append(base_url)
        if model is not None:
            sets.append("model = ?"); vals.append(model)
        if not sets:
            return self.get(config_id, owner_id)
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"UPDATE provider_configs SET {', '.join(sets)} "
                "WHERE config_id = ? AND owner_id = ?",
                (*vals, config_id, owner_id),
            )
            if cur.rowcount == 0:
                return None
        # 若改的是激活配置，同步到 users
        active = self.get_active(owner_id)
        if active and active["config_id"] == config_id:
            self._sync_to_users(owner_id)
        return self.get(config_id, owner_id)

    def activate(self, config_id: str, owner_id: str) -> bool:
        with self.db.transaction() as conn:
            # 校验归属
            row = conn.execute(
                "SELECT 1 FROM provider_configs WHERE config_id = ? AND owner_id = ?",
                (config_id, owner_id),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE provider_configs SET is_active = 0 WHERE owner_id = ?",
                (owner_id,),
            )
            conn.execute(
                "UPDATE provider_configs SET is_active = 1, last_used_at = ? "
                "WHERE config_id = ? AND owner_id = ?",
                (_now(), config_id, owner_id),
            )
        self._sync_to_users(owner_id)
        return True

    def delete(self, config_id: str, owner_id: str) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT is_active FROM provider_configs WHERE config_id = ? AND owner_id = ?",
                (config_id, owner_id),
            ).fetchone()
            if not row:
                return False
            was_active = bool(row["is_active"])
            conn.execute(
                "DELETE FROM provider_configs WHERE config_id = ? AND owner_id = ?",
                (config_id, owner_id),
            )
        if was_active:
            # 删的是激活配置：清空 users 表的 active 缓存
            UserRepository(self.db).clear_api_key(owner_id)
        return True

    def _sync_to_users(self, owner_id: str) -> None:
        """把激活配置的 key/base_url/model 同步到 users 表（供 build_writer_model 读）。"""
        active = self.get_active(owner_id)
        users = UserRepository(self.db)
        if active is None:
            users.clear_api_key(owner_id)
            return
        plain = decrypt_secret(active["api_key_enc"], self.db.master_key)
        users.set_api_key(owner_id, plain, active["base_url"], active["model"])


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
        self, *, owner_id: str, title: str, domain: str = "writing",
    ) -> dict:
        workspace_id = uuid4().hex
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO workspaces (workspace_id, owner_id, title, domain, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (workspace_id, owner_id, title, domain, now, now),
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


class ImageRepository:
    """文生图产物元数据访问层（DD7a）。所有读写带 owner_id。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self, *, image_id: str, workspace_id: str, owner_id: str,
        round: int, version_id: str, sample_index: int,
        direction: str | None, prompt: str | None, file_path: str,
    ) -> dict:
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO images (image_id, workspace_id, owner_id, round, version_id, "
                "sample_index, direction, prompt, file_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (image_id, workspace_id, owner_id, round, version_id,
                 sample_index, direction, prompt, file_path, now, now),
            )
        return self.get(image_id, owner_id)  # type: ignore[return-value]

    def get(self, image_id: str, owner_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM images WHERE image_id = ? AND owner_id = ?",
            (image_id, owner_id),
        ).fetchone()
        return dict(row) if row else None

    def get_any(self, image_id: str) -> dict | None:
        """不过滤 owner（图片服务端点鉴权后用，已确认归属）。"""
        row = self.db.conn.execute(
            "SELECT * FROM images WHERE image_id = ?", (image_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_by_workspace(self, workspace_id: str, owner_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM images WHERE workspace_id = ? AND owner_id = ? ORDER BY round, version_id, sample_index",
            (workspace_id, owner_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_by_round(self, workspace_id: str, owner_id: str, round: int) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM images WHERE workspace_id = ? AND owner_id = ? AND round = ? "
            "ORDER BY version_id, sample_index",
            (workspace_id, owner_id, round),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_final(self, workspace_id: str, owner_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM images WHERE workspace_id = ? AND owner_id = ? AND is_final = 1 "
            "ORDER BY round, version_id, sample_index",
            (workspace_id, owner_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_evaluation(
        self, image_id: str, owner_id: str, *,
        agent_analysis: str | None = None, user_score: int | None = None,
        user_note: str | None = None,
    ) -> dict | None:
        sets, vals = [], []
        if agent_analysis is not None:
            sets.append("agent_analysis = ?"); vals.append(agent_analysis)
        if user_score is not None:
            sets.append("user_score = ?"); vals.append(user_score)
        if user_note is not None:
            sets.append("user_note = ?"); vals.append(user_note)
        if not sets:
            return self.get(image_id, owner_id)
        sets.append("updated_at = ?"); vals.append(_now())
        vals.extend([image_id, owner_id])
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE images SET {', '.join(sets)} WHERE image_id = ? AND owner_id = ?",
                vals,
            )
        return self.get(image_id, owner_id)

    def set_final(self, image_id: str, owner_id: str, is_final: bool) -> dict | None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE images SET is_final = ?, updated_at = ? WHERE image_id = ? AND owner_id = ?",
                (1 if is_final else 0, _now(), image_id, owner_id),
            )
        return self.get(image_id, owner_id)

    def delete_non_final(self, workspace_id: str, owner_id: str) -> int:
        """删除某 workspace 的非定稿图（D11 废弃清理）。返回删除行数。"""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM images WHERE workspace_id = ? AND owner_id = ? AND is_final = 0",
                (workspace_id, owner_id),
            )
            return cur.rowcount

    def delete_by_workspace(self, workspace_id: str, owner_id: str) -> int:
        """删除某 workspace 的所有图（D11 workspace 删除连带）。返回删除行数。"""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM images WHERE workspace_id = ? AND owner_id = ?",
                (workspace_id, owner_id),
            )
            return cur.rowcount


class SkillRepository:
    """Skills 自进化系统元数据访问层（DD7b）。所有读写带 owner_id。

    SKILL.md 正文存文件系统（skills/<owner>/<skill_id>/SKILL.md），
    本表只存管理用元数据（name/scene_tag/description/revision_count）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self, *, owner_id: str, name: str, scene_tag: str | None = None,
        description: str = "",
    ) -> dict:
        skill_id = uuid4().hex
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO skills (skill_id, owner_id, name, scene_tag, description, "
                "revision_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (skill_id, owner_id, name, scene_tag, description, now, now),
            )
        return self.get(skill_id, owner_id)  # type: ignore[return-value]

    def get(self, skill_id: str, owner_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM skills WHERE skill_id = ? AND owner_id = ?",
            (skill_id, owner_id),
        ).fetchone()
        return dict(row) if row else None

    def list_by_owner(self, owner_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM skills WHERE owner_id = ? ORDER BY updated_at DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update(
        self, skill_id: str, owner_id: str, *,
        name: str | None = None, scene_tag: str | None = None,
        description: str | None = None,
    ) -> dict | None:
        sets, vals = [], []
        for k, v in [("name", name), ("scene_tag", scene_tag), ("description", description)]:
            if v is not None:
                sets.append(f"{k} = ?"); vals.append(v)
        if not sets:
            return self.get(skill_id, owner_id)
        sets.append("updated_at = ?"); vals.append(_now())
        vals.extend([skill_id, owner_id])
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"UPDATE skills SET {', '.join(sets)} WHERE skill_id = ? AND owner_id = ?",
                vals,
            )
            if cur.rowcount == 0:
                return None
        return self.get(skill_id, owner_id)

    def bump_revision(self, skill_id: str, owner_id: str) -> dict | None:
        """进化次数 +1（D8 持久化 Skill 时调用）。"""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE skills SET revision_count = revision_count + 1, updated_at = ? "
                "WHERE skill_id = ? AND owner_id = ?",
                (_now(), skill_id, owner_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(skill_id, owner_id)

    def delete(self, skill_id: str, owner_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM skills WHERE skill_id = ? AND owner_id = ?",
                (skill_id, owner_id),
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
