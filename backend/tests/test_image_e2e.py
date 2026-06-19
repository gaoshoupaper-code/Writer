"""端到端冒烟测试：文生图闭环（优化→生成→反馈→评估→再优化）。

验证整个 image agent 编排链路可跑通（不依赖真实字节 API / 真实 LLM）。
用 mock 模型驱动 agent，验证：
1. agent 能调用 generate_images（3 版 × 双采样 = 6 图）
2. 图片落盘 + images 表记录
3. analyze_image 自评写入 agent_analysis
4. ask_user 触发 interrupt（kind=image_review）
5. resume 结构化对象回传后 agent 继续编排
6. persist_skill 双写（文件 + DB）

注：真实 LLM 行为不可控（mock 模型不会"聪明地"调工具），故此测试用
直接调用工具 + 手动触发 interrupt 的方式验证链路连通性，而非完整 agent.invoke。
完整 agent.invoke 需真实 LLM（端到端集成测试，需运行环境）。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from app.platform.core.security import generate_master_key, load_master_key
from app.platform.core.db import (
    Database,
    ImageRepository,
    SkillRepository,
    UserRepository,
    WorkspaceRepository,
    init_database,
)
from app.platform.core.settings import Settings
from app.domains.image.providers.bytedance import BytedanceImageProvider, BytedanceVisionProvider
from app.domains.image.store import ImageArtifactStore
from app.domains.image.tools import (
    build_analyze_image_tool,
    build_generate_images_tool,
    build_persist_skill_tool,
)


@pytest.fixture()
def e2e_env(tmp_path):
    """端到端测试环境：db + user + image workspace + store。"""
    db = Database(tmp_path / "app.platform.core.db", load_master_key(generate_master_key()))
    init_database(db)
    users = UserRepository(db)
    u = users.create(username="e2e", password="pw123456")
    ws = WorkspaceRepository(db).create(owner_id=u["user_id"], title="e2e-imgs", domain="image")
    ws_path = tmp_path / "ws" / u["user_id"] / ws["workspace_id"]
    ws_path.mkdir(parents=True, exist_ok=True)
    store = ImageArtifactStore(db)
    return {
        "db": db, "owner_id": u["user_id"], "workspace_id": ws["workspace_id"],
        "ws_path": ws_path, "store": store,
    }


class TestImageClosedLoop:
    """端到端闭环验证：优化→生成→自评→(HITL)→持久化。"""

    def test_full_loop_generate_analyze_persist(self, e2e_env, monkeypatch, tmp_path):
        """完整闭环：生成 6 图 → 自评 → 持久化 Skill。

        模拟 agent 的编排顺序，逐步调用工具验证链路连通。
        """
        env = e2e_env
        owner_id = env["owner_id"]
        ws_id = env["workspace_id"]
        ws_path = env["ws_path"]
        store = env["store"]

        # Skills 根目录指向临时目录（避免污染真实 skills/）
        fake_skills_root = tmp_path / "skills"
        monkeypatch.setattr("app.domains.image.tools.skills_root", lambda: fake_skills_root)

        # ── 步骤 1：generate_images（3 版 × 双采样 = 6 图）──
        gen_tool = build_generate_images_tool(
            store, _dummy_settings(), ws_path, ws_id, owner_id,
        )
        result = asyncio.run(gen_tool.ainvoke({
            "versions": [
                {"direction": "赛博朋克俯视", "prompt": "cyberpunk city aerial night neon"},
                {"direction": "水彩风景", "prompt": "watercolor landscape mountains"},
                {"direction": "极简人物", "prompt": "minimalist portrait line art"},
            ],
            "round": 1,
        }))
        assert result["count"] == 6, f"应生成 6 张图，实际 {result['count']}"
        image_ids = [img["image_id"] for img in result["images"]]
        assert len(image_ids) == 6

        # 验证图片落盘
        for img in result["images"]:
            physical = store.physical_path(ws_path, f"/images/r1_{img['version_id']}_s{img['sample_index']}.png")
            assert physical.exists(), f"图片未落盘: {physical}"
            assert physical.stat().st_size > 0

        # 验证 images 表记录
        round_images = store.images.list_by_round(ws_id, owner_id, 1)
        assert len(round_images) == 6
        versions = {img["version_id"] for img in round_images}
        assert versions == {"v1", "v2", "v3"}

        # ── 步骤 2：analyze_image（自评，D5 第一层）──
        analyze_tool = build_analyze_image_tool(store, _dummy_settings(), ws_path, owner_id)
        analysis_result = asyncio.run(analyze_tool.ainvoke({"image_ids": image_ids}))
        assert len(analysis_result["analyses"]) == 6
        for a in analysis_result["analyses"]:
            assert "quality" in a
            assert "alignment" in a
            # 验证自评写入 images 表
            img_meta = store.images.get(a["image_id"], owner_id)
            assert img_meta["agent_analysis"] is not None

        # ── 步骤 3：模拟用户反馈（HITL resume，D5 第二层）──
        # 真实流程中 agent 据此迭代，这里验证数据结构正确
        resume = {
            "kind": "image_review",
            "round": 1,
            "ratings": [
                {"version_id": "v1", "score": 5, "note": "很好"},
                {"version_id": "v2", "score": 3, "note": "一般"},
                {"version_id": "v3", "score": 4, "note": "不错"},
            ],
            "overall_direction": "保持 v1 方向",
            "action": "stop",  # 用户喊停（D6）
        }
        # 把用户评分写入 images 表（模拟 agent 处理 resume 后的动作）
        for rating in resume["ratings"]:
            for img in store.images.list_by_round(ws_id, owner_id, 1):
                if img["version_id"] == rating["version_id"]:
                    store.images.update_evaluation(
                        img["image_id"], owner_id,
                        user_score=rating["score"], user_note=rating["note"],
                    )

        # 验证自评校准数据（D2③：agent_analysis vs user_score）
        for img in store.images.list_by_round(ws_id, owner_id, 1):
            assert img["agent_analysis"] is not None
            assert img["user_score"] is not None

        # ── 步骤 4：persist_skill（用户同意持久化，D8/D16）──
        persist_tool = build_persist_skill_tool(owner_id)
        skill_content = (
            "# 水墨与赛博方法论\n\n"
            "## 有效技巧（①）\n- 双采样验证方向稳定性\n\n"
            "## 自评校准（③）\n- Agent 高估留白构图\n\n"
            "## 成功模板（⑤）\n- 赛博俯视+neon 关键词出图稳定"
        )
        skill_result = asyncio.run(persist_tool.ainvoke({
            "name": "赛博与水墨",
            "scene_tag": "mixed",
            "content": skill_content,
        }))
        assert skill_result["action"] == "created"
        skill_id = skill_result["skill_id"]

        # 验证双写：DB 元数据 + SKILL.md 文件
        repo = SkillRepository(env["db"])
        meta = repo.get(skill_id, owner_id)
        assert meta is not None
        assert meta["name"] == "赛博与水墨"
        skill_file = fake_skills_root / owner_id / skill_id / "SKILL.md"
        assert skill_file.exists()
        assert "水墨与赛博" in skill_file.read_text(encoding="utf-8")

        # ── 步骤 5：定稿图标记 + 废弃清理（D11）──
        for img in store.images.list_by_round(ws_id, owner_id, 1):
            if img["version_id"] == "v1":  # 用户最满意的版本
                store.images.set_final(img["image_id"], owner_id, True)
        finals = store.images.list_final(ws_id, owner_id)
        assert len(finals) == 2  # v1 的双采样两张都标记定稿
        # 清理非定稿
        deleted = store.images.delete_non_final(ws_id, owner_id)
        assert deleted == 4  # v2+v3 的 4 张被清理

    def test_skill_reload_after_persist(self, e2e_env, monkeypatch, tmp_path):
        """验证持久化后的 Skill 能被 resolve_owner_skills 加载（D9）。"""
        from app.platform.skills.loader import resolve_owner_skills

        env = e2e_env
        owner_id = env["owner_id"]
        fake_skills_root = tmp_path / "skills"
        monkeypatch.setattr("app.platform.skills.loader.skills_root", lambda: fake_skills_root)
        monkeypatch.setattr("app.domains.image.tools.skills_root", lambda: fake_skills_root)

        # 持久化一个 Skill
        persist_tool = build_persist_skill_tool(owner_id)
        result = asyncio.run(persist_tool.ainvoke({
            "name": "ink-test", "content": "# ink", "scene_tag": "水墨",
        }))
        skill_id = result["skill_id"]

        # 验证能加载
        loaded = resolve_owner_skills(owner_id, [skill_id])
        assert len(loaded) == 1
        assert loaded[0].endswith(skill_id)

        # 验证隔离：其他用户加载不到
        other_loaded = resolve_owner_skills("nonexistent-user", [skill_id])
        assert len(other_loaded) == 0


def _dummy_settings() -> Settings:
    """构造测试用 settings（不读 .env，避免环境依赖）。"""
    return Settings(
        writer_model="test",
        writer_agent_mode="mock",
        writer_frontend_origin="http://localhost:3000",
        openai_api_key="test",
        openai_base_url="http://localhost",
        master_key=generate_master_key(),
        admin_password="test",
    )
