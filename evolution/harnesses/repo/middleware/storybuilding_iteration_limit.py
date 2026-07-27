"""StorybuildingIterationLimitMiddleware — Storybuilding 全局迭代上限中间件。

职责：
  挂载到 storybuilding 子代理。跨多次 task 调用累计计数（**不在 before_agent 中重置**），
  超过 max_iterations 后在 before_model 注入终止指令，使 storybuilding 子代理
  立即停止扩展、返回收尾摘要，从而迫使 meta-agent 推进到 detail-outline。

背景：
  RevisionLimitMiddleware 和 StorylineSingleLineLimitMiddleware 都在 before_agent
  中清零计数（计数周期=单次 task 调用）。本中间件不同——计数器在整个 meta-agent
  运行期间持续累计，实现"storybuilding 总共最多被调用 N 次"的全局约束。

计数周期 = 整个 meta-agent 运行会话（跨多次 task 调用，不重置）。
实例生命周期 = CompiledSubAgent 实例（一次编译、多次复用），与 RevisionLimit 一致。
"""
from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import HumanMessage


class StorybuildingIterationLimitMiddleware(AgentMiddleware):
    """Storybuilding 全局迭代上限中间件。

    每次 storybuilding 子代理被 task 调用时（before_agent 触发），计数器 +1。
    当计数器超过 max_iterations 时，before_model 注入一条强制收尾指令，
    使子代理立即停止扩展操作、返回摘要、建议父代理推进到 detail-outline。
    """

    def __init__(self, *, max_iterations: int = 5) -> None:
        """
        Args:
            max_iterations: storybuilding 子代理最多被调用的次数（全局，跨多次 task 调用）。
                            默认 5：初构 1 次 + 增量最多 4 次。
        """
        self.max_iterations = max_iterations
        self._iteration_count = 0

    # ------------------------------------------------------------------
    # 调用周期计数（不重置！跨多次 task 调用累计）
    # ------------------------------------------------------------------

    def before_agent(self, state: Any, runtime: Any) -> None:
        """每次 storybuilding graph 执行开始时 +1（不重置）。"""
        self._iteration_count += 1

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        """每次 storybuilding graph 执行开始时 +1（不重置）。"""
        self._iteration_count += 1

    # ------------------------------------------------------------------
    # 模型调用前注入终止指令
    # ------------------------------------------------------------------

    def before_model(self, request: Any) -> dict | None:
        """超过迭代上限时注入强制收尾指令。"""
        if self._iteration_count <= self.max_iterations:
            return None

        return {
            "messages": [
                HumanMessage(
                    content=(
                        f"[系统指令·迭代上限] 故事构建阶段已被调用 {self._iteration_count} 次，"
                        f"超过最大迭代数 {self.max_iterations}。\n\n"
                        "**必须立即执行以下操作，不得进行任何扩展：**\n"
                        "1. 不要读取文件、不要创建新内容、不要调用 review。\n"
                        "2. 直接基于当前已有的故事构建产物，写一段简要总结返回给父代理。\n"
                        "3. 在返回消息中**明确告知父代理**：故事构建已完成，"
                        "必须立即推进到 detail-outline（细纲）阶段，不得再次委托 storybuilding。\n\n"
                        "这是强制指令——跳过所有创作操作，直接收尾返回。"
                    )
                )
            ]
        }

    async def abefore_model(self, request: Any) -> dict | None:
        """异步版本：超过迭代上限时注入强制收尾指令。"""
        return self.before_model(request)
