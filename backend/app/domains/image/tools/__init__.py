"""image domain 工具集（供 image-agent 调用）。

三个工具对应闭环的核心能力（DD3）：
- ``generate_images``：3 版 × 双采样生图（D4）
- ``analyze_image``：视觉自评（D5 第一层，D14）
- ``persist_skill``：经验沉淀成 SKILL.md（D8/D16）

工具通过闭包捕获 ImageArtifactStore / providers / workspace 上下文，
在 agent 执行时按需调用。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.db import SkillRepository, get_database
from app.domains.image.store import ImageArtifactStore, resolve_image_provider, resolve_vision_provider
from app.core.settings import Settings


# ════════════════════════════════════════════════════════════
# generate_images：3 版 × 双采样生图（D4）
# ════════════════════════════════════════════════════════════


class GenerateImagesInput(BaseModel):
    """生图工具入参：3 个版本的提示词（每版一段），每版双采样。"""

    versions: list[VersionPrompt] = Field(
        min_length=3, max_length=3,
        description="3 个版本的提示词，每个含 direction（方向描述）和 prompt（生图提示词）",
    )
    round: int = Field(ge=1, description="当前轮次编号（第几轮迭代）")


class VersionPrompt(BaseModel):
    """单版提示词。"""

    direction: str = Field(description="这版的方向描述（如'赛博朋克俯视'）")
    prompt: str = Field(min_length=1, description="生图提示词正文")


def build_generate_images_tool(
    store: ImageArtifactStore,
    settings: Settings,
    workspace_path: Path,
    workspace_id: str,
    owner_id: str,
) -> StructuredTool:
    """构建 generate_images 工具。

    闭包捕获 workspace 上下文，agent 调用时只需传 versions + round。
    """
    async def _generate(versions: list[dict], round: int) -> dict:
        provider = resolve_image_provider(owner_id, settings)
        results: list[dict] = []
        for vi, ver in enumerate(versions, start=1):
            version_id = f"v{vi}"
            # 兼容 dict（直接调用）和 pydantic model（StructuredTool 调用）两种入参
            direction = ver.get("direction", "") if isinstance(ver, dict) else getattr(ver, "direction", "")
            prompt = ver.get("prompt", "") if isinstance(ver, dict) else getattr(ver, "prompt", "")
            # 双采样：同提示词不同 seed 调 2 次（D4）
            for sample_index in (1, 2):
                seed = hash((prompt, round, vi, sample_index)) & 0x7FFFFFFF
                images = await provider.generate(prompt, n=1, seed=seed)
                if not images:
                    continue
                img = images[0]
                image_id = uuid.uuid4().hex
                vpath = store.save_image(
                    workspace_path, img.image_data, img.format,
                    round, version_id, sample_index,
                )
                store.images.create(
                    image_id=image_id, workspace_id=workspace_id, owner_id=owner_id,
                    round=round, version_id=version_id, sample_index=sample_index,
                    direction=direction, prompt=prompt, file_path=vpath,
                )
                results.append({
                    "image_id": image_id, "version_id": version_id,
                    "sample_index": sample_index, "direction": direction,
                    "prompt": prompt, "url": f"/api/images/{image_id}",
                })
        return {"round": round, "images": results, "count": len(results)}

    return StructuredTool.from_function(
        name="generate_images",
        description=(
            "生成 3 版图片，每版双采样（共 6 张）。传入 3 个版本的提示词（direction + prompt），"
            "每个版本用同提示词不同随机种子生成 2 张。图片自动落盘并记录元数据。"
            "返回各图的 image_id 和 url，供后续 analyze_image 和用户评审用。"
        ),
        func=lambda **kw: _generate(**kw),
        coroutine=_generate,
        args_schema=GenerateImagesInput,
        infer_schema=False,
    )


# ════════════════════════════════════════════════════════════
# analyze_image：视觉自评（D5 第一层，D14）
# ════════════════════════════════════════════════════════════


class AnalyzeImageInput(BaseModel):
    """视觉自评入参。"""

    image_ids: list[str] = Field(
        min_length=1, description="待分析的 image_id 列表（通常是一轮的 6 张）"
    )


def build_analyze_image_tool(
    store: ImageArtifactStore,
    settings: Settings,
    workspace_path: Path,
    owner_id: str,
) -> StructuredTool:
    """构建 analyze_image 工具（D5 第一层自评）。"""
    async def _analyze(image_ids: list[str]) -> dict:
        provider = resolve_vision_provider(owner_id, settings)
        analyses: list[dict] = []
        for image_id in image_ids:
            img_meta = store.images.get(image_id, owner_id)
            if not img_meta:
                analyses.append({"image_id": image_id, "error": "not found"})
                continue
            physical = store.physical_path(workspace_path, img_meta["file_path"])
            if not physical.exists():
                analyses.append({"image_id": image_id, "error": "file missing"})
                continue
            image_data = physical.read_bytes()
            prompt = (
                "评估这张图的整体质量（构图、清晰度、是否有伪影/畸形）"
                "以及与原始提示词的匹配度。提示词：" + (img_meta.get("prompt") or "")
            )
            analysis = await provider.analyze(image_data, prompt=prompt)
            # 自评结果写入 images 表（agent_analysis 字段）
            store.images.update_evaluation(
                image_id, owner_id, agent_analysis=(
                    f"质量：{analysis.quality_assessment}\n匹配度：{analysis.prompt_alignment}"
                ),
            )
            analyses.append({
                "image_id": image_id,
                "version_id": img_meta["version_id"],
                "quality": analysis.quality_assessment,
                "alignment": analysis.prompt_alignment,
            })
        return {"analyses": analyses}

    return StructuredTool.from_function(
        name="analyze_image",
        description=(
            "对指定图片做视觉自评（D5 第一层）。评估整体质量（构图/清晰度/伪影）"
            "和与提示词的匹配度（D14）。结果写入图片元数据，供用户评审时参考。"
            "调用时机：generate_images 之后、请求用户评审之前。"
        ),
        func=lambda **kw: _analyze(**kw),
        coroutine=_analyze,
        args_schema=AnalyzeImageInput,
        infer_schema=False,
    )


# ════════════════════════════════════════════════════════════
# persist_skill：经验沉淀成 SKILL.md（D8/D16）
# ════════════════════════════════════════════════════════════


SKILLS_ROOT_NAME = "skills"  # backend/skills/<owner>/<skill_id>/SKILL.md（DD7b）


def skills_root() -> Path:
    """Skills 自进化系统的根目录（与 workspace 平级，DD7b）。"""
    # backend/skills/（相对 backend 包根的上一级）
    return Path(__file__).resolve().parents[3] / SKILLS_ROOT_NAME


class PersistSkillInput(BaseModel):
    """持久化 Skill 入参。"""

    name: str = Field(min_length=1, max_length=50, description="Skill 名称（如'水墨人物'）")
    scene_tag: str = Field(default="", description="场景标签（如'水墨'，D7 按类别分）")
    content: str = Field(min_length=1, description="SKILL.md 正文内容（方法论、技巧、模板、校准）")
    skill_id: str | None = Field(
        default=None,
        description="更新已有 Skill 时传其 skill_id；新建则留空",
    )


def build_persist_skill_tool(owner_id: str) -> StructuredTool:
    """构建 persist_skill 工具（D8/D16）。

    双写一致性（DD7b）：同时写 SKILL.md 文件 + skills 表元数据。
    """
    async def _persist(
        name: str, content: str, scene_tag: str = "", skill_id: str | None = None,
    ) -> dict:
        repo = SkillRepository(get_database())
        root = skills_root() / owner_id
        root.mkdir(parents=True, exist_ok=True)

        if skill_id:
            # 更新已有 Skill
            existing = repo.get(skill_id, owner_id)
            if not existing:
                return {"error": f"skill not found: {skill_id}"}
            skill_dir = root / skill_id
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            repo.update(skill_id, owner_id, name=name, scene_tag=scene_tag or None)
            repo.bump_revision(skill_id, owner_id)
            return {"skill_id": skill_id, "name": name, "action": "updated"}

        # 新建 Skill
        meta = repo.create(owner_id=owner_id, name=name, scene_tag=scene_tag or None)
        new_id = meta["skill_id"]
        skill_dir = root / new_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return {"skill_id": new_id, "name": name, "action": "created"}

    return StructuredTool.from_function(
        name="persist_skill",
        description=(
            "把本轮优化经验持久化成一个 Skill（D8/D16）。写入 SKILL.md 文件 + 元数据。"
            "content 应包含：有效的提示词技巧（①）、Agent 自评校准（③）、成功模板（⑤）。"
            "闭环结束、用户同意持久化时调用。更新已有 Skill 传 skill_id，新建则留空。"
        ),
        func=lambda **kw: _persist(**kw),
        coroutine=_persist,
        args_schema=PersistSkillInput,
        infer_schema=False,
    )


__all__ = [
    "GenerateImagesInput",
    "VersionPrompt",
    "build_generate_images_tool",
    "AnalyzeImageInput",
    "build_analyze_image_tool",
    "PersistSkillInput",
    "build_persist_skill_tool",
    "skills_root",
]
