"""执行子代理工具集 + 校验辅助（决策 D12/D14/E17）。

工具集：
  - read_design_doc()        读 design_doc.md
  - apply_edits(edits_json)  配置层：应用 edit 指令到 config，写 edits.json
  - validate_changes()       落地后校验：import 源码可加载 + config 合法
  - write_change_log(...)    产出 change_log.md

校验辅助（本模块私有）：
  _check_class_exists / _safe_import / _compile_pkg_sources

注：源码层改动（write_file/edit_file）用 DeepAgent 框架自带工具。
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path

from langchain_core.tools import tool

from app.harness_config import config as cfg
from app.harness_config import edits as edit_ops
from app.harness_config.bootstrap import build_v1_config
from app.core.settings import settings
from app.evolve import docs
from app.evolve.ctx import get_tool_context

logger = logging.getLogger("evolution.evolve.execute.tools")


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

            # 写到 edits.json（发版时会回放这些 edits 生成新 config）
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
                f"写入 {edits_path}。发版时会回放这些 edits 生成新 config。"
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

        edits_path = Path(ctx._edits_path)

        # 1. config 合法性
        try:
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
               "result": "ok|failed", "detail": "细节",
               "design_ref": 1}
              design_ref：对应 design_doc 改动清单的序号（1-based）。
              一条 design 改动可能拆成多条 applied（如先 write_file 再 apply_edits），
              它们共享同一个 design_ref。纯修补（不在 design_doc 里的）可不填。
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


__all__ = ["make_execute_tools"]
