"""Phase 1 验证：DB 初始化 / 管理员引导 / 注册 / 登录 / 会话 / 加解密往返。

跑法（在 backend 目录）：
    .venv/Scripts/python.exe -m pytest tests/test_phase1_auth.py -v
"""

from __future__ import annotations

import secrets

import pytest

from app.core.security import (
    decrypt_secret,
    encrypt_secret,
    generate_master_key,
    hash_password,
    load_master_key,
    verify_password,
)
from app.db import (
    Database,
    InviteCodeRepository,
    SessionRepository,
    UserRepository,
)


# ── fixtures ───────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path) -> Database:
    master_key = load_master_key(generate_master_key())
    return Database(tmp_path / "test.db", master_key)


# ── security.py ─────────────────────────────────────────────

class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        h = hash_password("s3cret-pass")
        assert h.startswith("scrypt$")
        assert verify_password("s3cret-pass", h)
        assert not verify_password("wrong", h)

    def test_unique_salts(self):
        assert hash_password("same") != hash_password("same")

    def test_malformed_hash_rejected(self):
        assert not verify_password("x", "garbage$not$valid")


class TestMasterKey:
    def test_hex_accepted(self):
        key = secrets.token_hex(32)
        assert load_master_key(key) == bytes.fromhex(key)

    def test_b64_accepted(self):
        import base64
        raw = secrets.token_bytes(32)
        assert load_master_key(base64.urlsafe_b64encode(raw).decode()) == raw

    def test_wrong_length_rejected(self):
        with pytest.raises(ValueError):
            load_master_key("tooshort")


class TestApikeyCrypto:
    def test_roundtrip(self):
        mk = secrets.token_bytes(32)
        ct = encrypt_secret("sk-dummy-key-12345", mk)
        assert ct != "sk-dummy-key-12345"
        assert decrypt_secret(ct, mk) == "sk-dummy-key-12345"

    def test_wrong_key_fails(self):
        ct = encrypt_secret("secret", secrets.token_bytes(32))
        with pytest.raises(Exception):
            decrypt_secret(ct, secrets.token_bytes(32))

    def test_ciphertext_unique(self):
        mk = secrets.token_bytes(32)
        assert encrypt_secret("same", mk) != encrypt_secret("same", mk)


# ── UserRepository ──────────────────────────────────────────

class TestUserRepository:
    def test_create_and_get(self, db):
        u = UserRepository(db).create(username="alice", password="pw123456")
        assert u["username"] == "alice"
        assert u["user_id"]
        assert UserRepository(db).get_by_id(u["user_id"])["username"] == "alice"
        assert UserRepository(db).get_by_username("alice") is not None

    def test_unique_username(self, db):
        repo = UserRepository(db)
        repo.create(username="bob", password="pw123456")
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            repo.create(username="bob", password="pw123456")

    def test_verify_credentials(self, db):
        repo = UserRepository(db)
        repo.create(username="carol", password="correct-pw")
        assert repo.verify("carol", "correct-pw") is not None
        assert repo.verify("carol", "wrong") is None
        assert repo.verify("nobody", "x") is None

    def test_disabled_cannot_login(self, db):
        repo = UserRepository(db)
        u = repo.create(username="dan", password="pw123456")
        repo.set_disabled(u["user_id"], True)
        assert repo.verify("dan", "pw123456") is None

    def test_api_key_roundtrip(self, db):
        repo = UserRepository(db)
        u = repo.create(username="eve", password="pw123456")
        assert not repo.has_api_key(u["user_id"])

        repo.set_api_key(u["user_id"], "sk-abc", "https://api.example.com")
        assert repo.has_api_key(u["user_id"])
        key, url = repo.get_api_key_plain(u["user_id"])
        assert key == "sk-abc"
        assert url == "https://api.example.com"

        repo.clear_api_key(u["user_id"])
        assert not repo.has_api_key(u["user_id"])

    def test_has_admin(self, db):
        repo = UserRepository(db)
        assert not repo.has_admin()
        repo.create(username="root", password="pw123456", is_admin=True)
        assert repo.has_admin()


# ── InviteCodeRepository ────────────────────────────────────

class TestInviteCodes:
    def test_create_and_usability(self, db):
        users = UserRepository(db)
        admin = users.create(username="admin", password="pw123456", is_admin=True)
        invites = InviteCodeRepository(db)

        codes = invites.create(created_by=admin["user_id"], count=2)
        assert len(codes) == 2
        assert invites.is_usable(codes[0])
        assert invites.is_usable(codes[1])

    def test_mark_used(self, db):
        users = UserRepository(db)
        admin = users.create(username="admin", password="pw123456", is_admin=True)
        u2 = users.create(username="u2", password="pw123456")
        invites = InviteCodeRepository(db)
        code = invites.create(created_by=admin["user_id"])[0]

        assert invites.mark_used(code, u2["user_id"]) is True
        assert invites.is_usable(code) is False  # 用过后不可再用

    def test_revoke(self, db):
        users = UserRepository(db)
        admin = users.create(username="admin", password="pw123456", is_admin=True)
        invites = InviteCodeRepository(db)
        code = invites.create(created_by=admin["user_id"])[0]

        assert invites.revoke(code) is True
        assert invites.is_usable(code) is False

    def test_nonexistent_code(self, db):
        invites = InviteCodeRepository(db)
        assert invites.is_usable("nonexistent") is False


# ── SessionRepository ───────────────────────────────────────

class TestSessions:
    def test_create_and_get(self, db):
        users = UserRepository(db)
        u = users.create(username="x", password="pw123456")
        sid = SessionRepository(db).create(user_id=u["user_id"], ttl_days=30)
        row = SessionRepository(db).get(sid)
        assert row is not None
        assert row["user_id"] == u["user_id"]

    def test_expiry(self, db):
        users = UserRepository(db)
        u = users.create(username="x", password="pw123456")
        # ttl=0 → expires_at ≈ now；current_user 的判断是 expires_at < now，
        # 故用一个已过期的旧时间直接写库更可靠。
        sid = SessionRepository(db).create(user_id=u["user_id"], ttl_days=30)
        # 手动改 expires_at 到过去，模拟过期
        with db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE session_id = ?",
                ("2000-01-01T00:00:00+00:00", sid),
            )
        row = SessionRepository(db).get(sid)
        assert row is not None
        assert row["expires_at"] < _utcnow_iso()

    def test_delete(self, db):
        users = UserRepository(db)
        u = users.create(username="x", password="pw123456")
        sessions = SessionRepository(db)
        sid = sessions.create(user_id=u["user_id"], ttl_days=30)
        assert sessions.delete(sid) is True
        assert sessions.get(sid) is None

    def test_purge_expired(self, db):
        users = UserRepository(db)
        u = users.create(username="x", password="pw123456")
        sessions = SessionRepository(db)
        expired = sessions.create(user_id=u["user_id"], ttl_days=30)
        sessions.create(user_id=u["user_id"], ttl_days=30)  # 有效
        # 手动改第一条到过去，模拟过期
        with db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE session_id = ?",
                ("2000-01-01T00:00:00+00:00", expired),
            )
        n = sessions.purge_expired()
        assert n >= 1
        assert sessions.get(expired) is None


# ── Bootstrap ───────────────────────────────────────────────

class TestAdminBootstrap:
    def test_creates_admin_when_none(self, db, monkeypatch, tmp_path):
        # 初始化 db 单例
        from app.db import init_database
        init_database(db)

        # 伪造 settings
        import app.core.settings as settings_mod

        class FakeSettings:
            admin_username = "rootadmin"
            admin_password = "boot-pw123"
            default_workspace_quota = 5

        monkeypatch.setattr(settings_mod, "get_settings", lambda: FakeSettings())
        from app.auth.bootstrap import bootstrap_admin
        result = bootstrap_admin()

        assert result is not None
        assert result["username"] == "rootadmin"
        assert UserRepository(db).has_admin() is True
        assert result["admin_invite_code"]

    def test_idempotent_when_admin_exists(self, db, monkeypatch):
        from app.db import init_database
        init_database(db)
        UserRepository(db).create(
            username="existing", password="pw123456", is_admin=True,
        )

        import app.core.settings as settings_mod

        class FakeSettings:
            admin_username = "rootadmin"
            admin_password = "boot-pw123"
            default_workspace_quota = 5

        monkeypatch.setattr(settings_mod, "get_settings", lambda: FakeSettings())
        from app.auth.bootstrap import bootstrap_admin
        assert bootstrap_admin() is None  # 已有管理员，跳过


def _utcnow_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()
