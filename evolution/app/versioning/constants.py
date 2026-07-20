"""Harness 包要素分类常量（versioning 域共享）。

被两处复用，确保前后端认定"什么是记忆要素"同源，避免清单漂移：
  - elements_api：扫描打 tag + memory-elements 接口返回
  - evolve/agent/prompt：STATIC_BLUEPRINT ③ 段 memory 类描述（已固化，不再条件注入）

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

# Tool 作用域登记：path → scope 描述。
#
# harness 的 tools/ 目录是全局平铺的，不存在"tool → agent"映射——每个文件的真实
# 归属各不相同（有的全局注入、有的经某 middleware 暴露、有的仅某 agent 间接受益、
# 有的是记忆系统策略）。这张表把真实归属诚实标注出来，供 Tools Tab 展示 scope 彩签。
#
# scope.kind 取值（与前端 ToolScope 类型对齐）：
#   global     全局注入（如进 MemoryRetriever 单例，所有 agent 间接受益）
#   middleware 经某 middleware 暴露（via 字段标 middleware 类名）
#   agent      仅某 agent 间接受益（agent 字段标 agent 名）
#   memory     记忆系统策略要素（与 MEMORY_FILES 重叠，流水线视角在 Memory Tab）
#
# 作用域在 harness 装配代码里查证得到（见 __init__.py 的 assemble()）。
# 新增 tool 时必须在此登记，否则前端 Tools Tab 会显示"⚠ 未登记作用域"。
TOOL_SCOPE_MAP: dict[str, dict[str, str]] = {
    "tools/goal.py": {
        "kind": "middleware", "via": "GoalMiddleware",
    },
    "tools/query_builder.py": {
        "kind": "agent", "agent": "writing",
    },
    "tools/join_rules.py": {
        "kind": "global",
    },
    "tools/packet_formatter.py": {
        "kind": "global",
    },
    "tools/narrative_schema.py": {
        "kind": "memory",
    },
}


__all__ = ["MEMORY_FILES", "MEMORY_ROLE_ORDER", "MEMORY_ROLE_LABELS", "TOOL_SCOPE_MAP"]
