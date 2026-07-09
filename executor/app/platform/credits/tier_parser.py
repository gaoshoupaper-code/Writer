"""demand.md 篇幅档位解析器（D9/D13）。

从 demand.md 文本解析两个信息：
1. status: draft / confirmed（D13 预扣时机判断）
2. 篇幅档位：1-6（D9 访谈 Agent 收集的 6 选 1）

篇幅档位映射（interview_system.md:75-91 的 6 档表）：
  1: ≤20章     2: 21-50章    3: 51-90章
  4: 91-130章  5: 131-170章  6: 171-200章

demand.md 中篇幅档位的实际写法（从真实产出样本归纳）：
  - "档位：21-50章（短篇）"
  - "档位：91-130章"
  - "篇幅档位 + 目标配比" 标题下含 "档位：XX-XX章"
"""
from __future__ import annotations

import re
from pathlib import Path


# 档位 → 章数范围映射（取自 interview_system.md 的 6 档表）
_TIER_RANGES: list[tuple[int, int, int]] = [
    (1, 1, 20),       # 档1: ≤20
    (2, 21, 50),      # 档2: 21-50
    (3, 51, 90),      # 档3: 51-90
    (4, 91, 130),     # 档4: 91-130
    (5, 131, 170),    # 档5: 131-170
    (6, 171, 200),    # 档6: 171-200
]


def parse_demand_status(content: str) -> str | None:
    """从 demand.md 文本解析 status 字段。

    返回 'confirmed' / 'draft' / None（未找到）。

    兼容 markdown 加粗写法："- **status**: confirmed" 和裸写 "status: confirmed"。
    """
    # status 前后可能有 ** 加粗标记，用 \*? 容错
    m = re.search(r'\*?\*?status\*?\*?\s*[:：]\s*(confirmed|draft)', content, re.IGNORECASE)
    return m.group(1).lower() if m else None


def parse_tier_from_demand(content: str) -> int | None:
    """从 demand.md 文本解析篇幅档位号（1-6）。

    匹配规则（按优先级）：
    1. "档位：21-50章" → 解析章数范围 → 映射档位号
    2. "91-130章" 等裸章数范围
    3. 无法确定时返回 None（调用方应降级为默认档位）

    返回 1-6 的档位号，或 None。
    """
    # 尝试匹配 "档位：XX-XX章" 或 "XX-XX章"
    # 支持：21-50 / 21～50 / 21—50 等连字符变体
    pattern = r'(\d+)\s*[-—～~]\s*(\d+)\s*章'
    matches = re.findall(pattern, content)
    if matches:
        # 取最后一个匹配（篇幅档位通常在核心层，但有多个时取最后出现的）
        low_str, high_str = matches[-1]
        low, high = int(low_str), int(high_str)
        return _chapter_range_to_tier(low, high)

    # 尝试匹配 "≤20章" 或 "20章以下"
    m = re.search(r'[≤<=]+\s*(\d+)\s*章', content)
    if m:
        return _chapter_range_to_tier(1, int(m.group(1)))

    return None


def _chapter_range_to_tier(low: int, high: int) -> int:
    """把章数范围映射到最近的档位号。"""
    mid = (low + high) // 2
    for tier, t_low, t_high in _TIER_RANGES:
        if t_low <= mid <= t_high:
            return tier
    # 超出范围取最近端
    if mid > 200:
        return 6
    return 1


def parse_demand_file(demand_path: Path) -> tuple[str | None, int | None]:
    """读 demand.md 文件，返回 (status, tier)。

    文件不存在时返回 (None, None)。
    """
    if not demand_path.exists():
        return None, None
    try:
        content = demand_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    return parse_demand_status(content), parse_tier_from_demand(content)
