"""StorylineGraph — 从 storyline.md(索引)+storyline/*.md(各线详情) 派生 mermaid 竖向泳道流程图。

纯后端、确定性生成（不依赖 LLM 画图）：
  读 workspace/storyline.md 索引 + storyline/*.md 各线详情 → 拼接后解析故事线/事件/交汇
  → 拓扑排序生成全局时间序号 T → 生成 mermaid 泳道图 → 写 workspace/storyline_graph.md。

设计契约见 .claude/md/20260611_164126_故事线流程图设计.md：
  - 直接操作 workspace 真实磁盘，绕过 agent 的 virtual fs / 权限系统（故 agent 权限零改动）；
  - storyline.md 与 storyline/ 只读不改；mermaid 语法由代码生成，100% 正确；
  - 解析失败 → 跳过 + 日志，绝不抛异常（图是派生视图，不能拖累编故事主流程）。

解析依据 storybuilding_system.md 规范格式（故事线一览表 + 故事线详情 ### S{XX}
+ 事件组 #### G{XX} + 事件详情 ### E{XXX} 及其「类型/所属故事线」字段），
并对事件标题/字段的常见漂移（有无 # / 列表序号、字段加粗与否）做兼容。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 正则：宽松匹配规范格式，兼容 LLM 产出的常见漂移
# ---------------------------------------------------------------------------

# 故事线详情标题：### S01-成长主线 [主线]（方括号类型可缺失，则从一览表补）
_STORYLINE_TITLE = re.compile(r"^#{2,4}\s*(S\d{2})\s*[-—]\s*(.+?)(?:\s*\[([^\]]*)\])?\s*$")

# 事件详情标题：两种合法形态——纯标题 `### E001-名` 或列表项 `1. ### E001-名` / `1. E001-名`
_EVENT_TITLE_HASH = re.compile(r"^#{2,4}\s*(E\d{3})\s*[-—]\s*(.+?)\s*$")
_EVENT_TITLE_LIST = re.compile(r"^\d+\.\s+(?:#{0,4}\s*)?(E\d{3})\s*[-—]\s*(.+?)\s*$")

_EVENT_ID = re.compile(r"E\d{3}")
_STORYLINE_ID = re.compile(r"S\d{2}")

def _field(name: str, rest: str = r"(.+)") -> re.Pattern[str]:
    """构造字段正则：兼容 `- 类型：x` / `- **类型**：x` / `类型：x` 三种写法。"""
    return re.compile(rf"^(?:-\s*)?(?:\*\*)?{name}(?:\*\*)?\s*[：:]\s*{rest}")


_F_TYPE = _field("类型")
_F_STORYLINES = _field("所属故事线")
_F_KEY_EVENTS = _field("关键事件")
_F_DIRECTION = _field("全局走向")
_F_STATUS = _field("状态")
_F_GROUP = _field("事件组", r"(G\d{2})")

# 故事线类型 → mermaid classDef 别名 / 配色。按「包含」匹配，兼容「角色线（苏雨）」「暗线→支线」等复合标注。
# 顺序敏感：先判暗线（复合标注里若含「暗线」视为暗线阶段），再主线/支线/角色。
_TYPE_RULES: list[tuple[str, str, str]] = [
    ("暗线", "laneDark", "#9b9b9b"),
    ("主线", "laneMain", "#4a90d9"),
    ("支线", "laneSub", "#7ac17a"),
    ("角色", "laneChar", "#d98a4a"),
]


def _classify_type(type_text: str) -> tuple[str, str]:
    """返回 (classDef 别名, 配色)。未命中给默认灰。"""
    for keyword, alias, color in _TYPE_RULES:
        if keyword in type_text:
            return alias, color
    return "laneOther", "#cccccc"


def _sanitize_label(text: str) -> str:
    """mermaid 节点/子图标签内不能出现双引号、换行、方括号（会破坏 ["..."] 语法）。"""
    return (
        text.replace('"', "'")
        .replace("\n", " ")
        .replace("[", "(")
        .replace("]", ")")
        .strip()
    )


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """一个事件节点。"""

    id: str  # E001
    name: str = ""  # 事件详情标题里的名称；缺事件详情时为空
    type: str = ""  # 冲突/反转/揭露…（事件详情「类型」字段）
    storylines: tuple[str, ...] = ()  # 所属故事线 ID，多条=交汇事件
    group: str = ""  # G01
    doc_order: int = 0  # 在 storyline.md 首次出现的行号，作拓扑排序的稳定 tiebreak


@dataclass
class Storyline:
    """一条故事线（= 图中的一列泳道）。"""

    id: str  # S01
    name: str  # 成长主线
    type: str  # 主线/支线/角色线/暗线
    status: str = ""  # 活跃/已收束/暗线…
    direction: str = ""  # 全局走向（拼「本卷脉络」导读用）
    key_events: list[str] = field(default_factory=list)  # 线内事件顺序（拓扑排序的保序约束来源）


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


def _parse_storyline_table(text: str) -> dict[str, dict[str, str]]:
    """解析「故事线一览表」→ {S01: {名称, 类型, 状态}}。

    按表头列名定位列索引，兼容列数/列顺序漂移（规范 4 列、旧样本 5 列含「锚点范围」）。
    """
    lines = text.splitlines()
    col_map: dict[str, int] = {}
    header_idx = -1
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("|") and "ID" in s and "名称" in s:
            cells = [c.strip() for c in s.strip("|").split("|")]
            for ci, cell in enumerate(cells):
                if cell == "ID" or _STORYLINE_ID.fullmatch(cell):
                    col_map["ID"] = ci
                elif "名称" in cell:
                    col_map["名称"] = ci
                elif "类型" in cell:
                    col_map["类型"] = ci
                elif "状态" in cell:
                    col_map["状态"] = ci
            header_idx = i
            break
    if header_idx < 0 or not col_map:
        return {}

    result: dict[str, dict[str, str]] = {}
    for raw in lines[header_idx + 1 :]:
        s = raw.strip()
        if not s.startswith("|"):
            break  # 表格结束
        if re.fullmatch(r"\|[\s|:-]+\|", s):
            continue  # 分隔行 |---|---|
        cells = [c.strip() for c in s.strip("|").split("|")]

        def cell(key: str) -> str:
            idx = col_map.get(key)
            return cells[idx] if idx is not None and idx < len(cells) else ""

        m = _STORYLINE_ID.search(cell("ID"))
        if not m:
            continue
        result[m.group()] = {"名称": cell("名称"), "类型": cell("类型"), "状态": cell("状态")}
    return result


def _match_event_title(line: str) -> re.Match | None:
    """匹配事件详情标题行（兼容 `### E001-名` 与 `1. ### E001-名` 两种形态）。"""
    return _EVENT_TITLE_HASH.match(line) or _EVENT_TITLE_LIST.match(line)


def _parse_storyline_md(text: str) -> tuple[list[Storyline], dict[str, Event]]:
    """解析 storyline.md → (故事线列表[按出现顺序], 事件字典 {E001: Event})。"""
    table = _parse_storyline_table(text)

    storylines: dict[str, Storyline] = {}
    events: dict[str, Event] = {}
    current_sid: str | None = None
    current_event: Event | None = None

    for idx, raw in enumerate(text.splitlines()):
        line = raw.strip()

        # 故事线详情标题
        m = _STORYLINE_TITLE.match(line)
        if m:
            sid, name, stype = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
            info = table.get(sid, {})
            storylines[sid] = Storyline(
                id=sid,
                name=info.get("名称") or name,
                type=stype or info.get("类型", ""),
                status=info.get("状态", ""),
            )
            current_sid = sid
            current_event = None
            continue

        # 事件详情标题
        m = _match_event_title(line)
        if m:
            eid, ename = m.group(1), m.group(2).strip()
            ev = events.get(eid)
            if ev is None:
                ev = Event(id=eid, name=ename, doc_order=idx)
                events[eid] = ev
            elif not ev.name:
                ev.name = ename
            # 所在故事线块作为默认归属；若后续读到「所属故事线」字段会覆盖
            if current_sid and current_sid not in ev.storylines:
                ev.storylines = (*ev.storylines, current_sid)
            current_event = ev
            continue

        # 事件字段
        if current_event is not None:
            if not current_event.type and (m := _F_TYPE.match(line)):
                current_event.type = m.group(1).strip()
                continue
            if (m := _F_STORYLINES.match(line)):
                ids = _STORYLINE_ID.findall(m.group(1))
                if ids:
                    current_event.storylines = tuple(ids)
                continue
            if not current_event.group and (m := _F_GROUP.match(line)):
                current_event.group = m.group(1)
                continue

        # 故事线字段
        if current_sid is not None and current_sid in storylines:
            sl = storylines[current_sid]
            if (m := _F_KEY_EVENTS.match(line)):
                sl.key_events = _EVENT_ID.findall(m.group(1))
                continue
            if (m := _F_DIRECTION.match(line)):
                sl.direction = m.group(1).strip()
                continue
            if (m := _F_STATUS.match(line)):
                sl.status = m.group(1).strip()
                continue

    # doc_order 重编号为连续序（0,1,2…），保证拓扑排序 tiebreak 稳定可复现
    for i, ev in enumerate(sorted(events.values(), key=lambda e: e.doc_order)):
        ev.doc_order = i

    return list(storylines.values()), events


# ---------------------------------------------------------------------------
# 拓扑排序：多线保序合并 → 全局时间序号 T
# ---------------------------------------------------------------------------


def _assign_global_t(storylines: list[Storyline], events: dict[str, Event]) -> dict[str, int]:
    """给每个事件分配全局时间序号 T（1-based）。

    约束：
      - 每条线的 key_events 序列保持先后（线内保序）；
      - 交汇事件属多条线，天然成为多线的汇聚锚点。
    算法：Kahn 拓扑排序；每步从入度为 0 的候选中取 doc_order 最小者，
    以保证跨线无交汇片段按「storyline.md 文档出现顺序」定序（稳定、可复现）。
    """
    edges: dict[str, set[str]] = {}  # 前置 → {后继}
    nodes: set[str] = set()
    for sl in storylines:
        prev: str | None = None
        for eid in sl.key_events:
            if eid not in events:
                continue  # 关键事件引用了不存在的事件（漂移），跳过该约束
            nodes.add(eid)
            if prev is not None:
                edges.setdefault(prev, set()).add(eid)
            prev = eid

    indeg = {eid: 0 for eid in nodes}
    for succs in edges.values():
        for s in succs:
            indeg[s] += 1

    t_map: dict[str, int] = {}
    t = 1
    ready = [eid for eid in nodes if indeg[eid] == 0]
    while ready:
        ready.sort(key=lambda e: events[e].doc_order)  # 文档顺序 tiebreak
        chosen = ready.pop(0)
        t_map[chosen] = t
        t += 1
        for succ in edges.get(chosen, ()):
            indeg[succ] -= 1
            if indeg[succ] == 0:
                ready.append(succ)

    # 兜底：环路或游离事件（拓扑未排完）按文档顺序追加，保证不丢节点
    for eid in sorted((n for n in nodes if n not in t_map), key=lambda e: events[e].doc_order):
        t_map[eid] = t
        t += 1
    return t_map


# ---------------------------------------------------------------------------
# mermaid 泳道图生成
# ---------------------------------------------------------------------------


def _build_mermaid(storylines: list[Storyline], events: dict[str, Event], t_map: dict[str, int]) -> str:
    """生成 flowchart TD 竖向泳道图。

    结构要点：
      - 每条故事线一个 subgraph（一列泳道）；事件节点定义在其「主属泳道」（首个所属线）；
      - 每条线按 key_events 顺序连边；同泳道实线 `-->`=时间先后，跨泳道虚线 `-.->`=交汇；
      - 交汇节点（属≥2 线）用红色粗边框 class 高亮，覆盖线底色以突出。
    """
    # 主属泳道：事件首次被某条线引用的线
    primary_lane: dict[str, str] = {}
    for sl in storylines:
        for eid in sl.key_events:
            if eid in events and eid not in primary_lane:
                primary_lane[eid] = sl.id

    lines = ["flowchart TD"]

    # classDef：每种出现过的故事线类型一套配色 + 交汇高亮
    seen_alias: dict[str, str] = {}  # alias -> color
    for sl in storylines:
        alias, color = _classify_type(sl.type)
        seen_alias.setdefault(alias, color)
    for alias, color in seen_alias.items():
        lines.append(f"  classDef {alias} fill:{color},color:#fff,stroke:#333,stroke-width:1px")
    lines.append("  classDef cross fill:#fff3e6,color:#000,stroke:#e8470b,stroke-width:3px")

    # 节点：定义在各自主属泳道内
    for sl in storylines:
        lines.append(f"  subgraph {sl.id} [\"{_sanitize_label(sl.id + ' · ' + sl.name)}\"]")
        for eid in sl.key_events:
            if primary_lane.get(eid) != sl.id:
                continue  # 只在主属泳道定义一次，避免 mermaid 节点重复归属报错
            ev = events[eid]
            t = t_map.get(eid, 0)
            label = f"T{t:02d}·{ev.id}·{ev.name or '未命名'}·{ev.type or '—'}"
            lines.append(f"    n_{eid}[\"{_sanitize_label(label)}\"]")
        lines.append("  end")

    # 边：每条线按 key_events 顺序连接（跨泳道自然形成交汇拓扑）
    for sl in storylines:
        prev: str | None = None
        for eid in sl.key_events:
            if eid not in events:
                continue
            if prev is not None:
                arrow = "-->" if primary_lane.get(prev) == sl.id == primary_lane.get(eid) else "-.->"
                lines.append(f"  n_{prev} {arrow} n_{eid}")
            prev = eid

    # 样式应用：先按主属泳道类型上色，交汇节点再用 cross 覆盖（突出交汇）
    for sl in storylines:
        alias, _ = _classify_type(sl.type)
        for eid in sl.key_events:
            if primary_lane.get(eid) == sl.id and len(events[eid].storylines) < 2:
                lines.append(f"  class n_{eid} {alias}")
    for eid, ev in events.items():
        if len(ev.storylines) >= 2 and eid in primary_lane:
            lines.append(f"  class n_{eid} cross")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 组装 storyline_graph.md
# ---------------------------------------------------------------------------


def _build_legend(storylines: list[Storyline]) -> str:
    seen: list[tuple[str, str]] = []
    done: set[str] = set()
    for sl in storylines:
        alias, color = _classify_type(sl.type)
        if alias not in done:
            done.add(alias)
            label = next((k for k, a, _ in _TYPE_RULES if a == alias), "其他")
            seen.append((label, color))

    lines = ["## 图例", ""]
    lines.append("- `T##` = 故事内时间顺序（全局连续，由小到大）")
    lines.append("- 每列 `subgraph` = 一条故事线（泳道）")
    lines.append("- 实线 `-->` = 同线时间先后；虚线 `-.->` = 跨线交汇")
    lines.append("- 红色粗边框节点 = 交汇事件（同时属于多条故事线）")
    for label, color in seen:
        lines.append(f"- {label}：{color}")
    return "\n".join(lines)


def _build_synopsis(storylines: list[Storyline]) -> str:
    """本卷脉络：拼接各故事线「全局走向」（代码无法润色，仅按线汇总）。"""
    parts = []
    for sl in storylines:
        direction = sl.direction or "（暂无全局走向）"
        parts.append(f"**{sl.id} {sl.name}（{sl.type or "未分类"}）**：{direction}")
    return "\n\n".join(parts) if parts else "（未解析到故事线）"


def _compose_markdown(storylines: list[Storyline], events: dict[str, Event], t_map: dict[str, int]) -> str:
    return (
        "# 故事线流程图\n\n"
        f"{_build_legend(storylines)}\n\n"
        f"## 本卷脉络\n\n{_build_synopsis(storylines)}\n\n"
        "## 流程图\n\n"
        f"```mermaid\n{_build_mermaid(storylines, events, t_map)}\n```\n"
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def generate_storyline_graph(workspace_path: Path) -> None:
    """从 workspace/storyline.md(索引)+storyline/*.md(各线详情) 派生 storyline_graph.md。

    确定性、纯后端。任何解析异常都吞掉并打日志——图是派生视图，
    绝不因自身问题阻断 storybuilding 主流程。
    """
    storyline_index = workspace_path / "storyline.md"
    storyline_dir = workspace_path / "storyline"

    # 拼接索引 + 各故事线详情文件，复刻原单文件结构（一览表 + ### S{XX} + ### E{XXX}）
    chunks: list[str] = []
    if storyline_index.exists():
        chunks.append(storyline_index.read_text(encoding="utf-8"))
    if storyline_dir.exists():
        for detail_path in sorted(storyline_dir.glob("*.md"), key=lambda p: p.name):
            chunks.append(detail_path.read_text(encoding="utf-8"))

    if not chunks:
        # storybuilding 尚未产出任何故事线产物，属正常状态，静默跳过
        return

    try:
        text = "\n\n".join(chunks)
        storylines, events = _parse_storyline_md(text)
        if not storylines or not events:
            print(
                "[storyline_graph] 跳过：未解析出故事线/事件详情"
                "（storyline.md / storyline/*.md 可能缺少 ### S{XX} 详情或 ### E{XXX} 事件段落）"
            )
            return
        t_map = _assign_global_t(storylines, events)
        markdown = _compose_markdown(storylines, events, t_map)
        graph_path = workspace_path / "storyline_graph.md"
        graph_path.write_text(markdown, encoding="utf-8")
        print(
            f"[storyline_graph] 已生成 {graph_path.name}"
            f"（{len(storylines)} 故事线 / {len(events)} 事件 / T01–T{len(t_map):02d}）"
        )
    except Exception as exc:  # noqa: BLE001 — 派生视图：任何失败都不上抛
        print(f"[storyline_graph] 跳过生成（{type(exc).__name__}: {exc}）")
