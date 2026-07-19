"""进化 Agent 工具集聚合入口（决策 S2 + T2.5 进化点工具）。

按功能类型分 4 模块，本 __init__.py 聚合为 make_evolve_tools(backend)：
  - inspect.py   4 探查工具（只读，给认知）
  - writers.py   5 写 + 1 edit（受控写，封装 backend）
  - flow.py      5 流程工具（评估消费 + 产出 + 校验）
  - points.py    4 进化点工具（对话式共创，决策 T2.5）

工具总数 19（Phase 2B 新增 4 个进化点工具）。调用方：
  from app.evolve.agent.tools import make_evolve_tools
  tools = make_evolve_tools(backend=backend)
"""
from __future__ import annotations

from app.evolve.agent.tools.flow import make_flow_tools
from app.evolve.agent.tools.inspect import make_inspect_tools
from app.evolve.agent.tools.points import make_points_tools
from app.evolve.agent.tools.writers import make_writer_tools


def make_evolve_tools(backend) -> list:
    """构建进化 Agent 的完整工具集（聚合 4 子模块，19 工具）。

    Args:
        backend: FilesystemBackend 实例（writers 工具内部调用它落盘）。
                 None 时报错——writers 必须有 backend 才能工作。

    Returns:
        19 个 BaseTool 实例的列表。
    """
    tools: list = []
    tools.extend(make_inspect_tools())     # 4 探查（不需 backend）
    tools.extend(make_writer_tools(backend))  # 5 写 + 1 edit（需 backend）
    tools.extend(make_flow_tools())        # 5 流程（不需 backend）
    tools.extend(make_points_tools())      # 4 进化点（决策 T2.5，不需 backend）
    return tools


__all__ = ["make_evolve_tools"]
