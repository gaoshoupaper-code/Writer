"""单文件 harness（worker 验证用，v1 的等价单文件入口）。

worker 的 load_harness_instance 用 importlib 加载此文件，找 WriterHarness 子类。
这个文件是 v1 包（WriterHarnessV1 + subagents）的单文件入口，行为等价。

部署时：每个 harness 版本 = harnesses/<version_id>/harness.py。
proposer 生成的也是这种单文件。
"""
from __future__ import annotations

from app.platform.harness import WriterHarness


class WriterHarnessV1Single(WriterHarness):
    """v1 harness 的单文件入口（委托给 v1 包的 WriterHarnessV1）。"""

    def __init__(self) -> None:
        from app.harnesses.v1 import WriterHarnessV1
        self._inner = WriterHarnessV1()

    def build_system_prompt(self, ctx):
        return self._inner.build_system_prompt(ctx)

    def build_skills(self, ctx):
        return self._inner.build_skills(ctx)

    def build_middleware(self, ctx):
        return self._inner.build_middleware(ctx)

    def build_tools(self, ctx):
        return self._inner.build_tools(ctx)

    def build_subagents(self, ctx):
        return self._inner.build_subagents(ctx)

    def harness_id(self) -> str:
        return "writer-harness-v1"
