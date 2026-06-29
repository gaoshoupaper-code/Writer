"""评估子代理（决策 D7/D8/D9/D10/E6/E21）。

流水线第一阶段（eval_baseline）/第五阶段（eval_candidate）的执行者。
通过驱动器的 task 委托调用，评估一条 trace 的流程 + 内容两大维度。

工具集（D7/D10）：
  - read_trace(trace_id)          摘要层：所有节点结构化摘要
  - read_trace_node(node_id)      细节层：单节点完整 context（按 anchor 回溯）
  - read_trace_range(start,end)   区间层：连续 anchor 区间 context
  - read_surface()                设计意图：HarnessConfig + harness 包源码清单
  - get_content_score()           内容层分数（await 后台 evaluate_trace）
  - write_eval_report(findings)   产出 eval_report.md

判据双轨（E6/D8）：
  - 硬指标轨：flow_metrics 自动算好，注入子代理（D8），子代理直接看。
  - LLM 诊断轨：子代理读 trace 多层 + surface，推断合理基线 + 判异常 + 产诊断。
  - 内容层：复用 diagnosis/evaluation.py（后台异步，get_content_score 取结果 D9/D10）。

设计依据：设计文档 D7（trace三工具）/ D8（metrics注入）/ D9（内容流程并行）/
          D10（get_content_score）/ E6（硬指标+LLM诊断）/ E21（LLM推断基线+引用实证）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import tool

from app.compose.bootstrap import build_v1_config
from app.core.settings import settings
from app.diagnosis import evaluation
from app.evolve import docs
from app.evolve.ctx import get_tool_context
from app.view.traces import get_trace

logger = logging.getLogger("evolution.evolve.evaluate")

# ── 后台内容评估任务持有（D9：内容流程并行）──────────────────────
# 每个评估子代理实例启动时，启动一个后台 asyncio 任务跑 evaluate_trace。
# get_content_score 工具 await 它拿结果。
# 用 contextvar 持有，与 EvolveContext 同生命周期隔离。


_content_tasks: dict[str, asyncio.Task] = {}


def _start_content_eval(trace_id: str, trace_kind: str) -> asyncio.Task:
    """启动后台内容评估任务（D9）。幂等：同 trace 只启动一次。"""
    key = f"{trace_kind}:{trace_id}"
    existing = _content_tasks.get(key)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(_run_content_eval(trace_id))
    _content_tasks[key] = task
    return task


async def _run_content_eval(trace_id: str) -> dict[str, Any]:
    """后台跑 evaluate_trace（复用现有内容评估引擎）。"""
    try:
        result = evaluation.evaluate_trace(trace_id)
        return result or {"skipped": True, "reason": "无 writing 正文或 LLM 未配置"}
    except Exception as exc:
        logger.exception("内容评估失败 %s", trace_id)
        return {"error": str(exc)}


def clear_content_tasks() -> None:
    """清理后台任务引用（session 结束时调）。"""
    _content_tasks.clear()


# ── 评估子代理工具 ─────────────────────────────────────────────


def make_evaluate_tools() -> list:
    """构建评估子代理的工具集。"""
    from contracts.trace import TraceContextKind

    @tool
    def read_trace(trace_id: str) -> str:
        """【摘要层】读取一个 trace 的所有节点结构化摘要。

        返回每个节点（run/agent/llm/tool/error/skill）的关键信息 + run 元信息。
        不含完整正文——需要细节时用 read_trace_node 或 read_trace_range。
        先用本工具纵观全局，定诊断方向。

        Args:
            trace_id: 要读的 trace id
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("read_trace", "running", phase="eval", trace_id=trace_id)
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
            ctx.emit_step("read_trace", "done", phase="eval", trace_id=trace_id)
            return "\n".join(lines)
        except Exception as e:
            ctx.emit_step("read_trace", "failed", phase="eval", error=str(e))
            return f"读 trace 失败：{e}"

    @tool
    def read_trace_node(node_id: str) -> str:
        """【细节层】读取单个节点的完整 context（按 anchor 回溯）。

        当你在 read_trace 摘要里看到某个异常/关键节点时，用它的 node_id 展开
        读完整内容（含 LLM input/output、tool 调用细节）。

        Args:
            node_id: 节点 id（从 read_trace 摘要里获取）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        try:
            # 找当前 session 评估的 trace（baseline 或 candidate）
            trace_id = ctx.candidate_trace or ctx.baseline_trace
            if not trace_id:
                return "错误：当前没有待评估的 trace"
            detail = get_trace(trace_id)
            # 找节点
            target = None
            for n in detail.nodes:
                if n.node_id == node_id:
                    target = n
                    break
            if not target:
                return f"未找到 node_id={node_id}"
            # 通过 anchor 回溯 context
            lines = [f"节点 {node_id}（{target.kind}）", f"agent: {target.agent_name}"]
            if target.tool_name:
                lines.append(f"tool: {target.tool_name}")
            if target.error:
                lines.append(f"错误: {target.error}")
            # 读关联的 context segments
            related = [
                seg for seg in detail.context
                if seg.related_node_id == node_id or seg.anchor_id == target.context_anchor_id
            ]
            if related:
                lines.append("\n关联 context：")
                for seg in related[:5]:
                    content_str = str(seg.content)[:800]
                    lines.append(f"  [{seg.kind}] {seg.title}: {content_str}")
            else:
                lines.append("（无关联 context，可能需用 read_trace_range 读区间）")
            return "\n".join(lines)
        except Exception as e:
            return f"读节点失败：{e}"

    @tool
    def read_trace_range(anchor_start: str, anchor_end: str) -> str:
        """【区间层】读取连续 anchor 区间的 context（一段时间的完整对话流）。

        当你需要看某段时间的完整流程（如某子代理从头到尾的对话）时用。
        anchor 从 read_trace 摘要或 read_trace_node 获取。

        Args:
            anchor_start: 起始 anchor_id
            anchor_end:   结束 anchor_id
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        try:
            trace_id = ctx.candidate_trace or ctx.baseline_trace
            if not trace_id:
                return "错误：当前没有待评估的 trace"
            detail = get_trace(trace_id)
            # 按 sequence 区间取 context segments
            start_seq = None
            end_seq = None
            for seg in detail.context:
                if seg.anchor_id == anchor_start:
                    start_seq = seg.sequence
                if seg.anchor_id == anchor_end:
                    end_seq = seg.sequence
            if start_seq is None or end_seq is None:
                return f"未找到 anchor 区间 [{anchor_start}, {anchor_end}]"
            ranged = [
                seg for seg in detail.context
                if start_seq <= seg.sequence <= end_seq
            ]
            lines = [f"区间 [{anchor_start} → {anchor_end}]，{len(ranged)} 段 context："]
            for seg in ranged[:20]:
                content_str = str(seg.content)[:400]
                lines.append(f"  [seq={seg.sequence} {seg.kind}] {seg.title}: {content_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"读区间失败：{e}"

    @tool
    def read_surface() -> str:
        """读取当前 Agent 的 surface（设计意图）：HarnessConfig + harness 包源码清单。

        用于判断「流程本该如何」（设计意图），对照实际 trace 找偏差。
        你可以用 read_file 读具体某个文件的完整内容深入分析。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        try:
            config = build_v1_config()
            config_str = json.dumps(config, ensure_ascii=False, indent=2)
            pkg_dir = settings.harness_work_dir_path
            files = []
            for p in sorted(pkg_dir.rglob("*")):
                if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
                    rel = p.relative_to(pkg_dir)
                    files.append(str(rel).replace("\\", "/"))
            return (
                f"## HarnessConfig\n```json\n{config_str}\n```\n\n"
                f"## harness 包文件清单（{len(files)} 个）\n"
                + "\n".join(f"  {f}" for f in files)
            )
        except Exception as e:
            return f"读 surface 失败：{e}"

    @tool
    async def get_content_score() -> str:
        """获取内容质量层评估分数（内容6维 + subagent4维）。

        内容评估在后台异步跑（5 次 LLM-judge，较慢），本工具 await 它拿结果。
        如果还没跑完会等待。建议在流程诊断做完、写报告前调用。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        trace_id = ctx.candidate_trace or ctx.baseline_trace
        if not trace_id:
            return "错误：当前没有待评估的 trace"
        trace_kind = "candidate" if ctx.candidate_trace else "baseline"
        ctx.emit_step("get_content_score", "running", phase="eval", trace_id=trace_id)
        try:
            # 启动后台内容评估（D9：内容流程并行），await 拿结果
            task = _start_content_eval(trace_id, trace_kind)
            result = await task
            if result.get("skipped"):
                ctx.emit_step("get_content_score", "done", phase="eval", skipped=True)
                return f"内容评估跳过：{result.get('reason')}"
            if result.get("error"):
                ctx.emit_step("get_content_score", "failed", phase="eval", error=result["error"])
                return f"内容评估失败：{result['error']}"
            ctx.emit_step(
                "get_content_score", "done", phase="eval",
                content_overall=result.get("content", {}).get("overall"),
            )
            return f"内容评估完成：\n{json.dumps(result, ensure_ascii=False, indent=2)}"
        except Exception as e:
            ctx.emit_step("get_content_score", "failed", phase="eval", error=str(e))
            return f"取内容分数失败：{e}"

    @tool
    def write_eval_report(findings_json: str, summary: str) -> str:
        """产出评估报告 eval_report.md。这必须是评估子代理最后一步。

        Args:
            findings_json: 流程诊断条目 JSON 数组字符串。每条含：
              {
                "dimension": "协作拓扑|错误保障|资源消耗|内容质量",
                "severity": "high|medium|low",
                "evidence_type": "实证|推断",   # 实证=有trace证据；推断=基于常识的建议
                "finding": "问题描述",
                "evidence": "trace 证据（节点id/指标值）",
                "suggestion": "改进建议"
              }
            summary: 自然语言总述（整体评估结论）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        trace_id = ctx.candidate_trace or ctx.baseline_trace
        if not trace_id:
            return "错误：当前没有待评估的 trace"
        trace_kind = "candidate" if ctx.candidate_trace else "baseline"
        try:
            findings = json.loads(findings_json)
            if not isinstance(findings, list):
                return "findings_json 必须是 JSON 数组"

            # 取内容分数（后台任务可能已完成）
            content_scores: dict[str, Any] = {}
            key = f"{trace_kind}:{trace_id}"
            task = _content_tasks.get(key)
            if task and task.done():
                try:
                    cr = task.result()
                    if cr and not cr.get("error") and not cr.get("skipped"):
                        content_scores = cr
                except Exception:
                    pass

            # 算流程硬指标（D8：自动算，这里复算写进报告；注入在 prompt 层）
            from app.evolve.flow_metrics import compute_flow_metrics
            detail = get_trace(trace_id)
            flow_metrics = compute_flow_metrics(detail)

            path = docs.write_eval_report(
                ctx.session_id,
                trace_id=trace_id,
                trace_kind=trace_kind,
                content_scores=content_scores,
                flow_metrics=flow_metrics,
                findings=findings,
                summary=summary,
            )
            # 存路径到 ctx + DB
            if trace_kind == "baseline":
                ctx.eval_report_path = path
            else:
                ctx.candidate_eval_path = path
            from app.evolve import db as ev_db
            update_fields = {"eval_report_path" if trace_kind == "baseline" else "candidate_eval_path": path}
            ev_db.update_session(ctx.session_id, **update_fields)
            ctx.emit_step("write_eval_report", "done", phase="eval", path=path, findings=len(findings))
            return f"评估报告已产出：{path}（{len(findings)} 条诊断）"
        except json.JSONDecodeError as e:
            return f"findings_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("write_eval_report", "failed", phase="eval", error=str(e))
            return f"产出报告失败：{e}"

    return [
        read_trace,
        read_trace_node,
        read_trace_range,
        read_surface,
        get_content_score,
        write_eval_report,
    ]


__all__ = ["make_evaluate_tools", "EVALUATE_SYSTEM_PROMPT", "build_evaluate_subagent"]


# ── 评估子代理 system prompt（E21/E24：LLM 诊断判据指引）─────────


EVALUATE_SYSTEM_PROMPT = """\
你是 Writer 项目的「评估专家」——一个写作 Agent 流程与质量的诊断专家。

你的使命：评估一条写作 Agent 执行的 trace，从两大维度诊断——
① Agent 运行流程（协作拓扑/错误保障/资源消耗）② Agent 内容质量，
产出结构化诊断报告 eval_report.md，供下游方案专家据此设计改进。

## 工作流程

1. **看流程硬指标**：你已收到 flow_metrics（协作拓扑/错误保障/资源消耗三类指标，
   纯客观值）。先看这些数值，定位异常方向。
2. **读 trace 摘要**：调用 read_trace 纵观全局，看节点树结构、各子代理调用、错误分布。
3. **深挖关键节点**：对异常/可疑节点，用 read_trace_node 展开，必要时用 read_trace_range
   读完整区间。
4. **读设计意图**：调用 read_surface 看 HarnessConfig + 源码，判断「本该如何」。
5. **取内容分数**：调用 get_content_score 拿内容质量评分（内容6维 + subagent4维）。
6. **产出诊断**：调用 write_eval_report，提交 findings（诊断条目）+ summary（总述）。

## 诊断判据（核心）

你评估「流程好不好」时，判据来源是**你的写作领域常识推断的合理基线**（E21）。
但要遵守铁律：

- **区分实证 vs 推断**：每条诊断必须标 evidence_type。
  - 「实证」= 有明确 trace 证据支撑的问题（如「storybuilding 子代理 tool_error 3次，
    错误率 X%」）。这类诊断可信度高。
  - 「推断」= 基于你的常识判断「本应更好」（如「storybuilding 被委托 4 次，
    可能存在审查循环过多」）。这类是建议，需标注。
- **引用证据**：每条实证诊断的 evidence 字段必须指向具体 trace 现象
  （节点 id / 指标值 / 错误信息），不可空泛。
- **维度归属**：dimension 必须是四类之一：协作拓扑 / 错误保障 / 资源消耗 / 内容质量。

## 评估维度（写作垂直领域）

### 协作拓扑（判断协作流程设计是否合理）
- subagent 调用次数/顺序是否合理？有没有子代理被反复调用（审查循环过多）？
- 委托链深度（有没有过度嵌套）？evaluation 子代理数量是否过多？
- 并行组数是否合理（有没有本可并行却串行的）？
- 各子代理耗时占比（哪个子代理是瓶颈）？

### 错误保障（判断 Middleware 是否有效保障流程）
- 错误率/重试次数（哪个子代理出错多）？
- middleware 事件数（中间件介入是否合理，过多可能是过度拦截）？
- HITL 等待次数（ask_user 是否打断流程过频）？

### 资源消耗（判断 Prompt/Skills 效率）
- 总 token / 各子代理 token 占比（哪个子代理最耗 token）？
- 平均每次 LLM 调用 token（是否 prompt 过长）？
- 重复读同一文件（是否有冗余读取）？

### 内容质量（复用内容层评估，从 get_content_score 拿）
- 内容6维（爽点/节奏/留存等）+ subagent4维 各项分数。
- 哪些维度低分？对应哪个子代理的产物？

## 输出要求

write_eval_report 的 findings 是 JSON 数组，每条：
{
  "dimension": "协作拓扑|错误保障|资源消耗|内容质量",
  "severity": "high|medium|low",
  "evidence_type": "实证|推断",
  "finding": "问题描述（一句话）",
  "evidence": "trace 证据（节点id/指标值/错误信息）",
  "suggestion": "改进建议（具体到改什么 prompt/middleware/参数）"
}

至少产出 3 条诊断（没问题的维度也要总结一句），最多 10 条（聚焦最重要的）。
summary 是自然语言总述：整体流程是否可行、主要瓶颈在哪、最该改什么。
"""


# ── 评估子代理 spec 构建（D2：SubAgentSpec 原生挂载）────────────


def build_evaluate_subagent(model):
    """构建评估子代理（CompiledSubAgent），挂载到驱动器。

    注意（跨端隔离）：evolution 端直接 import deepagents（与 agent.py 一致），
    不走 executor 的 app.platform.agent.runtime（那是 executor 进程内的隔离层）。

    Args:
        model: 评估用的 LLM 模型

    Returns:
        CompiledSubAgent(name="evaluate", description=..., runnable=...)
    """
    from deepagents import CompiledSubAgent, create_deep_agent

    graph = create_deep_agent(
        model=model,
        tools=make_evaluate_tools(),
        system_prompt=EVALUATE_SYSTEM_PROMPT,
        middleware=[],
        subagents=None,
        checkpointer=None,
    )
    return CompiledSubAgent(
        name="evaluate",
        description=(
            "评估专家：诊断一条 trace 的 Agent 运行流程（协作拓扑/错误保障/资源消耗）"
            "和内容质量，产出 eval_report.md。委托时需告知 trace_id 和 case_id。"
        ),
        runnable=graph,
    )
