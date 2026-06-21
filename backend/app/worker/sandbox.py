"""沙箱验证（Phase 4 T4.2，S7 临时容器）。

职责：候选 harness 代码进 A/B 前的「能加载 + 不崩」验证。
两步：
  1. 实例化检查：load_harness_instance（语法/import/有 WriterHarness 子类/可实例化）
  2. 契约方法冒烟：调 build_* 方法验证返回类型符合契约（C2/C3），不抛异常

完整沙箱（起 Docker 容器跑测试集验证不崩）依赖容器部署，本模块做实例化 + 冒烟
（纯 Python 可测）。容器化后可在此模块加「起容器跑测试集」的扩展。

与 monitoring 的 static_check 分工：
  - static_check（monitoring）：纯 AST + 正则，不需 backend 环境
  - sandbox（backend）：实例化 + 冒烟，需要 backend 基类环境

设计依据：设计文档 D4（沙箱）+ C1（需真实生成链路）+ harness 契约。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("sandbox")


def validate_candidate(code_path: str | Path) -> tuple[bool, list[str]]:
    """沙箱验证候选 harness：实例化 + 契约方法冒烟。

    Args:
        code_path: 候选 harness.py 文件路径

    Returns: (passed, errors)
    """
    from app.worker.server import load_harness_instance, HarnessLoadError
    from app.platform.harness import HarnessContext

    errors: list[str] = []

    # 1. 实例化检查（load_harness_instance 已含语法/import/子类检查）
    try:
        instance = load_harness_instance(code_path)
    except HarnessLoadError as exc:
        return False, [f"加载失败: {exc}"]
    except Exception as exc:
        return False, [f"实例化异常: {exc}"]

    # 2. 契约方法冒烟：调 build_* 验证返回类型 + 不抛
    ctx = HarnessContext(workspace_path=Path(code_path).parent)

    # build_system_prompt → str
    try:
        prompt = instance.build_system_prompt(ctx)
        if not isinstance(prompt, str):
            errors.append("[C1] build_system_prompt 必须返回 str")
    except Exception as exc:
        errors.append(f"[C1] build_system_prompt 调用失败: {exc}")

    # build_middleware → list
    try:
        mw = instance.build_middleware(ctx)
        if not isinstance(mw, list):
            errors.append("[C2] build_middleware 必须返回 list")
    except Exception as exc:
        errors.append(f"[C2] build_middleware 调用失败: {exc}")

    # build_skills → list[str]
    try:
        skills = instance.build_skills(ctx)
        if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
            errors.append("[C3] build_skills 必须返回 list[str]")
    except Exception as exc:
        errors.append(f"[C3] build_skills 调用失败: {exc}")

    # build_subagents → list
    try:
        subs = instance.build_subagents(ctx)
        if not isinstance(subs, list):
            errors.append("[C4] build_subagents 必须返回 list")
    except Exception as exc:
        errors.append(f"[C4] build_subagents 调用失败: {exc}")

    return len(errors) == 0, errors


def smoke_test_generation(code_path: str | Path, test_request: str) -> tuple[bool, str]:
    """完整沙箱：起容器跑一次生成验证不崩。

    ⚠️ 待接通：需要 Docker 容器 + backend 生成链路。
    本函数是占位，标注完整实现路径。

    完整实现（容器化后）：
      1. 起 Docker 容器加载该 harness 版本
      2. 用 test_request 跑一次 generate_stream
      3. 验证不抛异常 + 产出有效（含 outline.md 等）
    """
    # 先做实例化 + 契约冒烟
    ok, errors = validate_candidate(code_path)
    if not ok:
        return False, "; ".join(errors)

    # TODO（Docker 部署后）：起容器跑真实生成
    logger.info("沙箱冒烟通过（实例化+契约），完整生成验证待容器化接通: %s", code_path)
    return True, "实例化+契约冒烟通过（完整生成验证待容器化）"
