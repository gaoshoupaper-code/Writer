"""执行子代理装配（决策 D12/D14/E17）。

构建 CompiledSubAgent 并挂载到驱动器。工具来自 execute.tools，
system prompt 来自 execute.prompt。
"""
from __future__ import annotations


def build_execute_subagent(model):
    """构建执行子代理（CompiledSubAgent），挂载到驱动器。"""
    from deepagents import CompiledSubAgent, create_deep_agent

    from app.evolve.subagents.execute.prompt import EXECUTE_SYSTEM_PROMPT
    from app.evolve.subagents.execute.tools import make_execute_tools

    graph = create_deep_agent(
        model=model,
        tools=make_execute_tools(),
        system_prompt=EXECUTE_SYSTEM_PROMPT,
        middleware=[],
        subagents=None,
        checkpointer=None,
        # 工作目录设为项目根，让框架自带 write_file/edit_file 能改 harness 包源码。
        # path_guard 由调用方/框架约束只改 harnesses/current/。
    )
    return CompiledSubAgent(
        name="execute",
        description=(
            "执行专家：读 design_doc 落地改动（apply_edits 配置层 + write/edit_file 源码层），"
            "校验可加载，产 change_log.md。委托时无需额外参数。"
        ),
        runnable=graph,
    )


__all__ = ["build_execute_subagent"]
