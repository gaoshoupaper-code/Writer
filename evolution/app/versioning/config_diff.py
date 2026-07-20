"""config_diff —— HarnessConfig 版本间结构化 diff 引擎（版本差异展示功能）。

输入两份 HarnessConfig dict（v(n-1) 和 v(n)），输出按 agent 聚合的三要素 diff：
  - prompt：行级 diff（difflib hunk 序列）
  - skills：路径列表增删
  - processors：按 (hook, group) 配对识别 added/removed/modified

输出结构对应设计文档 D-T4 的 diff_json schema，直接存入 version_changes 表。

agent 名归一化：config 内部用 meta_pipeline + subagents.{name}，本引擎统一以
  meta_pipeline / storybuilding / detail_outline / writing / interview / general_purpose
作为 agent key 输出（与 config 顶层键一致，避免引入第二套命名）。

设计依据：设计文档 D-T2（后端算）/ D-T3（三要素）/ D-T6（hunk 序列）/ D-T7（params 新旧整个）。
"""
from __future__ import annotations

import difflib
from typing import Any

# agent 名归一化映射：design_doc 的 "meta-agent" / "meta" → config 的 "meta_pipeline"
_AGENT_NAME_ALIASES = {
    "meta": "meta_pipeline",
    "meta-agent": "meta_pipeline",
    "meta_pipeline": "meta_pipeline",
}


def _normalize_agent(name: str) -> str:
    """agent 名归一化（meta/meta-agent → meta_pipeline）。其余原样返回。"""
    return _AGENT_NAME_ALIASES.get(name, name)


def _iter_agents(config: dict) -> list[tuple[str, dict]]:
    """展开 config 为 [(agent_name, pipeline), ...]，顺序固定：meta_pipeline 在前。"""
    result = []
    meta = config.get("meta_pipeline")
    if meta is not None:
        result.append(("meta_pipeline", meta))
    for name, pipeline in config.get("subagents", {}).items():
        result.append((name, pipeline))
    return result


# ── 三要素 diff ────────────────────────────────────────────────────


def _extract_prompt_body(pipeline: dict) -> str | None:
    """从 pipeline.slots.system_prompt 提取 prompt 正文。

    system_prompt 形态不固定（validate 不校验 slots 内部，设计断层 3）：
      - 标准：{"class": "prompt", "params": {"body": "..."}}
      - 变体：{"class": "PromptSlot", "params": {"source": "...", "modifications": [...]}}

    提取 body 字段；不存在则返回 None（无法 diff）。
    """
    slot = pipeline.get("slots", {}).get("system_prompt")
    if not isinstance(slot, dict):
        return None
    params = slot.get("params")
    if not isinstance(params, dict):
        return None
    body = params.get("body")
    return body if isinstance(body, str) else None


def _diff_prompt(old_body: str | None, new_body: str | None) -> dict[str, Any] | None:
    """prompt 行级 diff：difflib.SequenceMatcher → hunk 序列。

    两版都无 body 或完全相同 → 返回 None（无变化，不入库）。
    """
    if old_body is None and new_body is None:
        return None
    if old_body == new_body:
        return None

    # 某一版无 prompt：整体增删
    if old_body is None:
        lines = new_body.splitlines()
        return {"hunks": [{"type": "insert", "lines": lines}], "summary": {"added": len(lines), "removed": 0}}
    if new_body is None:
        lines = old_body.splitlines()
        return {"hunks": [{"type": "delete", "lines": lines}], "summary": {"added": 0, "removed": len(lines)}}

    old_lines = old_body.splitlines()
    new_lines = new_body.splitlines()

    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    hunks: list[dict[str, Any]] = []
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            hunks.append({"type": "equal", "lines": old_lines[i1:i2]})
        elif tag == "insert":
            chunk = new_lines[j1:j2]
            hunks.append({"type": "insert", "lines": chunk})
            added += len(chunk)
        elif tag == "delete":
            chunk = old_lines[i1:i2]
            hunks.append({"type": "delete", "lines": chunk})
            removed += len(chunk)
        elif tag == "replace":
            del_chunk = old_lines[i1:i2]
            ins_chunk = new_lines[j1:j2]
            hunks.append({"type": "delete", "lines": del_chunk})
            hunks.append({"type": "insert", "lines": ins_chunk})
            removed += len(del_chunk)
            added += len(ins_chunk)

    if added == 0 and removed == 0:
        return None
    return {"hunks": hunks, "summary": {"added": added, "removed": removed}}


def _diff_skills(old_skills: list[str] | None, new_skills: list[str] | None) -> dict[str, Any] | None:
    """skills 路径列表 diff（集合差）。无变化 → None。"""
    old_set = set(old_skills or [])
    new_set = set(new_skills or [])
    if old_set == new_set:
        return None
    return {
        "added": sorted(new_set - old_set),
        "removed": sorted(old_set - new_set),
        "unchanged_count": len(old_set & new_set),
    }


def _processor_key(proc: dict) -> tuple[str, str]:
    """processor 的身份 key：(hook, group)。"""
    return (proc.get("hook", ""), proc.get("group", ""))


def _diff_processors(
    old_procs: list[dict] | None, new_procs: list[dict] | None
) -> list[dict[str, Any]]:
    """processors diff：按 (hook, group) 配对，识别 added/removed/modified。

    params 变化存新旧整个 dict（D-T7，不做字段级 diff）。
    无变化的 processor 不入结果（减少冗余）。
    """
    old_map = {_processor_key(p): p for p in (old_procs or []) if isinstance(p, dict)}
    new_map = {_processor_key(p): p for p in (new_procs or []) if isinstance(p, dict)}

    changes: list[dict[str, Any]] = []
    all_keys = sorted(set(old_map) | set(new_map))
    for key in all_keys:
        hook, group = key
        old_p = old_map.get(key)
        new_p = new_map.get(key)

        if old_p is None:
            # 新增
            spec = new_p.get("spec", {}) if new_p else {}
            changes.append({
                "key": {"hook": hook, "group": group},
                "change_type": "added",
                "class_change": {"old": None, "new": spec.get("class")},
                "params_change": {"old": None, "new": spec.get("params", {})},
            })
        elif new_p is None:
            # 删除
            spec = old_p.get("spec", {}) if old_p else {}
            changes.append({
                "key": {"hook": hook, "group": group},
                "change_type": "removed",
                "class_change": {"old": spec.get("class"), "new": None},
                "params_change": {"old": spec.get("params", {}), "new": None},
            })
        else:
            # 两版都有：比 class + params
            old_spec = old_p.get("spec", {}) or {}
            new_spec = new_p.get("spec", {}) or {}
            old_class = old_spec.get("class")
            new_class = new_spec.get("class")
            old_params = old_spec.get("params", {}) or {}
            new_params = new_spec.get("params", {}) or {}

            class_changed = old_class != new_class
            params_changed = old_params != new_params
            if not class_changed and not params_changed:
                continue  # 完全相同，跳过

            changes.append({
                "key": {"hook": hook, "group": group},
                "change_type": "modified",
                "class_change": {"old": old_class, "new": new_class},
                "params_change": {"old": old_params, "new": new_params},
            })

    return changes


# ── 单 agent diff 聚合 ─────────────────────────────────────────────


def _diff_pipeline(old_pipeline: dict | None, new_pipeline: dict | None) -> dict[str, Any] | None:
    """计算单个 agent 的三要素 diff。三要素都无变化 → 返回 None（不入库）。"""
    # 整体增删（agent 在某版不存在）
    if old_pipeline is None and new_pipeline is not None:
        new_prompt = _extract_prompt_body(new_pipeline)
        new_skills = new_pipeline.get("slots", {}).get("skills")
        new_procs = new_pipeline.get("processors", [])
        return {
            "prompt": _diff_prompt(None, new_prompt),
            "skills": _diff_skills(None, new_skills) if new_skills else None,
            "processors": _diff_processors(None, new_procs),
            "whole_agent": "added",
        }
    if new_pipeline is None and old_pipeline is not None:
        old_prompt = _extract_prompt_body(old_pipeline)
        old_skills = old_pipeline.get("slots", {}).get("skills")
        old_procs = old_pipeline.get("processors", [])
        return {
            "prompt": _diff_prompt(old_prompt, None),
            "skills": _diff_skills(old_skills, None) if old_skills else None,
            "processors": _diff_processors(old_procs, None),
            "whole_agent": "removed",
        }
    if old_pipeline is None and new_pipeline is None:
        return None

    # 两版都有：逐要素 diff
    prompt_diff = _diff_prompt(_extract_prompt_body(old_pipeline), _extract_prompt_body(new_pipeline))
    old_skills = old_pipeline.get("slots", {}).get("skills")
    new_skills = new_pipeline.get("slots", {}).get("skills")
    skills_diff = _diff_skills(old_skills, new_skills)
    proc_diff = _diff_processors(old_pipeline.get("processors"), new_pipeline.get("processors"))

    if prompt_diff is None and skills_diff is None and not proc_diff:
        return None  # 三要素都没变

    return {
        "prompt": prompt_diff,
        "skills": skills_diff,
        "processors": proc_diff,
    }


# ── 顶层入口 ───────────────────────────────────────────────────────


def compute_diff(config_old: dict | None, config_new: dict) -> dict[str, dict[str, Any]]:
    """计算两份 config 的 diff。

    Args:
        config_old: v(n-1) 的 HarnessConfig dict。None 表示无父版本（首版，返回空）。
        config_new: v(n) 的 HarnessConfig dict。

    Returns:
        {agent_name: diff_dict} —— 只含**有变化**的 agent。
        agent_name 取 config 顶层键（meta_pipeline / storybuilding / ...）。
        diff_dict 结构见模块 docstring / 设计文档 D-T4。
        无父版本或完全无变化 → 返回 {}。
    """
    if config_old is None:
        return {}

    old_agents = dict(_iter_agents(config_old))
    new_agents = dict(_iter_agents(config_new))

    result: dict[str, dict[str, Any]] = {}
    all_names = list(old_agents.keys()) + [n for n in new_agents if n not in old_agents]
    for name in all_names:
        diff = _diff_pipeline(old_agents.get(name), new_agents.get(name))
        if diff is not None:
            result[name] = diff

    return result


def has_changes(agent_diffs: dict[str, dict[str, Any]]) -> bool:
    """diff 结果是否有实质变化（用于决定是否入库）。"""
    return len(agent_diffs) > 0


__all__ = [
    "compute_diff",
    "has_changes",
    "_normalize_agent",
]
