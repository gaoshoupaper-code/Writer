"""执行子代理装配（决策 D12/D14/E17）。

构建 CompiledSubAgent 并挂载到驱动器。工具来自 execute.tools，
system prompt 来自 execute.prompt。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.trace.recorder import EvolutionTraceRecorder


def build_execute_subagent(
    model,
    *,
    recorder: "EvolutionTraceRecorder | None" = None,
    trace_id_self: str = "",
):
    """构建执行子代理（CompiledSubAgent），挂载到驱动器。

    Args:
        model:         复用驱动器的模型
        recorder:      自观测 trace recorder（进化 ctx 注入）
        trace_id_self: 自观测 trace id（middleware 据此写入事件）
    """
    from deepagents import CompiledSubAgent, create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    from app.core.settings import settings
    from app.evolve.subagents.execute.prompt import EXECUTE_SYSTEM_PROMPT
    from app.evolve.subagents.execute.tools import make_execute_tools
    from app.trace import TraceMiddleware

    # DeepAgents 的 middleware 不从父 agent 传播到子 agent
    # （SubAgentMiddleware._build_subagent_config 只转发 callbacks/tags/configurable），
    # 故子代理必须各自挂 TraceMiddleware，否则其内部 LLM/工具调用不会被记录。
    middleware_list = []
    if recorder and trace_id_self:
        middleware_list.append(
            TraceMiddleware(recorder=recorder, trace_id=trace_id_self, agent_name="evolve-execute")
        )

    # backend=FilesystemBackend：让框架自带 write_file/edit_file 真正落盘到
    # harnesses/current/（root_dir 锁定 + 路径越界拦截）。
    # 此前未传 backend，框架默认 StateBackend()（纯内存），execute 的源码改动
    # 从不落盘——发版 git commit 抓不到、bootstrap 也读不到，导致 v1==v2。
    # virtual_mode=True：把 root_dir 作为虚拟根，阻止绝对路径 / `..` 越界
    # （False 时绝对路径可绕过 root_dir，安全隐患）。
    backend = FilesystemBackend(
        root_dir=str(settings.harness_work_dir_path),
        virtual_mode=True,
    )

    graph = create_deep_agent(
        model=model,
        tools=make_execute_tools(),
        system_prompt=EXECUTE_SYSTEM_PROMPT,
        middleware=middleware_list,
        subagents=None,
        checkpointer=None,
        backend=backend,
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
