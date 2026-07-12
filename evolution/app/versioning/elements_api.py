"""elements_api —— 执行端 Agent 要素展示端点。

把 harness_snapshots 里冻结的 config_json 投影成面向展示的结构化视图，
供前端「Agent 要素」页渲染（Prompt/Middleware/Skills/Subagents 四要素）。

端点（/api/snapshots 前缀，复用 snapshot_api 的 router 命名空间）：
  GET /snapshots/{version}/elements   版本要素展示视图（prompt/skills 全文 + description，middleware 元信息 + docstring）
  GET /snapshots/{version}/source     指定文件源码（通用文件读取，保留备用）

设计依据：20260706_150000_Agent要素展示页_设计.md（D1-D7）。
"""
from __future__ import annotations

import ast
import logging
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Query

from app.core.git_ops import show_file
from app.harness_config.class_ref import class_to_source_path
from app.versioning import snapshot_repo

logger = logging.getLogger("evolution.elements_api")

router = APIRouter(prefix="/snapshots", tags=["snapshots"])

# subagent 机器名 → 中文角色名（职责摘要）。
# config 里只有机器名，没有 role 字段；从 prompt 正文提首标题也不可靠
# （正文首标题往往是"核心原则"之类的小节标题，非职责定义）。
# 因此用固定映射表，与 harness 包里 subagents/ 的 build_* 一一对应。
_SUBAGENT_ROLE_MAP: dict[str, str] = {
    "interview": "需求访谈",
    "storybuilding": "故事构建",
    "detail_outline": "细纲生成",
    "writing": "正文写作",
    "general_purpose": "通用助手",
}


# ── 投影逻辑 ────────────────────────────────────────────────────


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """从 markdown 全文提 YAML front matter（首尾 --- 之间）。

    与 app/evolve/docs.py:_load_doc 同构：无 front matter 返回 {}。
    """
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    return yaml.safe_load(parts[1]) or {}


def _build_skill_info(skill_path: str, source_commit: str | None) -> dict[str, Any]:
    """读单个 skill 的 SKILL.md 全文（git show），并解析 frontmatter 的 description。

    Args:
        skill_path:   config 里的 skill 路径（如 "skills/meta/auto-pipeline"）
        source_commit: 快照对应的 git commit；None 时直接标记无源码
    """
    name = skill_path.rstrip("/").split("/")[-1]
    if not source_commit:
        return {"path": skill_path, "name": name, "description": None, "content": None, "load_error": "该版本无 source_commit"}

    file_path = f"{skill_path.rstrip('/')}/SKILL.md"
    try:
        content = show_file(source_commit, file_path)
        description = _parse_frontmatter(content).get("description")
        return {"path": skill_path, "name": name, "description": description, "content": content, "load_error": None}
    except (RuntimeError, Exception) as e:  # noqa: BLE001
        msg = str(e)
        # git show 对不存在文件返回特定错误，精简提示
        if "does not exist" in msg or "exists on disk, but not in" in msg:
            msg = f"{file_path} 在该版本不存在"
        logger.debug("skill 全文读取失败: %s @ %s → %s", file_path, source_commit, msg)
        return {"path": skill_path, "name": name, "description": None, "content": None, "load_error": msg}


def _build_middleware_info(proc: dict, source_commit: str | None) -> dict[str, Any]:
    """单个 processor → middleware 展示元信息。

    预解析 source_path（class_name → middleware/xxx.py），并在有 source_commit 时
    读 .py 顶部模块 docstring 作为 description（用途说明，供前端弹窗展示）。
    读取/解析失败 description=None，不阻断其他 middleware。
    """
    spec = proc.get("spec", {})
    class_name = spec.get("class")
    source_path = class_to_source_path(class_name) if class_name else None

    description: str | None = None
    if source_commit and source_path:
        try:
            src = show_file(source_commit, source_path)
            description = ast.get_docstring(ast.parse(src))
        except (RuntimeError, Exception) as e:  # noqa: BLE001
            logger.debug("middleware docstring 解析失败: %s @ %s → %s", source_path, source_commit, e)

    return {
        "hook": proc.get("hook"),
        "group": proc.get("group"),
        "class_name": class_name,
        "params": spec.get("params", {}),
        "source_path": source_path,
        "description": description,
    }


def _build_agent_view(
    name: str,
    kind: str,
    pipeline: dict,
    source_commit: str | None,
) -> dict[str, Any]:
    """单个 agent pipeline → 展示视图（prompt 全文 + skills 全文 + middleware 元信息）。"""
    slots = pipeline.get("slots", {})

    # Prompt：直接投影 config 里的 body
    prompt_slot = slots.get("system_prompt", {})
    prompt_body = prompt_slot.get("params", {}).get("body", "") if prompt_slot else ""

    # Skills：每个 skill 调 git show 读 SKILL.md 全文（容错）
    skill_paths = slots.get("skills") or []
    skills = [_build_skill_info(p, source_commit) for p in skill_paths]

    # Middleware：元信息 + docstring（用途说明，供前端弹窗）
    processors = pipeline.get("processors", [])
    middlewares = [_build_middleware_info(p, source_commit) for p in processors]

    return {
        "name": name,
        "kind": kind,
        "prompt": {"body": prompt_body},
        "skills": skills,
        "middlewares": middlewares,
    }


def build_elements_view(config: dict, source_commit: str | None) -> dict[str, Any]:
    """config + source_commit → 要素展示视图。

    结构（对齐设计文档接口契约）：
      {
        "source_commit": str | None,
        "has_source": bool,
        "agents": [ {name, kind, prompt, skills, middlewares}, ... ],  # meta 在前
        "subagent_relations": [ {from, to, role}, ... ]
      }

    meta agent 始终排第一；subagents 按 config 里的出现顺序（bootstrap 构造顺序）。
    """
    agents: list[dict[str, Any]] = []

    # meta agent
    meta_pipeline = config.get("meta_pipeline", {})
    agents.append(_build_agent_view("meta", "meta", meta_pipeline, source_commit))

    # subagents（按 config 出现顺序）
    subagents = config.get("subagents", {})
    for sub_name, sub_pipeline in subagents.items():
        agents.append(_build_agent_view(sub_name, "subagent", sub_pipeline, source_commit))

    # subagent_relations：meta → 每个 subagent，role 用固定映射表
    relations = []
    for agent in agents:
        if agent["kind"] != "subagent":
            continue
        role = _SUBAGENT_ROLE_MAP.get(agent["name"], agent["name"])
        relations.append({"from": "meta", "to": agent["name"], "role": role})

    return {
        "source_commit": source_commit,
        "has_source": source_commit is not None,
        "agents": agents,
        "subagent_relations": relations,
    }


# ── 端点 ────────────────────────────────────────────────────────


@router.get("/{version}/elements")
def get_elements(version: int) -> dict[str, Any]:
    """版本要素展示视图。

    返回 config 投影后的结构化要素（prompt/skills 全文已读，middleware 仅元信息）。
    源码全文（middleware .py）由前端懒加载 GET /{version}/source 取。

    - 404: version 不存在或 config_json 为 NULL
    """
    config = snapshot_repo.get_snapshot_config(version)
    if config is None:
        raise HTTPException(status_code=404, detail=f"快照 v{version} 不存在或无 config_json")
    source_commit = snapshot_repo.get_snapshot_source_commit(version)

    return build_elements_view(config, source_commit)


@router.get("/{version}/source")
def get_source(
    version: int,
    path: str = Query(..., description="相对 harness 包根的文件路径，如 middleware/goal.py"),
) -> dict[str, Any]:
    """读指定版本指定文件的源码全文（middleware 懒加载用）。

    - 404: version 不存在 / source_commit 缺失 / 文件在该 commit 不存在
    """
    source_commit = snapshot_repo.get_snapshot_source_commit(version)
    if not source_commit:
        # 区分：version 不存在 vs source_commit 缺失
        snap = snapshot_repo.get_snapshot(version)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"快照 v{version} 不存在")
        raise HTTPException(status_code=404, detail=f"快照 v{version} 无 source_commit（无源码记录）")

    try:
        content = show_file(source_commit, path)
    except (RuntimeError, Exception) as e:  # noqa: BLE001
        msg = str(e)
        if "does not exist" in msg or "exists on disk, but not in" in msg:
            raise HTTPException(status_code=404, detail=f"{path} 在 v{version} 不存在")
        logger.warning("源码读取失败: %s @ v%s → %s", path, version, msg)
        raise HTTPException(status_code=500, detail=f"源码读取失败: {msg}")

    return {"path": path, "content": content}
