"""harness 契约层（Phase 1 T1.1，D16 契约化 Python）。

harness = 执行端 agent 的「可编辑装配定义」。proposer 在契约框架内自由写代码
（可新建 middleware/tool/改拓扑），静态检查（D10）验证契约满足。

分层（S4）：
  - WriterHarness（顶层，管 meta agent：prompt/skills/middleware/tools/subagents）
  - SubagentHarness（每个 subagent 各一：prompt/skills/middleware/permissions/evolution）

不归 harness（执行端保留，跨版本共享）：
  - model（多用户 key 解密）
  - backend（workspace FilesystemBackend）
  - checkpointer（分库 saver）
这些是基础设施，proposer 不碰。

runtime context（S5）作为 build 方法参数传入，harness 实例无状态可复用。

设计依据：设计文档 S4/S5/harness 基类契约骨架。
"""
from app.platform.harness.base import (
    HarnessContext,
    SubagentHarness,
    WriterHarness,
)

__all__ = ["HarnessContext", "SubagentHarness", "WriterHarness"]
