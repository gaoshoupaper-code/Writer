"""多轴分类受控词表（REQ-02 / DEC-19）。

采用「真实结构 + 受控语义」混合方案（REQ-02.1）：
  - 发生位置（location）：锚定 Trace 中真实存在的 Agent / 组件 / 阶段，可回钻（AC-32）。
    这三个子轴值不预设词表——直接取 trace 结构里的真实值；提取不到记为 UNCLASSIFIED。
  - 受影响机制（affected_mechanism）：受控词表，直接复用评估 finding 的 dimension（四类）。
  - 失败性质（failure_nature）：受控词表。
  - 任务场景（task_scenario）：受控词表。

受控词表的「未分类」语义（REQ-02.4 / AC-32）：
  - 暂时无法归类时一律取 UNCLASSIFIED，并保留原始问题描述（raw_description）。
  - Agent 不得静默创造正式类别。
  - 词表的新增/合并/拆分由唯一操作者确认，并保留历史映射（AC-32 不断切检索）。

严重度 / 频率 / 状态属于属性而非分类（REQ-02.3），不在此处定义。
"""
from __future__ import annotations

# ── 受控语义常量 ──────────────────────────────────────────────

# 未分类哨兵值。任何轴提取不到合法值时统一用它，绝不静默编造类别。
UNCLASSIFIED = "未分类"

# 受影响机制（直接复用评估 finding 的 dimension 四类，REQ-02.1）。
# 这与 eval_agent prompt 强制约束的 dimension 取值一致，避免引入第二套口径。
AFFECTED_MECHANISMS = frozenset({
    "协作拓扑",
    "错误保障",
    "资源消耗",
    "内容质量",
})

# 失败性质（受控词表，初期精简）。用于跨次问题的稳定筛选。
# 涵盖流程类（超时/错误/缺失/冗余）与质量类（退化）两类常见失败模式。
FAILURE_NATURES = frozenset({
    "超时",   # 响应/执行超时、卡死
    "错误",   # 抛异常、产出错误内容
    "退化",   # 质量下降、风格/能力回退
    "缺失",   # 该有的没有（信息丢失、步骤遗漏）
    "冗余",   # 不该有的多了（重复、啰嗦、幻觉）
})

# 任务场景（受控词表，初期精简）。描述创作 Agent 的工作阶段场景。
TASK_SCENARIOS = frozenset({
    "长篇",   # 长篇正文生成
    "短篇",   # 短篇/单章生成
    "续写",   # 基于已有内容续写
    "重写",   # 修订/重写已有内容
})

# 所有受控语义轴的合法值集合（用于校验写入）。
_CONTROLLED_AXES = {
    "affected_mechanism": AFFECTED_MECHANISMS,
    "failure_nature": FAILURE_NATURES,
    "task_scenario": TASK_SCENARIOS,
}


def validate_axis(axis: str, value: str) -> str:
    """校验某受控轴的值是否合法；非法或空值统一归 UNCLASSIFIED（REQ-02.4）。

    不抛错——分类失败不应阻断收录（AC-14）。未知值记为 UNCLASSIFIED，
    由调用方保留原始描述。
    """
    allowed = _CONTROLLED_AXES.get(axis)
    if not allowed:
        # 非受控轴（如 location 子轴）不做约束
        return value if value else UNCLASSIFIED
    return value if value in allowed else UNCLASSIFIED


# ── 历史映射（词表治理用，DEC-19/AC-32）──────────────────────
# 词表重命名/合并/拆分时，旧值 → 新值的映射登记在此。
# 检索时把历史分类值按映射归一，保证词表变更不切断历史检索（AC-32）。
# 初期为空——首次词表演进时补充。结构示例：
#   {"failure_nature": {"旧名": "新名"}, "task_scenario": {...}}
TAXONOMY_HISTORY_MAP: dict[str, dict[str, str]] = {
    "affected_mechanism": {},
    "failure_nature": {},
    "task_scenario": {},
}


def normalize_axis(axis: str, value: str) -> str:
    """把历史分类值按词表历史映射归一到当前口径（AC-32）。

    检索筛选用此函数，保证旧实例的分类值在新词表下仍可命中。
    """
    if not value:
        return UNCLASSIFIED
    mapping = TAXONOMY_HISTORY_MAP.get(axis, {})
    return mapping.get(value, value)


__all__ = [
    "UNCLASSIFIED",
    "AFFECTED_MECHANISMS",
    "FAILURE_NATURES",
    "TASK_SCENARIOS",
    "validate_axis",
    "TAXONOMY_HISTORY_MAP",
    "normalize_axis",
]
