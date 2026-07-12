"""流程工具（5 个）——进化 Agent 的评估消费 + 产出 + 校验（决策 S2/S9）。

从原 plan/execute 子代理工具归并而来，单体化后统一挂载到单体 Agent。
所有 emit_step 调用去掉了 phase 参数（单体 Agent 无阶段概念）。

工具：
  - read_eval_report()              读评估报告（从 ctx.eval_snapshot）
  - read_trace(trace_id)            读 trace 摘要
  - write_design_doc(changes, rationale)  产 design_doc.md
  - validate_changes()              纯源码校验（py_compile + import，废弃 config 校验）
  - write_change_log(applied, summary)    产 change_log.md
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path

from langchain_core.tools import tool

from app.core.settings import settings
from app.evolve import docs
from app.evolve.ctx import get_tool_context
from app.view.traces import get_trace

logger = logging.getLogger("evolution.evolve.agent.tools.flow")


def make_flow_tools() -> list:
    """构建流程工具集（5 个）。"""

    @tool
    def read_eval_report() -> str:
        """读取当前 session 的评估报告（由评估 Agent 产出，已加载到上下文）。

        评估报告包含：
          - scores：内容层评分 + 流程硬指标
          - findings：诊断条目（每条含 id/dimension/severity/evidence_type/finding/evidence）
          - report_md：可读报告全文

        注意：评估只诊断问题（不含改进方案）。你据此设计改进方案。
        记下每条 finding 的 id（f01/f02…），write_design_doc 的 evidence_ref 要引用它。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        if not ctx.eval_snapshot:
            return "错误：评估报告未加载（eval_snapshot 为空）"
        snap = ctx.eval_snapshot
        scores = snap.get("scores", {})
        findings = snap.get("findings", [])
        report_md = snap.get("report_md", "")
        return (
            f"## 评估报告（trace={snap.get('trace_id', '?')}）\n\n"
            f"### 结构化分数\n```json\n{json.dumps(scores, ensure_ascii=False, indent=2)}\n```\n\n"
            f"### 诊断条目\n```json\n{json.dumps(findings, ensure_ascii=False, indent=2)}\n```\n\n"
            f"### 报告正文\n{report_md}"
        )

    @tool
    def read_trace(trace_id: str) -> str:
        """读取一个 trace 的节点结构化摘要。

        用于看实际执行流程，对照评估诊断理解问题。

        Args:
            trace_id: 要读的 trace id
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("read_trace", "running", trace_id=trace_id)
        try:
            detail = get_trace(trace_id)
            run = detail.run
            lines = [
                f"trace_id: {run.trace_id}",
                f"状态: {run.status}  耗时: {run.duration_ms or '?'}ms  事件数: {run.event_count}",
            ]
            if run.error:
                lines.append(f"错误: {run.error[:300]}")
            lines.append(f"节点数: {len(detail.nodes)}")
            for node in detail.nodes:
                if node.kind == "run":
                    continue
                parts = [f"  [{node.kind}]"]
                if node.node_id:
                    parts.append(f"id={node.node_id}")
                if node.agent_name:
                    parts.append(node.agent_name)
                if node.tool_name:
                    parts.append(f"tool={node.tool_name}")
                if node.status and node.status != "ok":
                    parts.append(f"status={node.status}")
                if node.error:
                    parts.append(f"err={node.error[:120]}")
                if node.chain_summary:
                    parts.append(f"| {node.chain_summary[:160]}")
                lines.append(" ".join(parts))
            ctx.emit_step("read_trace", "done", trace_id=trace_id)
            return "\n".join(lines)
        except Exception as e:
            ctx.emit_step("read_trace", "failed", error=str(e))
            return f"读 trace 失败：{e}"

    @tool
    def write_design_doc(changes_json: str, rationale: str) -> str:
        """产出改动设计文档 design_doc.md。

        基于评估诊断设计具体改动方案。每个改动必须是可落地的具体指令。
        这应在实际改代码之前调用——先想清楚改什么、为什么改，再动手。

        Args:
            changes_json: 改动列表 JSON 数组字符串。每条含：
              {
                "target": "目标（要素路径，如 middleware/pacing.py 或 prompts/writing_system.md）",
                "change_desc": "改什么（描述性）",
                "reason": "依据评估证据（自然语言）",
                "evidence_ref": ["f01"],  # 必填：引用评估 finding 的 id
                "expected_up": "预期涨的方面",
                "expected_down": "预期跌的方面（诚实声明）"
              }
              **evidence_ref 是硬性必填**：每个改动必须引用至少一个评估 finding 的 id，
              证明"为什么改"。id 格式 f01/f02…，从 read_eval_report 的 findings 里取。
            rationale: 自然语言总述（基于评估报告的整体判断，为什么这么改）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("write_design_doc", "running")
        try:
            changes = json.loads(changes_json)
            if not isinstance(changes, list):
                return "changes_json 必须是 JSON 数组"
            if not changes:
                return "changes 不能为空（至少一个改动）"

            # 提取评估报告里的合法 finding id 集合
            valid_finding_ids: set[str] = set()
            snap = ctx.eval_snapshot or {}
            findings = snap.get("findings") or []
            for f in findings:
                if isinstance(f, dict) and f.get("id"):
                    valid_finding_ids.add(str(f["id"]))

            # 死局短路：评估报告无可用 finding id
            if not valid_finding_ids:
                ctx.emit_step("write_design_doc", "failed", reason="no_findings")
                return (
                    "评估报告没有可引用的结构化 finding（findings 为空或无 id）。"
                    "evidence_ref 校验无法满足，无法产出 design_doc。"
                    "这是评估阶段的问题——请结束当前进化，提示重新评估该 trace 后再启动进化。"
                )

            # 校验每个 change
            for i, c in enumerate(changes):
                if not isinstance(c, dict):
                    return f"changes[{i}] 必须是对象"
                for field in ("target", "change_desc", "reason"):
                    if not c.get(field):
                        return f"changes[{i}] 缺少必填字段：{field}"
                refs = c.get("evidence_ref")
                if not isinstance(refs, list) or not refs:
                    return (
                        f"changes[{i}] 缺少 evidence_ref：每个改动必须引用至少一个评估 "
                        f"finding 的 id（如 [\"f01\"]）。请先 read_eval_report 拿到 finding id。"
                    )
                bad = [r for r in refs if str(r) not in valid_finding_ids]
                if bad:
                    return (
                        f"changes[{i}] 的 evidence_ref {bad} 不存在于评估报告的 finding 列表中。"
                        f"合法 id：{sorted(valid_finding_ids) or '（无）'}。"
                    )

            path = docs.write_design_doc(ctx.session_id, changes=changes, rationale=rationale)
            ctx.design_doc_path = path
            from app.evolve import db as ev_db
            ev_db.update_session(ctx.session_id, design_doc_path=path)
            ctx.emit_step("write_design_doc", "done", path=path, changes=len(changes))
            return f"设计文档已产出：{path}（{len(changes)} 个改动）"
        except json.JSONDecodeError as e:
            ctx.emit_step("write_design_doc", "failed", error=str(e))
            return f"changes_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("write_design_doc", "failed", error=str(e))
            return f"产出设计文档失败：{e}"

    @tool
    def validate_changes() -> str:
        """校验 harness 包源码改动的合法性。

        在所有改动落地后（write_*/edit_source）调用。校验项：
          1. py_compile：harness 包内所有 .py 文件无语法错误。
          2. import 检查：尝试 import 改动过的模块，捕获运行时错误
             （如引用不存在的模块、类定义错误）。

        如果校验失败，按错误信息修复后重新校验。
        **建议最多调用 2 次**——若 2 次仍失败，如实写 change_log 收尾。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("validate_changes", "running")
        errors: list[str] = []
        pkg_root = settings.harness_work_dir_path

        # 1. py_compile 全包源码
        import py_compile
        for py in pkg_root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            try:
                py_compile.compile(str(py), doraise=True)
            except py_compile.PyCompileError as e:
                rel = py.relative_to(pkg_root)
                errors.append(f"语法错误 {rel}: {e}")

        # 2. import 检查：尝试 import 包内各模块（捕获运行时错误）
        _import_check_all(pkg_root, errors)

        passed = len(errors) == 0
        ctx.emit_step(
            "validate_changes", "done" if passed else "failed",
            passed=passed, errors=len(errors),
        )
        if passed:
            return "校验通过：harness 包所有源码无语法错误 + import 正常。"
        return "校验失败，发现以下问题：\n" + "\n".join(f"- {e}" for e in errors)

    @tool
    def write_change_log(applied_json: str, summary: str) -> str:
        """产出执行改动记录 change_log.md。这应是你最后一步。

        注意：write_design_doc 必须在 write_change_log 之前完成（FlowGuard 会强制检查）。

        Args:
            applied_json: 已落地改动 JSON 数组。每条：
              {"target": "改动目标", "action": "write_middleware|edit_source|write_prompt|...",
               "result": "ok|failed", "detail": "细节",
               "design_ref": 1}
              design_ref：对应 design_doc 改动清单的序号（1-based）。
            summary: 自然语言总述（落地了什么、是否通过校验）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("write_change_log", "running")
        try:
            applied = json.loads(applied_json)
            if not isinstance(applied, list):
                return "applied_json 必须是 JSON 数组"
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
            ctx.emit_step("write_change_log", "done", path=path, applied=len(applied))
            return f"改动记录已产出：{path}（{len(applied)} 个改动落地）"
        except json.JSONDecodeError as e:
            return f"applied_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("write_change_log", "failed", error=str(e))
            return f"产出记录失败：{e}"

    return [read_eval_report, read_trace, write_design_doc, validate_changes, write_change_log]


# ── import 检查辅助 ─────────────────────────────────────────────


def _import_check_all(pkg_root: Path, errors: list[str]) -> None:
    """尝试 import harness 包内所有 .py 模块，捕获运行时错误。

    harness 包以 harness_current 为模块名加载（与 executor loader 一致）。
    这里做兜底 import 检查——语法没错但 import 时报错（如引用不存在的模块）
    会被捕获。
    """
    # 收集所有 .py 相对路径 → 模块名
    py_files = [
        p for p in pkg_root.rglob("*.py")
        if "__pycache__" not in p.parts and p.name != "__init__.py"
    ]
    for py in py_files:
        rel = py.relative_to(pkg_root)
        # 构造模块名：harness_current.subagents.storybuilding 等
        parts = list(rel.parts)
        parts[-1] = parts[-1][:-3]  # 去 .py
        mod_name = "harness_current." + ".".join(parts)
        try:
            # 已加载则 reload（捕获改动后的运行时错误）
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
            else:
                importlib.import_module(mod_name)
        except Exception as e:
            errors.append(f"import 错误 {rel}: {e}")


__all__ = ["make_flow_tools"]
