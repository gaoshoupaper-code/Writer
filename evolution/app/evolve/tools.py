"""进化 Agent 的领域工具集（6 个）。

DeepAgent 框架自带 read_file/write_file/edit_file/glob/grep/execute/task，
覆盖了"读写 harness 源码"。这里补 6 个进化专属工具：

  run_baseline()    跑当前 Agent 产 baseline trace
  run_candidate()   跑进化后 Agent 产 candidate trace
  read_trace(id)    读 trace 节点摘要
  read_surface()    读当前 HarnessConfig + harness 包源码清单
  read_verifier(id) 调 verifier 打分
  report(content)   产出对比报告（必须最后调用）

工具共享 EvolveContext（session 信息 + 事件总线）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool

from app.compose.bootstrap import build_v1_config
from app.core.settings import settings
from app.evolve import events as ev_events
from app.evolve import verifier
from app.evolve.evalset import load_case_demand

logger = logging.getLogger("evolution.evolve.tools")

# executor /ab/status 轮询参数
_POLL_INTERVAL = 10
_POLL_TIMEOUT = 3600


class EvolveContext:
    """工具间共享的 session 上下文（一次进化流程的载体）。

    每个工具通过闭包捕获唯一的 ctx 实例，实现状态传递 + 事件推送。
    """

    def __init__(self, session_id: str, case_id: str) -> None:
        self.session_id = session_id
        self.case_id = case_id
        self.events: ev_events.SessionEvents | None = None  # 由 api 启动时注入

        # 流程状态（工具间传递）
        self.baseline_trace: str = ""
        self.candidate_trace: str = ""
        self.baseline_score: float | None = None
        self.candidate_score: float | None = None
        self.report: dict[str, Any] = {}

        # 当前 config（run_baseline 用原始 config，run_candidate 用 Agent 改后的）
        # 改动检测：Agent 改完源码/edits 后，run_candidate 时重新 build config
        self._edits_path = (
            Path(__file__).resolve().parent.parent.parent
            / "data" / "evolve_workspace" / "edits.json"
        )

    # ── 事件推送便捷方法 ──

    def emit_step(self, tool: str, status: str, **extra: Any) -> None:
        if self.events:
            self.events.emit_step(tool, status, **extra)

    def emit_log(self, message: str) -> None:
        if self.events:
            self.events.emit_log(message)


# ── executor HTTP 调用 ─────────────────────────────────────


def _executor_url(path: str) -> str:
    return f"{settings.executor_url.rstrip('/')}{path}"


def _run_on_executor(
    *,
    baseline: bool,
    demand_md: str,
) -> str:
    """调 executor /ab/run 跑一次生成，轮询 /ab/status 拿 trace_id。

    Args:
        baseline: True=用当前 config（baseline），False=用 Agent 改后的 config + 源码
        demand_md: 预置的 demand.md 内容（interview 直通用）

    Returns:
        trace_id
    """
    # 候选模式：读 Agent 产出的 edits.json，apply 到当前 config
    if baseline:
        config = build_v1_config()
        candidate = False
    else:
        from app.compose import edits as edit_ops
        from app.compose.bootstrap import build_v1_config as _build

        base = _build()
        if ctx_global and ctx_global._edits_path.exists():
            import json
            try:
                edits_list = json.loads(
                    ctx_global._edits_path.read_text(encoding="utf-8")
                )
                if isinstance(edits_list, list) and edits_list:
                    config = edit_ops.apply_edits(base, edits_list)
                else:
                    config = base  # 无 edit，等同 baseline
            except Exception:
                logger.warning("edits.json 解析失败，用 baseline config", exc_info=True)
                config = base
        else:
            config = base  # 无 edits.json，等同 baseline
        candidate = True

    # 发起异步任务
    resp = httpx.post(
        _executor_url("/internal/ab/run"),
        json={
            "config": config,
            "demand_md": demand_md,
            "baseline": baseline,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    task_id = resp.json()["task_id"]
    logger.info("executor /ab/run 启动: task=%s baseline=%s", task_id, baseline)

    # 轮询直到完成
    deadline = time.time() + _POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        status_resp = httpx.get(
            _executor_url(f"/internal/ab/status/{task_id}"),
            timeout=10.0,
        )
        if status_resp.status_code == 404:
            raise RuntimeError(f"executor task {task_id} not found")
        status_resp.raise_for_status()
        data = status_resp.json()

        if data["status"] == "done":
            trace_ids = data.get("trace_ids", [])
            if not trace_ids:
                raise RuntimeError(f"executor task {task_id} 完成但无 trace_id")
            logger.info("executor task %s done: %s", task_id, trace_ids[0])
            return trace_ids[0]
        if data["status"] == "failed":
            raise RuntimeError(f"executor task {task_id} failed: {data.get('error')}")

    raise TimeoutError(f"executor task {task_id} 轮询超时")


# 模块级 ctx 占位（工具工厂 set_tool_context 时注入）
ctx_global: EvolveContext | None = None


def set_tool_context(ctx: EvolveContext) -> None:
    """注入当前 session 的 ctx（每次进化流程启动时调用）。"""
    global ctx_global
    ctx_global = ctx


# ── 6 个领域工具 ────────────────────────────────────────────


def _make_tools() -> list:
    """构建 6 个领域工具（捕获 ctx_global）。"""
    from app.view.traces import get_trace

    @tool
    def run_baseline() -> str:
        """跑当前 Agent（baseline）一次，返回 baseline trace_id。

        用当前 production config + 当前 harness 源码跑生成。
        这是进化流程的第一步——拿到 baseline 作为改进基准。
        """
        if ctx_global is None:
            return "错误：session 未初始化"
        ctx_global.emit_step("run_baseline", "running")
        try:
            demand_md = load_case_demand(ctx_global.case_id)
            trace_id = _run_on_executor(baseline=True, demand_md=demand_md)
            ctx_global.baseline_trace = trace_id

            # 落库
            from app.evolve import db as ev_db
            ev_db.update_session(
                ctx_global.session_id, baseline_trace=trace_id
            )
            ctx_global.emit_step(
                "run_baseline", "done", trace_id=trace_id
            )
            return f"baseline 跑完，trace_id={trace_id}"
        except Exception as e:
            ctx_global.emit_step("run_baseline", "failed", error=str(e))
            return f"baseline 跑失败：{e}"

    @tool
    def run_candidate() -> str:
        """跑进化后的 Agent 一次，返回 candidate trace_id。

        用 Agent 产出的改动（edits.json + 改动后的源码）跑生成。
        必须在产出改动之后调用。
        """
        if ctx_global is None:
            return "错误：session 未初始化"
        ctx_global.emit_step("run_candidate", "running")
        try:
            demand_md = load_case_demand(ctx_global.case_id)
            trace_id = _run_on_executor(baseline=False, demand_md=demand_md)
            ctx_global.candidate_trace = trace_id

            from app.evolve import db as ev_db
            ev_db.update_session(
                ctx_global.session_id, candidate_trace=trace_id
            )
            ctx_global.emit_step(
                "run_candidate", "done", trace_id=trace_id
            )
            return f"candidate 跑完，trace_id={trace_id}"
        except Exception as e:
            ctx_global.emit_step("run_candidate", "failed", error=str(e))
            return f"candidate 跑失败：{e}"

    @tool
    def read_trace(trace_id: str) -> str:
        """读取一个 trace 的节点摘要，返回可读的文本摘要。

        用于分析 baseline 或 candidate 的执行过程，找出可改进的点。
        摘要包含每个节点（agent/llm/tool/error）的关键信息，不含完整正文。

        Args:
            trace_id: 要读的 trace id
        """
        if ctx_global is None:
            return "错误：session 未初始化"
        ctx_global.emit_step("read_trace", "running", trace_id=trace_id)
        try:
            detail = get_trace(trace_id)
            run = detail.run
            lines = [
                f"trace_id: {run.trace_id}",
                f"状态: {run.status}  耗时: {run.duration_ms or '?'}ms  事件数: {run.event_count}",
            ]
            if run.error:
                lines.append(f"错误: {run.error[:300]}")
            lines.append(f"\n节点数: {len(detail.nodes)}")
            # 摘要每个非 run 节点
            for node in detail.nodes:
                if node.kind == "run":
                    continue
                parts = [f"  [{node.kind}]"]
                if node.agent_name:
                    parts.append(node.agent_name)
                if node.tool_name:
                    parts.append(f"tool={node.tool_name}")
                if node.status and node.status != "ok":
                    parts.append(f"status={node.status}")
                if node.error:
                    parts.append(f"err={node.error[:100]}")
                if node.chain_summary:
                    parts.append(f"| {node.chain_summary[:120]}")
                lines.append(" ".join(parts))
            ctx_global.emit_step("read_trace", "done", trace_id=trace_id)
            return "\n".join(lines)
        except Exception as e:
            ctx_global.emit_step("read_trace", "failed", error=str(e))
            return f"读 trace 失败：{e}"

    @tool
    def read_surface() -> str:
        """读取当前 Agent 的 surface（HarnessConfig + harness 包源码清单）。

        返回完整的 config JSON + harnesses/current/ 下所有文件路径。
        你可以用 read_file 读具体某个文件的完整内容。
        """
        if ctx_global is None:
            return "错误：session 未初始化"
        ctx_global.emit_step("read_surface", "running")
        try:
            config = build_v1_config()
            import json
            config_str = json.dumps(config, ensure_ascii=False, indent=2)

            # 列出 harness 包所有文件
            pkg_dir = settings.harness_work_dir_path
            files = []
            for p in sorted(pkg_dir.rglob("*")):
                if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
                    rel = p.relative_to(pkg_dir)
                    files.append(str(rel).replace("\\", "/"))

            result = (
                f"## HarnessConfig\n```json\n{config_str}\n```\n\n"
                f"## harness 包文件清单（{len(files)} 个）\n"
                + "\n".join(f"  {f}" for f in files)
            )
            ctx_global.emit_step("read_surface", "done", file_count=len(files))
            return result
        except Exception as e:
            ctx_global.emit_step("read_surface", "failed", error=str(e))
            return f"读 surface 失败：{e}"

    @tool
    def read_verifier(trace_id: str) -> str:
        """对一个 trace 跑 verifier 多次打分，返回 overall 分数（0-1）。

        用于评分对比：分别对 baseline 和 candidate 调用，比较两个分数判断是否改进。
        分数 = 3 次 LLM-judge 的 overall 均值。

        Args:
            trace_id: 要打分的 trace id
        """
        if ctx_global is None:
            return "错误：session 未初始化"
        ctx_global.emit_step("read_verifier", "running", trace_id=trace_id)
        try:
            result = verifier.score_trace(trace_id)
            if result.get("skipped"):
                ctx_global.emit_step(
                    "read_verifier", "done", trace_id=trace_id, skipped=True
                )
                return f"trace {trace_id} 无 writing 正文，跳过打分"

            score = result["overall"]
            # 记录到 ctx（report 前护栏检查）
            if ctx_global.baseline_trace == trace_id:
                ctx_global.baseline_score = score
                from app.evolve import db as ev_db
                ev_db.update_session(ctx_global.session_id, baseline_score=score)
            elif ctx_global.candidate_trace == trace_id:
                ctx_global.candidate_score = score
                from app.evolve import db as ev_db
                ev_db.update_session(ctx_global.session_id, candidate_score=score)

            ctx_global.emit_step(
                "read_verifier", "done",
                trace_id=trace_id, overall=round(score, 4), std=round(result["std"], 4),
            )
            return (
                f"trace {trace_id} 打分完成：overall={score:.4f}±{result['std']:.4f} "
                f"(3次: {[round(s,3) for s in result['samples']]})"
            )
        except Exception as e:
            ctx_global.emit_step("read_verifier", "failed", error=str(e))
            return f"打分失败：{e}"

    @tool
    def report(content: str) -> str:
        """产出最终的对比报告。这必须是进化流程的最后一步。

        报告内容应包含：改了什么、为什么改、baseline/candidate 分数、是否改进、结论。
        提交后报告会保存并推送给前端，人据此决定是否采纳改动。

        Args:
            content: 报告正文（markdown）
        """
        if ctx_global is None:
            return "错误：session 未初始化"
        ctx_global.emit_step("report", "running")

        # 护栏检查：report 前必须有两次分数
        if ctx_global.baseline_score is None or ctx_global.candidate_score is None:
            msg = (
                "报告前必须先对 baseline 和 candidate 都调用 read_verifier 打分。"
                f"当前 baseline_score={ctx_global.baseline_score}, "
                f"candidate_score={ctx_global.candidate_score}"
            )
            ctx_global.emit_step("report", "blocked", reason="缺分数")
            return msg

        improved = ctx_global.candidate_score > ctx_global.baseline_score
        delta = ctx_global.candidate_score - ctx_global.baseline_score

        report_data = {
            "content": content,
            "baseline_score": ctx_global.baseline_score,
            "candidate_score": ctx_global.candidate_score,
            "delta": round(delta, 4),
            "improved": improved,
            "baseline_trace": ctx_global.baseline_trace,
            "candidate_trace": ctx_global.candidate_trace,
        }
        ctx_global.report = report_data

        # 落库 + 推送
        from app.evolve import db as ev_db
        ev_db.update_session(
            ctx_global.session_id, status="done", report=report_data,
        )
        ctx_global.emit_report(report_data)
        ctx_global.emit_step(
            "report", "done", improved=improved, delta=round(delta, 4),
        )
        return (
            f"报告已提交。baseline={ctx_global.baseline_score:.4f} "
            f"candidate={ctx_global.candidate_score:.4f} "
            f"{'↑改进' if improved else '↓未改进/持平'}（Δ={delta:+.4f}）"
        )

    return [run_baseline, run_candidate, read_trace, read_surface, read_verifier, report]


__all__ = ["EvolveContext", "set_tool_context", "_make_tools"]
