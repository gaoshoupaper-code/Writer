"""Phase 3 验证：image domain（DB / store / tools / skill loader / provider 占位）。

跑法（在 backend 目录）：
    python -m pytest tests/test_image_domain.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.platform.core.security import generate_master_key, load_master_key
from app.db import (
    Database,
    ImageRepository,
    SkillRepository,
    UserRepository,
    WorkspaceRepository,
    init_database,
)
from app.domains.image.providers.bytedance import BytedanceImageProvider, BytedanceVisionProvider
from app.domains.image.store import ImageArtifactStore
from app.domains.image.tools import (
    build_analyze_image_tool,
    build_generate_images_tool,
    build_persist_skill_tool,
    skills_root,
)
from app.platform.skills.loader import resolve_owner_skills


# ── fixtures ───────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "app.db", load_master_key(generate_master_key()))
    init_database(d)
    return d


@pytest.fixture()
def image_workspace(db, tmp_path):
    """建 user + image domain workspace，返回 (owner_id, workspace_id, ws_path)。"""
    users = UserRepository(db)
    u = users.create(username="imguser", password="pw123456")
    ws_repo = WorkspaceRepository(db)
    ws = ws_repo.create(owner_id=u["user_id"], title="test-imgs", domain="image")
    ws_path = tmp_path / "workspace" / u["user_id"] / ws["workspace_id"]
    ws_path.mkdir(parents=True, exist_ok=True)
    return u["user_id"], ws["workspace_id"], ws_path


# ── DB 层：images / skills 表 ──────────────────────────────


class TestImageRepository:
    def test_create_and_get(self, db, image_workspace):
        owner_id, ws_id, _ = image_workspace
        repo = ImageRepository(db)
        img = repo.create(
            image_id="i1", workspace_id=ws_id, owner_id=owner_id,
            round=1, version_id="v1", sample_index=1,
            direction="cyberpunk", prompt="test", file_path="/images/i1.png",
        )
        assert img["image_id"] == "i1"
        got = repo.get("i1", owner_id)
        assert got["prompt"] == "test"
        assert got["is_final"] == 0

    def test_update_evaluation(self, db, image_workspace):
        owner_id, ws_id, _ = image_workspace
        repo = ImageRepository(db)
        repo.create(
            image_id="i2", workspace_id=ws_id, owner_id=owner_id,
            round=1, version_id="v1", sample_index=1,
            direction="", prompt="", file_path="/images/i2.png",
        )
        repo.update_evaluation("i2", owner_id, agent_analysis="good", user_score=5, user_note="great")
        got = repo.get("i2", owner_id)
        assert got["agent_analysis"] == "good"
        assert got["user_score"] == 5
        assert got["user_note"] == "great"

    def test_set_final_and_cleanup(self, db, image_workspace):
        owner_id, ws_id, _ = image_workspace
        repo = ImageRepository(db)
        for iid in ("f1", "f2", "d1", "d2"):
            repo.create(
                image_id=iid, workspace_id=ws_id, owner_id=owner_id,
                round=1, version_id="v1", sample_index=1,
                direction="", prompt="", file_path=f"/images/{iid}.png",
            )
        repo.set_final("f1", owner_id, True)
        repo.set_final("f2", owner_id, True)
        assert len(repo.list_final(ws_id, owner_id)) == 2
        deleted = repo.delete_non_final(ws_id, owner_id)
        assert deleted == 2
        assert len(repo.list_final(ws_id, owner_id)) == 2

    def test_owner_isolation(self, db, image_workspace):
        owner_id, ws_id, _ = image_workspace
        users = UserRepository(db)
        other = users.create(username="other", password="pw123456")
        repo = ImageRepository(db)
        repo.create(
            image_id="iso", workspace_id=ws_id, owner_id=owner_id,
            round=1, version_id="v1", sample_index=1,
            direction="", prompt="", file_path="/images/iso.png",
        )
        # other 用户看不到 owner 的图
        assert repo.get("iso", other["user_id"]) is None
        assert repo.get_any("iso") is not None  # get_any 不过滤 owner


class TestSkillRepository:
    def test_create_and_bump(self, db, image_workspace):
        owner_id, _, _ = image_workspace
        repo = SkillRepository(db)
        sk = repo.create(owner_id=owner_id, name="ink", scene_tag="水墨")
        assert sk["revision_count"] == 0
        repo.bump_revision(sk["skill_id"], owner_id)
        repo.bump_revision(sk["skill_id"], owner_id)
        assert repo.get(sk["skill_id"], owner_id)["revision_count"] == 2

    def test_list_and_delete(self, db, image_workspace):
        owner_id, _, _ = image_workspace
        repo = SkillRepository(db)
        repo.create(owner_id=owner_id, name="a")
        repo.create(owner_id=owner_id, name="b")
        assert len(repo.list_by_owner(owner_id)) == 2
        sks = repo.list_by_owner(owner_id)
        assert repo.delete(sks[0]["skill_id"], owner_id) is True
        assert len(repo.list_by_owner(owner_id)) == 1


# ── Provider 占位 ─────────────────────────────────────────


class TestBytedanceProviders:
    def test_image_provider_generates_png(self):
        provider = BytedanceImageProvider()
        images = asyncio.run(
            provider.generate("cyberpunk city", n=2, seed=42)
        )
        assert len(images) == 2
        assert all(img.format == "png" for img in images)
        assert all(len(img.image_data) > 0 for img in images)
        # 同 seed 同 prompt 应出相同图（确定性）
        images2 = asyncio.run(
            provider.generate("cyberpunk city", n=1, seed=42)
        )
        assert images[0].image_data == images2[0].image_data

    def test_vision_provider_returns_analysis(self):
        provider = BytedanceVisionProvider()
        result = asyncio.run(
            provider.analyze(b"\x89PNG fake", prompt="test prompt")
        )
        assert isinstance(result.quality_assessment, str)
        assert isinstance(result.prompt_alignment, str)


# ── Store + Skill Loader ──────────────────────────────────


class TestImageArtifactStore:
    def test_save_and_physical_path(self, db, image_workspace, tmp_path):
        owner_id, ws_id, ws_path = image_workspace
        store = ImageArtifactStore(db)
        vpath = store.save_image(ws_path, b"\x89PNG data", "png", 1, "v1", 1)
        assert vpath == "/images/r1_v1_s1.png"
        physical = store.physical_path(ws_path, vpath)
        assert physical.exists()
        assert physical.read_bytes() == b"\x89PNG data"


class TestSkillLoader:
    def test_resolve_empty_returns_empty(self):
        assert resolve_owner_skills("nobody", None) == []
        assert resolve_owner_skills("nobody", []) == []

    def test_resolve_filters_missing(self, db, image_workspace, monkeypatch, tmp_path):
        owner_id, _, _ = image_workspace
        # 指向临时 skills 根
        fake_root = tmp_path / "skills"
        monkeypatch.setattr(
            "app.platform.skills.loader.skills_root", lambda: fake_root
        )
        repo = SkillRepository(db)
        sk = repo.create(owner_id=owner_id, name="ink")
        # DB 有记录但文件不存在 → 跳过
        assert resolve_owner_skills(owner_id, [sk["skill_id"]]) == []
        # 创建文件后 → 返回路径
        skill_dir = fake_root / owner_id / sk["skill_id"]
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# ink", encoding="utf-8")
        result = resolve_owner_skills(owner_id, [sk["skill_id"]])
        assert len(result) == 1
        assert result[0].endswith(sk["skill_id"])


# ── 工具构建（验证闭包能正确捕获上下文）────────────────


class TestToolBuilders:
    def test_build_generate_images_tool(self, db, image_workspace):
        owner_id, ws_id, ws_path = image_workspace
        from app.platform.core.settings import get_settings
        store = ImageArtifactStore(db)
        tool = build_generate_images_tool(
            store, get_settings(), ws_path, ws_id, owner_id,
        )
        assert tool.name == "generate_images"

    def test_build_persist_skill_tool_writes_file_and_db(self, db, image_workspace, monkeypatch, tmp_path):
        owner_id, _, _ = image_workspace
        fake_root = tmp_path / "skills"
        monkeypatch.setattr("app.domains.image.tools.skills_root", lambda: fake_root)
        tool = build_persist_skill_tool(owner_id)
        result = asyncio.run(
            tool.ainvoke({"name": "ink-test", "content": "# 水墨方法论\n...", "scene_tag": "水墨"})
        )
        assert result["action"] == "created"
        skill_id = result["skill_id"]
        # 文件存在
        assert (fake_root / owner_id / skill_id / "SKILL.md").exists()
        # DB 有记录
        repo = SkillRepository(db)
        assert repo.get(skill_id, owner_id) is not None
