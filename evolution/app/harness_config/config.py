"""HarnessConfig —— harness 的 first-class 配置对象（Task 1.1）。

定义配置对象的 schema：嵌套结构（meta_pipeline + subagents），每个 agent 一个
pipeline 配置。processor 用 (hook, group) 身份定位（决策 D5/#5），spec = {class, params}
（决策 7c）。slots 分资源型/内容型（决策 8a 变体）。

配置对象是纯静态数据（可 JSON 序列化、可 diff、可版本化）。运行时值（model/backend/
workspace）不进配置，由 assemble 从 ctx 注入（决策 D13a/D14b）。

结构概览（决策 #6/#7/#8 + E2a）：

  config = {
    "meta_pipeline": {
      "slots": {                              # 内容型 slot（可进化）
        "system_prompt": {"class":"prompt", "params":{"body":"..."}},
        "skills": ["/skills/meta/auto-pipeline", ...],
      },
      "processors": [                         # processor 列表
        {"hook":"before_model", "group":"goal", "spec":{"class":"GoalMiddleware","params":{}}},
        ...
      ],
    },
    "subagents": {
      "storybuilding": {"slots":{...}, "processors":[...]},
      ...
    }
  }

设计依据：设计文档 D1-D14（compose）+ E2a（target 对齐）。
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

# ── 常量 ──────────────────────────────────────────────────────────

# 合法的 hook 集合（决策 4a 六格，贴合 DeepAgents middleware 可覆盖方法）
VALID_HOOKS = frozenset({
    "before_agent",
    "before_model",
    "wrap_model_call",
    "after_model",
    "wrap_tool_call",
    "after_agent",
})

# 合法的 section 集合（E2a）
SECTION_PROCESSORS = "processors"
SECTION_SLOTS = "slots"
VALID_SECTIONS = frozenset({SECTION_PROCESSORS, SECTION_SLOTS})

# 合法的 op 集合（决策 #10）
VALID_OPS = frozenset({"replace", "insert", "remove"})


# ── 配置对象构建/校验 ──────────────────────────────────────────────


def empty_pipeline() -> dict[str, Any]:
    """返回一个空的 agent pipeline 配置（slots + processors）。"""
    return {"slots": {}, "processors": []}


def empty_config() -> dict[str, Any]:
    """返回一个空的 HarnessConfig（meta_pipeline + 空 subagents）。"""
    return {
        "meta_pipeline": empty_pipeline(),
        "subagents": {},
    }


def make_processor(hook: str, group: str, class_name: str, params: dict | None = None) -> dict:
    """构建一个 processor 条目。

    Args:
        hook:       生命周期 hook（VALID_HOOKS 之一）
        group:      processor 组名（同 hook 同 group 互斥）
        class_name: 类名（如 "GoalMiddleware"），assemble 时按包内约定解析
        params:     静态可调参数（运行时值不进这里，决策 D13a）
    """
    if hook not in VALID_HOOKS:
        raise ValueError(f"非法 hook: {hook!r}，合法值: {sorted(VALID_HOOKS)}")
    return {
        "hook": hook,
        "group": group,
        "spec": {"class": class_name, "params": params or {}},
    }


def make_prompt_slot(body: str) -> dict:
    """构建一个 prompt 类型的内容型 slot（与 processor spec 同构，决策 8a/#9）。"""
    return {"class": "prompt", "params": {"body": body}}


def make_skills_slot(paths: list[str]) -> list[str]:
    """构建 skills 内容型 slot（路径列表，相对包根）。"""
    return list(paths)


# ── slot spec 形状校验（apply_edits / validate 共用，决策 8a）──────────
#
# slot 有两种形态（与 make_prompt_slot / make_skills_slot 对应）：
#   - prompt slot：{"class": "prompt", "params": {"body": str}}
#   - skills slot：list[str]
# 此前 _apply_to_slots 不校验 spec 形状、_validate_pipeline 不校验 slots，
# 导致坏 spec（如 {"content": "..."}）能静默通过，写进 edits.json 又进 config。
# 这两道校验前置后，坏 slot spec 在 apply 阶段即报错（execute 修复），
# 即便漏网，发版 validate 时也能拦住。

# 已知的 prompt 类 slot 名（spec 必须是 {class:"prompt", params:{body:str}}）
_PROMPT_SLOTS = frozenset({"system_prompt"})
# 已知的 skills 类 slot 名（spec 必须是 list[str]）
_SKILLS_SLOTS = frozenset({"skills"})


def validate_slot_spec(slot_name: str, spec: Any) -> None:
    """校验单个 slot 的 spec 形状，不合法 raise ValueError。

    - system_prompt：必须是 {"class": "prompt", "params": {"body": str}}
    - skills：必须是 list[str]
    - 未知 slot 名：放宽（允许自由结构，避免阻塞未来扩展），不报错。

    供 _apply_to_slots（apply 阶段）和 _validate_pipeline（validate 阶段）共用。
    """
    if slot_name in _PROMPT_SLOTS:
        if not isinstance(spec, dict):
            raise ValueError(
                f"slot {slot_name!r} 的 spec 必须是 dict "
                f'{{"class": "prompt", "params": {{"body": str}}}}，'
                f"得到 {type(spec).__name__}"
            )
        if spec.get("class") != "prompt":
            raise ValueError(
                f"slot {slot_name!r} 的 spec.class 必须是 'prompt'，"
                f"得到 {spec.get('class')!r}"
            )
        params = spec.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("body"), str):
            raise ValueError(
                f"slot {slot_name!r} 的 spec.params.body 必须是 str，"
                f"得到 {type(params.get('body') if isinstance(params, dict) else None).__name__}"
            )
    elif slot_name in _SKILLS_SLOTS:
        if not isinstance(spec, list) or not all(isinstance(x, str) for x in spec):
            raise ValueError(
                f"slot {slot_name!r} 的 spec 必须是 list[str]（路径列表）"
            )
    # 未知 slot：放宽，不校验（未来扩展不阻塞）


# ── 序列化 ────────────────────────────────────────────────────────


def to_json(config: dict) -> str:
    """序列化 HarnessConfig 为 JSON 字符串（存 harness_snapshots.config_json）。"""
    validate(config)
    return json.dumps(config, ensure_ascii=False, indent=2)


def from_json(text: str) -> dict:
    """从 JSON 字符串反序列化 HarnessConfig。"""
    config = json.loads(text)
    validate(config)
    return config


def to_file(config: dict, path: Path) -> None:
    """写 HarnessConfig 到文件。"""
    path.write_text(to_json(config), encoding="utf-8")


def from_file(path: Path) -> dict:
    """从文件读 HarnessConfig。"""
    return from_json(path.read_text(encoding="utf-8"))


# ── 校验 ──────────────────────────────────────────────────────────


def validate(config: dict) -> None:
    """校验 HarnessConfig 结构合法性。不合法则 raise ValueError。

    校验项：
      - 顶层含 meta_pipeline / subagents
      - 每个 pipeline 含 slots / processors
      - 每个 processor 含 hook（合法）/ group / spec（含 class+params）
      - (hook, group) 在同一 pipeline 内不重复（互斥，决策 #5）
    """
    if not isinstance(config, dict):
        raise ValueError(f"config 必须是 dict，得到 {type(config).__name__}")

    if "meta_pipeline" not in config:
        raise ValueError("config 缺少 meta_pipeline")
    if "subagents" not in config:
        raise ValueError("config 缺少 subagents")
    if not isinstance(config["subagents"], dict):
        raise ValueError("config.subagents 必须是 dict")

    _validate_pipeline("meta", config["meta_pipeline"])
    for name, pipeline in config["subagents"].items():
        _validate_pipeline(name, pipeline)


def _validate_pipeline(name: str, pipeline: dict) -> None:
    """校验单个 agent pipeline。"""
    if not isinstance(pipeline, dict):
        raise ValueError(f"pipeline[{name}] 必须是 dict")
    if "slots" not in pipeline:
        raise ValueError(f"pipeline[{name}] 缺少 slots")
    if "processors" not in pipeline:
        raise ValueError(f"pipeline[{name}] 缺少 processors")

    processors = pipeline["processors"]
    if not isinstance(processors, list):
        raise ValueError(f"pipeline[{name}].processors 必须是 list")

    seen_keys: set[tuple[str, str]] = set()
    for i, proc in enumerate(processors):
        if not isinstance(proc, dict):
            raise ValueError(f"pipeline[{name}].processors[{i}] 必须是 dict")
        hook = proc.get("hook")
        group = proc.get("group")
        if hook not in VALID_HOOKS:
            raise ValueError(
                f"pipeline[{name}].processors[{i}] 非法 hook: {hook!r}"
            )
        if not group or not isinstance(group, str):
            raise ValueError(
                f"pipeline[{name}].processors[{i}] 缺少/非法 group"
            )
        key = (hook, group)
        if key in seen_keys:
            raise ValueError(
                f"pipeline[{name}] 重复的 (hook,group): {key}（互斥约束，决策 #5）"
            )
        seen_keys.add(key)

        spec = proc.get("spec")
        if not isinstance(spec, dict):
            raise ValueError(f"pipeline[{name}].processors[{i}] 缺少 spec")
        if "class" not in spec or not isinstance(spec["class"], str):
            raise ValueError(f"pipeline[{name}].processors[{i}].spec 缺少 class")
        if "params" not in spec or not isinstance(spec["params"], dict):
            raise ValueError(f"pipeline[{name}].processors[{i}].spec 缺少 params dict")

    # slots 校验（此前缺失：只校验 processors 不校验 slots，坏 slot spec 能漏网）。
    slots = pipeline["slots"]
    if not isinstance(slots, dict):
        raise ValueError(f"pipeline[{name}].slots 必须是 dict")
    for slot_name, slot_spec in slots.items():
        validate_slot_spec(slot_name, slot_spec)


# ── 查询工具（供 adapt/前端用）────────────────────────────────────


def find_processor(pipeline: dict, hook: str, group: str) -> dict | None:
    """在 pipeline 中找 (hook, group) 对应的 processor，找不到返回 None。"""
    for proc in pipeline.get("processors", []):
        if proc.get("hook") == hook and proc.get("group") == group:
            return proc
    return None


def get_agent_pipeline(config: dict, agent: str) -> dict:
    """取指定 agent 的 pipeline。agent="meta" → meta_pipeline。"""
    if agent == "meta":
        return config["meta_pipeline"]
    subagents = config.get("subagents", {})
    if agent not in subagents:
        raise KeyError(f"agent {agent!r} 不在 config.subagents 中")
    return subagents[agent]


def clone(config: dict) -> dict:
    """深拷贝配置（apply_edits 不改原对象的保障）。"""
    return deepcopy(config)


__all__ = [
    "VALID_HOOKS",
    "VALID_SECTIONS",
    "VALID_OPS",
    "SECTION_PROCESSORS",
    "SECTION_SLOTS",
    "empty_pipeline",
    "empty_config",
    "make_processor",
    "make_prompt_slot",
    "make_skills_slot",
    "to_json",
    "from_json",
    "to_file",
    "from_file",
    "validate",
    "find_processor",
    "get_agent_pipeline",
    "clone",
]
