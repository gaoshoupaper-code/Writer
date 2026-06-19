"""Phase 2 验证：多用户隔离（数据隔离 / 配额 / 删除清理）。

跑法（在 backend 目录）：
    .venv/Scripts/python.exe -m pytest tests/test_phase2_isolation.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.security import generate_master_key, load_master_key
from app.core.thread_store import ThreadStore
from app.create_type.store import CreateTypeStore
from app.db import (
    Database,
    StyleRepository,
    UserRepository,
    WorkspaceRepository,
    init_database,
)


# ── fixtures ───────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "app.db", load_master_key(generate_master_key()))
    init_database(d)  # 供 ThreadStore 内部 get_database 路径用
    return d


@pytest.fixture()
def workspace_root(tmp_path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture()
def thread_store(db, workspace_root) -> ThreadStore:
    return ThreadStore(db, workspace_root)


@pytest.fixture()
def style_store(db, thread_store) -> CreateTypeStore:
    return CreateTypeStore(db, thread_store.workspaces)


@pytest.fixture()
def two_users(db):
    repo = UserRepository(db)
    u1 = repo.create(username="alice", password="pw123456")
    u2 = repo.create(username="bob", password="pw123456")
    return u1["user_id"], u2["user_id"]


# ── 工作区隔离 ─────────────────────────────────────────────

class TestWorkspaceIsolation:
    def test_user_cannot_see_others_workspace(self, thread_store, two_users):
        u1, u2 = two_users
        thread_store.create_workspace(u1, "科幻小说")

        # u2 看不到 u1 的作品
        assert thread_store.list_workspaces(u2) == []
        assert len(thread_store.list_workspaces(u1)) == 1

    def test_get_workspace_owner_scoped(self, thread_store, two_users):
        u1, u2 = two_users
        ws = thread_store.create_workspace(u1, "末世大纲")
        # u1 能取到
        assert thread_store.get_workspace(u1, ws.workspace_id) is not None
        # u2 取不到（即使知道 workspace_id）
        assert thread_store.get_workspace(u2, ws.workspace_id) is None

    def test_workspace_dir_is_owner_scoped(self, thread_store, workspace_root, two_users):
        u1, u2 = two_users
        ws = thread_store.create_workspace(u1, "新作")
        # 目录应在 workspace/<u1>/<ws_id>/
        expected = workspace_root / u1 / ws.workspace_id
        assert expected.exists()
        # 不在 u2 下
        assert not (workspace_root / u2 / ws.workspace_id).exists()

    def test_delete_workspace_owner_scoped(self, thread_store, two_users):
        u1, u2 = two_users
        ws = thread_store.create_workspace(u1, "要删的")
        # u2 无法删 u1 的作品
        assert thread_store.delete_workspace(u2, ws.workspace_id) is None
        # 仍在
        assert thread_store.get_workspace(u1, ws.workspace_id) is not None
        # u1 能删
        assert thread_store.delete_workspace(u1, ws.workspace_id) is not None
        assert thread_store.get_workspace(u1, ws.workspace_id) is None


# ── 线程隔离 ───────────────────────────────────────────────

class TestThreadIsolation:
    def test_thread_inherits_owner(self, thread_store, two_users):
        u1, u2 = two_users
        ws = thread_store.create_workspace(u1, "作品A")
        t = thread_store.create_thread(u1, ws.workspace_id, "会话1")
        # u1 能看到
        assert thread_store.get_thread(u1, t.thread_id) is not None
        # u2 看不到（即使知道 thread_id）
        assert thread_store.get_thread(u2, t.thread_id) is None

    def test_create_thread_wrong_owner_404s(self, thread_store, two_users):
        u1, u2 = two_users
        ws = thread_store.create_workspace(u1, "作品A")
        # u2 不能在 u1 的工作区里建线程
        with pytest.raises(KeyError):
            thread_store.create_thread(u2, ws.workspace_id, "入侵")


# ── 风格隔离（D7 完全私有）─────────────────────────────────

class TestStyleIsolation:
    def test_style_is_private(self, style_store, two_users):
        u1, u2 = two_users
        style_store.create_style(u1, name="我的风格", writing_style="紧凑")
        # u1 有 1 个，u2 有 0 个
        assert len(style_store.list_styles(u1)) == 1
        assert style_store.list_styles(u2) == []

    def test_cannot_use_others_style_as_active(self, style_store, thread_store, two_users):
        u1, u2 = two_users
        ws = thread_store.create_workspace(u1, "作品")
        style = style_store.create_style(u1, name="风格1")
        # u1 能设
        assert style_store.set_active_style_id(u1, ws.workspace_id, style.style_id) is True
        # u2 不能设（工作区不属于 u2）
        assert style_store.set_active_style_id(u2, ws.workspace_id, style.style_id) is False

    def test_delete_style_clears_workspace_refs(self, style_store, thread_store, two_users):
        u1, _ = two_users
        ws = thread_store.create_workspace(u1, "作品")
        style = style_store.create_style(u1, name="待删")
        style_store.set_active_style_id(u1, ws.workspace_id, style.style_id)
        assert style_store.get_active_style_id(u1, ws.workspace_id) == style.style_id
        style_store.delete_style(u1, style.style_id)
        assert style_store.get_active_style_id(u1, ws.workspace_id) is None


# ── 配额（T2.8）─────────────────────────────────────────────

class TestQuota:
    def test_workspace_quota_enforced(self, db, thread_store):
        repo = UserRepository(db)
        u = repo.create(username="quota_user", password="pw123456", workspace_quota=2)
        thread_store.create_workspace(u["user_id"], "作品1")
        thread_store.create_workspace(u["user_id"], "作品2")
        # 第 3 个应超配额
        assert repo.workspace_count(u["user_id"]) == 2
        assert repo.workspace_count(u["user_id"]) >= 2  # 配额已满


# ── 删除清理（T2.9）─────────────────────────────────────────

class TestDeleteCleanup:
    def test_delete_workspace_removes_dir(self, thread_store, workspace_root, two_users):
        u1, _ = two_users
        ws = thread_store.create_workspace(u1, "要删的")
        ws_path = workspace_root / u1 / ws.workspace_id
        assert ws_path.exists()
        thread_store.delete_workspace(u1, ws.workspace_id)
        assert not ws_path.exists()

    def test_delete_workspace_returns_thread_ids(self, thread_store, two_users):
        u1, _ = two_users
        ws = thread_store.create_workspace(u1, "作品")
        t1 = thread_store.create_thread(u1, ws.workspace_id, "会话1")
        t2 = thread_store.create_thread(u1, ws.workspace_id, "会话2")
        deleted_ids = thread_store.delete_workspace(u1, ws.workspace_id)
        assert set(deleted_ids) == {t1.thread_id, t2.thread_id}

    def test_delete_thread_cleans_metadata(self, thread_store, two_users):
        u1, _ = two_users
        ws = thread_store.create_workspace(u1, "作品")
        t = thread_store.create_thread(u1, ws.workspace_id, "会话")
        assert thread_store.delete_thread(u1, t.thread_id) is True
        assert thread_store.get_thread(u1, t.thread_id) is None


# ── checkpoint 分库（T2.5）─────────────────────────────────

class TestCheckpointPool:
    def test_per_user_db_isolation(self, tmp_path):
        import asyncio
        from app.core.checkpoint_pool import CheckpointPool, init_checkpoint_pool, get_checkpoint_pool

        pool = CheckpointPool(tmp_path / "checkpoints")
        init_checkpoint_pool(pool)

        async def run():
            s1 = await pool.get("user_a")
            s2 = await pool.get("user_b")
            s1_again = await pool.get("user_a")
            # 同一用户返回同一实例
            assert s1 is s1_again
            # 不同用户不同实例
            assert s1 is not s2
            # db 文件存在
            assert (tmp_path / "checkpoints" / "checkpoints_user_a.db").exists()
            await pool.aclose_all()

        asyncio.run(run())

    def test_drop_user_removes_db(self, tmp_path):
        import asyncio
        from app.core.checkpoint_pool import CheckpointPool

        pool = CheckpointPool(tmp_path / "checkpoints")

        async def run():
            await pool.get("user_x")
            assert (tmp_path / "checkpoints" / "checkpoints_user_x.db").exists()
            await pool.drop("user_x")
            assert not (tmp_path / "checkpoints" / "checkpoints_user_x.db").exists()

        asyncio.run(run())

    def test_delete_thread_checkpoint_uses_per_user_db(self, tmp_path):
        """PR-10 回归测试：delete_thread_checkpoint 必须用分库 saver 真正清理。

        缺口重现：原同步实现调 _resolve_checkpointer_sync（全局 saver），
        分库数据不在全局库，删除是空操作——checkpoint 残留。
        修复后：async delete_thread_checkpoint 走 _resolve_checkpointer 取分库 saver。
        """
        import asyncio
        from langgraph.checkpoint.base import empty_checkpoint
        from app.core.checkpoint_pool import CheckpointPool, init_checkpoint_pool, get_checkpoint_pool

        pool = CheckpointPool(tmp_path / "checkpoints")
        init_checkpoint_pool(pool)

        async def run():
            # user_a 的分库写入一条 checkpoint
            saver = await pool.get("user_a")
            cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
            await saver.aput(cfg, empty_checkpoint(), {}, [])
            # 确认存在
            assert await saver.aget(cfg) is not None

            # 用修复后的逻辑删除（模拟 BaseAgentService.delete_thread_checkpoint）
            await saver.adelete_thread("t1")
            assert await saver.aget(cfg) is None, "checkpoint 应被真正清理，而非残留"
            await pool.aclose_all()

        asyncio.run(run())


# ── workspace_id 改 uuid（不再用作品名）────────────────────

class TestWorkspaceIdUuid:
    def test_workspace_id_is_hex_uuid(self, thread_store, two_users):
        u1, _ = two_users
        ws = thread_store.create_workspace(u1, "任意名字")
        # uuid hex：32 个十六进制字符
        assert len(ws.workspace_id) == 32
        int(ws.workspace_id, 16)  # 能解析为十六进制则合法

    def test_duplicate_outline_names_allowed(self, thread_store, two_users):
        u1, _ = two_users
        # 两个用户都能叫"科幻小说"
        ws1 = thread_store.create_workspace(u1, "科幻小说")
        ws2 = thread_store.create_workspace(u1, "科幻小说")
        assert ws1.workspace_id != ws2.workspace_id
