"""Provider 配置历史的单元测试（直接测 Repository，不走 TestClient）。

验证：创建/列表/激活/切换/删除/同步 users 表/model 注入。
"""

from __future__ import annotations

import pytest

from app.platform.core.security import generate_master_key, load_master_key
from app.db import (
    Database,
    ProviderConfigRepository,
    UserRepository,
    init_database,
)


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "app.db", load_master_key(generate_master_key()))
    init_database(d)
    return d


@pytest.fixture()
def user_id(db):
    return UserRepository(db).create(username="alice", password="pw123456")["user_id"]


class TestProviderConfig:
    def test_create_and_list(self, db, user_id):
        repo = ProviderConfigRepository(db)
        c = repo.create(
            owner_id=user_id, name="我的GLM", api_key="sk-aaa",
            base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-4.6",
        )
        assert c["name"] == "我的GLM"
        assert c["is_active"] == 1  # 默认激活
        lst = repo.list_by_owner(user_id)
        assert len(lst) == 1

    def test_create_does_not_leak_key_in_list(self, db, user_id):
        repo = ProviderConfigRepository(db)
        repo.create(
            owner_id=user_id, name="c1", api_key="sk-secret-123",
            base_url="http://x", model="m1",
        )
        lst = repo.list_by_owner(user_id)
        # 列表项里不应有 api_key_enc / 明文 key
        assert "api_key_enc" not in lst[0]
        assert "sk-secret-123" not in str(lst[0])

    def test_active_syncs_to_users(self, db, user_id):
        repo = ProviderConfigRepository(db)
        users = UserRepository(db)
        assert not users.has_api_key(user_id)

        repo.create(
            owner_id=user_id, name="c1", api_key="sk-aaa",
            base_url="http://x/v1", model="glm-4.6", activate=True,
        )
        # users 表应同步到激活配置
        key, base_url, model = users.get_api_key_plain(user_id)
        assert key == "sk-aaa"
        assert base_url == "http://x/v1"
        assert model == "glm-4.6"

    def test_switch_active_updates_users(self, db, user_id):
        repo = ProviderConfigRepository(db)
        c1 = repo.create(owner_id=user_id, name="GLM", api_key="sk-glm",
                         base_url="http://glm", model="glm-4.6")
        c2 = repo.create(owner_id=user_id, name="DS", api_key="sk-ds",
                         base_url="http://ds", model="deepseek-v3")
        # c2 应激活（新建时 activate=True 会切换）
        users = UserRepository(db)
        key, _, model = users.get_api_key_plain(user_id)
        assert key == "sk-ds"
        assert model == "deepseek-v3"

        # 切回 c1
        assert repo.activate(c1["config_id"], user_id) is True
        key, _, model = users.get_api_key_plain(user_id)
        assert key == "sk-glm"
        assert model == "glm-4.6"
        # c2 不再激活
        assert repo.get_active(user_id)["config_id"] == c1["config_id"]

    def test_create_without_activate(self, db, user_id):
        repo = ProviderConfigRepository(db)
        c1 = repo.create(owner_id=user_id, name="c1", api_key="k1",
                         base_url=None, model="m1", activate=True)
        c2 = repo.create(owner_id=user_id, name="c2", api_key="k2",
                         base_url=None, model="m2", activate=False)
        # 激活的还是 c1
        assert repo.get_active(user_id)["config_id"] == c1["config_id"]

    def test_update_config(self, db, user_id):
        repo = ProviderConfigRepository(db)
        c = repo.create(owner_id=user_id, name="c1", api_key="k1",
                        base_url="http://x", model="m1")
        repo.update(c["config_id"], user_id, name="改名", model="new-model")
        updated = repo.get(c["config_id"], user_id)
        assert updated["name"] == "改名"
        assert updated["model"] == "new-model"

    def test_update_active_config_resyncs_users(self, db, user_id):
        repo = ProviderConfigRepository(db)
        c = repo.create(owner_id=user_id, name="c1", api_key="k1",
                        base_url="http://x", model="m1", activate=True)
        repo.update(c["config_id"], user_id, api_key="k2", model="m2")
        users = UserRepository(db)
        key, _, model = users.get_api_key_plain(user_id)
        assert key == "k2"  # 同步了
        assert model == "m2"

    def test_delete_active_clears_users(self, db, user_id):
        repo = ProviderConfigRepository(db)
        c = repo.create(owner_id=user_id, name="c1", api_key="k1",
                        base_url="http://x", model="m1", activate=True)
        assert UserRepository(db).has_api_key(user_id)
        assert repo.delete(c["config_id"], user_id) is True
        assert not UserRepository(db).has_api_key(user_id)

    def test_delete_non_active_keeps_users(self, db, user_id):
        repo = ProviderConfigRepository(db)
        c1 = repo.create(owner_id=user_id, name="active", api_key="k1",
                         base_url=None, model="m1", activate=True)
        c2 = repo.create(owner_id=user_id, name="spare", api_key="k2",
                         base_url=None, model="m2", activate=False)
        repo.delete(c2["config_id"], user_id)
        # 激活的 c1 不受影响
        assert UserRepository(db).has_api_key(user_id)
        assert repo.get_active(user_id)["config_id"] == c1["config_id"]

    def test_owner_isolation(self, db, user_id):
        u2 = UserRepository(db).create(username="bob", password="pw123456")["user_id"]
        repo = ProviderConfigRepository(db)
        repo.create(owner_id=user_id, name="alice-c", api_key="k1",
                    base_url=None, model="m1")
        # bob 看不到 alice 的配置
        assert repo.list_by_owner(u2) == []
        assert repo.get_active(u2) is None

    def test_model_field_in_get_api_key_plain(self, db, user_id):
        """get_api_key_plain 现在返回三元组 (key, base_url, model)。"""
        repo = ProviderConfigRepository(db)
        repo.create(owner_id=user_id, name="c1", api_key="k1",
                    base_url="http://x", model="glm-4.6", activate=True)
        result = UserRepository(db).get_api_key_plain(user_id)
        assert result == ("k1", "http://x", "glm-4.6")

    def test_migration_adds_active_model_column(self, tmp_path):
        """旧库（无 active_model 列）打开后应自动迁移补列。"""
        import sqlite3
        db_path = tmp_path / "old.db"
        # 模拟一个没有 active_model 列的旧 users 表
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT, "
            "password_hash TEXT, is_admin INTEGER, encrypted_api_key TEXT, "
            "api_key_base_url TEXT, disabled INTEGER, workspace_quota INTEGER, "
            "created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO users VALUES ('u1','x','h',0,NULL,NULL,0,5,'t','t')"
        )
        conn.commit()
        conn.close()
        # 用 Database 打开，应触发迁移
        d = Database(db_path, load_master_key(generate_master_key()))
        cols = {row["name"] for row in d.conn.execute("PRAGMA table_info(users)")}
        assert "active_model" in cols
        # 旧行还在，active_model 为 NULL
        row = d.conn.execute("SELECT active_model FROM users WHERE user_id='u1'").fetchone()
        assert row["active_model"] is None
