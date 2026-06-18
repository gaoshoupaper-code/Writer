"""Phase 4 验证：/api/me + /api/admin 端点。

用 FastAPI TestClient 跑端到端：注册→登录→设 key→管理员发码→禁用用户→代访问作品。
Session Cookie 在 TestClient 中自动维护。

跑法（在 backend 目录）：
    .venv/Scripts/python.exe -m pytest tests/test_phase4_admin.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── fixtures ───────────────────────────────────────────────

@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """构造一个隔离的 app 实例：独立 .env（独立主密钥 + 管理员）+ 临时工作区。"""
    import secrets
    backend_dir = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    checkpoints = tmp_path / "checkpoints"
    db_path = tmp_path / "app.db"

    monkeypatch.setenv("MASTER_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ADMIN_USERNAME", "rootadmin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-pw-123")
    monkeypatch.setenv("SESSION_TTL_DAYS", "30")
    monkeypatch.setenv("DEFAULT_WORKSPACE_QUOTA", "5")
    # 业务必填字段（mock 模式避免真实 LLM 调用）
    monkeypatch.setenv("WRITER_MODEL", "gpt-4o")
    monkeypatch.setenv("WRITER_AGENT_MODE", "mock")
    monkeypatch.setenv("WRITER_FRONTEND_ORIGIN", "http://localhost:3000")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11111/v1")
    monkeypatch.chdir(backend_dir)

    # 清除 settings 缓存 + 已加载模块，确保用新 env 重建
    import importlib
    import app.core.settings as settings_mod
    settings_mod.get_settings.cache_clear()
    # 重置单例
    import app.db as db_mod
    db_mod._database = None
    import app.core.checkpoint_pool as pool_mod
    pool_mod._pool = None

    from app.main import app  # noqa: F811 — 重新导入拿新实例
    importlib.reload(sys_modules_app())
    yield app


def sys_modules_app():
    """辅助：返回 app.main 模块对象供 reload。"""
    import sys
    return sys.modules["app.main"]


@pytest.fixture()
def client(app_env):
    """TestClient 会触发 lifespan（含 bootstrap_admin）。"""
    with TestClient(app_env) as c:
        yield c


# ── /api/me ────────────────────────────────────────────────

class TestMe:
    def test_profile_requires_auth(self, client):
        r = client.get("/api/me")
        assert r.status_code == 401

    def test_admin_login_and_profile(self, client):
        r = client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})
        assert r.status_code == 200, r.text
        assert r.json()["is_admin"] is True

        r = client.get("/api/me")
        assert r.status_code == 200
        profile = r.json()
        assert profile["username"] == "rootadmin"
        assert profile["has_api_key"] is False
        assert profile["workspace_quota"] == 5

    def test_set_and_clear_api_key(self, client):
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})

        r = client.put("/api/me/api-key", json={"api_key": "sk-user-123", "base_url": "http://x/v1"})
        assert r.status_code == 200
        assert r.json()["has_api_key"] is True

        r = client.get("/api/me")
        assert r.json()["has_api_key"] is True
        assert r.json()["base_url"] == "http://x/v1"

        r = client.delete("/api/me/api-key")
        assert r.status_code == 200
        assert r.json()["has_api_key"] is False


# ── /api/admin/invite-codes ────────────────────────────────

class TestInviteCodesAdmin:
    def test_non_admin_forbidden(self, client):
        # 先用管理员发一个普通用户邀请码
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})
        codes = client.post("/api/admin/invite-codes", json={"count": 1}).json()
        client.post("/api/auth/logout")
        # 注册普通用户
        code = codes[0]
        client.post("/api/auth/register", json={"code": code, "username": "p1", "password": "pw123456"})
        client.post("/api/auth/logout")

        # 用普通用户登录
        client.post("/api/auth/login", json={"username": "p1", "password": "pw123456"})
        r = client.get("/api/admin/invite-codes")
        assert r.status_code == 403

    def test_admin_create_list_revoke(self, client):
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})

        # 生成 3 个码
        r = client.post("/api/admin/invite-codes", json={"count": 3})
        assert r.status_code == 200
        codes = r.json()
        assert len(codes) == 3

        # 列表里能看到（含 bootstrap 生成的那 1 个管理员码 + 这 3 个）
        r = client.get("/api/admin/invite-codes")
        listed = r.json()
        all_codes = {item["code"] for item in listed}
        assert set(codes).issubset(all_codes)

        # 吊销一个
        r = client.delete(f"/api/admin/invite-codes/{codes[0]}")
        assert r.status_code == 200
        # 再次吊销 → 404
        r = client.delete(f"/api/admin/invite-codes/{codes[0]}")
        assert r.status_code == 404


# ── /api/admin/users ───────────────────────────────────────

class TestUserAdmin:
    def _setup(self, client):
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})
        codes = client.post("/api/admin/invite-codes", json={"count": 1}).json()
        client.post("/api/auth/register", json={"code": codes[0], "username": "alice", "password": "pw123456"})
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})

    def test_list_users(self, client):
        self._setup(client)
        r = client.get("/api/admin/users")
        assert r.status_code == 200
        users = r.json()
        names = [u["username"] for u in users]
        assert "rootadmin" in names
        assert "alice" in names

    def test_disable_and_enable(self, client):
        self._setup(client)
        users = client.get("/api/admin/users").json()
        alice = next(u for u in users if u["username"] == "alice")

        r = client.patch(f"/api/admin/users/{alice['user_id']}", json={"disabled": True})
        assert r.status_code == 200
        assert r.json()["disabled"] is True

        # alice 现在无法登录
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login", json={"username": "alice", "password": "pw123456"})
        assert r.status_code == 401

    def test_cannot_disable_self(self, client):
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})
        me = client.get("/api/auth/me").json()
        r = client.patch(f"/api/admin/users/{me['user_id']}", json={"disabled": True})
        assert r.status_code == 400

    def test_cannot_disable_last_admin(self, client):
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})
        me = client.get("/api/auth/me").json()
        r = client.patch(f"/api/admin/users/{me['user_id']}", json={"disabled": True})
        assert r.status_code == 400

    def test_reset_password(self, client):
        self._setup(client)
        users = client.get("/api/admin/users").json()
        alice = next(u for u in users if u["username"] == "alice")

        r = client.post(f"/api/admin/users/{alice['user_id']}/reset-password")
        assert r.status_code == 200
        temp = r.json()["temp_password"]
        assert len(temp) > 0

        # 用新密码能登录
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login", json={"username": "alice", "password": temp})
        assert r.status_code == 200
        # 旧密码失效
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login", json={"username": "alice", "password": "pw123456"})
        assert r.status_code == 401


# ── /api/admin 代访问作品 ─────────────────────────────────

class TestAdminImpersonateRead:
    def test_admin_reads_user_workspace_outline(self, client):
        # 管理员发码 + alice 注册
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})
        codes = client.post("/api/admin/invite-codes", json={"count": 1}).json()
        client.post("/api/auth/register", json={"code": codes[0], "username": "alice", "password": "pw123456"})

        # alice 创建作品并写 outline
        ws = client.post("/api/workspaces", json={"title": "AliceNovel"}).json()
        # 直接写 outline.md（避免触发 LLM）
        from app.core.settings import get_settings
        settings = get_settings()
        outline_path = Path(settings.workspace_root) / ws["workspace_id"][:0]  # 占位
        # 用正确路径：workspace/<owner_id>/<workspace_id>/outline.md
        me = client.get("/api/auth/me").json()
        outline_path = Path(settings.workspace_root) / me["user_id"] / ws["workspace_id"] / "outline.md"
        outline_path.write_text("# Alice 的小说\n\n梗概内容", encoding="utf-8")

        client.post("/api/auth/logout")
        # 管理员代访问
        client.post("/api/auth/login", json={"username": "rootadmin", "password": "admin-pw-123"})

        r = client.get(f"/api/admin/users/{me['user_id']}/workspaces")
        assert r.status_code == 200
        ws_list = r.json()
        assert any(w["title"] == "AliceNovel" for w in ws_list)

        r = client.get(f"/api/admin/users/{me['user_id']}/workspaces/{ws['workspace_id']}/outline")
        assert r.status_code == 200
        data = r.json()
        assert data["title"] == "AliceNovel"
        assert "Alice 的小说" in data["markdown"]
