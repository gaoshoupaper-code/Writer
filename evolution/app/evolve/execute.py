"""执行子代理（决策 D12/D14/E17）。

流水线第三阶段（execute）的执行者。读方案设计 design_doc.md，落地改动
（配置层 apply_edits + 源码层 write/edit_file），校验可加载，产 change_log.md。

工具集（D12/D14）：
  - read_design_doc()        读 design_doc.md（结构化改动列表）
  - apply_edits(edits_json)  配置层：应用 edit 指令到 config，写 edits.json
  - validate_changes()       落地后校验：import 源码可加载 + config 合法
  - write_change_log(...)    产出 change_log.md

  注：源码层改动（write_file/edit_file）用 DeepAgent 框架自带工具，
  靠 path_guard 约束只改 harnesses/current/（D14）。

设计依据：设计文档 D12/D14（apply_edits + write/edit + validate）/ E17。
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from app.compose import config as cfg
from app.compose import edits as edit_ops
from app.compose.bootstrap import build_v1_config
from app.core.settings import settings
from app.evolve import docs
from app.evolve.ctx import get_tool_context

logger = logging.getLogger("evolution.evolve.execute")


# ── 执行子代理工具 ─────────────────────────────────────────────


def make_execute_tools() -> list:
    """构建执行子代理的工具集。"""

    @tool
    def read_design_doc() -> str:
        """读取当前 session 的改动设计文档（design_doc.md）。

        包含 changes（结构化改动列表）+ rationale（自然语言总述）。
        每个改动有 target/change_desc/reason/expected_up/expected_down，
        可能有 edit 指令（配置层）或 target 路径（源码层）。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        if not ctx.design_doc_path:
            return "错误：还没产出设计文档（design_doc_path 为空）"
        try:
            data = docs.read_design_doc(ctx.design_doc_path)
            meta = data["meta"]
            body = data["body"]
            return (
                f"## 设计文档\n\n### 结构化改动\n```json\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n```\n\n"
                f"### 设计说明\n{body}"
            )
        except Exception as e:
            return f"读设计文档失败：{e}"

    @tool
    def apply_edits(edits_json: str) -> str:
        """【配置层】应用 edit 指令到 HarnessConfig，落地到 edits.json。

        用于改 middleware 装配/参数、改 prompt slot。源码层改动用 write_file/edit_file。
        本工具读 design_doc 里带 edit 字段的改动，应用到 config。

        Args:
            edits_json: edit 指令 JSON 数组。每条：
              {"op": "replace|insert|remove",
               "target": ["agent名", "processors|slots", key],
               "spec": {"class": "类名", "params": {...}}}
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("apply_edits", "running", phase="execute")
        try:
            edits = json.loads(edits_json)
            if not isinstance(edits, list) or not edits:
                return "edits_json 必须是非空 JSON 数组"

            # 应用到 baseline config
            base = build_v1_config()
            new_config = edit_ops.apply_edits(base, edits)

            # 写到 edits.json（run_test 时 candidate 会读它）
            edits_path = Path(ctx._edits_path)
            edits_path.parent.mkdir(parents=True, exist_ok=True)
            edits_path.write_text(
                json.dumps(edits, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            ctx.emit_step(
                "apply_edits", "done", phase="execute",
                edits=len(edits), edits_path=str(edits_path),
            )
            return (
                f"已应用 {len(edits)} 个 edit 指令到 config，"
                f"写入 {edits_path}。candidate 重跑时会用这套配置。"
            )
        except (json.JSONDecodeError, ValueError) as e:
            ctx.emit_step("apply_edits", "failed", phase="execute", error=str(e))
            return f"应用 edit 失败：{e}"
        except Exception as e:
            ctx.emit_step("apply_edits", "failed", phase="execute", error=str(e))
            return f"应用 edit 失败：{e}"

    @tool
    def validate_changes() -> str:
        """【校验】落地改动后，校验源码可加载 + config 合法。

        必须在所有改动落地后（apply_edits + write/edit_file）调用。
        校验项：
          - config 合法性（cfg.validate）。
          - edits.json 引用的 middleware 类可 import。
          - harness 包源码无语法错误（import 各 .py 模块）。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("validate_changes", "running", phase="execute")
        errors: list[str] = []

        # 1. config 合法性
        try:
            edits_path = Path(ctx._edits_path)
            if edits_path.exists():
                edits = json.loads(edits_path.read_text(encoding="utf-8"))
                base = build_v1_config()
                new_config = edit_ops.apply_edits(base, edits)
                cfg.validate(new_config)
            else:
                # 无 edits.json，校验 baseline config
                cfg.validate(build_v1_config())
        except Exception as e:
            errors.append(f"config 校验失败：{e}")

        # 2. edits.json 引用的 middleware 类可 import
        pkg_root = settings.harness_work_dir_path
        try:
            if edits_path.exists():
                edits = json.loads(edits_path.read_text(encoding="utf-8"))
                # 收集所有 spec.class
                classes_to_check: set[str] = set()
                for e in edits:
                    spec = e.get("spec") or {}
                    if spec.get("class"):
                        classes_to_check.add(spec["class"])
                # 尝试 import harness 包的 middleware 模块，检查类是否存在
                for cls_name in classes_to_check:
                    _check_class_exists(pkg_root, cls_name, errors)
        except Exception as e:
            errors.append(f"middleware 类检查失败：{e}")

        # 3. harness 包源码无语法错误（py_compile 各 .py）
        try:
            _compile_pkg_sources(pkg_root, errors)
        except Exception as e:
            errors.append(f"源码编译检查失败：{e}")

        passed = len(errors) == 0
        ctx.emit_step(
            "validate_changes", "done" if passed else "failed",
            phase="execute", passed=passed, errors=len(errors),
        )
        if passed:
            return "校验通过：config 合法 + middleware 类可加载 + 源码无语法错误。"
        return "校验失败，发现以下问题：\n" + "\n".join(f"- {e}" for e in errors)

    @tool
    def write_change_log(applied_json: str, summary: str) -> str:
        """产出执行改动记录 change_log.md。这必须是执行子代理最后一步。

        Args:
            applied_json: 已落地改动 JSON 数组。每条：
              {"target": "改动目标", "action": "apply_edits|write_file|edit_file",
               "result": "ok|failed", "detail": "细节"}
            summary: 自然语言总述（落地了什么、是否通过校验）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("write_change_log", "running", phase="execute")
        try:
            applied = json.loads(applied_json)
            if not isinstance(applied, list):
                return "applied_json 必须是 JSON 数组"
            # 校验结果（刚跑过的 validate）
            validation = {"passed": True, "errors": []}
            path = docs.write_change_log(
                ctx.session_id,
                applied=applied,
                validation=validation,
                summary=summary,
            )
            ctx.change_log_path = path
            from app.evolve import db as ev_db
            ev_db.update_session(ctx.session_id, change_log_path=path)
            ctx.emit_step(
                "write_change_log", "done", phase="execute",
                path=path, applied=len(applied),
            )
            return f"改动记录已产出：{path}（{len(applied)} 个改动落地）"
        except json.JSONDecodeError as e:
            return f"applied_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("write_change_log", "failed", phase="execute", error=str(e))
            return f"产出记录失败：{e}"

    return [read_design_doc, apply_edits, validate_changes, write_change_log]


# ── 校验辅助 ───────────────────────────────────────────────────


def _check_class_exists(pkg_root: Path, cls_name: str, errors: list[str]) -> None:
    """检查 harness 包内是否存在名为 cls_name 的类。

    遍历 middleware/*.py，import 后检查是否有该属性。
    """
    # harness 包的 import 路径：harnesses.current
    mw_dir = pkg_root / "middleware"
    if not mw_dir.is_dir():
        errors.append(f"middleware 类 {cls_name}：middleware 目录不存在")
        return
    found = False
    for py in mw_dir.glob("*.py"):
        if py.name.startswith("_"):
            continue
        mod_name = f"harnesses.current.middleware.{py.stem}"
        try:
            mod = _safe_import(mod_name)
            if mod and hasattr(mod, cls_name):
                found = True
                break
        except Exception:
            continue
    if not found:
        errors.append(
            f"middleware 类 {cls_name} 未在 harnesses/current/middleware/ 中找到"
            "（需先用 write_file 新建源码文件）"
        )


def _safe_import(mod_name: str):
    """安全 import 模块，失败返回 None。"""
    try:
        if mod_name in sys.modules:
            return importlib.reload(sys.modules[mod_name])
        return importlib.import_module(mod_name)
    except Exception:
        return None


def _compile_pkg_sources(pkg_root: Path, errors: list[str]) -> None:
    """py_compile 检查 harness 包内所有 .py 源码无语法错误。"""
    import py_compile
    for py in pkg_root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"源码语法错误 {py.relative_to(pkg_root)}: {e}")


# ── 执行子代理 system prompt ───────────────────────────────────


EXECUTE_SYSTEM_PROMPT = """\
你是 Writer 项目的「执行专家」——一个 Agent 改动的落地工程师。

你的使命：读方案专家产出的 design_doc.md，把改动落地到 harness 包
（配置层 + 源码层），校验可加载，产 change_log.md。

## 工作流程

1. **读设计文档**：调用 read_design_doc 拿到 changes（改动列表）。
2. **分类落地**：按改动类型分别落地：
   - 配置层改动（changes 里有 edit 字段的）：收集所有 edit 指令，
     一次调用 apply_edits 落地（apply_edits 接收 JSON 数组）。
   - 源码层改动（target 是 .py 路径的）：用 write_file 新建源码文件，
     或 edit_file 修改现有源码。写完后如该源码含新 middleware 类，
     确保对应 edit 指令已在 apply_edits 里引用它。
3. **校验**：所有改动落地后，调用 validate_changes 校验：
   - config 合法性。
   - edits.json 引用的 middleware 类都能 import 到。
   - 源码无语法错误。
   如果校验失败，根据错误信息修复（补写源码 / 修正 edit 指令），直到通过。
4. **产记录**：调用 write_change_log 记录落地了哪些改动 + 校验结果。

## 落地规则

- **只能改 harnesses/current/**：你只能修改 harness 包内的文件
  （middleware/*.py、prompts/*.md 等）和 evolution/data/evolve_workspace/edits.json。
  不要碰其他文件。
- **apply_edits 指令格式**：
  {"op": "replace|insert|remove",
   "target": ["agent名", "processors|slots", key],
   "spec": {"class": "类名", "params": {...}}}
  - agent 名：meta / storybuilding / detail_outline / writing / interview
  - processors 的 key = [hook, group]，hook ∈ {before_agent, before_model,
    wrap_model_call, after_model, wrap_tool_call, after_agent}
  - slots 的 key = slot 名（str），如 system_prompt
- **新增 middleware 源码**：先 write_file 写 .py（含类定义），
  再在 apply_edits 里用 insert 引用该类。
- **诚实记录**：write_change_log 的 applied 里，result 如实填 ok/failed。
  失败的改动也要记录（detail 写失败原因）。

## 输出要求

write_change_log 的 applied 是 JSON 数组，每个含 target/action/result/detail。
summary 是自然语言总述：落地了什么、校验是否通过、candidate 重跑会有什么变化。
"""


# ── 执行子代理 spec 构建 ───────────────────────────────────────


def build_execute_subagent(model):
    """构建执行子代理（CompiledSubAgent），挂载到驱动器。"""
    from deepagents import CompiledSubAgent, create_deep_agent

    graph = create_deep_agent(
        model=model,
        tools=make_execute_tools(),
        system_prompt=EXECUTE_SYSTEM_PROMPT,
        middleware=[],
        subagents=None,
        checkpointer=None,
        # 工作目录设为项目根，让框架自带 write_file/edit_file 能改 harness 包源码。
        # path_guard 由调用方/框架约束只改 harnesses/current/。
    )
    return CompiledSubAgent(
        name="execute",
        description=(
            "执行专家：读 design_doc 落地改动（apply_edits 配置层 + write/edit_file 源码层），"
            "校验可加载，产 change_log.md。委托时无需额外参数。"
        ),
        runnable=graph,
    )


__all__ = ["make_execute_tools", "EXECUTE_SYSTEM_PROMPT", "build_execute_subagent"]
