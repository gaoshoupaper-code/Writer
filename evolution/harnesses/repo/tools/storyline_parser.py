"""Storybuilding 结构化解析器（可进化要素）。

解析 workspace 下的 storybuilding 产物（storyline/character/worldview），
提取结构化数据供 MemoryBackend.ingest_storybuilding 建节点/边。

纯 markdown 解析，无 LLM 依赖——因为 storybuilding_system.md prompt 产出的
markdown 格式是固定的，用正则/行扫描即可精确提取，质量比 LLM 抽取可控。

为什么放 harness tools（可进化）：
  解析的格式由 prompts/storybuilding_system.md（可进化 prompt）定义。
  prompt 改了产出格式，解析器要跟着变——放同包才能被 evolution 统一进化。

解析的事件格式（来自 storyline/S0X-*.md）：
  数字. ### E0XX-名称
     - **类型**：冲突/危机/反转/悬念/揭露/胜利/交汇
     - **描述**：...
     - **事件组**：G00/G01/G02
     - **所属故事线**：S01
     - **参与角色**：角色A（主角）、角色B（配角）
     - **场景/地点**：地点描述
     - **时间**：2087年2月15日
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedStoryEvent:
    """一个叙事事件（对应 storyline E0XX）。"""
    event_id: str = ""          # E019
    title: str = ""             # 棋手的错觉
    event_type: str = ""        # 冲突/危机/反转/悬念/揭露/胜利/交汇
    event_group: str = ""       # G00/G01/G02
    story_time: str = ""        # 2087年2月15日（原始文本，StoryCalendar 转 datetime）
    characters: list[str] = field(default_factory=list)  # ["陈远", "苏敏"]
    locations: list[str] = field(default_factory=list)   # ["新京", "联邦议会预算听证厅"]
    description: str = ""       # 事件描述全文
    thread_ids: list[str] = field(default_factory=list)  # ["S01"]


@dataclass
class ParsedThread:
    """一条故事线（对应 storyline/S0X-*.md）。"""
    thread_id: str = ""         # S01
    name: str = ""              # 代理觉醒
    thread_type: str = ""       # 主线/支线
    status: str = ""            # 活跃/完结/暂停
    global_arc: str = ""        # 全局走向（长文本）


@dataclass
class ParsedCharacter:
    """一个角色（对应 character/角色名.md）。"""
    name: str = ""              # 陈远
    aliases: list[str] = field(default_factory=list)
    role_type: str = ""         # 主角/配角/反派
    summary: str = ""           # 角色简述（取"基本信息"段的身份描述）


# ── 正则模式 ────────────────────────────────────────────────────────

# 事件头：`1. ### E019-棋手的错觉` 或 `### E019-棋手的错觉`
_EVENT_HEADER = re.compile(
    r"^[\d]*\.?\s*###\s*(E\d+)\s*[-—]\s*(.+?)\s*$"
)
# 字段行：`   - **类型**：冲突`
_FIELD_LINE = re.compile(
    r"^\s*-\s*\*\*(.+?)\*\*\s*[:：]\s*(.+?)\s*$"
)
# 事件组头：`#### G00 [开端]`
_GROUP_HEADER = re.compile(
    r"^####\s*(G\d+)\s*\[.+?\]"
)
# 故事线文件名：`S01-代理觉醒.md`
_THREAD_FILE = re.compile(r"^(S\d+)[-—](.+?)\.md$")
# 故事线头：`### S01-代理觉醒 [主线]`
_THREAD_HEADER = re.compile(
    r"^###\s*(S\d+)\s*[-—]\s*(.+?)\s*\[([^\]]+)\]"
)
# 角色名标题：`# 陈远`
_CHAR_TITLE = re.compile(r"^#\s*(.+?)\s*$")


# ── 公共解析入口 ────────────────────────────────────────────────────

def parse_storyline(workspace_root: Path) -> list[ParsedStoryEvent]:
    """解析 workspace 下所有 storyline/S0X-*.md，提取全部事件。

    返回扁平的事件列表（跨故事线合并，每个事件带 thread_ids）。
    """
    storyline_dir = workspace_root / "storyline"
    if not storyline_dir.exists():
        return []

    all_events: list[ParsedStoryEvent] = []
    for md_file in sorted(storyline_dir.glob("S*.md")):
        events = _parse_storyline_file(md_file)
        all_events.extend(events)

    return all_events


def parse_threads(workspace_root: Path) -> list[ParsedThread]:
    """解析 workspace 下所有 storyline/S0X-*.md，提取故事线元数据。"""
    storyline_dir = workspace_root / "storyline"
    if not storyline_dir.exists():
        return []

    threads: list[ParsedThread] = []
    for md_file in sorted(storyline_dir.glob("S*.md")):
        thread = _parse_thread_file(md_file)
        if thread:
            threads.append(thread)
    return threads


def parse_characters(workspace_root: Path) -> list[ParsedCharacter]:
    """解析 workspace 下所有 character/*.md，提取角色元数据。"""
    char_dir = workspace_root / "character"
    if not char_dir.exists():
        return []

    characters: list[ParsedCharacter] = []
    for md_file in sorted(char_dir.glob("*.md")):
        char = _parse_character_file(md_file)
        if char:
            characters.append(char)
    return characters


# ── 内部解析实现 ────────────────────────────────────────────────────

def _parse_storyline_file(md_path: Path) -> list[ParsedStoryEvent]:
    """解析单个 storyline/S0X-*.md 文件，提取事件列表。"""
    text = md_path.read_text(encoding="utf-8")
    # 从文件名提取 thread_id（如 S01）
    file_match = _THREAD_FILE.match(md_path.name)
    default_thread = file_match.group(1) if file_match else ""

    events: list[ParsedStoryEvent] = []
    current_event: ParsedStoryEvent | None = None
    current_group = ""

    for line in text.splitlines():
        # 事件组头
        group_match = _GROUP_HEADER.match(line)
        if group_match:
            current_group = group_match.group(1)
            continue

        # 事件头
        header_match = _EVENT_HEADER.match(line)
        if header_match:
            # 保存上一个事件
            if current_event is not None:
                events.append(current_event)
            current_event = ParsedStoryEvent(
                event_id=header_match.group(1),
                title=header_match.group(2).strip(),
                event_group=current_group,
                thread_ids=[default_thread] if default_thread else [],
            )
            continue

        # 字段行（只有当前在事件块内才有意义）
        if current_event is None:
            continue

        field_match = _FIELD_LINE.match(line)
        if not field_match:
            continue

        key = field_match.group(1).strip()
        value = field_match.group(2).strip()

        if key == "类型":
            current_event.event_type = value
        elif key == "描述":
            current_event.description = value
        elif key == "事件组" and not current_event.event_group:
            current_event.event_group = value
        elif key == "所属故事线":
            # 可能多个：S01, S02
            thread_ids = [t.strip() for t in re.split(r"[,，]", value) if t.strip()]
            current_event.thread_ids = thread_ids or ([default_thread] if default_thread else [])
        elif key == "参与角色":
            current_event.characters = _extract_char_names(value)
        elif key in ("场景/地点", "场景", "地点"):
            current_event.locations = [loc.strip() for loc in re.split(r"[/、，,]", value) if loc.strip()]
        elif key == "时间":
            current_event.story_time = value

    # 保存最后一个事件
    if current_event is not None:
        events.append(current_event)

    return events


def _parse_thread_file(md_path: Path) -> ParsedThread | None:
    """解析单个 storyline/S0X-*.md 文件，提取故事线元数据。"""
    text = md_path.read_text(encoding="utf-8")
    file_match = _THREAD_FILE.match(md_path.name)
    thread_id = file_match.group(1) if file_match else ""
    thread_name = file_match.group(2) if file_match else ""

    thread = ParsedThread(thread_id=thread_id, name=thread_name, status="活跃")

    for line in text.splitlines():
        header_match = _THREAD_HEADER.match(line)
        if header_match:
            thread.thread_id = header_match.group(1)
            thread.name = header_match.group(2).strip()
            thread.thread_type = header_match.group(3).strip()
            continue

        field_match = _FIELD_LINE.match(line)
        if not field_match:
            continue
        key = field_match.group(1).strip()
        value = field_match.group(2).strip()
        if key == "状态":
            thread.status = value
        elif key == "全局走向":
            thread.global_arc = value

    return thread


def _parse_character_file(md_path: Path) -> ParsedCharacter | None:
    """解析单个 character/角色名.md 文件，提取角色元数据。

    character 文件格式：
      # 角色名
      ## 基本信息
      - **姓名**：角色名
      - **身份**：...
      ## 角色类型
      主角
    """
    text = md_path.read_text(encoding="utf-8")
    # 角色名从文件名取（去掉 .md）
    name = md_path.stem

    char = ParsedCharacter(name=name)

    lines = text.splitlines()
    current_section = ""

    for line in lines:
        stripped = line.strip()

        # 段落标题
        if stripped.startswith("## "):
            current_section = stripped[3:].strip()
            continue

        field_match = _FIELD_LINE.match(line)
        if field_match:
            key = field_match.group(1).strip()
            value = field_match.group(2).strip()
            if key == "姓名" and value:
                char.name = value
            elif key == "身份" and not char.summary:
                char.summary = value
        elif current_section == "角色类型" and stripped and not stripped.startswith("-"):
            # 角色类型段下的纯文本行（如 "主角"）
            if not char.role_type:
                char.role_type = stripped

    return char


def _extract_char_names(raw: str) -> list[str]:
    """从"参与角色"字段值提取角色名列表。

    输入示例：'陈远（主角）、苏敏（妻子，电话中）、陈念（女儿，电话中间接提及）'
    输出：['陈远', '苏敏', '陈念']

    关键：先按顿号分割（顿号是角色分隔符），再从每段去掉括号注释取名字。
    不能按逗号分割——逗号在括号注释内部出现（如'电话中'），会误拆。
    """
    # 先去掉所有括号注释（中文/英文括号），再按顿号分割取名字
    cleaned = re.sub(r"[（(].*?[）)]", "", raw)
    parts = cleaned.split("、")
    names: list[str] = []
    for part in parts:
        name = part.strip().strip("，,").strip()
        if name and name not in names:
            names.append(name)
    return names


__all__ = [
    "ParsedStoryEvent",
    "ParsedThread",
    "ParsedCharacter",
    "parse_storyline",
    "parse_threads",
    "parse_characters",
]
