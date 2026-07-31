"""架构层问题上报工具（1 个）——六要素硬尺子的上报出口（DEC-003/006/007，FR-007）。

Agent 通过 report_architecture_issue 把"根因在六要素外、够不着"的问题上报给人类：
  - 落 issue_report.md（与 design_doc/change_log 同 session 目录）
  - 同步写 evolve_architecture_issues 持久表（前端列表数据源）
  - 前端待处理架构问题列表可见、可处置

与 propose_evolution_point 的区别：
  - propose 的 target = agent 可改要素（六要素内）→ 自修
  - report 的 layer = agent 够不着的归属层（六要素外）→ 上报，不硬修

调用约束（六要素硬尺子，DEC-007）：
  - 根因在 prompts/middleware/tools/subagents/skills/memory 内 → 自修，不要上报
  - 根因在装配入口/executor端/第三方框架/评估infra/数据管道 → 上报
  - 灰色地带（能绕不能根治）一律上报，不硬修兜底
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from app.evolve.ctx import get_tool_context
from app.evolve.docs import write_issue_report
from app.evolve.evolve_repo import ARCHITECTURE_LAYERS, ArchitectureIssuesRepo

logger = logging.getLogger("evolution.evolve.agent.tools.issues")


def make_issues_tools() -> list:
    """构建上报工具集（1 个：report_architecture_issue）。"""

    @tool
    def report_architecture_issue(
        layer: str,
        problem: str,
        evidence_ref: str,
        note: str = "",
    ) -> str:
        """上报一个"架构层问题"（根因在六要素外，你够不着改的层）。

        **何时调用**（六要素硬尺子，DEC-007）：
        - 根因在装配入口（__init__.py assemble，你只读）/ executor 端 / 第三方框架
          （deepagents 等）/ 评估 infra / 数据管道 → 调本工具上报。
        - 灰色地带（能绕不能根治，比如想用 middleware 兜底第三方包行为）→ 一律上报，
          不要硬修兜底（会留技术债）。
        - **不要用它上报六要素内的问题**（prompts/middleware/tools/subagents/skills/memory）——
          那些自己改（write_* / edit_source）。

        上报后会：落 issue_report.md + 写持久表，产品负责人在进化前端可见并处置。

        Args:
            layer: 归属层枚举（agent 够不着的层）：
                   "assembly"（装配入口）/ "executor"（executor 端：trace/recorder/隔离层/API）
                   / "framework"（第三方框架：deepagents/langchain）/ "eval-infra"（评估 infra）
                   / "data-pipeline"（数据管道：摄入/dataset）。选最贴近的。
            problem: 问题详细描述——现象、根因在哪一层、为什么你改不了。
            evidence_ref: 证据引用——评估 finding id（如 "f01"）或 trace 证据定位
                          （如 "trace xxx 的 llm_end 节点 output 为 None"）。必须非空。
            note: 你的归属层判断理由（可选）——为什么判这层、是否属灰色地带、人类处置建议。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"

        ctx.emit_step("report_architecture_issue", "running")
        try:
            if layer not in ARCHITECTURE_LAYERS:
                ctx.emit_step(
                    "report_architecture_issue", "failed",
                    error=f"非法 layer: {layer}",
                )
                return (
                    f"非法 layer '{layer}'，合法值：{', '.join(ARCHITECTURE_LAYERS)}。"
                    f"六要素内的问题不要上报，自己改。"
                )
            if not problem.strip() or not evidence_ref.strip():
                ctx.emit_step(
                    "report_architecture_issue", "failed",
                    error="problem/evidence_ref 不能为空",
                )
                return "problem 和 evidence_ref 都不能为空——上报必须带清楚的问题描述和证据。"

            # 预先生成 issue_id，让 issue_report 以最终文件名一次性落盘，再一次性 INSERT
            # （EDGE-004：落盘失败不污染表；这样无需占位文件 + rename + UPDATE 回填）。
            import uuid as _uuid_mod
            issue_id = _uuid_mod.uuid4().hex
            report_path = write_issue_report(
                ctx.session_id,
                layer=layer,
                problem=problem,
                evidence_ref=evidence_ref,
                note=note or None,
                issue_id=issue_id,
            )

            issue = ArchitectureIssuesRepo.report(
                ctx.session_id,
                layer=layer,
                problem=problem,
                evidence_ref=evidence_ref,
                note=note or None,
                report_path=report_path,
                issue_id=issue_id,
            )

            ctx.emit_step(
                "architecture_issue", "report",
                action="report",
                issue_id=issue["id"],
                seq=issue["seq"],
                layer=layer,
            )
            return (
                f"已上报架构层问题 #{issue['seq']}（layer={layer}，id={issue['id']}）。\n"
                f"issue_report 已落盘，产品负责人可在进化前端待处理架构问题列表看到并处置。\n"
                f"不要试图硬修该问题——它在你够不着的层。继续推进你能改的部分。"
            )
        except Exception as e:
            ctx.emit_step("report_architecture_issue", "failed", error=str(e))
            return f"上报失败：{e}"

    return [report_architecture_issue]
