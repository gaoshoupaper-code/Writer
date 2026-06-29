"""DemandPreloadMiddleware — interview 直通中间件（评估集自动跑）。

职责：
  当 workspace 已有 confirmed 的 demand.md 时（评估集预置场景），
  在 MetaAgent（Director）启动前注入指令：跳过 interview 子代理，
  直接委托 storybuilding 开始创作。

触发条件（全部满足才注入）：
  1. workspace 下存在 demand.md
  2. demand.md 元信息段含 "status: confirmed"

只在 A/B（/ab/run）路径装配——生产交互路径不挂此中间件，交互创作不受影响。

设计依据：.claude/md/20260627_135113_进化端单Agent设计.md（D3/D9）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime

# 元信息段检查窗口（demand.md 顶部的 HTML 注释块）
_META_WINDOW = 300

# 直通注入指令
_BYPASS_INSTRUCTION = (
    "【评估模式】检测到 workspace 已有 confirmed 的 demand.md，"
    "需求已就绪。跳过 interview 子代理，直接委托 storybuilding 子代理开始故事构建。"
    "委托 storybuilding 时，description 中注明「demand.md 已确认，请阅读后开始 skeleton 构建」。"
    "不要调用 interview，不要重复访谈。"
)


class DemandPreloadMiddleware(AgentMiddleware):
    """interview 直通：检测到 confirmed demand.md 则跳过 interview。

    装配时传入 workspace_path（assemble 从 ctx.workspace_path 注入）。
    """

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path

    def before_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """MetaAgent 启动前检查 demand.md。"""
        demand_path = self.workspace_path / "demand.md"
        if not demand_path.exists():
            return None

        try:
            content = demand_path.read_text(encoding="utf-8")
        except Exception:
            return None

        # 检查元信息段是否 confirmed
        meta_section = content[:_META_WINDOW]
        if "status: confirmed" not in meta_section:
            return None

        # 注入直通指令
        return {
            "messages": [SystemMessage(content=_BYPASS_INSTRUCTION)]
        }


__all__ = ["DemandPreloadMiddleware"]
