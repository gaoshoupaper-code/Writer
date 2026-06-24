"""包装配的运行时上下文契约（共享包，零三方依赖）。

定义 RuntimeContext——执行端构建、Agent 包 assemble(ctx) 消费的运行时上下文。
它是执行端与 Agent 包之间的唯一耦合点：包只读此对象，不直接依赖执行端其他任何东西。

设计依据：设计文档 D5=②（单 RuntimeContext 对象）+ T2（TraceMiddleware 类由 ctx 传入）。

为什么用 dataclass + TYPE_CHECKING（而非 pydantic）：
  - contracts 包铁律：零三方依赖（见 contracts/__init__.py）。pydantic 在 trace/api
    子包用，但 runtime_context 是纯类型契约，不应引入运行时校验依赖。
  - TYPE_CHECKING 让 langchain/deepagents 类型只在静态检查时可见，运行时不 import，
    保持本模块零依赖。annotations 全字符串化（from __future__ import annotations）。
  - dataclass 字段无运行时类型校验，调用方传错类型运行时不报错——但这是契约层的取舍，
    真实类型由执行端装配代码保证。

RuntimeContext 的语义分层（虽不强制拆分，供理解）：
  - 请求级（每次 assemble 新建）：model/backend/checkpointer/workspace_path/trace_id/owner_id
  - 平台级（进程内复用）：trace_recorder/trace_middleware_cls

trace_recorder + trace_middleware_cls 的组合语义（T2 设计）：
  - 两者都非 None → 包 assemble 内实例化 TraceMiddleware 并插入 middleware 列表
  - 任一为 None → 包不挂载 TraceMiddleware（无追踪）
  - TraceMiddleware 类定义在执行端（不进包，D2'），但实例化+挂载位置由包决定
    （create_deep_agent 编译后无法插 middleware，必须在编译前加入）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langgraph.types import Checkpointer

    # BackendProtocol 来自 deepagents，但此处只用于类型注解，不 import。
    # 用 Any 避免对 deepagents 的 TYPE_CHECKING 依赖（deepagents 仅 executor 端有）。


@dataclass
class RuntimeContext:
    """包装配的运行时上下文（请求级 + 平台级混合，装配时一次性注入）。

    包 assemble(ctx) 只读此对象，不直接依赖执行端其他任何东西。
    所有字段在执行端构建 ctx 时填入，包内只消费。

    Attributes:
        model: 已构建的聊天模型实例（按 owner/key 构建，执行端负责）。
        backend: 文件系统后端（按 workspace_path 构建，执行端负责）。
        checkpointer: checkpoint saver（按 owner 分库，执行端负责）。
        workspace_path: 当前请求的 workspace 绝对路径（请求级，PathGuard/
            ArtifactPrerequisite 等中间件用）。
        trace_id: 当前追踪会话标识。None 表示不追踪。
        owner_id: 请求所有者标识（多用户隔离用）。
        trace_recorder: TraceRecorder 实例（执行端基础设施）。None 时包不插 TraceMiddleware。
            类型用 object 而非具体类，避免 contracts 硬依赖执行端模块。
        trace_middleware_cls: TraceMiddleware 类（执行端定义，ctx 传入，包内实例化）。
            None 时包不挂载 Trace。与 trace_recorder 配合（T2 设计）。
            类型用 object 避免硬依赖；实际是 AgentMiddleware 子类。
        styles: 风格 suffix 映射（key=包内 scope 名，value=suffix 文本）。
            scope 名：meta / storybuilding / detail-outline / writing。
            执行端从 styling store 解析后填充；包内 assemble 读此字段把对应
            suffix 注入各 subagent 的 system_prompt（via apply_style_suffix）。
            None 或缺 key = 该 scope 无风格注入（用裸 prompt）。
    """

    # 请求级（每次 assemble 新建）
    model: "BaseChatModel"
    backend: object  # BackendProtocol（deepagents），用 object 避免 contracts 硬依赖
    checkpointer: "Checkpointer"
    workspace_path: Path
    trace_id: str | None = None
    owner_id: str | None = None
    styles: dict[str, str] | None = None  # scope 名 → 风格 suffix 文本
    # 平台级（进程内复用）
    trace_recorder: object | None = None
    trace_middleware_cls: object | None = None  # TraceMiddleware 类，执行端注入


__all__ = ["RuntimeContext"]
