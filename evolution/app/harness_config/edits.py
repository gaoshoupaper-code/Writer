"""apply_edits —— harness 配置的编辑算子（Task 1.2）。

实现决策 #9/#10：结构化 edit 指令 {op, target, spec, manifest}，op ∈ {replace,insert,remove}。
target = (agent, section, key)（E2a），定位到 config 的具体位置：
  - section="processors" → key=(hook, group)，编辑 processor
  - section="slots" → key=slot_name，编辑内容型 slot（prompt/skills）

apply_edits 不改原 config（返回新对象，决策 D6a 纯内存计算的保障）。
manifest 字段透传不解析（compose 层不管 manifest，adapt 的 Critic 消费，决策 A5a）。

设计依据：设计文档 #9/#10 + E2a。
"""
from __future__ import annotations

from typing import Any

from . import config as cfg


# ── edit 指令构建 ─────────────────────────────────────────────────


def make_edit(
    op: str,
    agent: str,
    section: str,
    key: Any,
    spec: dict | None = None,
    manifest: dict | None = None,
) -> dict:
    """构建一条 edit 指令。

    Args:
        op:       "replace" | "insert" | "remove"（决策 #10）
        agent:    目标 agent（"meta" 或 subagent 名）
        section:  "processors" | "slots"（E2a）
        key:      processor 的 key=(hook, group)；slot 的 key=slot_name(str)
        spec:     {class, params}（replace/insert 必填，remove 忽略）
        manifest: {intent, expected_up, expected_down, rationale}（adapt A5a，可选）
    """
    if op not in cfg.VALID_OPS:
        raise ValueError(f"非法 op: {op!r}，合法值: {sorted(cfg.VALID_OPS)}")
    if section not in cfg.VALID_SECTIONS:
        raise ValueError(f"非法 section: {section!r}，合法值: {sorted(cfg.VALID_SECTIONS)}")
    edit: dict[str, Any] = {"op": op, "target": [agent, section, key]}
    if spec is not None:
        edit["spec"] = spec
    if manifest is not None:
        edit["manifest"] = manifest
    return edit


# ── apply_edits 核心 ──────────────────────────────────────────────


def apply_edits(config: dict, edits: list[dict]) -> dict:
    """对 config 应用一组 edit 指令，返回新 config（不改原对象，D6a）。

    Args:
        config: 输入 HarnessConfig
        edits:  edit 指令列表，每条 = {op, target:[agent, section, key], spec?, manifest?}

    Returns:
        应用所有 edit 后的新 config（深拷贝，原 config 不变）

    Raises:
        ValueError: edit 指令非法或 target 找不到（replace/remove 时）
    """
    result = cfg.clone(config)
    for i, edit in enumerate(edits):
        _apply_one(result, edit, i)
    cfg.validate(result)
    return result


def _apply_one(config: dict, edit: dict, idx: int) -> None:
    """应用单条 edit（直接修改 config，已 clone 过）。"""
    op = edit.get("op")
    target = edit.get("target")
    if op not in cfg.VALID_OPS:
        raise ValueError(f"edits[{idx}] 非法 op: {op!r}")
    if not isinstance(target, list) or len(target) != 3:
        raise ValueError(f"edits[{idx}] target 必须是 [agent, section, key] 三元组")

    agent, section, key = target
    try:
        pipeline = cfg.get_agent_pipeline(config, agent)
    except KeyError:
        # agent 名非法时，列出所有合法名，方便快速定位（plan 子代理常把 meta 写成 meta-agent）
        valid = ["meta"] + list(config.get("subagents", {}).keys())
        raise ValueError(
            f"edits[{idx}] agent {agent!r} 不是合法 config 键名。"
            f"合法值（必须原样照写）：{valid}。"
            f"常见错误：把 meta 写成 meta-agent。"
        )

    if section == cfg.SECTION_PROCESSORS:
        _apply_to_processors(pipeline, op, key, edit.get("spec"), idx)
    elif section == cfg.SECTION_SLOTS:
        _apply_to_slots(pipeline, op, key, edit.get("spec"), idx)
    else:
        raise ValueError(f"edits[{idx}] 非法 section: {section!r}")


def _apply_to_processors(
    pipeline: dict, op: str, key: Any, spec: dict | None, idx: int
) -> None:
    """对 pipeline.processors 应用 edit。key = (hook, group)。"""
    if not isinstance(key, (list, tuple)) or len(key) != 2:
        raise ValueError(f"edits[{idx}] processor 的 key 必须是 (hook, group)")
    hook, group = key
    processors = pipeline["processors"]
    pos = _find_processor_index(processors, hook, group)

    if op == "replace":
        if pos is None:
            raise ValueError(
                f"edits[{idx}] replace 找不到 (hook={hook!r}, group={group!r})，"
                f"如需新增请用 insert"
            )
        processors[pos] = _build_processor_entry(hook, group, spec, idx)
    elif op == "insert":
        if pos is not None:
            raise ValueError(
                f"edits[{idx}] insert 冲突：(hook={hook!r}, group={group!r}) 已存在，"
                f"如需替换请用 replace"
            )
        processors.append(_build_processor_entry(hook, group, spec, idx))
    elif op == "remove":
        if pos is None:
            raise ValueError(
                f"edits[{idx}] remove 找不到 (hook={hook!r}, group={group!r})"
            )
        processors.pop(pos)


def _apply_to_slots(
    pipeline: dict, op: str, key: Any, spec: dict | None, idx: int
) -> None:
    """对 pipeline.slots 应用 edit。key = slot_name(str)。"""
    if not isinstance(key, str):
        raise ValueError(f"edits[{idx}] slot 的 key 必须是 str（slot_name）")
    slot_name = key
    slots = pipeline["slots"]

    if op == "replace":
        if slot_name not in slots:
            raise ValueError(
                f"edits[{idx}] replace slot 找不到 {slot_name!r}，如需新增请用 insert"
            )
        if spec is None:
            raise ValueError(f"edits[{idx}] replace slot 需要 spec")
        # 校验 spec 形状（如 system_prompt 必须是 {class:"prompt",params:{body:str}}），
        # 避免坏 spec（如 {"content":...}）静默写进 config。
        cfg.validate_slot_spec(slot_name, spec)
        slots[slot_name] = spec
    elif op == "insert":
        if slot_name in slots:
            raise ValueError(
                f"edits[{idx}] insert slot 冲突：{slot_name!r} 已存在，如需替换请用 replace"
            )
        if spec is None:
            raise ValueError(f"edits[{idx}] insert slot 需要 spec")
        cfg.validate_slot_spec(slot_name, spec)
        slots[slot_name] = spec
    elif op == "remove":
        if slot_name not in slots:
            raise ValueError(f"edits[{idx}] remove slot 找不到 {slot_name!r}")
        del slots[slot_name]


# ── 工具 ──────────────────────────────────────────────────────────


def _find_processor_index(processors: list, hook: str, group: str) -> int | None:
    """找 (hook, group) 在 processors 列表中的索引，找不到返回 None。"""
    for i, proc in enumerate(processors):
        if proc.get("hook") == hook and proc.get("group") == group:
            return i
    return None


def _build_processor_entry(hook: str, group: str, spec: dict | None, idx: int) -> dict:
    """从 edit 的 spec 构建 processor 条目（补 hook/group 包装）。"""
    if not isinstance(spec, dict) or "class" not in spec:
        raise ValueError(f"edits[{idx}] processor edit 缺少 spec.class")
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(f"edits[{idx}] spec.params 必须是 dict")
    return {"hook": hook, "group": group, "spec": {"class": spec["class"], "params": params}}


__all__ = ["make_edit", "apply_edits"]
