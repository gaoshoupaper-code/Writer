"""流程硬指标算子（决策 E10 / D8）。

从 TraceDetail（nodes + events）算四类流程硬指标，供评估子代理诊断。
代码从 trace 算，无 LLM 成本，确定性分数。

四类指标（E10 + G003）：
  1. 协作拓扑：subagent 调用顺序/次数、委托链深度、并行组数、各 subagent 耗时占比。
     评估「协作流程设计」。
  2. 错误保障：错误率、重试次数、middleware 拦截事件数、HITL 等待次数。
     评估「Middleware 保障」。
  3. 资源消耗：总 token、各阶段 token 分布、重复读同一文件次数。
     评估「Prompt/Skills 效率」。
  4. 记忆系统（G003）：memory_recall middleware 写的 run_meta 事件计数。
     记忆系统"本该参与却没参与"是缺失性缺陷的标本案例（EVD-002），需要可数才能
     做确定性契约校验（FR-001）。注意：成功参与只数 retrieval_ok==True 的事件——
     degraded/失败路径也写事件（middleware:106/115/119/144），不能只数事件存在。

数据来源：
  - nodes（TraceNode）：agent_role / agent_name / parallel_group_id / depth /
    duration_ms / usage / kind。
  - events（TraceLogEvent）：type / source / tool_name / usage / error。
  - 记忆埋点：run_meta 事件的 input.memory_quality（TraceMemoryQuality）。

判据来源（E21/E24）：本模块只算指标值（客观），不做"好坏"判断——
"是否异常"交给评估子代理的 LLM（凭指标值 + 写作常识判断）。
例外：记忆维度除供 LLM 诊断外，还供 FR-001 确定性契约层直接消费
（memory_participated 布尔值是契约可判定的客观信号）。

设计依据：设计文档 D8（flow_metrics 自动算注入）/ E10（三类指标）/ G003（记忆可数）。
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any

from contracts.trace import TraceDetail, TraceLogEvent, TraceNode

logger = logging.getLogger("evolution.common.flow_metrics")


def compute_flow_metrics(detail: TraceDetail) -> dict[str, Any]:
    """从 TraceDetail 算四类流程硬指标。

    Args:
        detail: 完整 trace 详情（nodes + events + run summary）

    Returns:
        {
          "topology": {协作拓扑指标},
          "reliability": {错误保障指标},
          "resources": {资源消耗指标},
          "memory": {记忆系统参与指标},
        }
    """
    return {
        "topology": _topology_metrics(detail.nodes),
        "reliability": _reliability_metrics(detail.nodes, detail.events),
        "resources": _resource_metrics(detail.nodes, detail.events),
        "memory": _memory_metrics(detail.events),
    }


# ── 1. 协作拓扑 ────────────────────────────────────────────────


def _topology_metrics(nodes: list[TraceNode]) -> dict[str, Any]:
    """协作拓扑：subagent 调用、委托链深度、并行组、耗时占比。

    判断子代理靠 agent_name.endswith("-subagent")（与 projector._agent_role 一致）。
    """
    agent_nodes = [n for n in nodes if n.kind == "agent" and n.agent_name]
    subagent_nodes = [n for n in agent_nodes if n.agent_name.endswith("-subagent")]

    # 各 subagent 调用次数（同 subagent 可被多次委托）
    subagent_calls = Counter(n.agent_name for n in subagent_nodes)

    # 各 subagent 总耗时占比（duration_ms 累加 / 全部 agent 耗时）
    total_agent_ms = sum(n.duration_ms or 0 for n in agent_nodes) or 1
    subagent_ms: dict[str, int] = defaultdict(int)
    for n in subagent_nodes:
        subagent_ms[n.agent_name] += n.duration_ms or 0
    duration_share = {
        name: round(ms / total_agent_ms, 3) for name, ms in subagent_ms.items()
    }

    # 委托链深度（nodes 的最大 depth；main=0, subagent=1, evaluation=2）
    max_depth = max((n.depth for n in nodes), default=0)

    # 并行组数（parallel_group_id 非空的唯一值）
    parallel_groups = {n.parallel_group_id for n in nodes if n.parallel_group_id}

    # 子代理调用顺序（按 started_at 排序的 subagent 名序列）
    ordered_subs = sorted(subagent_nodes, key=lambda n: n.started_at or "")
    call_sequence = [n.agent_name for n in ordered_subs]

    # evaluation 子代理数（嵌套委托，depth=2）
    eval_count = sum(1 for n in subagent_nodes if "evaluation" in (n.agent_name or ""))

    return {
        "subagent_types": len(subagent_calls),
        "subagent_calls_total": sum(subagent_calls.values()),
        "subagent_calls_by_name": dict(subagent_calls),
        "subagent_duration_share": duration_share,
        "delegation_depth_max": max_depth,
        "parallel_groups": len(parallel_groups),
        "call_sequence": call_sequence,
        "evaluation_subagent_calls": eval_count,
    }


# ── 2. 错误保障 ────────────────────────────────────────────────


def _reliability_metrics(
    nodes: list[TraceNode], events: list[TraceLogEvent]
) -> dict[str, Any]:
    """错误保障：错误率、重试、middleware 拦截、HITL 等待。

    数据来源：
      - tool_error / run_error / llm_error 事件 → 错误次数
      - source=middleware 事件 → middleware 拦截/介入次数
      - run_awaiting 事件 → HITL 等待次数
      - node.status != ok 的节点 → 异常节点
    """
    # 错误事件（按类型）
    error_events = [e for e in events if e.type.endswith("_error") or e.type == "run_error"]
    error_by_type = Counter(e.type for e in error_events)

    # 错误的 agent 分布（哪个子代理出错多）
    error_by_agent = Counter(e.agent_name for e in error_events if e.agent_name)

    # 工具调用总数（算错误率的分母）
    tool_starts = [e for e in events if e.type == "tool_start"]
    tool_total = len(tool_starts)
    tool_errors = error_by_type.get("tool_error", 0)
    tool_error_rate = round(tool_errors / tool_total, 4) if tool_total else 0.0

    # middleware 拦截/介入事件数（source=middleware）
    middleware_events = [e for e in events if e.source == "middleware"]

    # HITL 等待（run_awaiting）
    hitl_waits = sum(1 for e in events if e.type == "run_awaiting")

    # review/revision 相关（task 调用里含 review，或 tool_name 含 review）
    review_calls = sum(
        1 for e in events
        if e.tool_name and "review" in e.tool_name.lower()
    )

    return {
        "tool_error_rate": tool_error_rate,
        "tool_errors": tool_errors,
        "tool_calls_total": tool_total,
        "error_events_total": len(error_events),
        "error_by_type": dict(error_by_type),
        "error_by_agent": dict(error_by_agent),
        "middleware_events": len(middleware_events),
        "hitl_waits": hitl_waits,
        "review_calls": review_calls,
    }


# ── 3. 资源消耗 ────────────────────────────────────────────────


def _resource_metrics(
    nodes: list[TraceNode], events: list[TraceLogEvent]
) -> dict[str, Any]:
    """资源消耗：总 token、各阶段 token 分布、重复读同一文件次数。

    数据来源：
      - events 的 usage（llm_start/llm_end 携带）→ token
      - tool_name=read_file 的 tool_args（文件路径）→ 重复读
    """
    # 总 token（累加所有事件的 usage.total_tokens，llm_end 是实际消耗）
    llm_ends = [e for e in events if e.type == "llm_end" and e.usage]
    total_tokens = sum((e.usage.total_tokens or 0) for e in llm_ends)
    total_input = sum((e.usage.input_tokens or 0) for e in llm_ends)
    total_output = sum((e.usage.output_tokens or 0) for e in llm_ends)

    # 各 agent token 分布
    token_by_agent: dict[str, int] = defaultdict(int)
    for e in llm_ends:
        if e.agent_name and e.usage and e.usage.total_tokens:
            token_by_agent[e.agent_name] += e.usage.total_tokens

    # 各 agent token 占比
    token_share = {
        name: round(t / total_tokens, 3) for name, t in token_by_agent.items()
    } if total_tokens else {}

    # 重复读同一文件：tool_name=read_file 的 tool_args（路径）出现次数
    read_calls = [
        e for e in events
        if e.tool_name == "read_file" and e.type == "tool_start"
    ]
    # tool_args 可能是 dict（含 path）或被 sanitize 掉（None）
    file_reads: Counter[str] = Counter()
    for e in read_calls:
        args = e.tool_args
        path = None
        if isinstance(args, dict):
            path = args.get("path") or args.get("file_path")
        elif isinstance(args, str):
            path = args
        if path:
            file_reads[path] += 1
    # 重复读 = 同一文件被读 >1 次
    repeated_reads = {path: cnt for path, cnt in file_reads.items() if cnt > 1}
    repeated_read_waste = sum(cnt - 1 for cnt in file_reads.values() if cnt > 1)

    # 平均每次 LLM 调用 token（效率指标）
    llm_calls = len(llm_ends)
    avg_tokens_per_call = round(total_tokens / llm_calls) if llm_calls else 0

    return {
        "total_tokens": total_tokens,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "token_share_by_agent": token_share,
        "llm_calls": llm_calls,
        "avg_tokens_per_call": avg_tokens_per_call,
        "read_file_calls": len(read_calls),
        "repeated_read_files": len(repeated_reads),
        "repeated_read_waste": repeated_read_waste,
        "repeated_read_details": repeated_reads,
    }


# ── 4. 记忆系统参与（G003）────────────────────────────────────


def _memory_metrics(events: list[TraceLogEvent]) -> dict[str, Any]:
    """记忆系统参与指标：从 run_meta 的 memory_quality 埋点计数。

    执行端 memory_recall middleware 每次召回写一条 run_meta 事件（input.memory_quality），
    G003 要让"记忆系统本该参与却没参与"这类缺失性缺陷可数、可做确定性契约校验。

    关键：memory_participated 只看成功召回（retrieval_ok==True）的事件——
    degraded/失败路径（backend 不健康、健康检查失败、retrieve 抛异常）也写事件，
    不能只数事件存在，否则"记忆系统挂了"会被误判成"记忆系统参与了"。

    与 dossier/extractor._extract_memory_quality 的关系：两者扫同一批 run_meta 事件，
    但 extractor 冻结的是逐章明细（entries + 聚合 summary）进 facts.memory_quality；
    本函数在 flow_metrics 层给出"可数布尔信号"，供评估流程诊断与 FR-001 契约层
    直接消费 memory_participated。

    Returns:
        {
          "memory_recall_events": 写入的 run_meta 事件总数（含失败），
          "memory_recall_ok": 成功召回次数（retrieval_ok==True），
          "memory_recall_failed": 失败次数，
          "memory_participated": 成功召回 ≥1 次（契约可消费的布尔信号），
          "memory_available": 是否存在任何记忆埋点（含失败，即记忆系统被装配过），
        }
    """
    recall_events = [
        e for e in events
        if e.type == "run_meta"
        and isinstance(e.input, dict)
        and isinstance(e.input.get("memory_quality"), dict)
    ]
    ok_count = sum(
        1 for e in recall_events
        if bool(e.input.get("memory_quality", {}).get("retrieval_ok", True))
    )
    total = len(recall_events)
    return {
        "memory_recall_events": total,
        "memory_recall_ok": ok_count,
        "memory_recall_failed": total - ok_count,
        "memory_participated": ok_count >= 1,
        "memory_available": total >= 1,
    }


__all__ = ["compute_flow_metrics"]
