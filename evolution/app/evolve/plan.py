"""方案子代理（决策 D11/E16/E14）。

流水线第二阶段（plan）的执行者。读评估诊断 eval_report.md，据此产出
改动设计文档 design_doc.md（结构化改动列表 + 自然语言详述），交执行子代理落地。

工具集（D11）：
  - read_eval_report()   读当前 session 的 baseline 评估诊断
  - write_design_doc(changes_json, rationale)  产出 design_doc.md

产出格式（E16 结构化字段 + 自然语言详述）：
  changes 每条：
    {
      "target": "目标（agent/section/key 或源码路径）",
      "change_desc": "改什么（描述性）",
      "reason": "依据评估证据（引用 eval_report 的 finding）",
      "expected_up": "预期涨的方面",
      "expected_down": "预期跌的方面（诚实声明）",
      "edit": {  # 可选：直接给 apply_edits 的指令
          "op": "replace|insert|remove",
          "target": ["agent", "processors|slots", key],
          "spec": {"class": "...", "params": {...}}
      }
    }

设计依据：设计文档 D11（write_design_doc专用工具）/ E16（结构化+自然语言）/
          E14（可提多个改动批量落地，接受归因模糊）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

from app.evolve import docs
from app.evolve.ctx import get_tool_context

logger = logging.getLogger("evolution.evolve.plan")


# ── 方案子代理工具 ─────────────────────────────────────────────


def make_plan_tools() -> list:
    """构建方案子代理的工具集。"""

    @tool
    def read_eval_report() -> str:
        """读取当前 session 的 baseline 评估诊断（eval_report_baseline.md）。

        评估诊断包含：内容分数、流程硬指标（协作拓扑/错误保障/资源消耗）、
        流程诊断条目（findings）。你据此设计改进方案。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        if not ctx.eval_report_path:
            return "错误：还没产出 baseline 评估报告（eval_report_path 为空）"
        try:
            data = docs.read_eval_report(ctx.eval_report_path)
            meta = data["meta"]
            body = data["body"]
            return (
                f"## 评估报告（trace={meta.get('trace_kind')}）\n\n"
                f"### 结构化数据\n```json\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n```\n\n"
                f"### 诊断正文\n{body}"
            )
        except Exception as e:
            return f"读评估报告失败：{e}"

    @tool
    def write_design_doc(changes_json: str, rationale: str) -> str:
        """产出改动设计文档 design_doc.md。这必须是方案子代理最后一步。

        你可基于全局诊断一次提出多个方向的改动（批量落地，接受归因模糊）。
        每个改动必须是可被执行子代理落地的具体指令。

        Args:
            changes_json: 改动列表 JSON 数组字符串。每条含：
              {
                "target": "目标（agent/section/key 或源码路径）",
                "change_desc": "改什么（描述性）",
                "reason": "依据评估证据（引用 eval_report 的 finding）",
                "expected_up": "预期涨的方面",
                "expected_down": "预期跌的方面（诚实声明）",
                "edit": {  # 可选：直接给 apply_edits 的指令
                    "op": "replace|insert|remove",
                    "target": ["agent名", "processors|slots", key],
                    "spec": {"class": "类名", "params": {...}}
                }
              }
            rationale: 自然语言总述（基于 eval_report 的整体判断，为什么这么改）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("write_design_doc", "running", phase="plan")
        try:
            changes = json.loads(changes_json)
            if not isinstance(changes, list):
                return "changes_json 必须是 JSON 数组"
            if not changes:
                return "changes 不能为空（至少一个改动）"
            # 基本校验：每个 change 必须有 target + change_desc + reason
            for i, c in enumerate(changes):
                if not isinstance(c, dict):
                    return f"changes[{i}] 必须是对象"
                for field in ("target", "change_desc", "reason"):
                    if not c.get(field):
                        return f"changes[{i}] 缺少必填字段：{field}"

            path = docs.write_design_doc(
                ctx.session_id,
                changes=changes,
                rationale=rationale,
            )
            ctx.design_doc_path = path
            from app.evolve import db as ev_db
            ev_db.update_session(ctx.session_id, design_doc_path=path)
            ctx.emit_step(
                "write_design_doc", "done", phase="plan",
                path=path, changes=len(changes),
            )
            return f"设计文档已产出：{path}（{len(changes)} 个改动）"
        except json.JSONDecodeError as e:
            ctx.emit_step("write_design_doc", "failed", phase="plan", error=str(e))
            return f"changes_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("write_design_doc", "failed", phase="plan", error=str(e))
            return f"产出设计文档失败：{e}"

    return [read_eval_report, write_design_doc]


# ── 方案子代理 system prompt ───────────────────────────────────


PLAN_SYSTEM_PROMPT = """\
你是 Writer 项目的「方案设计专家」——一个 Agent 架构改进方案的设计者。

你的使命：读评估专家产出的 eval_report（流程诊断 + 内容分数），据此设计
具体的改进方案（改哪些 prompt / middleware / 参数 / 新增能力），产出
design_doc.md 交执行专家落地。

## 工作流程

1. **读评估诊断**：调用 read_eval_report 拿到 baseline 评估报告。
   关注 findings（诊断条目）——每条 finding 有 dimension/severity/
   evidence_type/finding/evidence/suggestion。
2. **设计改进方案**：基于 findings，设计具体改动。每个改动必须：
   - 指向一个明确的 target（哪个 agent / 哪个 middleware / 哪个 prompt）。
   - 说清改什么（change_desc）。
   - 引用评估证据（reason，关联到哪个 finding）。
   - 诚实声明预期（expected_up 涨什么 / expected_down 可能跌什么）。
   - 如能给出 apply_edits 指令（结构化 edit），执行专家会更高效。
3. **产出设计文档**：调用 write_design_doc 提交 changes + rationale。

## 改动类型（你能设计的改进范围）

### 配置层（apply_edits 可落地，给 edit 指令）
- 换 middleware 参数（如调大 max_revisions、改 GoalMiddleware 参数）。
- 换/增/删 middleware 装配（processor 的 op=replace/insert/remove）。
- 改 prompt 正文（slot 的 system_prompt）。
- edit 指令格式：
  {"op": "replace|insert|remove",
   "target": ["agent名(meta/storybuilding/detail_outline/writing/interview)",
              "processors|slots", key],
   "spec": {"class": "类名", "params": {...}}}
  - processors 的 key = [hook, group]，如 ["before_model", "revision"]
  - slots 的 key = slot 名（str），如 "system_prompt"

### 源码层（执行专家用 write/edit_file 落地，给 target 路径）
- 新增 middleware 源码（新建 .py 文件 + edit 指令引用它）。
- 改 middleware 源码内部逻辑（编辑现有 .py）。
- 改 prompt 文件原文（编辑 prompts/*.md）。
- 这类改动 target 写源码相对路径（如 middleware/xxx.py）。

## 设计原则

- **证据驱动**：每个改动的 reason 必须关联 eval_report 的具体 finding，
  不要凭空设计。如果某个改进没有 finding 支撑，不要加。
- **聚焦高 severity**：优先针对 severity=high 的 finding 设计改动。
  低 severity 的可合并或暂缓。
- **批量设计**：你可一次提出多个改动（批量落地）。但每个都要独立可追溯。
- **诚实预期**：expected_down 必须诚实——任何改动都有代价（耗时/复杂度/稳定性），
  不要只写 expected_up。
- **可落地**：改动必须是执行专家能落地的。配置层给 edit 指令，
  源码层给路径 + 改动描述（执行专家会写代码）。

## 输出要求

write_design_doc 的 changes 是 JSON 数组，每个含 target/change_desc/reason/
expected_up/expected_down/可选 edit。至少 1 个改动，最多 8 个（聚焦最重要的）。
rationale 是自然语言总述：基于 eval_report 整体判断、为什么选这些改动、
预期整体效果。
"""


# ── 方案子代理 spec 构建 ───────────────────────────────────────


def build_plan_subagent(model):
    """构建方案子代理（CompiledSubAgent），挂载到驱动器。"""
    from deepagents import CompiledSubAgent, create_deep_agent

    graph = create_deep_agent(
        model=model,
        tools=make_plan_tools(),
        system_prompt=PLAN_SYSTEM_PROMPT,
        middleware=[],
        subagents=None,
        checkpointer=None,
    )
    return CompiledSubAgent(
        name="plan",
        description=(
            "方案设计专家：读评估诊断 eval_report，设计具体改进方案（改 prompt/"
            "middleware/参数/源码），产出 design_doc.md。委托时无需额外参数。"
        ),
        runnable=graph,
    )


__all__ = ["make_plan_tools", "PLAN_SYSTEM_PROMPT", "build_plan_subagent"]
