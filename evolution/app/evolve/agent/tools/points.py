"""进化点工具（4 个）——对话式共创工作台核心工具（决策 B/T/T2.5）。

Agent 通过这 4 个工具维护进化点状态（双轨制权威源），前端浮窗据此渲染：
  - propose_evolution_point  提出新改进点（status=proposed）
  - update_evolution_point   更新已有进化点（用户表态后回写 chosen_option/user_note）
  - reject_evolution_point   否决进化点（status=rejected）
  - list_evolution_points    列出当前 session 所有进化点（含 status）

调用约束：
  - 这些工具在 conversing 阶段使用（FlowGuard 不拦——只拦落地工具）
  - 用户对话内容 → Agent 解析意图 → 调对应工具更新状态
  - 工具调用是"权威状态变更"，自由文本探讨不进浮窗（决策 B 双轨制）

propose 数据结构（决策 T 完整结构）：
  target / problem / options[] / recommendation / note
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

from app.evolve.ctx import get_tool_context
from app.evolve.evolve_repo import EvolvePointsRepo

logger = logging.getLogger("evolution.evolve.agent.tools.points")


def make_points_tools() -> list:
    """构建进化点工具集（4 个，决策 T2.5）。

    Returns:
        [propose_evolution_point, update_evolution_point,
         reject_evolution_point, list_evolution_points]
    """

    @tool
    def propose_evolution_point(
        target: str,
        problem: str,
        options_json: str,
        recommendation: str = "",
        note: str = "",
    ) -> str:
        """提出一个新的进化点（改进建议），等待用户在对话中表态。

        这是 conversing 阶段的核心工具——你（Agent）基于评估报告 + 探查要素
        的理解，提出一个改进点，列出 2-3 个备选方案，让用户选择/否决/补充。

        **何时调用**：
        - 你已经分析了某个评估问题，想出改进方向 → 调本工具提出
        - 不要在一条消息里 propose 多个点——一个点一个调用，让用户聚焦讨论
        - propose 前先用自由文本向用户解释你的分析（pros/cons 讨论），
          然后调本工具固化结构化方案

        Args:
            target: 要改的要素路径（如 "middleware/retry.py" / "prompts/meta_system.md" /
                    "subagents/writing.py"）。具体到文件，方便落地。
            problem: 为什么要改——基于哪条评估 finding，描述当前问题。
                     格式建议："评估 finding fXX 指出 ...（引用证据）"
            options_json: 备选方案 JSON 数组字符串。每个方案：
                {
                  "description": "方案描述（具体怎么改）",
                  "pros": ["优点1", "优点2"],
                  "cons": ["缺点1"],
                  "expected_impact": "预期影响（如 +5% / 改善 X 维度）"
                }
                至少 2 个方案（让用户有对比），至多 4 个（避免选择疲劳）。
            recommendation: 你推荐哪个方案 + 理由（自由文本）。
                            格式建议："推荐方案 N，因为 ..."
                            这只是建议，最终由用户决定。
            note: 补充说明（可选）——粒度理由、风险提示、与其他进化点的关系等。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"

        ctx.emit_step("propose_evolution_point", "running")
        try:
            options = json.loads(options_json)
            if not isinstance(options, list):
                return "options_json 必须是 JSON 数组"
            if not (2 <= len(options) <= 4):
                return f"options 数量需在 2-4 之间（当前 {len(options)}）——让用户有对比但不疲劳"
            for i, opt in enumerate(options):
                if not isinstance(opt, dict):
                    return f"options[{i}] 必须是对象"
                for field in ("description", "pros", "cons", "expected_impact"):
                    if field not in opt:
                        return f"options[{i}] 缺少必填字段：{field}"
                if not isinstance(opt["pros"], list) or not isinstance(opt["cons"], list):
                    return f"options[{i}].pros/cons 必须是数组"

            point = EvolvePointsRepo.propose(
                ctx.session_id,
                target=target,
                problem=problem,
                options=options,
                recommendation=recommendation or None,
                note=note or None,
            )
            ctx.emit_step(
                "propose_evolution_point", "done",
                point_id=point["id"], target=target,
            )
            return (
                f"已提出进化点 #{point['seq']}（id={point['id']}）：{target}\n"
                f"现在请在对话中告诉用户，等用户表态（选择方案/否决/补充）后，"
                f"调 update_evolution_point 或 reject_evolution_point 更新状态。"
            )
        except json.JSONDecodeError as e:
            ctx.emit_step("propose_evolution_point", "failed", error=str(e))
            return f"options_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("propose_evolution_point", "failed", error=str(e))
            return f"提出进化点失败：{e}"

    @tool
    def update_evolution_point(
        point_id: str,
        chosen_option: int,
        user_note: str = "",
    ) -> str:
        """更新进化点状态为「已采纳」（accepted）。

        **何时调用**：
        - 用户在对话中明确选择了某个方案 → 调本工具固化决策
        - chosen_option 是用户选的方案下标（0-based：第一个方案=0，第二个=1，...）
        - 用户可能附带了修改意见 → 填 user_note

        如果用户要求**修改方案内容**（不是选已有方案），先用 update 后再 propose，
        或与用户继续讨论后用自由文本说明 + 调本工具记录最终选择。

        Args:
            point_id: 进化点 id（propose 时返回的）
            chosen_option: 用户选的方案下标（0-based）
            user_note: 用户附加说明（修改意见、顾虑、期望等）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"

        ctx.emit_step("update_evolution_point", "running", point_id=point_id)
        try:
            # 校验 chosen_option 在合理范围（先查 point）
            point = EvolvePointsRepo.get_by_id(point_id)
            if point is None:
                return f"进化点 {point_id} 不存在"
            if point["session_id"] != ctx.session_id:
                return f"进化点 {point_id} 不属于当前 session"
            if not (0 <= chosen_option < len(point["options"])):
                return (
                    f"chosen_option {chosen_option} 越界——"
                    f"该进化点有 {len(point['options'])} 个方案，下标 0-{len(point['options'])-1}"
                )

            updated = EvolvePointsRepo.accept(
                point_id, chosen_option=chosen_option, user_note=user_note or None,
            )
            ctx.emit_step(
                "update_evolution_point", "done",
                point_id=point_id, chosen=chosen_option,
            )
            chosen_desc = updated["options"][chosen_option]["description"] if updated else "?"
            return (
                f"进化点 #{updated['seq']} 已采纳：方案 {chosen_option}（{chosen_desc}）\n"
                f"用户可在右侧浮窗看到状态变为 ✓ 已采纳。"
            )
        except Exception as e:
            ctx.emit_step("update_evolution_point", "failed", error=str(e))
            return f"更新进化点失败：{e}"

    @tool
    def reject_evolution_point(
        point_id: str,
        reason: str = "",
    ) -> str:
        """否决进化点（status=rejected）。

        **何时调用**：
        - 用户在对话中明确表示"不要这个点" / "暂时不做" / "风险太大"
        - reason 记录用户否决的理由（便于后续回顾）

        注意：rejected 进化点不会进入最终 design_doc（拍板时只取 accepted）。

        Args:
            point_id: 进化点 id
            reason: 否决理由（用户的话总结）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"

        ctx.emit_step("reject_evolution_point", "running", point_id=point_id)
        try:
            point = EvolvePointsRepo.get_by_id(point_id)
            if point is None:
                return f"进化点 {point_id} 不存在"
            if point["session_id"] != ctx.session_id:
                return f"进化点 {point_id} 不属于当前 session"

            updated = EvolvePointsRepo.reject(point_id, user_note=reason or None)
            ctx.emit_step("reject_evolution_point", "done", point_id=point_id)
            return (
                f"进化点 #{updated['seq']} 已否决（{point['target']}）。"
                f"用户可在右侧浮窗看到状态变为 ✗ 已否决。"
            )
        except Exception as e:
            ctx.emit_step("reject_evolution_point", "failed", error=str(e))
            return f"否决进化点失败：{e}"

    @tool
    def list_evolution_points() -> str:
        """列出当前 session 的所有进化点（含状态）。

        用于你（Agent）在对话中回顾已讨论的进化点——避免重复 propose、
        检查哪些还没表态。

        Returns:
            进化点清单（按提出顺序）。每个点含 seq/target/status/已选方案。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"

        points = EvolvePointsRepo.list_by_session(ctx.session_id)
        if not points:
            return "当前 session 还没有提出任何进化点。"

        lines = [f"共 {len(points)} 个进化点："]
        status_icon = {"proposed": "○", "accepted": "✓", "rejected": "✗"}
        for p in points:
            icon = status_icon.get(p["status"], "?")
            line = f"  {icon} #{p['seq']} [{p['status']}] {p['target']}"
            if p["status"] == "accepted" and p["chosen_option"] is not None:
                chosen = p["options"][p["chosen_option"]]["description"] if p["chosen_option"] < len(p["options"]) else "?"
                line += f" → 方案：{chosen}"
            lines.append(line)
        return "\n".join(lines)

    return [
        propose_evolution_point,
        update_evolution_point,
        reject_evolution_point,
        list_evolution_points,
    ]


__all__ = ["make_points_tools"]
