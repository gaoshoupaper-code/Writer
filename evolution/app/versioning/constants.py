"""Harness 包要素分类常量（versioning 域共享）。

被两处复用，确保前后端认定"什么是记忆要素"同源，避免清单漂移：
  - elements_api：扫描打 tag + memory-elements 接口返回
  - evolve/agent：_format_memory_section 探测当前工作副本是否有记忆要素 → prompt 注入

记忆子系统是 NWM（Narrative World Model）重构后的 6 个要素，物理上散落在
prompts/middleware/tools 三个目录，但语义上构成一条协同链：
  抽取(extract) → 存储(store) → 检索(retrieve) → 回填(recall)
"""
from __future__ import annotations

# 记忆子系统要素清单：path → (type, file_role, description)
#
# 字段含义：
#   type      要素物理类型：prompt | middleware | tool
#   file_role 在记忆协同链中的角色：extract | store | retrieve | recall
#   description 一句话作用说明（对齐 elements_api 既有的"用途说明"风格）
#
# path 相对 harness 包根（如 harnesses/repo/）。
MEMORY_FILES: dict[str, tuple[str, str, str]] = {
    "prompts/memory_extraction_guide.md": (
        "prompt", "extract",
        "记忆抽取器 system prompt 覆写源——引导 LLM 从章节正文抽取 typed records",
    ),
    "tools/narrative_schema.py": (
        "tool", "store",
        "NWM 记忆可进化 schema 策略——决定抽哪些类型记录、按题材启用/禁用 record 类型",
    ),
    "tools/query_builder.py": (
        "tool", "retrieve",
        "查询构造器——把写作子代理的 task description 转成检索查询",
    ),
    "tools/join_rules.py": (
        "tool", "retrieve",
        "One-Hop JOIN 规则——把 anchor 节点扩展一跳邻域，暴露关联边",
    ),
    "tools/packet_formatter.py": (
        "tool", "retrieve",
        "证据包排版器——把召回结果按叙事优先级排版成可注入文本",
    ),
    "middleware/memory_recall_middleware.py": (
        "middleware", "recall",
        "MemoryRecallMiddleware——写作子代理调 LLM 前召回记忆证据注入 prompt",
    ),
}

# file_role 的展示顺序：抽取 → 存储 → 检索 → 回填（NWM 数据流方向）。
# 前端横向流水线卡片 + 进化 prompt 表格均按此顺序排列。
MEMORY_ROLE_ORDER: list[str] = ["extract", "store", "retrieve", "recall"]

# file_role → 中文展示名（前端流水线阶段标题 + prompt 协同说明复用）。
MEMORY_ROLE_LABELS: dict[str, str] = {
    "extract": "抽取",
    "store": "存储",
    "retrieve": "检索",
    "recall": "回填",
}


__all__ = ["MEMORY_FILES", "MEMORY_ROLE_ORDER", "MEMORY_ROLE_LABELS"]
